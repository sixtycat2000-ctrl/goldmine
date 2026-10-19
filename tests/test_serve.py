"""R8 offline suite: the serve daemon + RemoteBackend client against the fake
llama-server (tier-2 discipline: NO real llama-server, NO network beyond UDS
+ 127.0.0.1, NO mocks of the daemon itself — it runs for real in a thread or
subprocess on a tmp_path socket).

Covers spec §7.1 T1-T36 as amended by docs/v2.1-r8-serve.md §12:
protocol round-trips + caps (F6), ABC/fingerprint parity incl. the T8
cross-batch byte-equality (F10), discovery liveness, model drift (F2 sidecar
check), lifecycle (flock/SIGTERM-drain/SIGKILL-orphan-sweep verify-before-kill
F3), linger (F7), guard hysteresis + mid-embed unload (F5), serve status/stop,
doctor rows, config (F9: no `auto` key), fuzz-lite, and the semantics freeze
(stdout byte-equality serve vs cold; `via:` footer on stderr, D6).
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import pytest

from goldmine.backends.base import NotSupported
from goldmine.backends.llama import LlamaServerBackend
from goldmine.backends.serve import (
    PROTO,
    LineReader,
    ServeRequestError,
    connect_or_cold,
    connect_socket,
    one_shot_request,
    recv_response,
    send_line,
)
from goldmine.config import Config, LlamaCfg, ServeCfg, load
from goldmine.serve import (
    ServeDaemon,
    ServeOpError,
    _render_status_human,
    read_discovery_file,
    verify_and_kill,
)

FAKE_SERVER = Path(__file__).resolve().parent / "fake_llama_server.py"
REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT))


# --------------------------------------------------------------------- helpers


def _fake_model(tmp_path: Path, name: str = "fake-embedding-model.gguf") -> Path:
    model = tmp_path / name
    model.write_bytes(b"GGUF" + b"\x00" * 1024)  # any bytes: only the hash matters
    return model


def short_sock_dir() -> Path:
    """A SHORT fresh socket dir under the system tmp — pytest's tmp_path
    prefixes blow the macOS 104-byte sun_path cap, and the daemon (rightly)
    refuses to bind one. Each test gets its own dir so the daemon's 0700 chmod
    only ever touches a directory this test created."""
    return Path(tempfile.gettempdir()) / f"gm-svc-{os.getuid()}-{uuid.uuid4().hex[:10]}"


def make_cfg(tmp_path: Path, *, model: Path | None = None,
             sock: Path | None = None, **llama_overrides) -> Config:
    cfg = Config(
        home=tmp_path / "home",
        llama=LlamaCfg(
            server=FAKE_SERVER,
            server_configured=True,  # explicit: never resolve to the vendor build
            model=model or _fake_model(tmp_path),
            ctx=512,
            batch=16,
            startup_timeout=10.0,
            request_timeout=10.0,
            **llama_overrides,
        ),
        serve=ServeCfg(
            linger=60, timeout_s=10.0,
            socket=str(sock or (short_sock_dir() / "s.sock")),
        ),
    )
    cfg.llama.home = cfg.home
    return cfg


class DaemonBox:
    """A started in-process daemon + its thread, with deterministic teardown."""

    def __init__(self, daemon: ServeDaemon, thread: threading.Thread):
        self.daemon = daemon
        self.thread = thread

    def stop(self, timeout: float = 15.0) -> None:
        self.daemon.request_stop("test teardown")
        self.thread.join(timeout)


def start_daemon(cfg: Config, **kwargs) -> DaemonBox:
    kwargs.setdefault("sampler", lambda: 1)  # pressure-immune unless a guard test overrides
    daemon = ServeDaemon(cfg, **kwargs)
    thread = threading.Thread(
        target=daemon.serve_forever, daemon=True, name="goldmine-serve-test"
    )
    thread.start()
    assert daemon.ready.wait(10), "daemon did not become ready"
    return DaemonBox(daemon, thread)


def raw_roundtrip(sock_path, payload: dict, timeout: float = 10.0) -> dict:
    """Raw protocol access for the tests: returns the RESPONSE DICT as-is (so
    ok:false error objects are inspectable, unlike the client's recv_response
    which raises on them)."""
    sock = connect_socket(sock_path, 2.0)
    try:
        send_line(sock, payload)
        sock.settimeout(timeout)
        line = LineReader(sock).readline()
        assert line is not None, "daemon closed the connection (EOF)"
        return json.loads(line.decode("utf-8"))
    finally:
        sock.close()


def scripted_sampler(levels, default: int = 1):
    seq = list(levels)

    def sample() -> int:
        return seq.pop(0) if seq else default

    return sample


def embed_via(backend, texts):
    return backend.embed_texts(texts)


@pytest.fixture
def daemon_box(tmp_path):
    """A ready daemon with the fake server; box.sock is the (short) UDS path."""
    cfg = make_cfg(tmp_path)
    box = start_daemon(cfg, sampler=lambda: 1)  # pressure-immune by default
    box.sock = cfg.serve.socket
    yield box
    box.stop()


@pytest.fixture
def serve_env(tmp_path, monkeypatch):
    """cli-main-level env: everything redirected into tmp (the suite-wide
    tmp_home discipline, plus the serve specifics)."""
    home = tmp_path / "home"
    home.mkdir()
    cfgdir = tmp_path / "cfg"
    cfgdir.mkdir()
    (cfgdir / "config.toml").write_text("")
    model = _fake_model(tmp_path)
    # NOTE: HOME is deliberately NOT redirected — user-site packages (numpy)
    # resolve through HOME and the SUBPROCESS daemon needs them. Isolation is
    # provided by GOLDMINE_HOME/GOLDMINE_CONFIG, which every code path honors.
    monkeypatch.setenv("GOLDMINE_HOME", str(home))
    monkeypatch.setenv("GOLDMINE_CONFIG", str(cfgdir / "config.toml"))
    monkeypatch.setenv("GOLDMINE_MODEL", str(model))
    monkeypatch.setenv("GOLDMINE_LLAMA_SERVER", str(FAKE_SERVER))
    env_sock = short_sock_dir() / "s.sock"
    monkeypatch.setenv("GOLDMINE_SERVE", str(env_sock))
    monkeypatch.delenv("GOLDMINE_BACKEND", raising=False)

    # a plain namespace (class bodies do not close over function locals)
    class Env:
        pass

    env = Env()
    env.home = home
    env.model = model
    env.sock = env_sock
    env.config = cfgdir / "config.toml"

    def cfg() -> Config:
        return load(config_path=env.config)

    env.cfg = cfg
    return env


def _subprocess_daemon(serve_env, extra_args=()) -> subprocess.Popen:
    env = os.environ.copy()
    env["GOLDMINE_SERVE_PRESSURE_LEVEL"] = "1"  # deterministic: never guard-skip
    proc = subprocess.Popen(
        [sys.executable, "-m", "goldmine", "serve", *extra_args],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if serve_env.sock.exists() and read_discovery_file(serve_env.cfg()):
            return proc
        if proc.poll() is not None:
            out, err = proc.communicate(timeout=5)
            raise AssertionError(f"daemon exited early rc={proc.returncode}: {err.decode()[-2000:]}")
        time.sleep(0.05)
    proc.terminate()
    raise AssertionError("daemon never became ready")


def _alive(pid) -> bool:
    from goldmine.backends.serve import pid_alive

    return pid is not None and pid_alive(pid)


def _child_pid(box: DaemonBox) -> int | None:
    return (box.daemon.child_receipt or {}).get("pid")


# =============================================================== T1-T8 protocol


def test_ping_round_trip_id_echo(daemon_box):
    sock_path = str(daemon_box.sock)
    resp = raw_roundtrip(sock_path, {"op": "ping", "id": "abc", "pid": 424242})
    assert resp["ok"] is True
    assert resp["op"] == "ping"
    assert resp["id"] == "abc"  # echoed (D2)
    assert resp["proto"] == PROTO == 1
    assert resp["goldmine_version"] == __import__("goldmine").__version__
    assert isinstance(resp["pid"], int)
    assert resp["uptime_s"] >= 0
    # F8: a CONNECTED client carrying its pid shows in the registry (a closed
    # connection leaves it — the registry is the live client set, D4)
    sock = connect_socket(sock_path, 2.0)
    try:
        send_line(sock, {"op": "ping", "id": "reg", "pid": 424242})
        assert json.loads(LineReader(sock).readline().decode())["ok"] is True
        status = one_shot_request(sock_path, "status")
        assert any(c["pid"] == 424242 for c in status["clients"])
    finally:
        sock.close()


def test_info_payload_shape_and_fresh_sha(daemon_box, tmp_path):
    resp = raw_roundtrip(str(daemon_box.sock), {"op": "info"})
    assert resp["backend_id"] == "llama"
    assert resp["dim"] == 768
    assert resp["capability"] == {"text": True, "image": False, "audio": False}
    assert resp["max_input_tokens"] == 512  # from cfg.llama.ctx (D1)
    assert resp["resident"] is False  # lazy spawn: nothing embedded yet (D6)
    import hashlib

    expected = hashlib.sha256((tmp_path / "fake-embedding-model.gguf").read_bytes()).hexdigest()
    fields = resp["fingerprint_fields"]
    assert fields["model_sha256"] == expected  # F2: computed fresh from bytes
    assert fields["model_id"] == "fake-embedding-model"
    assert fields["pooling"] == "server:mean"
    assert fields["normalization"] == "l2_post"
    assert "task: search result | query:" in fields["prompts"]


def test_embed_shape_and_l2_norms(daemon_box):
    resp = raw_roundtrip(
        str(daemon_box.sock),
        {"op": "embed", "texts": ["alpha one", "beta two", "gamma three"]},
        timeout=30,
    )
    assert resp["ok"] is True
    assert resp["dim"] == 768
    vectors = np.asarray(resp["vectors"], dtype=np.float32)
    assert vectors.shape == (3, 768)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-3)
    status = one_shot_request(str(daemon_box.sock), "status")
    assert status["resident"] is True  # lazy spawn happened on the embed
    assert status["child_pid"]


def test_unknown_op_is_bad_request_and_connection_reusable(daemon_box):
    sock = connect_socket(str(daemon_box.sock), 2.0)
    try:
        send_line(sock, {"op": "bogus_op"})
        resp = json.loads(LineReader(sock).readline().decode())
        assert resp["ok"] is False
        assert resp["error"]["kind"] == "bad_request"
        # the connection SURVIVES a bad request (D2)
        send_line(sock, {"op": "ping"})
        assert json.loads(LineReader(sock).readline().decode())["ok"] is True
    finally:
        sock.close()


def test_oversized_line_closes_connection_daemon_survives(daemon_box):
    sock_path = str(daemon_box.sock)
    sock = connect_socket(sock_path, 5.0)
    try:
        sock.sendall(b"x" * (17 * 1024 * 1024) + b"\n")  # > 16 MiB MAX_LINE
        reader = LineReader(sock)
        assert reader.readline() is None  # connection closed, no response
    except OSError:
        pass  # a mid-send reset is an equally honest close
    finally:
        sock.close()
    # the daemon itself is fine (T5)
    assert raw_roundtrip(sock_path, {"op": "ping"})["ok"] is True


def test_malformed_json_error_connection_stays_open(daemon_box):
    sock = connect_socket(str(daemon_box.sock), 2.0)
    try:
        sock.sendall(b"this is not json\n")
        resp = json.loads(LineReader(sock).readline().decode())
        assert resp["ok"] is False and resp["error"]["kind"] == "bad_request"
        send_line(sock, {"op": "ping"})
        assert json.loads(LineReader(sock).readline().decode())["ok"] is True  # reusable (T24 basis)
    finally:
        sock.close()


def test_embed_cap_65_texts_bad_request(daemon_box):
    resp = raw_roundtrip(
        str(daemon_box.sock),
        {"op": "embed", "texts": [f"t{i}" for i in range(65)]},
    )
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "bad_request"
    assert "too many texts" in resp["error"]["message"]


def test_embed_cap_256kib_text_bad_request(daemon_box, tmp_path):
    big = "x" * (256 * 1024 + 1)
    resp = raw_roundtrip(str(daemon_box.sock), {"op": "embed", "texts": [big]})
    assert resp["ok"] is False
    assert resp["error"]["kind"] == "bad_request"
    assert "KiB" in resp["error"]["message"]


def test_embed_cap_ordering_and_line_cap_wall(daemon_box):
    """F6: the caps are validated COUNT FIRST (unit-level — with these numbers
    a both-violated request cannot also fit the 16 MiB line, so the wire path
    hits the documented hard wall: line cap closes the connection, which the
    second half of this test pins)."""
    both = {"op": "embed", "texts": ["x" * (300 * 1024)] * 65}
    with pytest.raises(ServeOpError) as exc:
        daemon_box.daemon._validate_texts(both)
    assert "too many texts" in exc.value.message  # the count error wins (F6)
    only_count = {"op": "embed", "texts": ["t"] * 65}
    with pytest.raises(ServeOpError) as exc2:
        daemon_box.daemon._validate_texts(only_count)
    assert "too many texts" in exc2.value.message
    only_size = {"op": "embed", "texts": ["x" * (300 * 1024)]}
    with pytest.raises(ServeOpError) as exc3:
        daemon_box.daemon._validate_texts(only_size)
    assert "KiB" in exc3.value.message

    # the hard wall: 65 x 300 KiB is over MAX_LINE -> connection CLOSED
    from goldmine.backends.serve import ServeRequestError, encode_line

    sock = connect_socket(str(daemon_box.sock), 5.0)
    try:
        with pytest.raises((ServeRequestError, OSError, BrokenPipeError)):
            sock.sendall(encode_line(both))
            sock.settimeout(10)
            LineReader(sock).readline()
    finally:
        sock.close()
    # daemon unharmed
    assert raw_roundtrip(str(daemon_box.sock), {"op": "ping"})["ok"] is True


def test_embed_byte_equality_with_cold_incl_cross_batch(tmp_path):
    """T8 as amended by F10: RemoteBackend (via the daemon) vs a cold
    LlamaServerBackend on the same deterministic fake — EXACT array equality,
    including the cross-batch case (20 texts, batch 16 -> 16+4)."""
    cfg = make_cfg(tmp_path)
    box = start_daemon(cfg)
    try:
        served, via = connect_or_cold(cfg)
        assert via == "serve"
        cold = LlamaServerBackend(cfg.llama)
        texts = [f"cross batch text number {i} eviction" for i in range(20)]
        try:
            warm = served.embed_texts(texts)
            cold_vecs = cold.embed_texts(texts)
            assert warm.shape == cold_vecs.shape == (20, 768)
            assert np.array_equal(warm, cold_vecs)  # BYTE-equal across the 16|4 boundary
            few_w = served.embed_texts(["a", "b", "c"])
            few_c = cold.embed_texts(["a", "b", "c"])
            assert np.array_equal(few_w, few_c)
        finally:
            served.close()
            cold.close()
    finally:
        box.stop()


# ========================================================= T9-T14 ABC / gate


def test_fingerprint_fields_parity_with_cold(tmp_path):
    """T6 — THE gate test: fp_build input byte-identical to cold (D1)."""
    from goldmine import fingerprints

    fp_build = fingerprints.build  # cli imports it as fp_build; same function

    cfg = make_cfg(tmp_path)
    box = start_daemon(cfg)
    try:
        served, _ = connect_or_cold(cfg)
        cold = LlamaServerBackend(cfg.llama)
        try:
            assert served.fingerprint_fields() == cold.fingerprint_fields()
            assert fp_build(served) == fp_build(cold)
        finally:
            served.close()
            cold.close()
    finally:
        box.stop()


def test_capability_local_and_not_supported_messages_verbatim(tmp_path):
    cfg = make_cfg(tmp_path)
    box = start_daemon(cfg)
    try:
        served, _ = connect_or_cold(cfg)
        cold = LlamaServerBackend(cfg.llama)
        try:
            assert served.capability() == cold.capability()
            with pytest.raises(NotSupported) as warm_exc:
                served.embed_image(Path("/dev/null"))
            with pytest.raises(NotSupported) as cold_exc:
                cold.embed_image(Path("/dev/null"))
            assert str(warm_exc.value) == str(cold_exc.value)  # verbatim (D1)
            with pytest.raises(NotSupported):
                served.embed_audio_wav(b"RIFF....")
        finally:
            served.close()
            cold.close()
    finally:
        box.stop()


def test_max_input_tokens_and_count_tokens_parity(tmp_path):
    cfg = make_cfg(tmp_path)
    box = start_daemon(cfg)
    try:
        served, _ = connect_or_cold(cfg)
        cold = LlamaServerBackend(cfg.llama)
        texts = ["count me in", "zwei Wörter", "véry unicode text here"]
        try:
            assert served.max_input_tokens() == cold.max_input_tokens() == 512
            assert served.count_tokens(texts) == cold.count_tokens(texts)
            assert served.count_tokens([]) == []
        finally:
            served.close()
            cold.close()
    finally:
        box.stop()


def test_token_pieces_cut_returns_none_f9(tmp_path):
    """F9: the token_pieces OP is cut (G6); the client answers None so the
    chunker takes its estimator path — while cold still implements it."""
    cfg = make_cfg(tmp_path)
    box = start_daemon(cfg)
    try:
        served, _ = connect_or_cold(cfg)
        try:
            assert served.token_pieces("any text") is None
        finally:
            served.close()
    finally:
        box.stop()


def test_describe_stub_f9(tmp_path):
    cfg = make_cfg(tmp_path)
    box = start_daemon(cfg)
    try:
        served, _ = connect_or_cold(cfg)
        try:
            info = served.describe()
            assert info["id"] == "llama"
            assert info["via"] == "serve"
            assert "serve status" in info["detail"]
        finally:
            served.close()
    finally:
        box.stop()


def test_close_closes_socket_only_daemon_and_child_survive(tmp_path):
    """T19: client close NEVER signals the daemon (D1)."""
    cfg = make_cfg(tmp_path)
    box = start_daemon(cfg)
    try:
        served, _ = connect_or_cold(cfg)
        served.embed_texts(["make it resident"])
        child = _child_pid(box)
        served.close()  # socket-only by contract
        assert served._sock is None
        assert _alive(child)  # llama-server untouched
        status = one_shot_request(str(cfg.serve.socket), "status")
        assert status["resident"] is True  # daemon still serving
    finally:
        box.stop()
    assert _child_pid(box) is None or not _alive(_child_pid(box))


# ==================================================== T15-T19 discovery/liveness


def test_discovery_file_written_with_receipt_fields(tmp_path):
    cfg = make_cfg(tmp_path)
    box = start_daemon(cfg)
    try:
        data = read_discovery_file(cfg)
        assert data["proto"] == 1
        assert data["pid"] == os.getpid()  # in-process daemon
        assert data["socket"] == str(cfg.serve.socket)
        assert isinstance(data["started_at"], float)
        assert data["start_time"]  # daemon start-time recorded (stop's F3 verify)
        assert data["model_sha256"]
        assert data["children"] == []  # lazy: no child yet
    finally:
        box.stop()


def test_dead_pid_discovery_cold_silent(tmp_path, capsys):
    dead = subprocess.Popen(["sleep", "30"])
    dead.terminate()
    dead.wait()
    cfg = make_cfg(tmp_path)
    cfg.serve.socket = ""  # exercise the FILE discovery leg, not the explicit one
    cfg.home.mkdir(parents=True, exist_ok=True)
    (cfg.home / "serve.json").write_text(json.dumps({
        "proto": 1, "pid": dead.pid, "socket": str(tmp_path / "absent.sock"),
    }))
    backend, via = connect_or_cold(cfg)
    try:
        assert via == "cold"
        assert capsys.readouterr().err == ""  # SILENT: absence, not failure (T15)
    finally:
        backend.close()


def test_alive_pid_refused_socket_cold_with_one_notice(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    cfg.serve.socket = ""  # FILE discovery leg
    cfg.home.mkdir(parents=True, exist_ok=True)
    (cfg.home / "serve.json").write_text(json.dumps({
        "proto": 1, "pid": os.getpid(), "socket": str(tmp_path / "nothing.sock"),
    }))
    backend, via = connect_or_cold(cfg)
    try:
        assert via == "cold"
        err = capsys.readouterr().err
        assert err.count("goldmine serve:") == 1  # found-but-unusable -> ONE notice
        assert "connect failed" in err
    finally:
        backend.close()


def test_version_skew_and_proto_skew_cold_with_notice(tmp_path, capsys, monkeypatch):
    import goldmine.backends.serve as bs

    cfg = make_cfg(tmp_path)
    box = start_daemon(cfg)
    try:
        monkeypatch.setattr(bs, "__version__", "9.9.9")
        backend, via = connect_or_cold(cfg)
        assert via == "cold"
        assert "version mismatch" in capsys.readouterr().err
        backend.close()
        monkeypatch.setattr(bs, "PROTO", 2)
        backend2, via2 = connect_or_cold(cfg)
        assert via2 == "cold"
        assert "protocol mismatch" in capsys.readouterr().err
        backend2.close()
    finally:
        box.stop()


def test_goldmine_serve_env_wins_over_discovery_file(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    box = start_daemon(cfg)
    try:
        stale = tmp_path / "stale.sock"
        cfg.home.mkdir(parents=True, exist_ok=True)
        (cfg.home / "serve.json").write_text(json.dumps({
            "proto": 1, "pid": os.getpid(), "socket": str(stale),
        }))
        backend, via = connect_or_cold(cfg)
        try:
            assert via == "serve"  # env (cfg.serve.socket) beats the file (D3)
        finally:
            backend.close()
    finally:
        box.stop()


def test_stale_files_swept_by_start_never_by_clients(tmp_path):
    """D3: stale socket + stale json survive CLIENTS; the next START sweeps."""
    cfg = make_cfg(tmp_path)
    cfg.home.mkdir(parents=True, exist_ok=True)
    stale_sock = Path(cfg.serve.socket)  # the START will bind this same path
    stale_sock.parent.mkdir(parents=True, exist_ok=True)
    leftover = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    leftover.bind(str(stale_sock))  # a bound-but-dead socket file
    json_path = cfg.home / "serve.json"
    json_path.write_text(json.dumps({"proto": 1, "pid": 999999, "socket": str(stale_sock)}))

    backend, via = connect_or_cold(cfg)  # a CLIENT runs first
    backend.close()
    assert stale_sock.exists() and json_path.exists()  # clients never delete

    box = start_daemon(cfg)  # the START sweeps (flock -> previous daemon dead)
    try:
        data = read_discovery_file(cfg)
        assert data["pid"] == os.getpid()  # fresh discovery, not the stale row
        assert raw_roundtrip(str(stale_sock), {"op": "ping"})["ok"] is True
    finally:
        box.stop()


def test_socket_dir_wrong_mode_treated_absent(tmp_path, capsys):
    bad_dir = tmp_path / "baddir"
    bad_dir.mkdir()
    os.chmod(bad_dir, 0o755)
    cfg = make_cfg(tmp_path, sock=bad_dir / "s.sock")
    cfg.home.mkdir(parents=True, exist_ok=True)
    (cfg.home / "serve.json").write_text(json.dumps({
        "proto": 1, "pid": os.getpid(), "socket": str(bad_dir / "s.sock"),
    }))
    backend, via = connect_or_cold(cfg)
    try:
        assert via == "cold"
        assert capsys.readouterr().err == ""  # D5: wrong-mode dir -> treat as ABSENT
    finally:
        backend.close()
    os.chmod(bad_dir, 0o700)


# ========================================================== T20-T21 model drift


def test_model_path_drift_cold_with_drift_line(tmp_path, capsys):
    daemon_model = _fake_model(tmp_path, "daemon-model.gguf")
    client_model = _fake_model(tmp_path, "client-model.gguf")
    cfg = make_cfg(tmp_path, model=daemon_model)
    box = start_daemon(cfg)
    try:
        client_cfg = make_cfg(tmp_path, model=client_model, sock=cfg.serve.socket)
        backend, via = connect_or_cold(client_cfg)
        try:
            assert via == "cold"
            err = capsys.readouterr().err
            assert "model drift" in err
            assert "using cold" in err
        finally:
            backend.close()
    finally:
        box.stop()


def test_sidecar_sha_mismatch_drift_cold(tmp_path, capsys):
    """F2: the client's VALID sidecar sha is compared against the daemon's
    FRESH hash; a mismatch (tampered sidecar) cold-falls-back with the line."""
    cfg = make_cfg(tmp_path)
    box = start_daemon(cfg)
    try:
        client_cfg = make_cfg(tmp_path, model=cfg.llama.model, sock=cfg.serve.socket)
        model = cfg.llama.model
        st = model.stat()
        (model.parent / (model.name + ".sha256")).write_text(json.dumps({
            "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "sha256": "0" * 64,  # valid shape, wrong hash
        }))
        backend, via = connect_or_cold(client_cfg)
        try:
            assert via == "cold"
            err = capsys.readouterr().err
            assert "model drift" in err and "00000000" in err
        finally:
            backend.close()
    finally:
        box.stop()


# ============================================================== T22-T27 lifecycle


def test_second_daemon_already_serving_exit_0(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    box = start_daemon(cfg)
    try:
        second = ServeDaemon(cfg)
        rc = second.serve_forever()
        assert rc == 0
        err = capsys.readouterr().err
        assert "already serving (pid " in err  # flock refused the second instance
    finally:
        box.stop()


def test_sigterm_drains_in_flight_then_cleans(tmp_path, serve_env):
    """SIGTERM: the in-flight embed COMPLETES (drain <= 10s), then socket +
    serve.json are gone and the child is reaped (D3)."""
    proc = _subprocess_daemon(serve_env)
    try:
        first = one_shot_request(str(serve_env.sock), "embed",
                                 {"texts": ["warm the model"]}, timeout_s=30)
        assert first["ok"] is True
        data = read_discovery_file(serve_env.cfg())
        child = data["children"][0]
        # slow the NEXT embed down via the fake's control plane
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{child['port']}/control",
            data=json.dumps({"delay": 2.0}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
        result: dict = {}

        def inflight():
            result["resp"] = one_shot_request(str(serve_env.sock), "embed",
                                              {"texts": ["slow embed"]},
                                              timeout_s=30)

        thread = threading.Thread(target=inflight)
        thread.start()
        time.sleep(0.6)  # the request is now in flight
        proc.send_signal(signal.SIGTERM)
        thread.join(15)
        assert result["resp"]["ok"] is True  # drain completed the request
        rc = proc.wait(15)
        assert rc == 0
        assert not serve_env.sock.exists()
        assert not (serve_env.home / "serve.json").exists()
        assert not _alive(child["pid"])  # our child reaped, no orphan
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(10)


def test_sigkill_orphan_swept_by_next_start(tmp_path, serve_env, capsys):
    """SIGKILL -> orphaned child survives -> next `serve start` receipt-kills
    it (F3), by recorded pid, with the sweep reported honestly."""
    proc = _subprocess_daemon(serve_env)
    try:
        one_shot_request(str(serve_env.sock), "embed",
                         {"texts": ["make a child"]}, timeout_s=30)
        data = read_discovery_file(serve_env.cfg())
        child = data["children"][0]
        assert _alive(child["pid"])
        proc.kill()  # SIGKILL: no cleanup anywhere
        assert proc.wait(10) == -9
        assert _alive(child["pid"])  # the orphan is real (the 484MB class of bug)

        cfg = serve_env.cfg()
        box = start_daemon(cfg)  # the next start holds the flock -> sweeps
        try:
            err = capsys.readouterr().err
            assert "orphan sweep" in err
            assert "killed" in err
            assert not _alive(child["pid"])
        finally:
            box.stop()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(10)


def test_receipt_sweep_verify_before_kill(tmp_path, capsys):
    """F3 unit: a receipted pid is killed ONLY when the recorded start-time
    (or procname fallback) still matches; unverifiable -> never touched."""
    verified = subprocess.Popen(["sleep", "30"])
    time.sleep(0.05)
    from goldmine.serve import _ps_field

    wrong_start = subprocess.Popen(["sleep", "30"])
    unverifiable = subprocess.Popen(["sleep", "30"])
    try:
        assert verify_and_kill({"pid": 999999, "start_time": "nope", "comm": None}) \
            == "already gone"
        good_receipt = {
            "pid": verified.pid,
            "start_time": _ps_field(verified.pid, "lstart"),
            "comm": _ps_field(verified.pid, "comm"),
        }
        assert "killed" in verify_and_kill(good_receipt)
        assert verified.poll() is not None

        bad_receipt = {"pid": wrong_start.pid, "start_time": "Mon Jan 1 00:00:00 2000",
                       "comm": _ps_field(wrong_start.pid, "comm")}
        assert "skip" in verify_and_kill(bad_receipt)  # pid-reuse guard held
        assert wrong_start.poll() is None  # untouched

        assert "skip" in verify_and_kill({"pid": unverifiable.pid, "start_time": None,
                                          "comm": None})
        assert unverifiable.poll() is None  # unverifiable -> never killed
    finally:
        for proc in (wrong_start, unverifiable):
            proc.terminate()
            proc.wait(10)


def test_linger_idle_exit(tmp_path):
    cfg = make_cfg(tmp_path)
    box = start_daemon(cfg, sampler=lambda: 1, linger_s=0.5, idle_tick_s=0.05)
    t0 = time.monotonic()
    box.thread.join(10)
    assert not box.thread.is_alive()
    assert time.monotonic() - t0 < 8  # exited on its own
    assert box.daemon._stop_reason and "linger" in box.daemon._stop_reason
    assert not (cfg.home / "serve.json").exists()
    assert not Path(cfg.serve.socket).exists()


def test_linger_refresh_only_on_model_touch_f7(tmp_path):
    """F7: pings do NOT keep the daemon alive; embeds DO."""
    # -- pings only: exits after ~linger despite constant pings
    cfg = make_cfg(tmp_path, sock=short_sock_dir() / "s.sock")
    box = start_daemon(cfg, sampler=lambda: 1, linger_s=0.8, idle_tick_s=0.05)
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline and box.thread.is_alive():
        try:
            raw_roundtrip(str(cfg.serve.socket), {"op": "ping"}, timeout=2)
        except (OSError, ServeRequestError):
            break
        time.sleep(0.1)
    box.thread.join(5)
    assert not box.thread.is_alive()  # pings could not save it (F7)

    # -- embeds refresh: stays alive well past the linger window
    cfg2 = make_cfg(tmp_path, sock=short_sock_dir() / "s.sock")
    box2 = start_daemon(cfg2, sampler=lambda: 1, linger_s=0.8, idle_tick_s=0.05)
    try:
        served, via = connect_or_cold(cfg2)
        assert via == "serve"
        deadline = time.monotonic() + 1.8  # > 2x linger
        while time.monotonic() < deadline:
            served.embed_texts(["keepalive embed"])
            time.sleep(0.15)
        assert box2.thread.is_alive()  # model touches kept it alive
        served.close()
    finally:
        box2.stop()


# ===================================================== guard (D3 + F4/F5 + F1)


def test_guard_warn_sustained_unloads_then_recovers(tmp_path, capsys):
    """warn x2 (>= confirm apart) -> unload + cooldown -> model_not_resident ->
    client cold-falls-back; a NORMAL reading past cooldown resumes residency."""
    phase = {"level": 1}  # 1 while warming up; 2 = sustained pressure; 1 = recovered

    def sampler() -> int:
        return phase["level"]

    cfg = make_cfg(tmp_path)
    box = start_daemon(
        cfg, sampler=sampler, idle_tick_s=0.05,
        guard_sample_cache_s=0.05, guard_confirm_s=0.25, guard_cooldown_s=0.8,
    )
    try:
        served, _ = connect_or_cold(cfg)
        served.embed_texts(["resident please"])
        assert served.via == "serve"  # warmed up at level 1
        old_child = _child_pid(box)
        assert old_child
        phase["level"] = 2  # sustained pressure begins
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _child_pid(box) is not None:
            time.sleep(0.05)
        assert _child_pid(box) is None  # unloaded
        assert not _alive(old_child)
        assert box.daemon.guard.unloads == 1
        assert box.daemon.guard.cooling
        # F5 client face: model_not_resident -> NO retry, straight to cold,
        # results intact, exactly ONE stderr notice from THIS backend
        vectors = served.embed_texts(["during cooldown"])
        expected = LlamaServerBackend(cfg.llama)
        want = expected.embed_texts(["during cooldown"])
        expected.close()
        assert np.array_equal(vectors, want)
        assert served.via == "cold"
        err = capsys.readouterr().err
        # EXACTLY one CLIENT fallback notice (daemon's own startup/guard lines
        # also say "goldmine serve:" — count only the "using cold" notice)
        assert err.count("— using cold") == 1
        assert "model not resident" in err
        # recovery: normal level past the cooldown -> a NEW child spawns
        time.sleep(1.0)  # ride out the 0.8s cooldown
        phase["level"] = 1
        time.sleep(0.15)  # > the guard's 50ms sample cache: let the level land
        served2, _ = connect_or_cold(cfg)
        try:
            served2.embed_texts(["resume residency"])
            new_child = _child_pid(box)
            assert new_child and new_child != old_child
        finally:
            served2.close()
        served.close()
    finally:
        box.stop()


def test_guard_single_warn_no_action(tmp_path):
    """A single warn reading acts on nothing (the no-flapping guarantee, D3)."""
    cfg = make_cfg(tmp_path)
    box = start_daemon(
        cfg, sampler=scripted_sampler([2, 1, 1, 1, 1, 1], default=1),
        idle_tick_s=0.05, guard_sample_cache_s=0.05,
        guard_confirm_s=0.2, guard_cooldown_s=0.5,
    )
    try:
        served, _ = connect_or_cold(cfg)
        served.embed_texts(["resident"])
        child = _child_pid(box)
        time.sleep(0.8)  # several guard ticks
        assert _child_pid(box) == child
        assert box.daemon.guard.unloads == 0
        assert box.daemon.guard.first_warn is None  # the normal reading reset it
        served.close()
    finally:
        box.stop()


def test_guard_critical_sustained_clean_exit(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    box = start_daemon(
        cfg, sampler=scripted_sampler([3, 3], default=3),
        idle_tick_s=0.05, guard_sample_cache_s=0.05,
        guard_confirm_s=0.2, guard_cooldown_s=5.0,
    )
    box.thread.join(10)
    assert not box.thread.is_alive()
    assert "critical" in (box.daemon._stop_reason or "")
    assert not (box.daemon.discovery_path).exists()
    assert not Path(box.daemon.sock_path).exists()
    assert "goldmine serve: stopped" in capsys.readouterr().err


def test_guard_pressure_nothing_resident_counts_no_unload(tmp_path, capsys):
    """F3: sustained warn with NO model ever spawned — the episode still opens
    the cooldown hold (the model must stay aside), but no phantom unload is
    counted or announced, and the status explains the stepped-aside state."""
    cfg = make_cfg(tmp_path)
    box = start_daemon(
        cfg, sampler=lambda: 2,  # pinned warn the whole test
        idle_tick_s=0.05, guard_sample_cache_s=0.05,
        guard_confirm_s=0.2, guard_cooldown_s=30.0,
    )
    try:
        # never warm the model: nothing resident while pressure is sustained
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and box.daemon.guard.cooldown_until is None:
            time.sleep(0.05)
        guard = box.daemon.guard
        assert guard.cooldown_until is not None  # the hold opened
        assert guard.cooling
        assert guard.unloads == 0, "a phantom unload was counted"
        assert guard.last_unload is None  # last REAL unload: none happened
        err = capsys.readouterr().err
        assert "unloading the model" not in err
        assert "nothing resident to unload" in err
        # the wire refusal explains itself: nothing was ever unloaded, so the
        # hold reads as the pressure hold — not "cooling down after an unload"
        resp = raw_roundtrip(box.daemon.sock_path,
                             {"op": "embed", "texts": ["x"], "id": "f3"})
        assert resp["ok"] is False
        assert resp["error"]["kind"] == "model_not_resident"
        assert "held aside by sustained memory pressure" in resp["error"]["message"]
        assert "cooling down after a memory-pressure unload" \
            not in resp["error"]["message"]
        # status: unloads stays 0 and the note names the pressure hold
        status = box.daemon._op_status()
        assert status["guard"]["unloads"] == 0
        assert status["guard"]["state"] == "cooldown"
        rendered = _render_status_human(status)
        assert "stepped aside under memory pressure" in rendered
        assert "lazy — spawns on the first search" not in rendered
    finally:
        box.stop()


def test_refusal_reason_names_the_active_hold(tmp_path):
    """F4: inside a post-UNLOAD cooldown window the refusal says cooling down;
    past the window with pressure still elevated the hold is the PRESSURE hold
    — the refusal must name it, not the expired cooldown (and a nothing-
    resident episode, F3, never claims an unload happened)."""
    cfg = make_cfg(tmp_path)
    daemon = ServeDaemon(cfg, sampler=lambda: 1)  # constructed only: no sockets

    def refuse():
        raised = None
        try:
            daemon._ensure_resident()
        except ServeOpError as exc:
            raised = exc
        assert raised is not None, "expected model_not_resident"
        return raised.message

    # inside a REAL post-unload cooldown window: that window is the active hold
    daemon.guard.cooldown_until = time.monotonic() + 60
    daemon.guard.last_unload = time.time()
    daemon.guard.sampler = lambda: 1
    daemon.guard.sampled_at = None
    cool_msg = refuse()
    assert "cooling down after a memory-pressure unload" in cool_msg

    # past the window at level 2: the pressure hold is what refuses
    daemon.guard.cooldown_until = time.monotonic() - 1
    daemon.guard.last_unload = None
    daemon.guard.sampler = lambda: 2
    daemon.guard.sampled_at = None
    held_msg = refuse()
    assert "held aside by sustained memory pressure" in held_msg
    assert "cooling down" not in held_msg
    # and the two states are visibly distinct
    assert cool_msg != held_msg


def test_guard_unload_during_delayed_embed_results_intact(tmp_path, capsys):
    """F5 THE test: the guard unloads MID-EMBED (no waiting for in-flight);
    the client retries once, hits model_not_resident, cold-falls-back, and the
    results are byte-intact with exactly ONE stderr notice."""
    cfg = make_cfg(tmp_path)
    box = start_daemon(
        cfg, sampler=lambda: 1, idle_tick_s=0.05,
        guard_sample_cache_s=0.05, guard_confirm_s=0.25, guard_cooldown_s=5.0,
    )
    import urllib.request

    try:
        served, _ = connect_or_cold(cfg)
        served.embed_texts(["warm"])
        child = read_discovery_file(cfg)["children"][0]
        req = urllib.request.Request(
            f"http://127.0.0.1:{child['port']}/control",
            data=json.dumps({"delay": 1.5}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass

        result: dict = {}

        def slow_embed():
            result["vectors"] = served.embed_texts(["the slow query"])

        thread = threading.Thread(target=slow_embed)
        thread.start()
        time.sleep(0.4)  # the embed is in flight on the now-doomed child
        box.daemon.guard.sampler = lambda: 2
        box.daemon._tick()  # first warn reading
        time.sleep(0.3)  # > confirm_s
        box.daemon._tick()  # second warn -> unload NOW (does not wait, F5)
        thread.join(30)
        assert box.daemon.guard.unloads == 1
        expected = LlamaServerBackend(cfg.llama)
        want = expected.embed_texts(["the slow query"])
        expected.close()
        assert np.array_equal(result["vectors"], want)  # results intact
        err = capsys.readouterr().err
        assert err.count("— using cold") == 1  # EXACTLY one client notice (F5)
        assert "model not resident" in err
        assert served.via == "cold"
        assert not _alive(child["pid"])
        served.close()
    finally:
        box.stop()


def test_client_survives_daemon_death_mid_request(tmp_path, serve_env, capsys):
    """F5 connection flavor: SIGKILL between requests -> transient retry-once
    -> cold fallback -> results intact, one notice."""
    proc = _subprocess_daemon(serve_env)
    try:
        cfg = serve_env.cfg()
        served, via = connect_or_cold(cfg)
        assert via == "serve"
        first = served.embed_texts(["before the kill"])
        proc.kill()
        assert proc.wait(10) == -9
        second = served.embed_texts(["after the kill"])
        cold_expected = LlamaServerBackend(cfg.llama)
        want = cold_expected.embed_texts(["after the kill"])
        cold_expected.close()
        assert np.array_equal(second, want)
        assert served.via == "cold"
        assert capsys.readouterr().err.count("— using cold") == 1
        del first
        served.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(10)


# ==================================================== T28-T31 client-side extras


def test_serve_stop_graceful(tmp_path, serve_env):
    proc = _subprocess_daemon(serve_env)
    try:
        one_shot_request(str(serve_env.sock), "embed", {"texts": ["warm"]},
                         timeout_s=30)
        from goldmine.cli import main

        rc = main(["serve", "stop"])
        assert rc == 0
        assert proc.wait(15) == 0
        assert not serve_env.sock.exists()
        assert not (serve_env.home / "serve.json").exists()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(10)


def test_serve_stop_when_not_running(serve_env, capsys):
    from goldmine.cli import main

    assert main(["serve", "stop"]) == 0
    assert "not running" in capsys.readouterr().out


def test_serve_stop_falls_back_to_verified_sigterm(tmp_path, serve_env):
    """A daemon whose socket is somehow gone still gets stopped — but ONLY
    after the start-time receipt verifies the pid (F3 for the daemon pid)."""
    proc = _subprocess_daemon(serve_env)
    try:
        data = read_discovery_file(serve_env.cfg())
        assert data["start_time"]
        serve_env.sock.unlink()  # socket lost; the shutdown op cannot fire
        from goldmine.cli import main

        rc = main(["serve", "stop"])
        assert rc == 0
        assert proc.wait(15) is not None
        assert not (serve_env.home / "serve.json").exists()
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(10)


def test_serve_status_human_json_and_not_running(serve_env, capsys):
    from goldmine.cli import main

    # not running first: honest zero, exit 0 (D6)
    assert main(["serve", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "goldmine/1"
    assert payload["command"] == "serve-status"
    assert payload["running"] is False

    cfg = serve_env.cfg()
    box = start_daemon(cfg)
    try:
        assert main(["serve", "status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "serve-status"
        assert payload["pid"] == os.getpid()
        assert payload["proto"] == 1
        assert payload["resident"] is False
        assert payload["guard"]["level"] in (1, 2, 3)
        assert payload["linger_remaining_s"] > 0
        assert isinstance(payload["clients"], list)

        assert main(["serve", "status"]) == 0
        out = capsys.readouterr().out
        assert "serve: running" in out
        assert "goldmine" in out
    finally:
        box.stop()


def test_doctor_rows_absent_stale_running(serve_env, monkeypatch):
    from goldmine import doctor
    from goldmine.config import load as _load

    import goldmine.config as config_mod

    monkeypatch.delenv("GOLDMINE_SERVE", raising=False)
    absent = doctor.check_serve_daemon(config_mod.load(config_path=serve_env.config))
    assert absent.status == "warn"
    assert "not running" in absent.detail

    cfg = _load(config_path=serve_env.config)
    cfg.home.mkdir(parents=True, exist_ok=True)
    (cfg.home / "serve.json").write_text(json.dumps({
        "proto": 1, "pid": 999999, "socket": str(serve_env.sock),
    }))
    stale = doctor.check_serve_daemon(cfg)
    assert stale.status == "warn"
    assert "stale discovery" in stale.detail
    assert "self-heal" in stale.detail

    box = start_daemon(cfg)
    try:
        running = doctor.check_serve_daemon(cfg)
        assert running.status == "ok"
        assert f"pid {os.getpid()}" in running.detail
    finally:
        box.stop()


def test_doctor_memory_yielded_row_carries_f4_copy(tmp_path):
    """F4: a running-but-yielded daemon warns with the words, not a flag."""
    from goldmine import doctor

    cfg = make_cfg(tmp_path)
    box = start_daemon(cfg, sampler=lambda: 2, guard_sample_cache_s=0.05,
                       guard_confirm_s=10.0)  # sustained config; one reading acts not
    try:
        result = doctor.check_serve_daemon(cfg)
        assert result.status == "ok"  # still resident: the guard hasn't acted
        box.daemon.guard.unloads = 1
        box.daemon.guard.last_unload = time.time()
        box.daemon.guard.cooldown_until = time.monotonic() + 60
        box.daemon._guard_unload(2)
        yielded = doctor.check_serve_daemon(cfg)
        assert yielded.status == "warn"
        assert "steps aside and stays aside" in yielded.detail
        assert "current level" in yielded.detail
    finally:
        box.stop()


def test_serve_config_section_and_f9_auto_cut(tmp_path, caplog, monkeypatch):
    """[serve] parses; `auto` is an UNKNOWN key now (F9 cut) and warns; env
    GOLDMINE_SERVE lands in cfg.serve.socket."""
    import logging as _logging

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        "[serve]\nlinger = 42\ntimeout_s = 1.5\nsocket = '/x/s.sock'\nauto = true\n"
    )
    with caplog.at_level(_logging.WARNING, logger="goldmine.config"):
        cfg = load(config_path=cfg_path)
    assert cfg.serve.linger == 42
    assert cfg.serve.timeout_s == 1.5
    assert cfg.serve.socket == "/x/s.sock"
    assert any("auto" in rec.message for rec in caplog.records)  # F9: warns, no key
    monkeypatch.setenv("GOLDMINE_SERVE", "/env/s.sock")
    cfg2 = load(config_path=cfg_path)
    assert cfg2.serve.socket == "/env/s.sock"  # env wins over the file


def test_no_serve_flag_skips_discovery_entirely(tmp_path, serve_env, capsys, monkeypatch):
    """D1/§6: --no-serve takes the cold path without a single discovery probe."""
    _ingest_corpus(serve_env)
    import goldmine.backends.serve as bs
    from goldmine.cli import main

    real_discover = bs.read_discovery
    probes: list = []

    def spy(cfg):
        probes.append(1)
        return real_discover(cfg)

    monkeypatch.setattr(bs, "read_discovery", spy)
    rc = main(["search", "eviction", "--no-serve"])
    assert rc == 0
    captured = capsys.readouterr()
    assert probes == []  # --no-serve never even looked (the §6 order's first leg)
    assert "via: cold" in captured.err  # and the footer says so, on stderr


# ================================================================== T32 fuzz-lite


def test_fuzz_lite_daemon_survives_and_serves(daemon_box):
    """T32/T33: 200 rounds of random bytes / truncations / abrupt closes as
    protocol input — the daemon answers a correct ping+embed afterwards."""
    sock_path = str(daemon_box.sock)
    rng = np.random.default_rng(20261019)
    valid_ping = json.dumps({"op": "ping"}).encode()
    for round_no in range(200):
        mode = round_no % 4
        try:
            sock = connect_socket(sock_path, 2.0)
            if mode == 0:  # random bytes
                sock.sendall(rng.integers(0, 256, size=rng.integers(1, 300), dtype=np.uint8).tobytes())
            elif mode == 1:  # truncation of a valid line (no newline), then EOF
                sock.sendall(valid_ping[: rng.integers(1, len(valid_ping))])
            elif mode == 2:  # abrupt close, nothing sent
                pass
            else:  # newline-split garbage
                sock.sendall(b"\n\n\n" + bytes(rng.integers(0, 256, size=40, dtype=np.uint8)) + b"\n")
            sock.close()
        except OSError:
            pass  # a refused/-reset connection must not matter either
    resp = raw_roundtrip(sock_path, {"op": "ping"})
    assert resp["ok"] is True
    embed = raw_roundtrip(sock_path, {"op": "embed", "texts": ["post-fuzz"]},
                          timeout=30)
    assert embed["ok"] is True and len(embed["vectors"][0]) == 768


# ========================================================= T34-T36 semantics freeze


_CORPUS = {
    "notes.txt": "The scheduler evicts a pod when a node is under pressure.\n",
    "guide.md": "# Sourdough Guide\n\nFeed the starter daily and bulk ferment.\n",
    "deep.txt": "kubernetes eviction budgets protect workloads during node drains.\n",
}


def _ingest_corpus(serve_env) -> Path:
    corpus = serve_env.home.parent / "corpus"
    corpus.mkdir(exist_ok=True)
    for name, text in _CORPUS.items():
        (corpus / name).write_text(text, encoding="utf-8")
    from goldmine.cli import main

    rc = main(["ingest", str(corpus)])
    assert rc == 0, "ingest for the freeze test failed"
    return corpus


def test_cmd_search_stdout_byte_equal_serve_vs_cold(tmp_path, serve_env, capsys):
    """T34-T36, THE freeze: with search.py untouched, a served search's stdout
    is byte-identical to the cold run's; the `via:` line lives on stderr."""
    _ingest_corpus(serve_env)
    capsys.readouterr()  # consume the ingest summary; only searches count below
    from goldmine.cli import main

    cfg = serve_env.cfg()
    box = start_daemon(cfg)
    try:
        rc = main(["search", "eviction"])
        assert rc == 0
        captured_serve = capsys.readouterr()
        out_serve = captured_serve.out
        err_serve = captured_serve.err
        assert "via: serve" in err_serve
    finally:
        box.stop()

    rc = main(["search", "eviction", "--no-serve"])
    assert rc == 0
    captured_cold = capsys.readouterr()
    out_cold = captured_cold.out
    err_cold = captured_cold.err
    assert "via: cold" in err_cold
    assert out_serve == out_cold  # stdout BYTE-stable across serve/cold
    for line in err_serve.splitlines():
        if line.startswith("via:"):
            import re

            assert re.fullmatch(r"via: serve \(\d+\.\d\ds\)", line)


def test_fingerprint_gate_refuses_identically_served_and_cold(tmp_path, serve_env, capsys):
    """A mismatched fingerprint refuses with exit 3 through BOTH paths — the
    gate consumes the ABC, not the transport (D1)."""
    _ingest_corpus(serve_env)
    from goldmine.cli import main

    # poison the stored fingerprint id -> the gate must refuse either way
    from goldmine.store import Store

    store = Store(serve_env.home / "default.sqlite")
    store.set_meta("fingerprint_id", "deadbeefdeadbeef")
    store.close()

    cfg = serve_env.cfg()
    box = start_daemon(cfg)
    try:
        rc_serve = main(["search", "eviction"])
        capsys.readouterr()
    finally:
        box.stop()
    rc_cold = main(["search", "eviction", "--no-serve"])
    capsys.readouterr()
    assert rc_serve == 3 == rc_cold
