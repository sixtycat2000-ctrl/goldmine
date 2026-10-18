"""Tier-2: LlamaServerBackend client logic against tests/fake_llama_server.py.

Exercises the full subprocess + HTTP path (spawn argv, /health readiness,
port-pick, batching, deadlines, retry-once, dim assertion, shutdown) with NO
real llama-server binary and NO network beyond 127.0.0.1.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from goldmine.backends.base import BackendStartupError, EmbeddingError, NotSupported
from goldmine.backends.llama import LlamaServerBackend, resolve_server_path
from goldmine.config import LlamaCfg

FAKE_SERVER = Path(__file__).resolve().parent / "fake_llama_server.py"


def _fake_model(tmp_path: Path) -> Path:
    model = tmp_path / "fake-embedding-model.gguf"
    model.write_bytes(b"GGUF" + b"\x00" * 1024)  # any bytes: only the hash matters
    return model


@pytest.fixture
def fake_cfg(tmp_path) -> LlamaCfg:
    FAKE_SERVER.chmod(
        FAKE_SERVER.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return LlamaCfg(
        server=FAKE_SERVER,
        server_configured=True,  # explicit: never resolve to the vendor build
        model=_fake_model(tmp_path),
        ctx=512,
        batch=2,
        startup_timeout=10.0,
        request_timeout=10.0,
    )


def _stats(backend: LlamaServerBackend) -> dict:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{backend._port}/stats", timeout=5
    ) as resp:
        return json.loads(resp.read())


def _control(backend: LlamaServerBackend, **payload) -> None:
    """Inject faults into the RUNNING fake server (post startup-probe)."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{backend._port}/control",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5):
        pass


def test_startup_readiness_and_batching(fake_cfg):
    with LlamaServerBackend(fake_cfg) as backend:
        assert backend.server_source == "env/config"  # cfg.server != brew default
        vectors = backend.embed_texts(["alpha one", "beta two", "gamma three"])
        assert vectors.shape == (3, 768)
        norms = np.linalg.norm(vectors, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-3)  # l2_post normalization
        # batch=2 -> ceil(3/2)=2 POSTs; +1 for the startup dim-probe embed
        assert _stats(backend)["embed_requests"] == 1 + 2
        assert _stats(backend)["embed_texts"] == 1 + 3


def test_deterministic_and_prompt_agnostic(fake_cfg):
    with LlamaServerBackend(fake_cfg) as backend:
        first = backend.embed_texts(["same text"])
        second = backend.embed_texts(["same text"])
        assert np.allclose(first, second)  # deterministic across calls
        other = backend.embed_texts(["different text"])
        assert not np.allclose(first, other)


def test_foreign_server_on_port_is_never_adopted(fake_cfg, monkeypatch):
    """A healthy server already on the picked port: pick another port, start
    there, leave the foreign one running (probe-then-start)."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        foreign_port = sock.getsockname()[1]
    foreign = subprocess.Popen(
        [sys.executable, str(FAKE_SERVER), "--port", str(foreign_port),
         "--host", "127.0.0.1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{foreign_port}/health", timeout=1
                ) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        real_free_port = LlamaServerBackend._free_port
        state = {"picks": 0}

        def scripted_free_port(self):
            # exactly one colliding pick; every later pick is a real free port
            state["picks"] += 1
            return foreign_port if state["picks"] == 1 else real_free_port(self)
        monkeypatch.setattr(LlamaServerBackend, "_free_port", scripted_free_port)

        with LlamaServerBackend(fake_cfg) as backend:
            assert backend._port != foreign_port
            assert backend.embed_texts(["still works"]).shape == (1, 768)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{foreign_port}/health", timeout=2
        ) as resp:
            assert resp.status == 200  # untouched
    finally:
        foreign.terminate()
        foreign.wait(timeout=5)


def _cfg(tmp_path: Path, **overrides) -> LlamaCfg:
    base = dict(
        server=FAKE_SERVER,
        server_configured=True,
        model=_fake_model(tmp_path),
        batch=2,
        startup_timeout=10.0,
        request_timeout=5.0,
    )
    base.update(overrides)
    return LlamaCfg(**base)


def test_startup_timeout_raises_with_log_tail(tmp_path):
    cfg = _cfg(tmp_path, startup_timeout=0.4)  # fake 503s /health forever -> x3
    backend = LlamaServerBackend(cfg)
    backend._spawn = _spawn_with_extra_args("--health-fail", "999").__get__(
        backend, LlamaServerBackend
    )
    with pytest.raises(BackendStartupError) as excinfo:
        backend.startup()
    assert "failed to start" in str(excinfo.value)
    assert backend._proc is None  # reaped, nothing left running


def test_crash_on_start_raises_backend_startup_error(tmp_path):
    cfg = _cfg(tmp_path)
    backend = LlamaServerBackend(cfg)
    backend._spawn = _spawn_with_extra_args("--crash-on-start").__get__(
        backend, LlamaServerBackend
    )
    with pytest.raises(BackendStartupError):
        backend.embed_texts(["never served"])
    tail = backend.stderr_tail()
    assert "crash-on-start" in tail  # bounded log ring captured the child output


def test_wrong_dim_refused_at_start(tmp_path):
    cfg = _cfg(tmp_path)
    backend = LlamaServerBackend(cfg)
    backend._spawn = _spawn_with_extra_args("--wrong-dim", "256").__get__(
        backend, LlamaServerBackend
    )
    with pytest.raises(BackendStartupError) as excinfo:
        backend.startup()
    message = str(excinfo.value)
    assert "256" in message and "768" in message  # dim mismatch surfaced at start


def _spawn_with_extra_args(*extra):
    """Spawn helper: the real argv (mirrors llama.py:_spawn — fixed -np 2,
    -c/-b = ctx*2, -ub = ctx) plus fake-server-only test flags."""

    def spawn(self, port):
        from goldmine.backends.llama import SERVER_SLOTS

        argv = [
            str(self.server_path),
            "--embeddings", "--pooling", "mean",
            "-m", str(self.model_path()),
            "-c", str(int(self.cfg.ctx) * SERVER_SLOTS),
            "-b", str(int(self.cfg.ctx) * SERVER_SLOTS),
            "-ub", str(int(self.cfg.ctx)),
            "-np", str(SERVER_SLOTS),
            "--host", "127.0.0.1", "--port", str(port),
            *extra,
        ]
        return subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )

    return spawn


def test_request_deadline_raises_embedding_error_with_tail(fake_cfg, monkeypatch):
    fake_cfg.request_timeout = 0.5
    with LlamaServerBackend(fake_cfg) as backend:
        _control(backend, delay=2.0)  # every embed now takes 2s > 0.5s deadline
        with pytest.raises(EmbeddingError) as excinfo:
            backend.embed_texts(["slow"])
        assert not isinstance(excinfo.value, BackendStartupError)
        assert excinfo.value.tail  # ring-buffer tail attached


def test_embed_500_retried_once_then_succeeds(fake_cfg):
    with LlamaServerBackend(fake_cfg) as backend:
        _control(backend, embed_fail=1)  # the NEXT embeddings request 500s once
        vectors = backend.embed_texts(["retry me"])
        assert vectors.shape == (1, 768)
        stats = _stats(backend)
        assert stats["embed_failures"] == 1  # first 500, retry succeeded


def test_embed_500_forever_is_embedding_error(fake_cfg):
    with LlamaServerBackend(fake_cfg) as backend:
        _control(backend, embed_fail=999)  # every request from here 500s
        with pytest.raises(EmbeddingError):
            backend.embed_texts(["doomed"])


def test_close_kills_only_our_child(fake_cfg):
    backend = LlamaServerBackend(fake_cfg)
    backend.startup()
    child = backend._proc
    assert child is not None and child.poll() is None
    backend.close()
    assert child.poll() is not None  # SIGTERM reaped our child only


def test_fingerprint_fields_and_model_hash(fake_cfg, tmp_path):
    backend = LlamaServerBackend(fake_cfg)
    fields = backend.fingerprint_fields()
    assert fields["model_id"] == "fake-embedding-model"
    assert fields["normalization"] == "l2_post"
    assert "task: search result | query:" in fields["prompts"]
    expected = __import__("hashlib").sha256(fake_cfg.model.read_bytes()).hexdigest()
    assert fields["model_sha256"] == expected
    # sidecar cache written next to the model
    sidecar = Path(str(fake_cfg.model) + ".sha256")
    assert json.loads(sidecar.read_text())["sha256"] == expected


def test_capability_text_only_and_not_supported(fake_cfg):
    backend = LlamaServerBackend(fake_cfg)
    assert backend.capability().text is True
    assert backend.capability().image is False
    assert backend.capability().audio is False
    with pytest.raises(NotSupported):
        backend.embed_image(Path("/dev/null"))
    with pytest.raises(NotSupported):
        backend.embed_audio_wav(b"RIFF....")


def test_model_missing_is_startup_error(tmp_path, monkeypatch):
    # v2: with no explicit model, resolution falls back to <home>/models and
    # the repo models/ pin — point the repo leg at an empty dir so this test
    # keeps exercising the original intent: model missing everywhere -> error.
    import goldmine.backends.llama as llama

    monkeypatch.setattr(llama, "REPO_MODELS", tmp_path / "empty")
    cfg = LlamaCfg(
        server=FAKE_SERVER, server_configured=True,
        model=tmp_path / "absent.gguf",
    )
    with pytest.raises(BackendStartupError) as excinfo:
        LlamaServerBackend(cfg).startup()
    assert "not found" in str(excinfo.value)


def test_pinned_model_hash_enforced(tmp_path):
    model = tmp_path / "embeddinggemma-2-Q8_0.gguf"  # pinned basename, wrong bytes
    model.write_bytes(b"not the real model")
    cfg = LlamaCfg(server=FAKE_SERVER, server_configured=True, model=model)
    with pytest.raises(BackendStartupError) as excinfo:
        LlamaServerBackend(cfg).startup()
    assert "sha256 mismatch" in str(excinfo.value)


def test_resolve_server_path_order(tmp_path, monkeypatch):
    """env/config (explicit) > vendor > brew; the DEFAULT brew value falls
    through to vendor so a vendor build always beats the brew default."""
    import goldmine.backends.llama as llama
    from goldmine.config import LlamaCfg as LC

    vendor_bin = tmp_path / "vendor-llama-server"
    vendor_bin.write_text("#!/bin/sh\n")
    vendor_bin.chmod(0o755)
    brew = tmp_path / "brew-llama-server"
    brew.write_text("#!/bin/sh\n")
    brew.chmod(0o755)
    explicit = tmp_path / "my-llama-server"
    explicit.write_text("#!/bin/sh\n")
    explicit.chmod(0o755)
    monkeypatch.setattr(llama, "VENDOR_SERVER", vendor_bin)
    monkeypatch.setattr(llama, "BREW_SERVER", brew)

    # default (server_configured=False): vendor wins over the brew default
    resolved, source = llama.resolve_server_path(LC())
    assert source == "vendor" and resolved == vendor_bin

    # explicit env/config path wins even when it points at the brew binary
    resolved2, source2 = llama.resolve_server_path(
        LC(server=explicit, server_configured=True)
    )
    assert source2 == "env/config" and resolved2 == explicit

    # no vendor, no explicit -> brew default value resolves as "brew"
    monkeypatch.setattr(llama, "VENDOR_SERVER", tmp_path / "absent-vendor")
    resolved3, source3 = llama.resolve_server_path(LC())
    assert source3 == "brew" and resolved3 == brew

    # nothing exists -> (None, "none")
    monkeypatch.setattr(llama, "BREW_SERVER", tmp_path / "absent-brew")
    monkeypatch.setattr(llama, "VENDOR_SERVER", tmp_path / "absent-vendor")
    resolved4, source4 = llama.resolve_server_path(LC())
    assert resolved4 is None and source4 == "none"


def test_explicit_missing_server_never_falls_back(tmp_path, monkeypatch):
    """P3-14: an explicitly configured llama.server/GOLDMINE_LLAMA_SERVER that
    does not exist resolves to (None, "explicit") and startup refuses with a
    fix hint — the vendor/brew fallback applies ONLY when nothing is explicit."""
    import goldmine.backends.llama as llama

    vendor = tmp_path / "vendor-llama-server"
    vendor.write_text("#!/bin/sh\n")
    vendor.chmod(0o755)
    monkeypatch.setattr(llama, "VENDOR_SERVER", vendor)  # a fallback EXISTS

    cfg = LlamaCfg(
        server=tmp_path / "absent-my-server", server_configured=True,
        model=_fake_model(tmp_path),
    )
    resolved, source = llama.resolve_server_path(cfg)
    assert resolved is None and source == "explicit"

    with pytest.raises(BackendStartupError) as excinfo:
        LlamaServerBackend(cfg).startup()
    message = str(excinfo.value)
    assert "configured llama-server not found" in message
    assert "GOLDMINE_LLAMA_SERVER" in message
    # and the vendor binary was NOT silently adopted
    assert str(vendor) not in message


def test_describe_does_not_spawn_server(fake_cfg, monkeypatch):
    """Availability probing must never spawn llama-server (backends cmd)."""
    spawned = []
    real_popen = subprocess.Popen

    def guarded_popen(*args, **kwargs):
        spawned.append(args[0])
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("goldmine.backends.llama.subprocess.Popen", guarded_popen)
    backend = LlamaServerBackend(fake_cfg)
    info = backend.describe()
    assert info["available"] is True
    assert info["capability"] == {"text": True, "image": False, "audio": False}
    assert info["server_source"] == "env/config"  # explicit cfg.server honored
    assert info["build"]  # --version ran (a fast exiting subprocess, not a server)
    assert "gemma-embedding2" in info["note"]  # brew-vs-vendor explanation
    # only the --version probe ran; no long-lived server child
    assert all("--port" not in str(a) for a in spawned)
