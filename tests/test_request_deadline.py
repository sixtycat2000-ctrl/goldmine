"""Request deadlines cap real blocking calls and prevent retries/cold starts."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from goldmine.backends.llama import LlamaServerBackend
from goldmine.backends.serve import RemoteBackend, ServeRequestError
from goldmine.config import Config, LlamaCfg
from goldmine.deadline import Deadline, RequestTimeout, current_deadline, request_timeout


def test_nested_deadline_never_extends_parent_and_resets():
    with request_timeout(1) as parent:
        with request_timeout(60) as child:
            assert child == parent
        assert current_deadline() == parent
    assert current_deadline() is None
    with pytest.raises(ValueError):
        Deadline.after(float("inf"))


def test_context_deadline_does_not_leak_to_another_thread():
    observed = []
    with request_timeout(1):
        worker = threading.Thread(target=lambda: observed.append(current_deadline()))
        worker.start(); worker.join(1)
    assert observed == [None]


def test_expired_cold_start_never_spawns(tmp_path, monkeypatch):
    backend = LlamaServerBackend(LlamaCfg(model=tmp_path / "absent.gguf"))
    calls = []
    monkeypatch.setattr(backend, "_spawn", lambda port: calls.append(port))
    with pytest.raises(RequestTimeout):
        with request_timeout(0.01):
            time.sleep(0.02)
            backend.startup()
    assert not calls


def test_remote_deadline_closes_call_without_reconnect_or_cold(tmp_path, monkeypatch):
    backend = RemoteBackend(Config(), tmp_path / "unused.sock")
    client, server = socket.socketpair()
    backend._sock = client
    received = []

    def peer():
        with server:
            received.append(server.recv(4096))
            received.append(server.recv(4096))  # closes at timeout, not a retry

    thread = threading.Thread(target=peer)
    thread.start()
    monkeypatch.setattr("goldmine.backends.serve.connect_socket", lambda *a: pytest.fail("reconnected after deadline"))
    started = time.monotonic()
    try:
        with pytest.raises(RequestTimeout):
            with request_timeout(0.05):
                backend.embed_texts(["slow"])
        assert time.monotonic() - started < 0.5
        assert backend._cold is None
        thread.join(1)
        assert not thread.is_alive()
        assert received[1] == b""
    finally:
        backend.close()
        client.close()
        server.close()


@pytest.mark.parametrize("operation", ["embed", "count"])
def test_late_remote_error_never_retries_or_hides_timeout(tmp_path, monkeypatch, operation):
    backend = RemoteBackend(Config(), tmp_path / "unused.sock")
    calls = []

    def late_error(*args, **kwargs):
        calls.append(args)
        time.sleep(0.03)
        raise ServeRequestError("late failure")

    monkeypatch.setattr(backend, "_request", late_error)
    with pytest.raises(RequestTimeout):
        with request_timeout(0.01):
            if operation == "embed":
                backend.embed_texts(["q"])
            else:
                backend.count_tokens(["q"])
    assert len(calls) == 1
    assert backend._cold is None


def test_actual_llama_http_timeout_does_not_retry(tmp_path):
    fake = Path(__file__).with_name("fake_llama_server.py")
    fake.chmod(fake.stat().st_mode | 0o111)
    model = tmp_path / "fake.gguf"
    model.write_bytes(b"GGUF-test-model")
    cfg = LlamaCfg(server=fake, server_configured=True, model=model,
                   startup_timeout=10, request_timeout=10)
    with LlamaServerBackend(cfg) as backend:
        url = f"http://127.0.0.1:{backend._port}"
        req = urllib.request.Request(url + "/control", data=b'{"delay":0.5}',
                                     headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=2):
            pass
        with urllib.request.urlopen(url + "/stats", timeout=2) as response:
            before = json.loads(response.read())["embed_requests"]
        started = time.monotonic()
        with pytest.raises(RequestTimeout):
            with request_timeout(0.05):
                backend.embed_texts(["slow"])
        assert time.monotonic() - started < 0.5
        with urllib.request.urlopen(url + "/stats", timeout=2) as response:
            after = json.loads(response.read())["embed_requests"]
        assert after - before == 1


def test_deadline_during_actual_startup_reaps_owned_process(tmp_path, monkeypatch):
    fake = Path(__file__).with_name("fake_llama_server.py")
    fake.chmod(fake.stat().st_mode | 0o111)
    model = tmp_path / "fake.gguf"
    model.write_bytes(b"GGUF-startup-test")
    backend = LlamaServerBackend(LlamaCfg(
        server=fake, server_configured=True, model=model, startup_timeout=10,
    ))
    monkeypatch.setattr(backend, "_wait_healthy", lambda *a: time.sleep(0.6))
    spawned = []
    original = backend._spawn

    def record_spawn(port):
        proc = original(port)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(backend, "_spawn", record_spawn)
    try:
        with pytest.raises(RequestTimeout):
            with request_timeout(0.5):
                backend.startup()
        assert spawned
        assert all(proc.poll() is not None for proc in spawned)
        assert backend._proc is None
    finally:
        backend.close()
