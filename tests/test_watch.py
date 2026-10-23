"""R9 offline suite: `goldmine watch` (docs/v2.1-r9-watch.md §7.1 as amended
by review R-F1..R-F8).

Every constant is testable offline: injectable clock/rng/sleeper/pmset
fixtures, the mock backend, and `tmp_home`. The daemon LOOP ITSELF is never
mocked — tests drive `WatchDaemon.run(max_ticks=N)` with a fake clock and a
tick_hook that mutates the filesystem between ticks, the same loop production
runs.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from pathlib import Path

import pytest

from conftest import make_docx
from goldmine.backends.base import EmbeddingError
from goldmine.backends.mock import MockBackend
from goldmine.config import load
from goldmine.search import Searcher
from goldmine.store import Store
from goldmine.watch import (
    BATTERY_UNPARSEABLE_LIMIT,
    DUTY_MAX_SPAWNS_PER_10MIN,
    Cadence,
    PowerProbes,
    WatchDaemon,
    check_watch,
    heartbeat_path_for,
    lock_path_for,
    parse_battery,
    parse_therm,
    read_heartbeat,
    render_status_line,
    resolve_watch_roots,
    watch_status_payload,
)

# ---------------------------------------- measured pmset fixtures (R-F4/R-F5)

PMSET_AC = (
    "Now drawing from 'AC Power'\n"
    " -InternalBattery-0 (id=5383721)\t100%; charged; 0:00 remaining present: true\n"
)
PMSET_BATTERY = (
    "Now drawing from 'Battery Power'\n"
    " -InternalBattery-0 (id=5383721)\t92%; discharging; 4:11 remaining present: true\n"
)
PMSET_UPS = "Now drawing from 'UPS Power'\n"
PMSET_NO_BATTERY = "No batteries available.\n"
THERM_CLEAR = (
    "Note: No thermal pressure warning recorded.\n"
    "Note: No events recorded.\n"
    "Note: No warnings found.\n"
)
THERM_THROTTLED = "Note: cpu_power has exceeded limits\nCPU_Speed_Limit = 45\n"
LPM_OFF = " lowpowermode         0\n"
LPM_ON = " lowpowermode         1\n"


class FakeClock:
    """The injectable clock + sleeper: sleep advances the fake clock."""

    def __init__(self, start: float = 1000.0):
        self.now = float(start)
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedPower:
    """Fixture pmset runner + pressure sampler. `level` scripts the kernel
    pressure; battery/therm/lpm texts are the measured shapes above."""

    def __init__(self, *, battery_text: str = PMSET_AC,
                 therm_text: str = THERM_CLEAR, lpm_text: str = LPM_OFF,
                 level: int = 1):
        self.battery_text = battery_text
        self.therm_text = therm_text
        self.lpm_text = lpm_text
        self.level = level
        self.pmset_calls: list[str] = []

    def pmset(self, arg: str) -> str:
        self.pmset_calls.append(arg)
        return {
            "batt": self.battery_text,
            "therm": self.therm_text,
            "": self.lpm_text,
        }[arg]

    def sampler(self) -> int:
        return self.level

    def probes(self, clock: FakeClock, cache_s: float = 0.0) -> PowerProbes:
        return PowerProbes(clock=clock, cache_s=cache_s,
                           pmset_runner=self.pmset, sampler=self.sampler)


# ------------------------------------------------------------------- helpers


def make_cfg(tmp_path: Path, *, config_text: str | None = None) -> object:
    cfg_path = tmp_path / "cfg.toml"
    if config_text is not None:
        cfg_path.write_text(config_text)
    cfg = load(config_path=cfg_path if cfg_path.is_file() else None)
    cfg.backend = "mock"
    return cfg


def make_daemon(
    tmp_path: Path,
    *,
    roots: list[str],
    cfg=None,
    power: ScriptedPower | None = None,
    clock: FakeClock | None = None,
    tick_hook=None,
    backend_factory=None,
    **kwargs,
) -> tuple[WatchDaemon, FakeClock]:
    """A daemon wired for offline tests: mock backend, tiny gaps, fake clock,
    scripted power. `cfg.watch` knobs stay overridable via kwargs prefix
    `watch_` (e.g. watch_min_pass_gap_s=1)."""
    cfg = cfg or make_cfg(tmp_path)
    for key in list(kwargs):
        if key.startswith("watch_"):
            setattr(cfg.watch, key[len("watch_"):], kwargs.pop(key))
    clock = clock or FakeClock()
    power = power or ScriptedPower()
    kwargs.setdefault("interval_fast", 1)
    kwargs.setdefault("interval_slow", 120)
    kwargs.setdefault("settle_s", 0.0)
    kwargs.setdefault("linger_s", 10_000)
    daemon = WatchDaemon(
        cfg,
        tmp_path / "gmhome" / "default.sqlite",
        roots=roots,
        clock=clock,
        sleeper=clock.sleep,
        probes=power.probes(clock),
        tick_hook=tick_hook,
        backend_factory=backend_factory,
        **kwargs,
    )
    return daemon, clock


def drop(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def hits_for(db: Path, term: str) -> list[str]:
    store = Store(db)
    try:
        return [h.path for h in
                Searcher(store, None).search(term, scorer="lexical", limit=10)]
    finally:
        store.close()


def last_pass_of(daemon: WatchDaemon) -> dict:
    assert daemon._last_pass is not None, "no pass was recorded"
    return daemon._last_pass


def run_daemon(daemon: WatchDaemon, *, max_ticks: int):
    """Drive the real loop; snapshot the heartbeat every tick. The heartbeat
    file is DELETED on a clean shutdown (D4), so tests read the in-run
    snapshots — the last one reflects the final tick's end state."""
    snaps: list[dict] = []
    base = daemon.tick_hook

    def hook(d):
        if base is not None:
            base(d)
        data = read_heartbeat(d.cfg)
        if data is not None:
            snaps.append(data)

    daemon.tick_hook = hook
    rc = daemon.run(max_ticks=max_ticks)
    return rc, (snaps[-1] if snaps else None)


# =========================================================== snapshot / diff


class TestSnapshotDiff:
    def test_new_changed_gone_classification(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, _ = make_daemon(tmp_path, roots=[str(root)])
        daemon.snapshot, _, _ = daemon._scan()  # the startup-walk equivalent
        drop(root / "b.txt", "bravo two file\n")
        (root / "a.txt").write_text("alpha edited content\n")
        os.utime(root / "a.txt", (2000, 2000))
        new, _, _ = daemon._scan()
        added, changed, gone = daemon._diff(daemon.snapshot, new)
        assert added == [str(root / "b.txt")]
        assert changed == [str(root / "a.txt")]
        assert not gone

    def test_unchanged_corpus_is_zero_action_tick(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        ran = []
        daemon, clock = make_daemon(
            tmp_path, roots=[str(root)],
            tick_hook=lambda d: ran.append(d._passes_started),
            watch_min_pass_gap_s=0.0,
        )
        rc = daemon.run(max_ticks=3)
        assert rc == 0
        # the resume pass ran once (tick 1); the two quiet ticks spawned nothing
        assert ran == [0, 1, 1]
        assert daemon._last_pass["kind"] == "resume"

    def test_unsupported_files_not_tracked(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        drop(root / "secret.pem", "-----BEGIN PRIVATE KEY-----\nno\n")
        drop(root / "app.min.js", "var a=1;\n")
        daemon, _ = make_daemon(tmp_path, roots=[str(root)])
        snap, _, _ = daemon._scan()
        assert str(root / "a.txt") in snap
        assert str(root / "secret.pem") not in snap  # sweep tier owns these
        assert str(root / "app.min.js") not in snap


# ==================================================================== cadence


class TestCadence:
    def test_decay_under_fake_clock(self, tmp_path):
        cad = Cadence(5, 120, rng=lambda: 0.5)
        seen = []
        for _ in range(6):
            cad.quiet()
            seen.append(round(cad.interval, 1))
        assert seen == [10.0, 20.0, 40.0, 80.0, 120.0, 120.0]

    def test_reset_to_fast_on_change_and_start(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, clock = make_daemon(tmp_path, roots=[str(root)])
        daemon.run(max_ticks=1)
        assert daemon.cadence.interval == 1  # startup = fast (D2)
        # quiet scans double...
        daemon.run(max_ticks=2)
        quiet_interval = daemon.cadence.interval
        assert quiet_interval > 1
        # ...and a detected change resets to fast (D2)
        daemon.tick_hook = lambda d: drop(root / "b.txt", "bravo two file\n")
        daemon.run(max_ticks=1)
        assert daemon.cadence.interval == 1

    def test_jitter_bounds_seeded_rng(self, tmp_path):
        cad = Cadence(10, 120, rng=lambda: 0.0)
        assert cad.sleep_for(0) == pytest.approx(9.0)
        cad2 = Cadence(10, 120, rng=lambda: 1.0)
        assert cad2.sleep_for(0) == pytest.approx(11.0)

    def test_poll_interval_floor_2x_scan_duration(self, tmp_path, monkeypatch):
        """R-F1b: the sleep never drops below 2x the last measured scan."""
        import goldmine.watch as w

        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, clock = make_daemon(tmp_path, roots=[str(root)])
        real_walk = w.walk_files

        def slow_walk(*args, **kwargs):
            clock.now += 4.0  # the walk consumes 4 fake seconds
            return real_walk(*args, **kwargs)

        monkeypatch.setattr(w, "walk_files", slow_walk)
        daemon.run(max_ticks=2)
        assert daemon.last_scan_s == pytest.approx(4.0)
        assert clock.sleeps[1] >= 2.0 * 4.0 * 0.9  # the R-F1b floor

    def test_settle_debounce_collapses_burst_to_union_pass(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, clock = make_daemon(
            tmp_path, roots=[str(root)], settle_s=1.0,
            watch_min_pass_gap_s=0.0,
        )

        state = {"phase": 0}

        def hook(d):
            if state["phase"] == 1:
                # an editor burst: three writes inside the settle window
                drop(root / "b1.txt", "bravo one\n")
                drop(root / "b2.txt", "bravo two\n")
                drop(root / "b3.txt", "bravo three\n")
                state["phase"] = 2

        daemon.tick_hook = hook
        # tick 1: resume; tick 2: the hook drops the burst
        daemon.run(max_ticks=1)
        state["phase"] = 1
        daemon.run(max_ticks=1)
        lp = last_pass_of(daemon)
        assert lp["kind"] == "delta"
        assert lp["scanned"] == 3  # the union set in ONE pass (D2 debounce)
        for term in ("bravo one", "bravo two", "bravo three"):
            assert hits_for(daemon.db_path, term)


# ============================================================ scope invariant


class TestScopeInvariant:
    def test_no_roots_refusal_exit4(self, tmp_home, tmp_path, capsys):
        from goldmine.cli import main

        cfg_path = tmp_path / "cfg.toml"
        cfg_path.write_text("")
        rc = main(["watch", "--config", str(cfg_path)])
        assert rc == 4
        err = capsys.readouterr().err
        assert "watch refused: no folders to watch" in err
        assert "goldmine only follows folders you explicitly added" in err
        assert "fix: goldmine init" in err

    def test_roots_flag_replaces_config_default(self, tmp_home, tmp_path):
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        root_a.mkdir()
        root_b.mkdir()
        drop(root_b / "b.txt", "bravo only in b\n")
        cfg = make_cfg(tmp_path, config_text=f'[init]\nroots = ["{root_a}"]\n')
        daemon, _ = make_daemon(tmp_path, roots=[str(root_b)], cfg=cfg)
        _, hb = run_daemon(daemon, max_ticks=2)
        assert hb["roots"] == [str(root_b.resolve())]
        assert hits_for(daemon.db_path, "bravo only")

    def test_roots_session_config_bytes_unchanged(self, tmp_home, tmp_path):
        """THE ephemeral pin: a --roots session never writes config.toml."""
        from goldmine.cli import main

        root = tmp_path / "r"
        root.mkdir()
        cfg_path = tmp_path / "cfg.toml"
        cfg_path.write_text("")
        before = cfg_path.read_bytes()
        rc = main(["watch", "--config", str(cfg_path), "--roots", str(root),
                   "--backend", "mock", "--linger", "1"])
        assert rc == 0  # the real cmd_watch round trip (scope->flock->linger)
        assert cfg_path.read_bytes() == before  # byte-diff proof

    def test_delta_pass_leaves_roots_json_identical(self, tmp_path):
        """THE scope test (D3/R-F6): delta passes never touch roots_json."""
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, clock = make_daemon(tmp_path, roots=[str(root)],
                                    watch_min_pass_gap_s=0.0)
        daemon.run(max_ticks=1)
        store = Store(daemon.db_path)
        before = store.meta("roots_json")
        store.close()
        assert json.loads(before) == [str(root.resolve())]

        target = root / "deep" / "dropped.txt"

        def hook(d):
            if str(target) not in d.snapshot:
                drop(target, "quokka token\n")

        daemon.tick_hook = hook
        daemon.run(max_ticks=3)
        store = Store(daemon.db_path)
        after = store.meta("roots_json")
        store.close()
        assert after == before  # byte-identical through delta passes
        assert hits_for(daemon.db_path, "quokka token")

    def test_startup_records_roots_once(self, tmp_path):
        root = tmp_path / "r"
        root.mkdir()
        cfg = make_cfg(tmp_path)
        daemon, _ = make_daemon(tmp_path, roots=[str(root)], cfg=cfg)
        daemon.run(max_ticks=1)
        store = Store(daemon.db_path)
        first = store.meta("roots_json")
        store.close()
        # a SECOND session over the same root must not duplicate the entry
        daemon2, _ = make_daemon(tmp_path, roots=[str(root)], cfg=cfg)
        daemon2.run(max_ticks=1)
        store = Store(daemon.db_path)
        assert store.meta("roots_json") == first
        store.close()


# ============================================================== pass mechanics


class TestPassMechanics:
    def test_delta_pass_end_to_end_searchable(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, _ = make_daemon(tmp_path, roots=[str(root)],
                                watch_min_pass_gap_s=0.0)
        daemon.run(max_ticks=1)
        daemon.tick_hook = lambda d: drop(root / "n.txt", "unique quokka note\n")
        daemon.run(max_ticks=2)
        assert last_pass_of(daemon)["kind"] == "delta"
        assert last_pass_of(daemon)["new"] == 1
        assert hits_for(daemon.db_path, "quokka note")

    def test_second_pass_reports_unchanged(self, tmp_path):
        """The crash-resume shape (D5): a second session over an up-to-date
        corpus sha-skips everything — unchanged, no re-embedding."""
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, _ = make_daemon(tmp_path, roots=[str(root)],
                                watch_min_pass_gap_s=0.0)
        daemon.run(max_ticks=1)
        assert last_pass_of(daemon)["new"] == 1
        daemon2, _ = make_daemon(tmp_path, roots=[str(root)],
                                 watch_min_pass_gap_s=0.0)
        daemon2.run(max_ticks=1)
        lp = last_pass_of(daemon2)
        assert lp["kind"] == "resume"
        assert lp["unchanged"] == 1  # the sha check decided: no work
        assert lp["new"] == 0

    def test_failed_extraction_failed_row_watch_continues(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        (root / "broken.docx").write_bytes(b"this is not a zip file")
        daemon, _ = make_daemon(tmp_path, roots=[str(root)],
                                watch_min_pass_gap_s=0.0)
        rc = daemon.run(max_ticks=1)  # the resume pass hits the broken file
        assert rc == 0  # the daemon NEVER crashes on a per-file failure
        assert last_pass_of(daemon)["failed"] == 1
        assert hits_for(daemon.db_path, "alpha one file")  # the rest indexed

    def test_lexical_only_session_backendless(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        spawned = []
        daemon, _ = make_daemon(
            tmp_path, roots=[str(root)], lexical_only=True,
            backend_factory=lambda: spawned.append(1) or MockBackend(),
        )
        rc = daemon.run(max_ticks=1)
        assert rc == 0
        assert not spawned  # no backend was ever constructed
        assert hits_for(daemon.db_path, "alpha one file")
        store = Store(daemon.db_path)
        counts = store.counts()
        store.close()
        assert counts.vectors == 0

    def test_deletion_sweep_prunes_rows(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "zanzibar unique file\n")
        daemon, clock = make_daemon(tmp_path, roots=[str(root)],
                                watch_min_pass_gap_s=0.0,
                                watch_full_period_s=1)
        daemon.run(max_ticks=1)
        assert hits_for(daemon.db_path, "zanzibar unique")
        (root / "a.txt").unlink()
        clock.sleep(daemon.full_period_s)
        # Observe the first due sweep. A second legitimate periodic sweep
        # would overwrite last_pass with deleted=0 after doing no extra work.
        daemon.run(max_ticks=1)
        lp = last_pass_of(daemon)
        assert lp["kind"] == "sweep"
        assert lp["deleted"] == 1
        assert hits_for(daemon.db_path, "zanzibar unique") == []
        clock.sleep(daemon.full_period_s)
        daemon.run(max_ticks=1)
        assert last_pass_of(daemon)["kind"] == "sweep"
        assert last_pass_of(daemon)["deleted"] == 0
        assert hits_for(daemon.db_path, "zanzibar unique") == []


# =============================================================== sweep policy


class TestSweepPolicy:
    def test_sweep_throttled_to_one_per_window(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        drop(root / "b.txt", "bravo two file\n")
        daemon, _ = make_daemon(tmp_path, roots=[str(root)],
                                watch_min_pass_gap_s=0.0,
                                watch_full_period_s=1000)
        daemon.run(max_ticks=1)
        (root / "a.txt").unlink()
        daemon.run(max_ticks=1)
        sweeps_after_first = daemon._passes_started
        (root / "b.txt").unlink()
        daemon.run(max_ticks=1)
        # both deletions inside the full_period/2 window -> ONE sweep
        assert daemon._passes_started == sweeps_after_first

    def test_idle_full_period_sweep_fires(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, _ = make_daemon(tmp_path, roots=[str(root)],
                                watch_min_pass_gap_s=0.0,
                                watch_full_period_s=5)
        daemon.run(max_ticks=1)
        kinds = [daemon._last_pass["kind"]]
        daemon.run(max_ticks=4)  # > full_period_s of quiet fake time
        kinds.append(daemon._last_pass["kind"])
        assert kinds[-1] == "sweep"  # the idle self-heal fired

    def test_battery_defers_sweep_and_resume(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        power = ScriptedPower(battery_text=PMSET_BATTERY)
        daemon, _ = make_daemon(tmp_path, roots=[str(root)], power=power,
                                watch_min_pass_gap_s=0.0)
        daemon.run(max_ticks=2)
        assert daemon._passes_started == 0  # sweep-class waits for AC
        power.battery_text = PMSET_AC
        daemon.run(max_ticks=1)
        assert daemon._passes_started == 1  # AC -> the resume runs


# ==================================================== power probes (F4/F5/F6)


class TestPowerProbes:
    def test_pmset_battery_fixture_matrix(self):
        assert parse_battery(PMSET_AC) == "ac"
        assert parse_battery(PMSET_BATTERY) == "battery"
        assert parse_battery(PMSET_UPS) == "battery"  # UPS holds, conservative
        assert parse_battery(PMSET_NO_BATTERY) == "no_battery"
        assert parse_battery("garbage output") == "unparseable"

    def test_pmset_therm_fixture_matrix(self):
        assert parse_therm(THERM_CLEAR) is False
        assert parse_therm(THERM_THROTTLED) is True  # `= 45`, separator-agnostic
        assert parse_therm("CPU_Speed_Limit: 50") is True
        assert parse_therm("CPU_Speed_Limit: 100") is False  # full speed (§11.9)
        assert parse_therm("CPU_Speed_Limit = 0") is False
        assert parse_therm("Note: cpu_power limit exceeded") is True

    def test_probe_cache_30s(self, tmp_path):
        power = ScriptedPower()
        clock = FakeClock()
        probes = power.probes(clock, cache_s=30.0)
        probes.sample()
        probes.sample(clock.now + 1)  # within the cache window
        assert len(power.pmset_calls) == 3  # batt+therm+lpm, once
        probes.sample(clock.now + 31)  # cache expired -> re-probe
        assert len(power.pmset_calls) == 6

    def test_thermal_hold_pauses_passes_scans_continue(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        power = ScriptedPower(therm_text=THERM_THROTTLED)
        daemon, _ = make_daemon(tmp_path, roots=[str(root)], power=power,
                                watch_min_pass_gap_s=0.0)
        _, hb = run_daemon(daemon, max_ticks=2)
        assert daemon._passes_started == 0
        assert hb["state"] == "held-thermal"
        assert hb["last_scan_s"] >= 0.0  # scans never stop (cheap tier, D6)
        power.therm_text = THERM_CLEAR
        daemon.run(max_ticks=1)
        assert daemon._passes_started == 1

    def test_battery_batch_boundary_50_51(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        power = ScriptedPower(battery_text=PMSET_BATTERY)
        daemon, _ = make_daemon(tmp_path, roots=[str(root)], power=power,
                                watch_min_pass_gap_s=0.0,
                                watch_battery_pass_gap_s=0.0)
        daemon.run(max_ticks=1)
        assert daemon._passes_started == 0  # resume waits for AC
        power.battery_text = PMSET_AC
        daemon.run(max_ticks=1)  # AC resume
        assert daemon._passes_started == 1
        power.battery_text = PMSET_BATTERY

        def burst_50(d):
            if not d.pending and d._last_pass and d._last_pass["kind"] == "resume":
                for i in range(50):
                    drop(root / f"n{i:02d}.txt", f"batch file {i} zeta\n")

        daemon.tick_hook = burst_50
        daemon.run(max_ticks=1)
        assert daemon._passes_started == 2  # exactly 50 files: passes
        assert last_pass_of(daemon)["kind"] == "delta"

        def burst_51(d):
            if not d.pending:
                for i in range(51):
                    drop(root / f"m{i:02d}.txt", f"over batch {i} yang\n")

        daemon.tick_hook = burst_51
        _, hb = run_daemon(daemon, max_ticks=2)
        assert daemon._passes_started == 2  # 51 > 50: waits for AC
        assert hb["holds"]["waiting"] == 51
        assert hb["state"] == "held-battery"

    def test_battery_paces_pass_frequency(self, tmp_path):
        """R-F1c: on battery, embed passes obey battery_pass_gap_s too."""
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        power = ScriptedPower()
        daemon, clock = make_daemon(
            tmp_path, roots=[str(root)], power=power,
            watch_min_pass_gap_s=0.0, watch_battery_pass_gap_s=30.0,
        )
        daemon.run(max_ticks=1)  # resume on AC
        power.battery_text = PMSET_BATTERY
        times: list[float] = []
        real_run_pass = daemon._run_pass

        def recording_pass(kind, roots):
            ran = real_run_pass(kind, roots)
            if ran:
                times.append(clock.now)
            return ran

        daemon._run_pass = recording_pass  # type: ignore[method-assign]

        def churn(d):
            (root / "churn.txt").write_text(f"churn {clock.now} xenon\n")

        daemon.tick_hook = churn
        daemon.run(max_ticks=75)  # enough fake time for two battery-paced passes
        assert len(times) >= 2  # the resume ran pre-recording; these are deltas
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert min(gaps) >= 30.0  # paced on battery, not per-tick

    def test_pressure_defers_then_recovers(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        power = ScriptedPower(level=2)
        daemon, _ = make_daemon(tmp_path, roots=[str(root)], power=power,
                                watch_min_pass_gap_s=0.0)
        _, hb = run_daemon(daemon, max_ticks=2)
        assert daemon._passes_started == 0  # level 2: defer (D7)
        assert hb["state"] == "deferred-memory"
        power.level = 1
        daemon.run(max_ticks=1)
        assert daemon._passes_started == 1

    def test_pressure_defer_skipped_for_lexical_only(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        power = ScriptedPower(level=2)
        daemon, _ = make_daemon(tmp_path, roots=[str(root)], power=power,
                                watch_min_pass_gap_s=0.0, lexical_only=True)
        daemon.run(max_ticks=1)
        assert daemon._passes_started == 1  # no model spawn: no defer (D7)

    def test_pressure_sampler_is_serves(self):
        """Identity pin (D7): one kernel signal, two consumers."""
        from goldmine.serve import default_pressure_sampler
        from goldmine.watch import default_pressure_sampler as watch_sampler

        assert watch_sampler is default_pressure_sampler

    def test_unparseable_battery_holds_then_releases(self, tmp_path):
        """R-F5: 1-2 unparseable probes hold (caution); the 3rd releases with
        a note; `No batteries available.` is always-AC."""
        assert BATTERY_UNPARSEABLE_LIMIT == 3
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        power = ScriptedPower(battery_text="total garbage")
        daemon, clock = make_daemon(tmp_path, roots=[str(root)], power=power,
                                    watch_min_pass_gap_s=0.0)
        daemon.run(max_ticks=1)
        assert daemon._passes_started == 0  # held (fail-safe toward caution)
        daemon.run(max_ticks=1)
        assert daemon._passes_started == 0  # still held (streak 2)
        _, hb = run_daemon(daemon, max_ticks=2)
        assert daemon._passes_started == 1  # streak 3: released
        assert any("unparseable" in n for n in hb["notes"])

    def test_no_battery_text_is_always_ac(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        power = ScriptedPower(battery_text=PMSET_NO_BATTERY)
        daemon, _ = make_daemon(tmp_path, roots=[str(root)], power=power,
                                watch_min_pass_gap_s=0.0)
        daemon.run(max_ticks=1)
        assert daemon._passes_started == 1  # desktop/VM: never a hold (R-F5)

    def test_lpm_read_reported_never_gates(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        power = ScriptedPower(lpm_text=LPM_ON)
        daemon, _ = make_daemon(tmp_path, roots=[str(root)], power=power,
                                watch_min_pass_gap_s=0.0)
        _, hb = run_daemon(daemon, max_ticks=2)
        assert daemon._passes_started == 1  # H12: LPM does not gate in R9
        assert hb["holds"]["lpm"] is True


# ========================================================== pass-rate floor F1


class TestPassRateFloor:
    def test_min_pass_gap_coalesces_churn(self, tmp_path, monkeypatch):
        """R-F1a: a periodic writer cannot spawn a backend per tick — no
        embed-bearing pass starts within min_pass_gap_s of the last."""
        import goldmine.watch as w

        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, clock = make_daemon(tmp_path, roots=[str(root)],
                                    watch_min_pass_gap_s=25.0)
        spawn_counter = {"n": 0}
        real_create = w.create_backend

        def counting_create(cfg, name=None):
            spawn_counter["n"] += 1
            return real_create(cfg, name)

        monkeypatch.setattr(w, "create_backend", counting_create)

        def writer(d):
            (root / "churn.txt").write_text(f"churn {clock.now} yonder\n")

        daemon.tick_hook = writer
        daemon.run(max_ticks=20)
        # the writer keeps the cadence fast (1s ticks, 20 fake seconds total)
        # so the 25s gap blocks EVERY delta pass: resume only, one spawn
        assert daemon._passes_started == 1
        assert spawn_counter["n"] == 1
        assert DUTY_MAX_SPAWNS_PER_10MIN == 24  # the live duty gate bound

    def test_gap_allows_burst_coalescing_across_it(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, clock = make_daemon(tmp_path, roots=[str(root)],
                                    watch_min_pass_gap_s=3.0)
        daemon.run(max_ticks=1)
        b = root / "b.txt"
        c = root / "c.txt"

        def burst_once(d):
            if str(b) not in d.snapshot:  # fire once, not every tick
                drop(b, "bravo burst\n")
                drop(c, "charlie burst\n")

        daemon.tick_hook = burst_once
        daemon.run(max_ticks=1)  # detected, held back by the gap
        assert daemon.pending  # the burst is waiting, not lost
        daemon.run(max_ticks=4)  # the gap has elapsed (fake clock moved)
        assert last_pass_of(daemon)["kind"] == "delta"
        assert last_pass_of(daemon)["scanned"] == 2
        assert not daemon.pending


# ==================================================================== lifecycle


class TestLifecycle:
    def test_heartbeat_field_contract(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, _ = make_daemon(tmp_path, roots=[str(root)])
        daemon.run(max_ticks=1)
        assert not heartbeat_path_for(daemon.cfg).exists()  # clean stop swept it
        # during the run it carried the D4 contract:
        seen = {}

        def hook(d):
            hb = read_heartbeat(d.cfg)
            seen.update(hb)

        daemon2, _ = make_daemon(tmp_path, roots=[str(root)], tick_hook=hook)
        daemon2.run(max_ticks=1)
        for key in ("proto", "goldmine_version", "pid", "start_time",
                    "started_at", "roots", "state", "interval_s",
                    "last_scan_s", "last_pass", "files_indexed_total",
                    "passes_started", "holds", "children", "notes"):
            assert key in seen, key
        assert not heartbeat_path_for(daemon.cfg).with_name("watch.json.tmp").exists()

    def test_heartbeat_state_ingesting_before_pass(self, tmp_path):
        """R-F7: state=ingesting is written BEFORE the pass runs."""
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, _ = make_daemon(tmp_path, roots=[str(root)])
        cfg = daemon.cfg
        hb_path = heartbeat_path_for(cfg)
        states: list[str] = []

        class Recording(MockBackend):
            def embed_texts(self, texts):
                states.append(json.loads(hb_path.read_text())["state"])
                return super().embed_texts(texts)

        daemon._backend_factory = lambda: Recording(dim=768)
        daemon.run(max_ticks=1)
        assert states, "the mock embed never ran"
        assert set(states) == {"ingesting"}

    def test_stale_heartbeat_surfaces(self, tmp_home, tmp_path):
        cfg = make_cfg(tmp_path)
        hb = heartbeat_path_for(cfg)
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(json.dumps({"pid": 999999999, "state": "idle"}))
        line = render_status_line(cfg)
        assert line is not None
        assert "not running (stale heartbeat, dead pid 999999999" in line
        row = check_watch(cfg)
        assert row.status == "warn"
        assert "stale heartbeat" in row.detail

    def test_status_line_shapes(self, tmp_home, tmp_path):
        cfg = make_cfg(tmp_path)
        assert render_status_line(cfg) is None  # silent when never used
        hb = heartbeat_path_for(cfg)
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(json.dumps({
            "pid": os.getpid(), "state": "idle",
            "last_pass": {"at": 1.0, "new": 3, "kind": "delta"},
            "files_indexed_total": 42, "holds": {"waiting": 0},
        }))
        line = render_status_line(cfg)
        assert "watch: running" in line
        assert "3 new files indexed (42 this session)" in line
        hb.write_text(json.dumps({
            "pid": os.getpid(), "state": "held-battery",
            "last_pass": {"at": 1.0, "new": 0, "kind": "delta"},
            "files_indexed_total": 0, "holds": {"waiting": 120},
        }))
        line = render_status_line(cfg)
        assert "held (battery — 120 files waiting)" in line

    def test_second_daemon_already_watching(self, tmp_path):
        root = tmp_path / "r"
        root.mkdir()
        cfg = make_cfg(tmp_path)
        holder, _ = make_daemon(tmp_path, roots=[str(root)], cfg=cfg)
        assert holder._acquire_lock() is True
        second, _ = make_daemon(tmp_path, roots=[str(root)], cfg=cfg)
        import io

        buf = io.StringIO()
        second.stream = buf
        rc = second.run()
        assert rc == 0
        assert "already watching" in buf.getvalue()
        holder._release_lock()

    def test_request_stop_cleans_and_releases(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, _ = make_daemon(tmp_path, roots=[str(root)], forever=True)

        def stopper(d):
            d.request_stop("test stop")

        daemon.tick_hook = stopper
        rc = daemon.run()
        assert rc == 0
        assert not heartbeat_path_for(daemon.cfg).exists()
        # the flock is released: a second daemon acquires it
        second, _ = make_daemon(tmp_path, roots=[str(root)])
        assert second._acquire_lock() is True
        second._release_lock()

    def test_sigterm_cancels_inflight_pass(self, tmp_path):
        """R-F2: SIGTERM during a pass cancels the pipeline (wave boundary),
        the daemon drains, exits 0, and cleans up."""
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, _ = make_daemon(tmp_path, roots=[str(root)])
        cancelled = []

        class Slow(MockBackend):
            def embed_texts(self, texts):
                daemon.request_stop("signal SIGTERM")
                return super().embed_texts(texts)

        daemon._backend_factory = lambda: Slow(dim=768)
        rc = daemon.run()
        assert rc == 0
        assert not heartbeat_path_for(daemon.cfg).exists()

    def test_sigkill_next_start_sweeps_and_completes(self, tmp_path):
        """T26, the crash-only pin: a stale heartbeat + a dead session leave
        work that the next start completes with NO duplicate rows."""
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, _ = make_daemon(tmp_path, roots=[str(root)])
        daemon._acquire_lock()
        daemon.store = Store(daemon.db_path)
        daemon._record_scope()
        daemon.snapshot, _, _ = daemon._scan()
        daemon._write_heartbeat(state="idle")
        # --- CRASH: fds closed without cleanup, heartbeat stays behind ---
        import copy

        stale_hb = copy.deepcopy(read_heartbeat(daemon.cfg))
        daemon._release_lock()
        daemon.store.close()
        drop(root / "b.txt", "bravo post-crash file\n")

        # the next start sweeps the stale heartbeat and finishes the work
        daemon2, _ = make_daemon(tmp_path, roots=[str(root)],
                                 watch_min_pass_gap_s=0.0)
        rc = daemon2.run(max_ticks=1)
        assert rc == 0
        assert hits_for(daemon2.db_path, "alpha one file")
        assert hits_for(daemon2.db_path, "bravo post-crash file")
        store = Store(daemon2.db_path)
        dupes = store.db.execute(
            "SELECT path, COUNT(*) c FROM files GROUP BY path HAVING c > 1"
        ).fetchall()
        vectors = store.counts().vectors
        store.close()
        assert dupes == []
        # vector count identical to a single-path control (D5/L2 offline pin)
        control = tmp_path / "control.sqlite"
        cfg = make_cfg(tmp_path)
        from goldmine.ingest import IngestPipeline

        pipe = IngestPipeline(cfg, Store(control), MockBackend(dim=768))
        pipe.run([str(root)])
        control_store = Store(control)
        assert control_store.counts().vectors == vectors
        control_store.close()
        assert stale_hb["pid"]  # the stale file was the thing swept

    def test_watch_stop_verified_sigterm(self, tmp_home, tmp_path, monkeypatch, capsys):
        import goldmine.watch as w

        cfg = make_cfg(tmp_path)
        hb = heartbeat_path_for(cfg)
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(json.dumps({"pid": 4242, "start_time": "Wed Jan  1 00:00:00 2026"}))
        monkeypatch.setattr(w, "proc_matches_receipt", lambda pid, data: True)
        alive = {"n": 0}

        def flaky_alive(pid):
            alive["n"] += 1
            return alive["n"] < 3  # alive at check + first wait, then gone

        monkeypatch.setattr(w, "pid_alive", flaky_alive)
        sent = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
        rc = w.cmd_watch_stop(type("A", (), {"config": str(tmp_path / "cfg.toml")})())
        assert rc == 0
        assert sent and sent[0][0] == 4242
        assert "watch: stopped" in capsys.readouterr().out

    def test_watch_stop_blind_kill_refused(self, tmp_home, tmp_path, monkeypatch, capsys):
        import goldmine.watch as w

        cfg = make_cfg(tmp_path)
        hb = heartbeat_path_for(cfg)
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(json.dumps({"pid": 4242}))
        monkeypatch.setattr(w, "pid_alive", lambda pid: True)
        monkeypatch.setattr(w, "proc_matches_receipt", lambda pid, data: None)
        sent = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(sig))
        rc = w.cmd_watch_stop(type("A", (), {"config": str(tmp_path / "cfg.toml")})())
        assert rc == 1
        assert not sent  # never signalled blind (R8 F3 discipline)
        assert "not killed" in capsys.readouterr().err

    def test_watch_stop_not_running(self, tmp_home, tmp_path, capsys):
        from goldmine.cli import main

        cfg_path = tmp_path / "cfg.toml"
        cfg_path.write_text("")
        assert main(["watch", "stop", "--config", str(cfg_path)]) == 0
        assert "watch: not running" in capsys.readouterr().out

    def test_forever_disables_linger(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, _ = make_daemon(tmp_path, roots=[str(root)], linger_s=1,
                                forever=True)
        daemon.run(max_ticks=5)  # >> linger of quiet fake time
        assert daemon._stop_reason is None  # linger never fired

    def test_linger_exit_after_quiet(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, clock = make_daemon(tmp_path, roots=[str(root)], linger_s=10)
        rc = daemon.run(max_ticks=50)
        assert rc == 0
        assert "linger expired" in (daemon._stop_reason or "")

    def test_fingerprint_mismatch_exit3(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        cfg = make_cfg(tmp_path)
        db = tmp_path / "gmhome" / "default.sqlite"
        db.parent.mkdir(parents=True, exist_ok=True)
        store = Store(db)
        store.set_meta("fingerprint_id", "bogus-fingerprint-id")
        store.set_meta("fingerprint_json", "{}")
        store.close()
        daemon, _ = make_daemon(tmp_path, roots=[str(root)], cfg=cfg)
        rc = daemon.run(max_ticks=2)
        assert rc == 3  # R-F2: rendered by watch, exit 3, no retry loop
        assert daemon._fatal[0] == "fingerprint_mismatch"


# ===================================================================== surfaces


class TestSurfaces:
    def test_status_json_watch_key(self, tmp_home, tmp_path, capsys):
        from goldmine.cli import main

        root = tmp_path / "r"
        root.mkdir()
        cfg_path = tmp_path / "cfg.toml"
        cfg_path.write_text("")
        db = tmp_path / "gmhome" / "default.sqlite"
        db.parent.mkdir(parents=True, exist_ok=True)
        Store(db).close()  # an index must exist for status
        assert main(["status", "--config", str(cfg_path), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["watch"] == {"running": False}

        hb = heartbeat_path_for(load(config_path=cfg_path))
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(json.dumps({"pid": 999999999, "state": "idle"}))
        capsys.readouterr()
        assert main(["status", "--config", str(cfg_path), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["watch"]["running"] is False
        assert payload["watch"]["stale"] is True

    def test_status_json_watch_running_key(self, tmp_home, tmp_path, capsys):
        from goldmine.cli import main

        cfg_path = tmp_path / "cfg.toml"
        cfg_path.write_text("")
        db = tmp_path / "gmhome" / "default.sqlite"
        db.parent.mkdir(parents=True, exist_ok=True)
        Store(db).close()
        hb = heartbeat_path_for(load(config_path=cfg_path))
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(json.dumps({
            "pid": os.getpid(), "state": "idle", "roots": ["/x"],
            "last_pass": {"at": 1.0, "new": 1, "kind": "delta"},
            "files_indexed_total": 1, "holds": {"waiting": 0, "pressure": 1},
            "notes": [],
        }))
        assert main(["status", "--config", str(cfg_path), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        watch = payload["watch"]
        assert watch["running"] is True
        assert watch["state"] == "idle"
        assert watch["pid"] == os.getpid()

    def test_doctor_rows(self, tmp_home, tmp_path):
        cfg = make_cfg(tmp_path)
        absent = check_watch(cfg)
        assert absent.status == "warn"  # NEVER fail (D4)
        assert "not running" in absent.detail

        hb = heartbeat_path_for(cfg)
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(json.dumps({"pid": 999999999, "state": "idle"}))
        assert check_watch(cfg).status == "warn"

        hb.write_text(json.dumps({
            "pid": os.getpid(), "state": "held-battery",
            "last_pass": {"at": 1.0}, "last_scan_s": 0.1,
            "interval_s": 5, "holds": {"waiting": 9, "pressure": 1},
            "notes": [],
        }))
        held = check_watch(cfg)
        assert held.status == "ok"  # a hold is working as designed
        assert "held (battery)" in held.detail

        hb.write_text(json.dumps({
            "pid": os.getpid(), "state": "idle", "last_pass": {"at": 1.0},
            "last_scan_s": 42.0, "interval_s": 120,
            "holds": {"waiting": 0, "pressure": 1}, "notes": [],
        }))
        slow = check_watch(cfg)
        assert slow.status == "warn"
        assert "narrower roots" in slow.detail

        hb.write_text(json.dumps({
            "pid": os.getpid(), "state": "idle", "last_pass": {"at": 1.0},
            "last_scan_s": 0.2, "interval_s": 5,
            "holds": {"waiting": 0, "pressure": 1},
            "notes": ["pmset battery output unparseable 3 consecutive "
                      "probes — battery hold disabled (doctor warned)"],
        }))
        degraded = check_watch(cfg)  # R-F5: the probe warn reaches doctor
        assert degraded.status == "warn"
        assert "unparseable" in degraded.detail

    def test_running_doctor_row_ok(self, tmp_home, tmp_path):
        cfg = make_cfg(tmp_path)
        hb = heartbeat_path_for(cfg)
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(json.dumps({
            "pid": os.getpid(), "state": "idle", "last_pass": {"at": 1.0},
            "last_scan_s": 0.2, "interval_s": 5,
            "holds": {"waiting": 0, "pressure": 1}, "notes": [],
        }))
        row = check_watch(cfg)
        assert row.status == "ok"
        assert f"pid {os.getpid()}" in row.detail

    def test_config_watch_section_unknown_warn_and_clamps(
            tmp_home, tmp_path, capsys, monkeypatch):
        cfg_path = tmp_path / "cfg.toml"
        cfg_path.write_text(
            "[watch]\ninterval_fast = 2\ninterval_slow = 9\nnope_key = 1\n"
        )
        cfg = load(config_path=cfg_path)
        assert cfg.watch.interval_fast == 2
        assert cfg.watch.interval_slow == 9

        from goldmine.cli import main

        root = tmp_path / "r"
        root.mkdir()
        capsys.readouterr()
        assert main(["watch", "--config", str(cfg_path), "--roots", str(root),
                     "--interval-fast", "0"]) == 2  # clamp: usage error
        assert "--interval-fast must be >= 1" in capsys.readouterr().err
        capsys.readouterr()
        assert main(["watch", "--config", str(cfg_path), "--roots", str(root),
                     "--interval-slow", "1", "--interval-fast", "5"]) == 2
        assert "must be >= --interval-fast" in capsys.readouterr().err

    def test_coexistence_separate_locks(self, tmp_home, tmp_path):
        """D7: hold serve.lock -> watch still starts (and vice versa)."""
        from goldmine.config import resolve_home

        cfg = make_cfg(tmp_path)
        home = resolve_home(cfg)
        serve_lock = home / "serve.lock"
        fd = os.open(serve_lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # serve holds ITS lock
        try:
            root = tmp_path / "r"
            drop(root / "a.txt", "alpha one file\n")
            daemon, _ = make_daemon(tmp_path, roots=[str(root)], cfg=cfg,
                                    watch_min_pass_gap_s=0.0)
            rc = daemon.run(max_ticks=1)
            assert rc == 0
            assert daemon._passes_started == 1
        finally:
            os.close(fd)
        # the watch lock file exists alongside (never deleted)
        assert lock_path_for(cfg).exists()

    def test_concurrent_manual_ingest_consistency(self, tmp_path):
        """T36: a manual ingest interleaved with watch passes -> consistent
        counts, no duplicate rows (SQLite writer serialization)."""
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        cfg = make_cfg(tmp_path)
        daemon, _ = make_daemon(tmp_path, roots=[str(root)], cfg=cfg,
                                watch_min_pass_gap_s=0.0)
        daemon.run(max_ticks=1)
        # manual ingest of the SAME root while the watch session is between
        # passes (the real interleaving shape; different Store connection)
        from goldmine.ingest import IngestPipeline

        manual_db = daemon.db_path
        pipe = IngestPipeline(cfg, Store(manual_db), MockBackend(dim=768))
        pipe.run([str(root)])

        m = root / "m.txt"

        def drop_manual(d):
            if str(m) not in d.snapshot:  # the user drops a file mid-session
                drop(m, "manual mangrove file\n")

        daemon.tick_hook = drop_manual
        daemon.run(max_ticks=3)
        store = Store(manual_db)
        rows = store.db.execute(
            "SELECT path, COUNT(*) c FROM files GROUP BY path HAVING c > 1"
        ).fetchall()
        n_files = store.db.execute(
            "SELECT COUNT(DISTINCT path) FROM files WHERE source='file'"
        ).fetchone()[0]
        store.close()
        assert rows == []
        assert n_files == 2  # a.txt + m.txt: interleaving produced no dupes
        assert hits_for(manual_db, "mangrove file")

    def test_stdout_empty_stderr_content(self, tmp_path, capsys):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        import io

        buf = io.StringIO()
        daemon, _ = make_daemon(tmp_path, roots=[str(root)])
        daemon.stream = buf
        daemon.run(max_ticks=2)
        captured = capsys.readouterr()
        assert captured.out == ""  # SPEC §1.2: stdout stays data-only (empty)
        assert "goldmine watch: pid" in buf.getvalue()
        assert "watch pass (resume)" in buf.getvalue()

    def test_fmt_ago_shapes(self):
        from goldmine.watch import _fmt_ago

        assert _fmt_ago(None) == "never"
        assert _fmt_ago(10) == "10s ago"
        assert _fmt_ago(130) == "2m ago"
        assert _fmt_ago(7320) == "2h02m ago"


# ================================================================= hostile roots


class TestHostileRoots:
    def test_tcc_notice_surfaces(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "sub" / "a.txt", "alpha one file\n")
        locked = root / "locked"
        locked.mkdir()
        os.chmod(locked, 0o000)
        try:
            daemon, _ = make_daemon(tmp_path, roots=[str(root)])
            _, hb = run_daemon(daemon, max_ticks=2)
            assert any("TCC" in n for n in hb["notes"])
        finally:
            os.chmod(locked, 0o755)

    def test_phantom_roots_all_refuse_mixed_warn(self, tmp_home, tmp_path, capsys):
        from goldmine.cli import main

        cfg_path = tmp_path / "cfg.toml"
        cfg_path.write_text("")
        real = tmp_path / "real"
        real.mkdir()
        capsys.readouterr()
        rc = main(["watch", "--config", str(cfg_path), "--roots",
                   str(tmp_path / "nope1"), str(tmp_path / "nope2")])
        assert rc == 4
        assert "none of the given roots exist" in capsys.readouterr().err
        # mixed: the real root is watched, the phantom is a startup note
        daemon, _ = make_daemon(
            tmp_path, roots=[str(real)],
            startup_notes=["1 root(s) missing — skipped"],
        )
        _, hb = run_daemon(daemon, max_ticks=2)
        assert hb["roots"] == [str(real.resolve())]
        assert any("missing" in n for n in hb["notes"])

    def test_url_roots_filtered_with_notice(self, tmp_home, tmp_path):
        cfg_path = tmp_path / "cfg.toml"
        cfg_path.write_text('[init]\nroots = ["http://example.com/x"]\n')
        resolved, notes, missing = resolve_watch_roots(
            load(config_path=cfg_path), None
        )
        assert resolved == []
        assert any("URL root(s) ignored" in n for n in notes)


# ==================================================== R-F6 dataset-folder parity


class TestDatasetParity:
    def test_delta_pass_excludes_dataset_folder(self, tmp_path):
        """R-F6: a few changed files inside a dataset folder are NOT ingested
        by a delta pass (evaluated against the watch roots + snapshot); they
        keep the resume pass's full-parity 'dataset folder' rows."""
        root = tmp_path / "r"
        ds = root / "dataset"
        ds.mkdir(parents=True)
        for i in range(300):
            (ds / f"clip_{i:04d}.png").write_bytes(b"\x89PNG tiny")
        drop(root / "a.txt", "alpha one file\n")
        daemon, _ = make_daemon(tmp_path, roots=[str(root)],
                                watch_min_pass_gap_s=0.0,
                                watch_full_period_s=10_000)  # no sweep yet
        daemon.run(max_ticks=1)  # the resume records all 300 with parity

        def touch_one(d):
            if not getattr(d, "_parity_touched", False):
                d._parity_touched = True
                (ds / "clip_0005.png").write_bytes(b"\x89PNG changed")
                os.utime(ds / "clip_0005.png", (2000, 2000))

        daemon.tick_hook = touch_one
        daemon.run(max_ticks=2)
        # the delta never even spawned: the changed member was excluded
        assert daemon._passes_started == 1
        store = Store(daemon.db_path)
        upgraded = store.db.execute(
            "SELECT COUNT(*) FROM files WHERE path LIKE '%/dataset/%' "
            "AND detail IS NOT 'dataset folder'"
        ).fetchone()[0]
        store.close()
        assert upgraded == 0  # no media row replaced the parity row

    def test_sweep_records_dataset_members_with_parity(self, tmp_path):
        root = tmp_path / "r"
        ds = root / "dataset"
        ds.mkdir(parents=True)
        for i in range(300):
            (ds / f"clip_{i:04d}.png").write_bytes(b"\x89PNG tiny")
        drop(root / "a.txt", "alpha one file\n")
        daemon, _ = make_daemon(tmp_path, roots=[str(root)],
                                watch_min_pass_gap_s=0.0,
                                watch_full_period_s=1)
        daemon.run(max_ticks=3)  # the idle sweep runs with full context
        store = Store(daemon.db_path)
        rows = store.db.execute(
            "SELECT detail, COUNT(*) FROM files WHERE path LIKE '%/dataset/%' "
            "GROUP BY detail"
        ).fetchall()
        store.close()
        assert ("dataset folder", 300) in rows  # full-pass parity


# ============================================== R-F2 poison + exception matrix


class TestExceptionsAndPoison:
    def test_poison_not_reattempted_until_changed(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, clock = make_daemon(tmp_path, roots=[str(root)],
                                    watch_min_pass_gap_s=0.0)
        failing = {"on": True}

        class Flaky(MockBackend):
            def embed_texts(self, texts):
                if failing["on"]:
                    raise EmbeddingError("backend exploded mid-pass")
                return super().embed_texts(texts)

        daemon._backend_factory = lambda: Flaky(dim=768)
        daemon.run(max_ticks=1)
        assert daemon._passes_started == 1
        poison_path = str(root / "a.txt")
        assert poison_path in daemon._poison  # failed paths poisoned (R-F2)
        stat = os.stat(poison_path)
        assert daemon._poison[poison_path] == (stat.st_size, stat.st_mtime)
        assert last_pass_of(daemon)["failed"] == 1  # the pass completed

        # the backend recovers; the file is UNCHANGED -> not re-attempted
        failing["on"] = False
        daemon.run(max_ticks=2)
        assert daemon._passes_started == 1  # no new pass was spawned
        # ...but a real content change retries it (poison is stat-keyed)
        daemon.tick_hook = lambda d: (
            (root / "a.txt").write_text("alpha one file REWRITTEN\n"),
            os.utime(root / "a.txt", (3000, 3000)),
        )
        daemon.run(max_ticks=2)
        assert last_pass_of(daemon)["kind"] == "delta"
        assert last_pass_of(daemon)["new"] == 1

    def test_backend_startup_failure_keeps_pending(self, tmp_path):
        """R-F2: a pass that could not START retains the pending set; the
        backoff paces the retries."""
        from goldmine.backends.base import BackendStartupError

        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, _ = make_daemon(tmp_path, roots=[str(root)],
                                watch_min_pass_gap_s=100.0)

        class NoStart(MockBackend):
            def startup(self):
                raise BackendStartupError("spawn failed")

        daemon._backend_factory = lambda: NoStart(dim=768)
        _, hb = run_daemon(daemon, max_ticks=2)
        assert daemon._passes_started == 1  # attempted once, then backoff
        # the resume is still needed: the work was not lost
        assert daemon._need_resume is True
        assert any("backend failed to start" in n for n in hb["notes"])

    def test_pass_backoff_ladder(self, tmp_path):
        from goldmine.backends.base import BackendStartupError

        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, clock = make_daemon(tmp_path, roots=[str(root)])  # gap 25

        class NoStart(MockBackend):
            def startup(self):
                raise BackendStartupError("spawn failed")

        daemon._backend_factory = lambda: NoStart(dim=768)
        daemon.run(max_ticks=1)
        assert daemon._consecutive_failures == 1
        first_block = daemon._next_pass_allowed_at - daemon.last_pass_end
        assert first_block == pytest.approx(25.0)  # min_pass_gap x 2^0
        daemon.run(max_ticks=6)  # the 25s backoff elapses (decayed sleeps)
        assert daemon._consecutive_failures >= 2
        block = daemon._next_pass_allowed_at - daemon.last_pass_end
        expected = min(25.0 * 2 ** (daemon._consecutive_failures - 1), 600.0)
        assert block == pytest.approx(expected)  # the x2 ladder, capped

    def test_unexpected_error_limit_stops_loudly(self, tmp_path):
        root = tmp_path / "r"
        drop(root / "a.txt", "alpha one file\n")
        daemon, _ = make_daemon(tmp_path, roots=[str(root)],
                                watch_min_pass_gap_s=0.0)

        class Bomb(MockBackend):
            def startup(self):
                raise ValueError("a bug, not a backend failure")

        daemon._backend_factory = lambda: Bomb(dim=768)
        rc = daemon.run(max_ticks=8)
        assert rc == 1  # loud stop after the consecutive-error limit
