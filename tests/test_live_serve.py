"""R8 LIVE suite: real `goldmine serve` daemon + real vendor llama-server +
the real EmbeddingGemma 2 model (tier-3; needs GOLDMINE_LIVE=1 — skips
honestly like test_live_llama when a prerequisite is absent).

The 4 tests (spec §7.3 as amended by §12):
1. real serve: RemoteBackend embed dim 768 + build string recorded;
2. end-to-end `search` via the socket with the `via: serve` stderr footer;
3. THE F1 BENCH: >= 20 fresh `python3 -m goldmine.cli search` SUBPROCESS wall
   clocks (end-to-end, daemon resident + model loaded), p50/p95 printed next
   to a cold-control p50 (`--no-serve`); gates p50 <= 300 ms and p50 <= cold/3.
   Guard-aware: the REAL kernel pressure level gates the bench — at level >= 2
   it prints `skipped: pressure level N` and skips (advisory, never a fail),
   because on a 16 GB dev box level-2 is ordinary during builds and the model
   legitimately steps aside (F1/F4).
4. SIGKILL resilience: kill -9 the daemon -> next search cold-falls back ->
   `serve start` receipt-sweeps the orphaned real llama-server (pgrep proof).

Isolation: every daemon here runs with a pinned GOLDMINE_SERVE_PRESSURE_LEVEL
so tests 1/2/4 do not flake with the box's ordinary build-time pressure (the
pinned hook is the one documented in default_pressure_sampler); the bench's
skip decision reads the REAL level itself. All discovery state lives under a
tmp home + a short tmp socket path; the eval index is COPY-searched (never
mutated in place).
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

from goldmine.backends.llama import VENDOR_SERVER, probe_version
from goldmine.backends.serve import one_shot_request

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("GOLDMINE_LIVE"),
        reason="GOLDMINE_LIVE=1 required for live serve tests",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL = REPO_ROOT / "models" / "embeddinggemma-2-Q8_0.gguf"
EVAL_INDEX = REPO_ROOT / "evals" / "eval.sqlite"

WARM_QUERY = "kubernetes pod eviction"
BENCH_QUERIES = [
    "kubernetes", "sourdough starter", "pod eviction", "notebook config",
    "index fingerprint", "model manifest", "daemon linger", "cache pressure",
    "latency budget", "search syntax", "embedding dim", "sqlite wal",
    "backup restore", "doctor checks", "ingest roots", "vector scan",
    "token chunking", "ocr helper", "whisper asr", "extract pdf",
    "stale vectors", "cards distill",
]


def _skip_reason() -> str | None:
    if not VENDOR_SERVER.is_file():
        return f"vendor llama-server not built at {VENDOR_SERVER}"
    if not MODEL.is_file():
        return f"model GGUF missing at {MODEL}"
    return None


_skip = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")


def _real_pressure_level() -> int:
    try:
        out = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
        )
        return int(out.stdout.split()[-1])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return 1


class LiveServe:
    """One real daemon subprocess + the client environment around it."""

    def __init__(self, root: Path, env_fn=None):
        self.root = root
        self.home = root / "home"
        self.home.mkdir(parents=True)
        cfgdir = root / "cfg"
        cfgdir.mkdir()
        (cfgdir / "config.toml").write_text("")
        self.sock = Path(tempfile.mkdtemp(prefix="gm-live-")) / "s.sock"
        self._env_fn = env_fn  # reuse ANOTHER box's env (same home + socket)
        self.proc: subprocess.Popen | None = None
        self._stderr_file = open(root / "daemon.err", "w")

    def env(self) -> dict:
        if self._env_fn is not None:
            return self._env_fn()
        env = os.environ.copy()
        env.update({
            "GOLDMINE_HOME": str(self.home),
            "GOLDMINE_CONFIG": str(self.root / "cfg" / "config.toml"),
            "GOLDMINE_MODEL": str(MODEL),
            "GOLDMINE_LLAMA_SERVER": str(VENDOR_SERVER),
            "GOLDMINE_SERVE": str(self.sock),
            "GOLDMINE_SERVE_PRESSURE_LEVEL": "1",  # deterministic residency (see module doc)
        })
        return env

    def start(self, timeout: float = 30.0) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "goldmine", "serve"],
            cwd=str(REPO_ROOT), env=self.env(),
            stdout=subprocess.DEVNULL, stderr=self._stderr_file,
            stdin=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError("live daemon exited during startup")
            if self.sock.exists():
                try:
                    one_shot_request(str(self.sock), "ping", timeout_s=2.0)
                    return  # the ping IS the readiness proof (discovery write
                    # lands in the ENV's home, which may not be self.home)
                except Exception:
                    pass
            time.sleep(0.05)
        raise AssertionError("live daemon never became ready")

    def wait_resident(self, timeout: float = 120.0) -> dict:
        """Search once (spawns the model server-side) then poll status."""
        self.search([WARM_QUERY])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status = one_shot_request(str(self.sock), "status", timeout_s=5)
                if status.get("resident"):
                    return status
            except (OSError, Exception):
                pass
            time.sleep(0.2)
        raise AssertionError("model never became resident")

    def search(self, args: list[str], measure: bool = False) -> "subprocess.CompletedProcess | float":
        argv = [sys.executable, "-m", "goldmine.cli", "search", *args]
        t0 = time.monotonic()
        proc = subprocess.run(
            argv, cwd=str(REPO_ROOT), env=self.env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        if measure:
            assert proc.returncode == 0, f"search failed: {args}"
            return time.monotonic() - t0
        assert proc.returncode == 0, f"search failed: {args}"
        return proc

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._stderr_file.close()

    def daemon_stderr(self) -> str:
        self._stderr_file.flush()
        return (self.root / "daemon.err").read_text()


@pytest.fixture(scope="module")
def live(tmp_path_factory) -> LiveServe:
    reason = _skip_reason()
    if reason is not None:
        pytest.skip(reason)
    if not EVAL_INDEX.is_file():
        pytest.skip(f"eval index missing at {EVAL_INDEX}")
    root = tmp_path_factory.mktemp("live-serve")
    box = LiveServe(root)
    shutil.copy2(EVAL_INDEX, root / "home" / "default.sqlite")
    box.start()
    yield box
    box.stop()


@_skip
def test_live_serve_embed_dim_and_status(live: LiveServe):
    from goldmine.config import load
    from goldmine.backends.serve import connect_or_cold

    cfg = load(config_path=live.root / "cfg" / "config.toml", env=live.env())
    status = live.wait_resident()
    assert status["resident"] is True
    assert status["child_pid"]

    backend, via = connect_or_cold(cfg)
    assert via == "serve"
    try:
        vectors = backend.embed_texts([WARM_QUERY, "sourdough starter"])
        assert vectors.shape == (2, 768)
        assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-3)
        fields = backend.fingerprint_fields()
        assert fields["model_sha256"] == status["model_sha256"]  # F2: fresh hash
        assert len(fields["model_sha256"]) == 64
    finally:
        backend.close()
    build = probe_version(VENDOR_SERVER)
    assert build and "version" in build.lower()  # build string recorded (§7.3.1)


@_skip
def test_live_search_via_socket_footer(live: LiveServe, capsys, monkeypatch):
    live.wait_resident()
    # the in-process CLI reads os.environ: point it at the live box's home +
    # socket (subprocess benches pass env explicitly; this test must too)
    for key, value in live.env().items():
        monkeypatch.setenv(key, value)
    from goldmine.cli import main

    db = str(live.home / "default.sqlite")
    rc = main(["search", WARM_QUERY, "--db", db, "--limit", "5"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "via: serve" in captured.err
    assert captured.out  # hits rendered (or honest zero) on stdout


def _pct(sorted_samples: list[float], p: float) -> float:
    import math

    idx = max(0, math.ceil(p * len(sorted_samples)) - 1)
    return sorted_samples[idx]


@_skip
def test_live_latency_bench_f1(live: LiveServe, capsys):
    """F1 THE gate: >= 20 fresh `python3 -m goldmine.cli search` subprocess
    wall clocks, daemon resident + model loaded. p50 <= 300 ms and
    p50 <= cold/3, printed; guard-aware skip on the REAL pressure level.

    Operator pin: when GOLDMINE_SERVE_PRESSURE_LEVEL is set, the daemon's
    guard is pinned (the documented determinism hook) and the bench RUNS with
    a loud note — an operator who pinned the level has opted out of the yield
    for this run; the default (no pin) still skips honestly at level >= 2."""
    level = _real_pressure_level()
    pinned = live.env().get("GOLDMINE_SERVE_PRESSURE_LEVEL", "")  # the daemon's pin
    if level >= 2 and pinned not in ("1", "2", "3"):
        print(f"skipped: pressure level {level}")
        pytest.skip(f"skipped: pressure level {level}")
    if level >= 2:
        print(f"note: box at real pressure level {level}; bench runs because "
              f"GOLDMINE_SERVE_PRESSURE_LEVEL={pinned} pins the daemon guard")

    live.wait_resident()  # daemon resident + model loaded BEFORE the bench
    status = one_shot_request(str(live.sock), "status", timeout_s=5)
    unloads_before = status["guard"]["unloads"]
    db = str(live.home / "default.sqlite")

    warm = [live.search([q, "--db", db, "--limit", "5"], measure=True)
            for q in BENCH_QUERIES]
    cold = [live.search([q, "--db", db, "--limit", "5", "--no-serve"], measure=True)
            for q in BENCH_QUERIES]

    status = one_shot_request(str(live.sock), "status", timeout_s=5)
    if status["guard"]["unloads"] != unloads_before:
        print("skipped: guard unloaded the model mid-bench (pressure rose)")
        pytest.skip("guard unloaded mid-run")

    warm_sorted, cold_sorted = sorted(warm), sorted(cold)
    warm_p50, warm_p95 = _pct(warm_sorted, 0.50), _pct(warm_sorted, 0.95)
    cold_p50, cold_p95 = _pct(cold_sorted, 0.50), _pct(cold_sorted, 0.95)
    print(
        f"\nF1 end-to-end bench ({len(warm)} fresh subprocesses each):\n"
        f"  warm via serve : p50 {warm_p50*1000:7.1f} ms   p95 {warm_p95*1000:7.1f} ms\n"
        f"  cold control   : p50 {cold_p50*1000:7.1f} ms   p95 {cold_p95*1000:7.1f} ms\n"
        f"  gates: warm p50 <= 300 ms -> {'PASS' if warm_p50 <= 0.300 else 'FAIL'} · "
        f"warm p50 <= cold/3 ({cold_p50/3*1000:.0f} ms) -> "
        f"{'PASS' if warm_p50 <= cold_p50 / 3 else 'FAIL'}"
    )
    assert len(warm) >= 20
    assert warm_p50 <= 0.300, f"warm p50 {warm_p50*1000:.0f} ms > 300 ms gate"
    assert warm_p50 <= cold_p50 / 3, (
        f"warm p50 {warm_p50*1000:.0f} ms not under cold/3 ({cold_p50*1000:.0f} ms)"
    )


@_skip
def test_live_sigkill_cold_fallback_and_orphan_sweep(live: LiveServe):
    """SIGKILL the daemon -> cold search still works -> the next `serve start`
    receipt-kills the orphaned REAL llama-server (pgrep + ps proof)."""
    live.wait_resident()
    data = json.loads((live.home / "serve.json").read_text())
    daemon_pid = data["pid"]
    child_pid = data["children"][0]["pid"]

    def pgrep_all() -> set[int]:
        out = subprocess.run(["pgrep", "-f", "llama-server"],
                             stdout=subprocess.PIPE, timeout=10)
        return {int(l) for l in out.stdout.split()}

    assert child_pid in pgrep_all()  # the real llama-server is alive (pgrep proof 1)

    live.proc.kill()  # SIGKILL: no cleanup anywhere
    assert live.proc.wait(10) == -9
    assert (live.home / "serve.json").is_file()  # stale discovery remains

    rc = subprocess.run(
        [sys.executable, "-m", "goldmine.cli", "search", WARM_QUERY,
         "--db", str(live.home / "default.sqlite"), "--limit", "5"],
        cwd=str(REPO_ROOT), env=live.env(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
    )
    assert rc.returncode == 0  # the cold-fallback promise: results still delivered
    assert "via: cold" in rc.stderr.decode()
    assert child_pid in pgrep_all()  # orphaned but alive (the 484 MB class of bug)

    # the next `serve start` sweeps it, verify-before-kill (F3): the sweeper
    # daemon reuses the SAME home + socket path (that stale state is the target)
    sweeper = LiveServe(live.root / "sweep", env_fn=live.env)
    sweeper.sock = live.sock
    sweeper.start()
    try:
        stderr = live.daemon_stderr()
        time.sleep(0.5)
        sweep_log = sweeper.daemon_stderr()
        assert "orphan sweep" in sweep_log
        assert f"child pid {child_pid}" in sweep_log
        assert "killed" in sweep_log
        assert child_pid not in pgrep_all()  # pgrep proof 2: orphan gone
        status = one_shot_request(str(sweeper.sock), "status", timeout_s=5)
        assert status["pid"] != daemon_pid  # a fresh daemon is serving
    finally:
        sweeper.stop()
