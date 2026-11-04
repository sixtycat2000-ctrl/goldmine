"""Request, timer and process ownership of the optional local reranker."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from goldmine.application import Application
from goldmine.contracts import SearchRequest
from goldmine.deadline import RequestTimeout, request_timeout
from goldmine.quality import rerank_candidates
from goldmine.rerank import LocalReranker, _SharedReranker
from goldmine.search import Hit
import goldmine.rerank as rerank


class RecordingTimer:
    """Cancellation cannot retract a callback that already started."""

    def __init__(self, interval, function, args=(), kwargs=None):
        self.interval, self.function = interval, function
        self.args, self.kwargs = args, kwargs or {}
        self.daemon = False
        self.cancelled = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.function(*self.args, **self.kwargs)


class CountingModel:
    def __init__(self):
        self.closes = 0

    def rank(self, query, documents):
        return [.5] * len(documents), {}

    def close(self):
        self.closes += 1


@pytest.fixture
def manual_timers(monkeypatch):
    monkeypatch.setattr(rerank.threading, "Timer", RecordingTimer)


class AbortRequest(BaseException):
    pass


@pytest.mark.parametrize("error", [None, RuntimeError, RequestTimeout, KeyboardInterrupt,
                                   SystemExit, AbortRequest])
def test_window_product_released_once_after_every_request_outcome(monkeypatch, error):
    model = CountingModel()
    app = Application("/unused", reranker_factory=lambda: model)
    request = SearchRequest("query", pack=True)
    sentinel = object()

    def retrieve(*args):
        assert args[-1] is model
        assert model.closes == 0
        if error is not None:
            raise error("request failed")
        return sentinel

    monkeypatch.setattr(app, "_retrieve_with_ranker", retrieve)
    if error is None:
        assert app._retrieve(None, None, request, request.scorer) is sentinel
    else:
        with pytest.raises(error, match="request failed"):
            app._retrieve(None, None, request, request.scorer)
    assert model.closes == 1


def test_listing_and_failed_factory_never_release_unacquired_product(monkeypatch):
    calls = []

    def factory():
        calls.append("factory")
        raise RuntimeError("model absent")

    app = Application("/unused", reranker_factory=factory)
    monkeypatch.setattr(app, "_retrieve_with_ranker", lambda *args: args[-1])
    listing = SearchRequest("query", pack=False)
    assert app._retrieve(None, None, listing, listing.scorer) is None
    assert calls == []
    packed = SearchRequest("query", pack=True)
    assert app._retrieve(None, None, packed, packed.scorer) is None
    assert calls == ["factory"]


def test_factory_timeout_is_not_a_fallback(monkeypatch):
    def factory():
        raise RequestTimeout("factory wait exceeded deadline")

    app = Application("/unused", reranker_factory=factory)
    monkeypatch.setattr(app, "_retrieve_with_ranker", lambda *args: pytest.fail("retrieval after timeout"))
    request = SearchRequest("query", pack=True)
    with pytest.raises(RequestTimeout, match="factory wait"):
        app._retrieve(None, None, request, request.scorer)


def test_quality_inner_lease_cannot_release_held_window_model(monkeypatch, manual_timers):
    model = CountingModel()
    owner = _SharedReranker(model, rerank.IDLE_RELEASE_S)
    balances = []

    def factory():
        owner.acquire()
        balances.append(owner._refs)
        return owner

    hit = Hit(1, 1, None, None, 1, "doc", "/a", 1, 1, "chunk", "body",
              "content_indexed", "lexical", segment_id=1)

    class Stored:
        def segment_text(self, segment_id):
            return "body"

    app = Application("/unused", reranker_factory=factory)

    def retrieve(*args):
        assert owner._refs == 1
        hits, meta, fallback = rerank_candidates(Stored(), "query", [hit], factory, why=False)
        assert meta["executed"] and fallback is None
        assert owner._refs == 1 and owner._timer is None and model.closes == 0
        return hits

    monkeypatch.setattr(app, "_retrieve_with_ranker", retrieve)
    request = SearchRequest("query", pack=True, mode="quality", rerank="local")
    assert len(app._retrieve(None, None, request, request.scorer)) == 1
    assert balances == [1, 2]
    assert owner._refs == 0 and model.closes == 0
    assert owner._timer.interval == 300.0
    owner._timer.fire()
    assert model.closes == 1


def test_cancelled_timer_cannot_reap_a_newer_idle_generation(manual_timers):
    model = CountingModel()
    owner = _SharedReranker(model, 300)
    owner.acquire()
    owner.close()
    obsolete = owner._timer
    owner.acquire()
    assert obsolete.cancelled
    obsolete.fire()
    assert model.closes == 0 and owner._refs == 1
    owner.close()
    current = owner._timer
    obsolete.fire()
    assert model.closes == 0 and owner._timer is current
    current.fire()
    current.fire()
    obsolete.fire()
    assert model.closes == 1


def test_timer_identity_is_published_before_start(monkeypatch):
    owner = _SharedReranker(CountingModel(), 0)

    class CheckedTimer(RecordingTimer):
        def start(self):
            assert owner._timer is self
            assert owner._idle_token is self.args[0]

    monkeypatch.setattr(rerank.threading, "Timer", CheckedTimer)
    owner.acquire()
    owner.close()
    owner._timer.fire()
    assert owner._ranker.closes == 1


def test_extra_release_does_not_restart_idle_timer(manual_timers):
    owner = _SharedReranker(CountingModel(), 300)
    owner.acquire()
    owner.close()
    timer = owner._timer
    owner.close()
    assert owner._refs == 0 and owner._timer is timer


def test_shutdown_waits_for_inflight_shared_operation_and_cancels_timer(manual_timers):
    entered, release, closing_started = threading.Event(), threading.Event(), threading.Event()
    order = []
    errors = []

    class Blocking(CountingModel):
        def rank(self, *args):
            order.append("rank entered")
            entered.set()
            assert release.wait(3)
            order.append("rank finished")
            return [], {}

        def close(self):
            order.append("closed")
            super().close()

    owner = _SharedReranker(Blocking(), 300)
    owner.acquire()

    def rank():
        try:
            owner.rank("q", ["doc"])
        except BaseException as exc:
            errors.append(exc)

    def shutdown():
        closing_started.set()
        try:
            owner.shutdown()
        except BaseException as exc:
            errors.append(exc)

    ranking = threading.Thread(target=rank)
    closing_thread = threading.Thread(target=shutdown)
    ranking.start()
    try:
        assert entered.wait(2)
        closing_thread.start()
        assert closing_started.wait(2)
        # Bookkeeping stays available while shutdown waits on the operation.
        owner.acquire()
        owner.close()
        assert "closed" not in order
    finally:
        release.set()
        ranking.join(3)
        if closing_thread.ident is not None:
            closing_thread.join(3)
        owner.close()
    assert not ranking.is_alive() and not closing_thread.is_alive() and not errors
    assert order == ["rank entered", "rank finished", "closed"]
    owner.shutdown()
    assert owner._timer is None


class BlockingLocal(LocalReranker):
    def __init__(self, entered, release):
        super().__init__(None, Path("/missing/test-model"))
        self.entered, self.release = entered, release
        self.calls = []

    def _startup_locked(self):
        pass

    def _bounded_text(self, text, maximum):
        return text, len(text)

    def _post(self, endpoint, payload):
        self.calls.append(payload["query"])
        if payload["query"] == "first":
            self.entered.set()
            assert self.release.wait(3)
        return {"results": [{"index": 0, "relevance_score": .5}]}


def test_second_model_operation_observes_its_wait_deadline():
    entered, release = threading.Event(), threading.Event()
    model = BlockingLocal(entered, release)
    results, errors = [], []

    def first():
        try:
            results.append(model.rank("first", ["doc"]))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert entered.wait(2)
        with pytest.raises(RequestTimeout, match="waiting for a backend"):
            with request_timeout(.03):
                model.rank("second", ["doc"])
        assert model.calls == ["first"]
    finally:
        release.set()
        thread.join(3)
        model.close()
    assert not thread.is_alive() and len(results) == 1 and not errors


def test_local_close_waits_for_rank_and_works_after_expired_deadline():
    entered, release, closing_started = threading.Event(), threading.Event(), threading.Event()
    model = BlockingLocal(entered, release)
    closed = []
    errors = []

    class Owned:
        def poll(self):
            return None

        def terminate(self):
            assert release.is_set()
            closed.append("terminated")

        def wait(self, timeout):
            return 0

    model._proc = Owned()
    def rank():
        try:
            model.rank("first", ["doc"])
        except BaseException as exc:
            errors.append(exc)

    ranking = threading.Thread(target=rank)

    def close():
        closing_started.set()
        try:
            model.close()
        except BaseException as exc:
            errors.append(exc)

    closing_thread = threading.Thread(target=close)
    ranking.start()
    try:
        assert entered.wait(2)
        closing_thread.start()
        assert closing_started.wait(2)
        assert model._proc is not None and not closed
    finally:
        release.set()
        ranking.join(3)
        if closing_thread.ident is not None:
            closing_thread.join(3)
    assert not ranking.is_alive() and not closing_thread.is_alive() and not errors
    assert model._proc is None and closed == ["terminated"]
    model._proc = Owned()
    with pytest.raises(RequestTimeout):
        with request_timeout(.001):
            time.sleep(.005)
            model.close()
    assert model._proc is None and closed == ["terminated", "terminated"]


def test_registry_cleanup_continues_after_one_owner_error(monkeypatch, manual_timers):
    class Broken(CountingModel):
        def close(self):
            raise RuntimeError("owned child cleanup failed")

    broken, healthy = Broken(), CountingModel()
    first, second = _SharedReranker(broken, 300), _SharedReranker(healthy, 300)
    for owner in (first, second):
        owner.acquire()
        owner.close()
    monkeypatch.setattr(rerank, "_SHARED", {"a": first, "b": second})
    monkeypatch.setattr(rerank, "_FROM_CONFIG_CACHE", {"a": broken, "b": healthy})
    with pytest.raises(RuntimeError, match="cleanup failed"):
        rerank._close_cached_rankers()
    assert first._timer is None and second._timer is None
    assert healthy.closes == 1


def test_new_child_stamps_current_owner_even_if_ranker_was_created_earlier(tmp_path, monkeypatch):
    server, path = tmp_path / "server", tmp_path / "model"
    server.write_bytes(b"fake binary")
    path.write_bytes(b"x")
    entry = SimpleNamespace(bytes=1, sha256=hashlib.sha256(b"x").hexdigest())
    model = LocalReranker(server, path)
    earlier_pid = model._owner_pid
    terminated = []

    class Owned:
        def poll(self):
            return None

        def terminate(self):
            terminated.append(True)

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(rerank, "by_key", lambda *args: entry)
    monkeypatch.setattr(rerank.os, "getpid", lambda: earlier_pid + 1000)
    monkeypatch.setattr(rerank.subprocess, "Popen", lambda *args, **kwargs: Owned())
    monkeypatch.setattr(rerank, "http_request", lambda *args, **kwargs: SimpleNamespace(status=200))
    model.startup()
    assert model._owner_pid == earlier_pid + 1000
    model.close()
    assert terminated == [True]


def test_fork_child_cleanup_skips_parent_process_and_inherited_locks(tmp_path):
    bootstrap = """
import hashlib,json,os,subprocess,sys
from pathlib import Path
from types import SimpleNamespace
import goldmine.rerank as rr
model=rr.LocalReranker(None,Path('/missing/probe-model'))
model._proc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],
    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
rr._FROM_CONFIG_CACHE[('test','test')]=model
# Construct another instance before fork, but let the child start its model.
server,path=Path(sys.argv[1])/'server',Path(sys.argv[1])/'model'
server.write_bytes(b'fake binary');path.write_bytes(b'x')
future=rr.LocalReranker(server,path)
rr._FROM_CONFIG_CACHE[('future','future')]=future
owner=rr._SharedReranker(model,rr.IDLE_RELEASE_S)
rr._SHARED[('test','test')]=owner
read_fd,write_fd=os.pipe()
rr._SHARED_LOCK.acquire()
rr._FROM_CONFIG_LOCK.acquire()
owner._lock.acquire()
forked=os.fork()
if forked==0:
    os.close(read_fd)
    spawn=subprocess.Popen
    rr.by_key=lambda *args:SimpleNamespace(bytes=1,sha256=hashlib.sha256(b'x').hexdigest())
    rr.http_request=lambda *args,**kwargs:SimpleNamespace(status=200)
    rr.subprocess.Popen=lambda *args,**kwargs:spawn(
        [sys.executable,'-c','import time;time.sleep(60)'],**kwargs)
    future.startup()
    owned=future._proc
    rr._close_cached_rankers()
    model.close()
    os.write(write_fd,json.dumps({'fork_owned_reaped':owned.poll() is not None,
        'new_owner_pid':future._owner_pid,'fork_pid':os.getpid()}).encode())
    os.close(write_fd)
    os._exit(0)
os.close(write_fd)
try:
    _,status=os.waitpid(forked,0)
    fork_result=json.loads(os.read(read_fd,4096))
    print(json.dumps({'fork_exit':os.waitstatus_to_exitcode(status),
        'model_survived_child_cleanup':model._proc.poll() is None,
        'owned_child_pid':model._proc.pid,**fork_result}),flush=True)
finally:
    os.close(read_fd)
    owner._lock.release()
    rr._FROM_CONFIG_LOCK.release()
    rr._SHARED_LOCK.release()
"""
    parent = subprocess.Popen([sys.executable, "-B", "-c", bootstrap, str(tmp_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              start_new_session=True)
    try:
        stdout, stderr = parent.communicate(timeout=5)
        assert parent.returncode == 0, (stdout, stderr)
        result = json.loads(stdout)
        assert result["fork_exit"] == 0 and result["model_survived_child_cleanup"]
        assert result["fork_owned_reaped"] and result["new_owner_pid"] == result["fork_pid"]
        with pytest.raises(ProcessLookupError):
            os.kill(result["owned_child_pid"], 0)
        assert not stderr
    finally:
        # Only this test's newly-created process group can contain its probe
        # child if an assertion or inherited-lock regression prevented exit.
        try:
            os.killpg(parent.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if parent.poll() is None:
            parent.wait(timeout=2)


@pytest.mark.parametrize("shared", [False, True])
def test_normal_process_exit_reaps_owned_cached_child(shared):
    bootstrap = """
import json, subprocess, sys
from pathlib import Path
import goldmine.rerank as rr
model=rr.LocalReranker(None,Path('/missing/probe-model'))
model._proc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],
    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
rr._FROM_CONFIG_CACHE[('test','test')]=model
if sys.argv[1]=='shared':
    owner=rr._SharedReranker(model,rr.IDLE_RELEASE_S)
    rr._SHARED[('test','test')]=owner
    owner.acquire();owner.close()
print(json.dumps({'child':model._proc.pid}),flush=True)
"""
    child = None
    parent = subprocess.Popen([sys.executable, "-B", "-c", bootstrap,
                               "shared" if shared else "direct"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = parent.communicate(timeout=5)
        assert parent.returncode == 0, (stdout, stderr)
        child = json.loads(stdout)["child"]
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
        child = None
        assert not stderr
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=2)
        if child is not None:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
