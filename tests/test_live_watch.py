"""R9 LIVE suite: real `goldmine watch` daemon + real vendor llama-server +
the real EmbeddingGemma 2 model (tier-3; needs GOLDMINE_LIVE=1 — skips
honestly like test_live_serve when a prerequisite is absent).

The 4 tests (spec §7.3 as amended by the task brief + review R-F1/R-F4):
1. THE R9 GATE (D8): a dropped file becomes lexically searchable within 90 s
   at `--interval-slow 3`, with the measured breakdown printed; the semantic
   hit is asserted too.
2. Crash-resume: SIGKILL mid-pass -> restart -> the index converges with
   vector/segment counts IDENTICAL to a single-path control (never duplicate
   work), a final full pass reports everything unchanged, and the orphaned
   llama-server child is receipt-swept by the next start (R8 F3 discipline).
3. Coexistence smoke: a real `serve` daemon and `watch` run simultaneously
   (separate locks); a drop is indexed by watch while search answers via the
   serve daemon.
4. F1 duty measurement: a 4 s-periodic writer on the watched root for a
   10-minute churn simulation must produce <= 24 backend spawns (the
   min-pass-gap bound), counted by a llama-server WRAPPER script's honest
   spawn log — not self-reporting. Also asserts the real `pmset` parse
   (R-F4) on this machine.

Isolation: every daemon runs with GOLDMINE_SERVE_PRESSURE_LEVEL=1 (the
documented determinism hook — on a 16 GB box level 2 during a build is
ordinary and would defer passes); all state lives under tmp homes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from goldmine.backends.llama import VENDOR_SERVER
from goldmine.watch import parse_battery, parse_therm

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("GOLDMINE_LIVE"),
        reason="GOLDMINE_LIVE=1 required for live watch tests",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL = REPO_ROOT / "models" / "embeddinggemma-2-Q8_0.gguf"


def _skip_reason() -> str | None:
    if not VENDOR_SERVER.is_file():
        return f"vendor llama-server not built at {VENDOR_SERVER}"
    if not MODEL.is_file():
        return f"model GGUF missing at {MODEL}"
    return None


_skip = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")


class LiveWatch:
    """One real `goldmine watch` subprocess + its environment."""

    def __init__(self, root: Path, roots: list[Path], *, extra_argv=None,
                 server: Path | None = None, env_extra=None):
        self.root = root
        self.roots = [Path(r) for r in roots]
        self.home = root / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        cfgdir = root / "cfg"
        cfgdir.mkdir(exist_ok=True)
        (cfgdir / "config.toml").write_text("")
        self.db = self.home / "default.sqlite"
        self.server = server if server is not None else VENDOR_SERVER
        self.env_extra = dict(env_extra or {})
        self.proc: subprocess.Popen | None = None
        self._stderr_file = open(root / "watch.err", "w")

    @property
    def heartbeat(self) -> Path:
        return self.home / "watch.json"

    def env(self) -> dict:
        env = os.environ.copy()
        env.update({
            "GOLDMINE_HOME": str(self.home),
            "GOLDMINE_CONFIG": str(self.root / "cfg" / "config.toml"),
            "GOLDMINE_MODEL": str(MODEL),
            "GOLDMINE_LLAMA_SERVER": str(self.server),
            "GOLDMINE_SERVE_PRESSURE_LEVEL": "1",  # deterministic (module doc)
        })
        env.update(self.env_extra)
        return env

    def argv(self) -> list[str]:
        argv = ["watch"]
        for r in self.roots:
            argv += ["--roots", str(r)]
        argv += ["--interval-fast", "1", "--interval-slow", "3",
                 "--settle", "1", "--forever"]
        return argv

    def start(self, timeout: float = 30.0) -> None:
        assert self.proc is None
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "goldmine.cli", *self.argv()],
            cwd=str(REPO_ROOT), env=self.env(),
            stdout=subprocess.DEVNULL, stderr=self._stderr_file,
            stdin=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError("watch exited during startup")
            if self.heartbeat.exists():
                return
            time.sleep(0.05)
        raise AssertionError("watch heartbeat never appeared")

    def run_cli(self, args: list[str], timeout: float = 300) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "goldmine.cli", *args],
            cwd=str(REPO_ROOT), env=self.env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, timeout=timeout,
        )

    def kill(self) -> None:
        """SIGKILL: the crash-only path — no cleanup anywhere."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        try:
            self._stderr_file.close()
        except ValueError:
            pass

    def stderr_text(self) -> str:
        try:
            self._stderr_file.flush()
        except ValueError:
            pass
        return (self.root / "watch.err").read_text()


@pytest.fixture
def workroot(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("live-watch")


def _llama_children() -> set[int]:
    out = subprocess.run(
        ["pgrep", "-f", "llama-server"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
    )
    return {int(line) for line in out.stdout.split() if line.strip().isdigit()}


def _vector_count(db: Path) -> int:
    from goldmine.store import Store

    store = Store(db)
    try:
        return store.counts().vectors
    finally:
        store.close()


# =============================================================== 1: THE GATE


@_skip
def test_live_watch_drop_becomes_searchable_within_90s(workroot: Path, capsys):
    """D8/R9 acceptance: drop -> searchable <= 90 s at --interval-slow 3,
    measured breakdown printed; the semantic hit asserted too."""
    root = workroot / "docs"
    root.mkdir()
    box = LiveWatch(workroot, [root])
    term = f"quokka{int(time.time())}"  # unique per run
    try:
        t0 = time.monotonic()
        box.start()
        started = time.monotonic()
        (root / "note.txt").write_text(
            f"A field note about the {term} and its harbor chart.\n" * 20
        )
        dropped = time.monotonic()
        first_hit = None
        breakdown = {}
        while time.monotonic() - dropped < 90.0:
            rc = box.run_cli(["search", term, "--lexical", "--db", str(box.db)])
            if rc.returncode == 0 and term.encode() in rc.stdout:
                first_hit = time.monotonic()
                hb = json.loads(box.heartbeat.read_text()) if box.heartbeat.exists() else {}
                lp = hb.get("last_pass") or {}
                breakdown = {
                    "detect_to_pass_end_s": round(
                        (lp.get("at") or 0) - dropped, 1)
                    if lp.get("at") else None,
                    "pass_kind": lp.get("kind"),
                    "pass_duration_s": lp.get("duration_s"),
                }
                break
            time.sleep(1.0)
        wall = first_hit - dropped if first_hit else None
        print(
            f"\nR9 drop->searchable gate: total {wall:.1f}s "
            f"(start {started - t0:.1f}s, drop->hit {wall:.1f}s) "
            f"breakdown={breakdown}"
        )
        assert first_hit is not None, "the dropped file never became searchable"
        assert wall <= 90.0, f"lexical hit took {wall:.1f}s > 90 s gate"
        # the semantic path answers too (same pass committed the vector)
        rc = box.run_cli(["search", term, "--db", str(box.db), "--limit", "3"])
        assert rc.returncode == 0
        assert term.encode() in rc.stdout
        captured = capsys.readouterr()  # keep the printed measurement
    finally:
        box.stop()


# ============================================================ 2: crash-resume


@_skip
def test_live_watch_sigkill_restart_never_duplicates(workroot: Path):
    """SIGKILL mid-pass -> restart -> converges with the CONTROL's vector
    count (zero duplicate work), the final full pass reports everything
    unchanged, and the orphaned llama-server child is receipt-swept."""
    root = workroot / "docs"
    root.mkdir()
    for i in range(8):
        (root / f"f{i}.txt").write_text(
            f"Document {i} about lighthouse keeper logs and {i * 7} meters.\n" * 30
        )

    # the single-path control: one plain ingest into a fresh home
    control_home = workroot / "control-home"
    control_home.mkdir()
    control_cfg = workroot / "control-cfg"
    control_cfg.mkdir()
    (control_cfg / "config.toml").write_text("")
    control_env = os.environ.copy()
    control_env.update({
        "GOLDMINE_HOME": str(control_home),
        "GOLDMINE_CONFIG": str(control_cfg / "config.toml"),
        "GOLDMINE_MODEL": str(MODEL),
        "GOLDMINE_LLAMA_SERVER": str(VENDOR_SERVER),
    })
    rc = subprocess.run(
        [sys.executable, "-m", "goldmine.cli", "ingest", str(root)],
        cwd=str(REPO_ROOT), env=control_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, timeout=300,
    )
    assert rc.returncode == 0, rc.stderr.decode()[-2000:]
    control_db = control_home / "default.sqlite"
    control_vectors = _vector_count(control_db)

    box = LiveWatch(workroot, [root])
    try:
        box.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:  # wait until the first pass starts
            hb = json.loads(box.heartbeat.read_text())
            if hb["state"] == "ingesting" or hb["passes_started"] >= 1:
                child = (hb.get("children") or [{}])[0].get("pid")
                break
            time.sleep(0.05)
        else:
            child = None
        box.kill()  # SIGKILL mid-pass (the model spawn alone takes ~1s)
        assert box.proc.wait(10) == -9
        assert box.heartbeat.exists()  # the stale heartbeat remains
        if child:
            assert child in _llama_children()  # the orphaned real llama-server

        # the next start sweeps the orphan (verify-before-kill) + finishes
        box2 = LiveWatch(workroot, [root])
        box2.start()
        try:
            if child:
                deadline = time.monotonic() + 20
                while child in _llama_children() and time.monotonic() < deadline:
                    time.sleep(0.2)
                assert child not in _llama_children(), "orphan never swept"
                assert "orphan sweep" in box2.stderr_text()

            # the index converges to the CONTROL's vector count exactly
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if _vector_count(box.db) == control_vectors:
                    break
                time.sleep(1.0)
            assert _vector_count(box.db) == control_vectors, (
                f"watch converged to {_vector_count(box.db)} vectors, "
                f"control has {control_vectors}"
            )
            segments = None
            from goldmine.store import Store

            store = Store(box.db)
            segments = store.counts().segments
            store.close()
            cstore = Store(control.db)
            assert cstore.counts().segments == segments
            cstore.close()

            # a final full pass reports EVERYTHING unchanged: no re-embedding
            rc = box.run_cli(["ingest", str(root)])
            assert rc.returncode == 0
            assert "skipped-unchanged 8" in rc.stdout.decode()
        finally:
            box2.stop()
    finally:
        box.stop()


# ============================================================= 3: coexistence


@_skip
def test_live_watch_serve_coexistence(workroot: Path):
    """D7: a real serve daemon + watch run together (separate locks); a drop
    is indexed by watch while search answers via the serve daemon."""
    root = workroot / "docs"
    root.mkdir()
    watch = LiveWatch(workroot, [root])
    serve = subprocess.Popen(
        [sys.executable, "-m", "goldmine", "serve"],
        cwd=str(REPO_ROOT), env=watch.env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (watch.home / "serve.json").exists():
                break
            time.sleep(0.1)
        else:
            raise AssertionError("serve never wrote its discovery file")

        watch.start()
        assert watch.heartbeat.exists()
        assert (watch.home / "serve.json").exists()  # both discovery files

        term = f"petrel{int(time.time())}"
        (root / "coexist.txt").write_text(f"A sighting of the {term} at dusk.\n" * 20)
        deadline = time.monotonic() + 90
        hit = False
        while time.monotonic() < deadline:
            rc = watch.run_cli(["search", term, "--db", str(watch.db)])
            if rc.returncode == 0 and term.encode() in rc.stdout:
                hit = True
                break
            time.sleep(1.0)
        assert hit, "the drop was never indexed while serve ran"
        # ...and a semantic search runs through the serve daemon (via footer)
        rc = watch.run_cli(["search", term, "--db", str(watch.db), "--limit", "3"])
        assert b"via: serve" in rc.stderr
    finally:
        watch.stop()
        serve.terminate()
        try:
            serve.wait(15)
        except subprocess.TimeoutExpired:
            serve.kill()


# ======================================================= 4: F1 duty + pmset


@_skip
def test_live_watch_duty_and_pmset(workroot: Path, capsys):
    """R-F1: a 4 s-periodic writer for a 10-minute churn simulation costs
    <= 24 backend spawns (the min_pass_gap bound), counted by a wrapper
    script around llama-server. Also asserts the real pmset parse (R-F4)."""
    root = workroot / "docs"
    root.mkdir()

    # --- the real pmset parse (R-F4: the live test ASSERTS the parse) ---
    batt_out = subprocess.run(
        ["pmset", "-g", "batt"], stdout=subprocess.PIPE, timeout=10
    ).stdout.decode()
    therm_out = subprocess.run(
        ["pmset", "-g", "therm"], stdout=subprocess.PIPE, timeout=10
    ).stdout.decode()
    mode = parse_battery(batt_out)
    throttled = parse_therm(therm_out)
    print(f"\npmset live parse: battery={mode!r} thermal_throttled={throttled}")
    assert mode in ("ac", "no_battery"), f"this dev mini is on battery? {mode!r}"
    assert throttled is False

    # --- the duty measurement: wrapper counts honest spawns ---
    spawn_log = workroot / "spawns.log"
    wrapper = workroot / "llama-wrap.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'echo "$(date +%s) $$" >> {spawn_log}\n'
        f'exec "{VENDOR_SERVER}" "$@"\n'
    )
    wrapper.chmod(0o755)

    box = LiveWatch(workroot, [root], server=wrapper)
    churn_file = root / "churn.txt"
    writer = subprocess.Popen(
        ["/bin/sh", "-c",
         'i=0; while true; do i=$((i+1)); '
         f'echo "churn line $i" > {churn_file}.tmp && '
         f'mv {churn_file}.tmp {churn_file}; sleep 4; done'],
    )
    try:
        box.start()
        window = 600  # the 10-minute churn simulation
        t_end = time.monotonic() + window
        samples = []
        while time.monotonic() < t_end:
            time.sleep(30)
            try:
                hb = json.loads(box.heartbeat.read_text())
                samples.append(
                    (hb["passes_started"], hb["state"],
                     hb["last_pass"]["kind"] if hb.get("last_pass") else None)
                )
            except (OSError, ValueError):
                pass
        spawns = len(spawn_log.read_text().splitlines()) if spawn_log.exists() else 0
        delta_passes = sum(1 for s in samples if s[2] == "delta")
        print(
            f"\nF1 duty: {spawns} spawns / {window}s churn (4s writer) — "
            f"gate <= {24} · delta passes sampled: {delta_passes} · "
            f"last samples: {samples[-3:]}"
        )
        assert spawns >= 5, "the churn never triggered passes — measurement void"
        assert spawns <= 24, (
            f"{spawns} backend spawns in 600s exceeds the min-pass-gap bound"
        )
    finally:
        writer.kill()
        box.stop()
    captured = capsys.readouterr()
