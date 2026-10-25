"""goldmine watch — the folder auto-follow daemon (R9, docs/v2.1-r9-watch.md
as amended by review R-F1..R-F8).

Two-tier detection (D2): a cheap scandir snapshot diff decides WHO to re-check;
the unchanged IngestPipeline (its own walk + sha256 skip + per-file atomic
commits) decides WHAT happens. Watch decides when; the pipeline owns how.

Lifecycle (D4): flock single-instance -> stale-heartbeat sweep -> ungated
startup walk (R-F3: seeds the snapshot + records scope via the roots_json
merge — no backend, no embedding) -> loop [sleep -> power probes (30 s cache)
-> scan -> debounce -> passes] -> SIGTERM/SIGINT drains the in-flight pass at
its next wave boundary (R-F2) -> delete watch.json -> exit 0. SIGKILL leaves a
stale heartbeat + a held flock + possibly an orphaned llama-server child; the
next start sweeps all three (the child only after verifying its receipt, the
R8 F3 discipline).

Power (D6 + R-F4/R-F5): pmset battery/thermal/lowpowermode + the kernel
pressure level via the SAME sampler serve uses (one signal, two consumers).
Only the expensive tier (embed-bearing passes) gates; scans, diffing and the
heartbeat never stop.

Pass-rate floor (R-F1): no embed-bearing pass more often than
`min_pass_gap_s` (bursts coalesce across it); the poll interval never drops
below 2x the measured scan duration; battery mode additionally enforces
`battery_pass_gap_s`. A 4 s-periodic writer therefore costs <= ~2.4
spawns/minute instead of the measured 29-per-10-min churn case.

stdout stays empty in every mode (SPEC §1.2); banner, pass summaries and stop
lines go to stderr (§11.5, the serve precedent).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .backends import BackendUnavailable, create_backend
from .backends.base import BackendStartupError, EmbeddingError
from .backends.serve import pid_alive
from .config import Config, resolve_home
from .errors import UserFacingError, render_error
from .fingerprints import FingerprintMismatch
from .ingest import rules, walk
from .ingest.walk import Candidate, SkipNotice, walk_files
from .serve import (
    _ps_field,
    default_pressure_sampler,
    proc_matches_receipt,
    verify_and_kill,
)
from .store import Store

logger = logging.getLogger("goldmine.watch")

HEARTBEAT_FILE = "watch.json"
LOCK_FILE = "watch.lock"

#: Consecutive unparseable battery probes before the hold is dropped (R-F5).
BATTERY_UNPARSEABLE_LIMIT = 3

#: Doctor's loud-cap for one scan (D2, 02 §5.6:459-460).
SCAN_COST_WARN_S = 30.0

#: Cap of the R-F2 failure backoff ladder (min_gap x 2^n).
PASS_BACKOFF_CAP_S = 600.0

#: Consecutive UNEXPECTED pass exceptions before the daemon stops loudly.
MAX_UNEXPECTED_PASS_ERRORS = 5

#: The R-F1d duty bound a 10-min churn must stay under (the min-pass-gap
#: bound: 600 s / 25 s). The live duty test asserts the measured spawn count
#: against this number.
DUTY_MAX_SPAWNS_PER_10MIN = 24


# ------------------------------------------------------------- pmset parsing


def parse_battery(text: str) -> str:
    """`pmset -g batt` -> "ac" | "battery" | "no_battery" | "unparseable"
    (spec §1 measured shapes + R-F4/R-F5). Anything not 'AC Power' holds
    (UPS included, conservative); `No batteries available.` is always-AC."""
    if "No batteries available" in text:
        return "no_battery"
    if "'AC Power'" in text:
        return "ac"
    if re.search(r"Now drawing from '", text):
        return "battery"
    return "unparseable"


def parse_therm(text: str) -> bool:
    """`pmset -g therm` -> True when the machine is throttled (R-F4).

    Measured shapes: the CLEAR state prints NO CPU_Speed_Limit line at all,
    only `Note: No … recorded` lines; a throttled machine prints
    `CPU_Speed_Limit = 45` (separator varies across macOS builds, so the
    parse takes the substring + the first following integer). A limit of 100
    is FULL SPEED — never a hold (§11.9)."""
    idx = text.find("CPU_Speed_Limit")
    if idx >= 0:
        m = re.search(r"(\d+)", text[idx:])
        if m is not None:
            return 0 < int(m.group(1)) < 100
        return True  # the throttle report is present but numberless — caution
    for line in text.splitlines():
        if "Note:" in line:
            rest = line.split("Note:", 1)[1].strip()
            if rest and not rest.startswith("No"):
                return True
    return False


def parse_lowpower(text: str) -> bool | None:
    """`pmset -g` -> lowpowermode flag, or None when absent/unparseable.
    Read + reported in the heartbeat; never a gate in R9 (H12)."""
    m = re.search(r"lowpowermode\s+(\d)", text)
    return bool(int(m.group(1))) if m is not None else None


def _default_pmset_runner(arg: str) -> str | None:
    """One bounded `pmset -g <arg>` probe; None when pmset is missing or
    fails (non-macOS dev boxes degrade to no-hold, honestly)."""
    try:
        proc = subprocess.run(
            ["pmset", "-g", arg],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


@dataclass
class Holds:
    """One probed power state (heartbeat `holds/`)."""
    battery: bool = False
    thermal: bool = False
    pressure: int = 1
    lpm: bool | None = None
    notes: tuple[str, ...] = ()

    def payload(self) -> dict:
        return {
            "battery": self.battery,
            "thermal": self.thermal,
            "pressure": self.pressure,
            "lpm": self.lpm,
        }


class PowerProbes:
    """Cached power probes (D6: a pmset spawn is ~10-20 ms — never per
    decision; the 30 s cache is the serve guard's sample-cache shape). The
    pmset runner and the pressure sampler are injectable; the default sampler
    IS serve's `default_pressure_sampler` (identity pinned by a test — one
    signal, two consumers, and the GOLDMINE_SERVE_PRESSURE_LEVEL determinism
    hook applies to both)."""

    def __init__(self, *, cache_s: float = 30.0, clock=time.monotonic,
                 pmset_runner=None, sampler=None):
        self.cache_s = float(cache_s)
        self._clock = clock
        self._pmset = pmset_runner or _default_pmset_runner
        self._sampler = sampler or default_pressure_sampler
        self._sampled_at: float | None = None
        self._battery_streak = 0
        self._streak_note_shown = False
        self.state = Holds()

    def sample(self, now: float | None = None) -> Holds:
        now = self._clock() if now is None else now
        if self._sampled_at is not None and now - self._sampled_at < self.cache_s:
            return self.state
        self._sampled_at = now
        notes: list[str] = []

        batt_text = self._pmset("batt")
        if batt_text is None:
            # probe UNAVAILABLE (no pmset / spawn failed) — never hold on a
            # signal we could not read at all (a Linux CI box must not
            # deadlock); a real battery answer always parses to a mode.
            battery = False
            self._battery_streak = 0
            notes.append("pmset unavailable — battery hold disabled")
        else:
            mode = parse_battery(batt_text)
            if mode in ("ac", "battery", "no_battery"):
                self._battery_streak = 0
                self._streak_note_shown = False
            else:  # unparseable output
                self._battery_streak += 1
            if mode == "battery":
                battery = True
            elif mode in ("ac", "no_battery"):
                battery = False
            else:
                battery = self._battery_streak < BATTERY_UNPARSEABLE_LIMIT
                if not battery and not self._streak_note_shown:
                    self._streak_note_shown = True
                    notes.append(
                        f"pmset battery output unparseable "
                        f"{self._battery_streak} consecutive probes — battery "
                        "hold disabled (doctor warned)"
                    )

        therm_text = self._pmset("therm")
        thermal = parse_therm(therm_text) if therm_text is not None else False

        lpm_text = self._pmset("")
        lpm = parse_lowpower(lpm_text) if lpm_text is not None else None

        try:
            pressure = int(self._sampler())
        except Exception:
            pressure = 1
        if pressure not in (1, 2, 3):
            pressure = 1

        self.state = Holds(
            battery=battery, thermal=thermal, pressure=pressure, lpm=lpm,
            notes=tuple(notes),
        )
        return self.state


# -------------------------------------------------------------------- cadence


class Cadence:
    """Adaptive poll cadence (D2/G11 + R-F1b): reset to fast on any detected
    change and at start; each quiet scan doubles; capped at slow. The
    effective sleep never drops below 2x the last measured scan duration
    (the cheap tier may not cost more than ~50% duty). Jitter is
    `x uniform(0.9, 1.1)` with an injectable rng so offline tests are exact."""

    def __init__(self, fast: float, slow: float, rng=None):
        self.fast = max(1.0, float(fast))
        self.slow = max(self.fast, float(slow))
        self.rng = rng if rng is not None else random.random
        self.interval = self.fast

    def activity(self) -> None:
        self.interval = self.fast

    def quiet(self) -> None:
        self.interval = min(self.interval * 2.0, self.slow)

    def sleep_for(self, last_scan_s: float = 0.0) -> float:
        base = max(self.interval, 2.0 * float(last_scan_s))  # R-F1b floor
        return base * (0.9 + 0.2 * self.rng())


# ----------------------------------------------------------------- rendering


def heartbeat_path_for(cfg: Config) -> Path:
    return resolve_home(cfg) / HEARTBEAT_FILE


def lock_path_for(cfg: Config) -> Path:
    return resolve_home(cfg) / LOCK_FILE


def read_heartbeat(cfg: Config) -> dict | None:
    """Raw watch.json contents (dict) or None — read-only, never swept by
    clients (D4: swept by the next start)."""
    try:
        data = json.loads(heartbeat_path_for(cfg).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _fmt_ago(seconds: float | None) -> str:
    """`2m ago` shape (D4); None/absent -> "never". The fmt_clock family
    (output.py) renders durations; this renders an AGE for the status line."""
    if seconds is None:
        return "never"
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    hours = seconds // 3600
    return f"{hours}h{(seconds % 3600) // 60:02d}m ago"


def _hold_label(state: str, holds: dict) -> str:
    if state == "held-thermal":
        return "thermal"
    if state == "held-battery":
        return "battery"
    if state == "deferred-memory":
        return f"memory pressure (level {holds.get('pressure')})"
    return state


def render_status_line(cfg: Config, *, now: float | None = None) -> str | None:
    """The H8 `status` line (D4 + R-F7). None = the user never used watch —
    silent, no noise for non-users. Staleness keys on PID LIVENESS (R-F7),
    never on heartbeat age."""
    data = read_heartbeat(cfg)
    if data is None:
        return None
    pid = data.get("pid")
    if not isinstance(pid, int) or not pid_alive(pid):
        return (
            f"watch: not running (stale heartbeat, dead pid {pid} — swept by "
            "the next start)"
        )
    now = time.time() if now is None else now
    state = str(data.get("state") or "idle")
    holds = data.get("holds") or {}
    last = data.get("last_pass") or {}
    ago = _fmt_ago(now - last["at"]) if last.get("at") else "never"
    waiting = holds.get("waiting") or 0
    if state.startswith("held") or state == "deferred-memory" or waiting:
        line = (
            f"watch: running · held ({_hold_label(state, holds)} — "
            f"{waiting} files waiting)"
        )
        if last.get("at"):
            line += f" · last pass {ago}"
        return line
    line = f"watch: running · last pass {ago}"
    new_n = last.get("new")
    if new_n:
        line += (
            f" · {new_n} new files indexed "
            f"({data.get('files_indexed_total', 0)} this session)"
        )
    return line


def watch_status_payload(cfg: Config) -> dict:
    """The `status --json` envelope key (D4): the heartbeat's stable fields,
    or the two pinned not-running shapes."""
    data = read_heartbeat(cfg)
    if data is None:
        return {"running": False}
    pid = data.get("pid")
    if not isinstance(pid, int) or not pid_alive(pid):
        return {"running": False, "stale": True, "pid": pid}
    out = {
        "running": True,
        "pid": pid,
        "state": data.get("state"),
        "started_at": data.get("started_at"),
        "roots": data.get("roots") or [],
        "interval_s": data.get("interval_s"),
        "last_scan_s": data.get("last_scan_s"),
        "last_pass": data.get("last_pass"),
        "files_indexed_total": data.get("files_indexed_total"),
        "passes_started": data.get("passes_started"),
        "holds": data.get("holds"),
        "notes": data.get("notes") or [],
    }
    return out


# ------------------------------------------------------------------ the daemon


class WatchDaemon:
    """Foreground daemon: flock -> sweep -> ungated startup walk -> loop.
    `run()` returns an int exit code (0 normally; 3 fingerprint refusal;
    1 unexpected-crash limit). Scope resolution + the start-time backend
    preflight live in cmd_watch; the daemon owns everything per-tick."""

    def __init__(
        self,
        cfg: Config,
        db_path: Path,
        *,
        roots: list[str],
        startup_notes: list[str] | None = None,
        code: bool = False,
        exclude: list[str] | None = None,
        include_datasets: bool = False,
        lexical_only: bool = False,
        backend_name: str | None = None,
        interval_fast: float | None = None,
        interval_slow: float | None = None,
        settle_s: float | None = None,
        linger_s: float | None = None,
        forever: bool = False,
        json_mode: bool = False,
        stream=None,
        clock=time.monotonic,
        wall=time.time,
        sleeper=time.sleep,
        rng=None,
        probes: PowerProbes | None = None,
        backend_factory=None,
        tick_hook=None,
    ):
        self.cfg = cfg
        self.db_path = Path(db_path)
        self.home = resolve_home(cfg)
        self.lock_path = lock_path_for(cfg)
        self.heartbeat_path = heartbeat_path_for(cfg)
        self.roots = list(roots)                     # resolved absolute paths
        self.startup_notes = list(startup_notes or [])
        self.code = bool(code)
        self.exclude = list(exclude or [])
        self.include_datasets = bool(include_datasets)
        self.lexical_only = bool(lexical_only)
        self.backend_name = backend_name
        wc = cfg.watch
        self.interval_fast = float(wc.interval_fast if interval_fast is None else interval_fast)
        self.interval_slow = float(wc.interval_slow if interval_slow is None else interval_slow)
        self.settle_s = float(wc.settle_s if settle_s is None else settle_s)
        self.linger_s = float(wc.linger if linger_s is None else linger_s)
        self.forever = bool(forever)
        self.min_pass_gap_s = float(wc.min_pass_gap_s)
        self.battery_pass_gap_s = float(wc.battery_pass_gap_s)
        self.battery_max_batch = int(wc.battery_max_batch)
        self.full_period_s = float(wc.full_period_s)
        self.json_mode = bool(json_mode)
        self.stream = stream if stream is not None else sys.stderr
        self._clock = clock
        self._wall = wall
        self._sleep_fn = sleeper
        self.cadence = Cadence(self.interval_fast, self.interval_slow, rng=rng)
        self.probes = probes if probes is not None else PowerProbes(clock=clock)
        self.tick_hook = tick_hook
        self._backend_factory = backend_factory

        self.store: Store | None = None
        self.lock_fd: int | None = None
        self.snapshot: dict[str, tuple[int, float, str]] = {}
        self.pending: set[str] = set()
        self._gone_detected = False
        self._poison: dict[str, tuple[int, float]] = {}
        self._need_resume = True
        self._stopping = False
        self._stop_reason: str | None = None
        self._fatal: tuple[str, str] | None = None      # (kind, why) -> exit 3
        self._exit_code = 0
        self._pipeline = None                            # in-flight (R-F2)
        self._child_receipt: dict | None = None
        self._unexpected_errors = 0
        self._consecutive_failures = 0
        self._next_pass_allowed_at = 0.0
        self.last_scan_s = 0.0
        self.last_pass_end = 0.0
        self.last_sweep_at: float | None = None
        self.last_activity: float = 0.0
        self.started_mono = self._clock()
        self.started_wall = self._wall()
        self._self_start: str | None = None
        self._notes: list[str] = list(self.startup_notes)
        self._dir_skips: dict[str, int] = {}
        self._last_pass: dict | None = None
        self._files_indexed_total = 0
        self._passes_started = 0
        self._holds = Holds()
        self._state = "starting"
        self._waiting = 0

    # ------------------------------------------------------------- lifecycle

    def run(self, *, max_ticks: int | None = None) -> int:
        if not self._acquire_lock():
            return 0
        try:
            self._sweep_stale_state()
            self.store = Store(self.db_path)
            self._record_scope()                      # R-F3 scope record
            self._install_signals()
            # R-F3: the ungated startup walk seeds the snapshot; the
            # embed-bearing resume pass is deferred to the first tick.
            self.snapshot, scan_s, dir_skips = self._scan()
            self.last_scan_s = scan_s
            self._dir_skips = dir_skips
            self._refresh_skip_notes()
            self.last_activity = self._clock()
            self._self_start = self._self_start or _ps_field(os.getpid(), "lstart")
            self._print(
                f"goldmine watch: pid {os.getpid()} · {len(self.roots)} root(s) · "
                f"scan {self.interval_fast:g}s→{self.interval_slow:g}s · "
                f"linger {self.linger_s:g}s · holds: battery>"
                f"{self.battery_max_batch} batch /{self.battery_pass_gap_s:g}s · "
                "thermal pause · pressure defer"
                + (" · lexical-only" if self.lexical_only else "")
            )
            self._write_heartbeat(state="idle")
            ticks = 0
            while not self._stopping and self._fatal is None:
                if max_ticks is not None and ticks >= max_ticks:
                    break
                self._sleep_fn(self.cadence.sleep_for(self.last_scan_s))
                if self._stopping:
                    break
                if self.tick_hook is not None:
                    self.tick_hook(self)
                ticks += 1
                self._tick()
                if self._fatal is not None:
                    self._exit_code = 3
                    break
                if (
                    not self.forever
                    and not self.pending
                    and (self._clock() - self.last_activity) > self.linger_s
                ):
                    self._stop_reason = (
                        f"linger expired ({int(self._clock() - self.last_activity)}s quiet)"
                    )
                    break
        finally:
            self._shutdown()
        return self._exit_code

    def request_stop(self, reason: str) -> None:
        """The clean-stop path (signals, tests): set the stop flag AND cancel
        the in-flight pipeline so the pass unwinds at the next wave boundary
        (R-F2)."""
        if self._stop_reason is None:
            self._stop_reason = reason
        self._stopping = True
        pipeline = self._pipeline
        if pipeline is not None:
            try:
                pipeline.cancelled = True
            except Exception:  # pragma: no cover - defensive
                pass

    def _acquire_lock(self) -> bool:
        self.lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            self.lock_fd = None
            data = read_heartbeat(self.cfg)
            pid = (data or {}).get("pid")
            if isinstance(pid, int) and pid_alive(pid):
                self._print(f"already watching (pid {pid})")
            else:
                self._print("already watching (lock held)")
            return False
        return True

    def _sweep_stale_state(self) -> None:
        """We hold the flock, so any previous daemon is gone: receipt-sweep a
        possibly-orphaned llama-server child (R8 F3 discipline — never a
        blind kill), then the stale heartbeat file (swept by the next start,
        never by clients)."""
        data = read_heartbeat(self.cfg)
        if data:
            for child in data.get("children") or []:
                action = verify_and_kill(child)
                self._print(
                    f"goldmine watch: orphan sweep: child pid "
                    f"{child.get('pid')}: {action}"
                )
            self._print(
                f"goldmine watch: swept stale heartbeat (pid {data.get('pid')})"
            )
        try:
            self.heartbeat_path.unlink()
        except OSError:
            pass

    def _record_scope(self) -> None:
        """R-F3: the scope record is the roots_json merge with `_finalize`'s
        exact shape, done directly (no backend, no ingest pass). `--roots`
        lands here exactly once per session — the same thing a manual
        `goldmine ingest <path>` records; delta/resume passes never touch it
        (D3, update_roots=False)."""
        assert self.store is not None
        stored = self.store.meta("roots_json")
        try:
            previous = json.loads(stored) if stored else []
        except json.JSONDecodeError:
            previous = []
        merged = list(dict.fromkeys([*previous, *self.roots]))
        self.store.set_meta("roots_json", json.dumps(merged))

    def _install_signals(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(
                    sig,
                    lambda signum, _frame: self.request_stop(
                        f"signal {signal.Signals(signum).name}"
                    ),
                )
            except ValueError:
                pass  # not the main thread (in-process tests)

    def _shutdown(self) -> None:
        # R-F2 drain: bounded wait for the in-flight pass to unwind (the
        # signal handler already set pipeline.cancelled; the loop is
        # single-threaded so the pass has normally returned by now).
        deadline = self._clock() + 10.0
        while self._pipeline is not None and self._clock() < deadline:
            self._sleep_fn(0.05)
        try:
            self.heartbeat_path.unlink()
        except OSError:
            pass
        try:
            self.heartbeat_path.with_name(self.heartbeat_path.name + ".tmp").unlink()
        except OSError:
            pass
        if self.store is not None:
            try:
                self.store.close()
            except Exception:  # pragma: no cover - shutdown must not raise
                pass
            self.store = None
        self._release_lock()
        self._state = "stopping"
        self._print(
            f"goldmine watch: stopped ({self._stop_reason or 'shutdown'})"
        )

    def _release_lock(self) -> None:
        if self.lock_fd is not None:
            try:
                os.close(self.lock_fd)  # releases the flock
            except OSError:
                pass
            self.lock_fd = None

    # ------------------------------------------------------------------ tick

    def _tick(self) -> None:
        now = self._clock()
        self._holds = self.probes.sample(now)
        for note in self._holds.notes:
            self._note(note)

        self._write_heartbeat(state="scanning")
        new_snap, scan_s, dir_skips = self._scan()
        self.last_scan_s = scan_s
        added, changed, gone = self._diff(self.snapshot, new_snap)
        self.snapshot = new_snap
        self._dir_skips = dir_skips
        self._refresh_skip_notes()

        detected = bool(added or changed)
        if detected:
            self.cadence.activity()
            self.last_activity = now
            # D2 debounce: settle, rescan ONCE, merge the second diff — a
            # burst of editor writes collapses into one pass with the union.
            if self.settle_s > 0:
                self._sleep_fn(self.settle_s)
                new_snap2, _s2, dir_skips2 = self._scan()
                self._dir_skips = dir_skips2
                self._refresh_skip_notes()
                a2, c2, g2 = self._diff(self.snapshot, new_snap2)
                self.snapshot = new_snap2
                added += a2
                changed += c2
                gone = gone or bool(g2) or any(p not in new_snap2 for p in added)
            self.pending.update(added)
            self.pending.update(changed)
            # paths that vanished before their pass: sweep territory, not
            # delta roots (the pipeline would record per-root failures)
            self.pending = {p for p in self.pending if p in self.snapshot}
            if gone:
                self._gone_detected = True
        else:
            self.cadence.quiet()

        now = self._clock()
        resume_ran = False
        if self._need_resume:
            if self._pass_allowed("resume", len(self.snapshot), now):
                if self._run_pass("resume", self.roots):
                    self._need_resume = False
                    self.last_sweep_at = now
                    self.pending.clear()
                    resume_ran = True
        elif self.pending:
            if self._pass_allowed("delta", len(self.pending), now):
                roots = self._delta_roots()
                if roots:
                    self._run_pass("delta", roots)
                # the snapshot reflects post-pass state: uncommitted paths
                # resurface via content change or sweep (R-F2 policy)
                self.pending.clear()

        now = self._clock()
        if not self._need_resume and self._sweep_due(now):
            if self._pass_allowed("sweep", len(self.snapshot), now):
                if self._run_pass("sweep", self.roots):
                    self.last_sweep_at = now
                    self._gone_detected = False

        state = "idle"
        waiting_work = bool(self.pending) or self._need_resume or self._gone_detected
        if waiting_work:
            if self._holds.thermal:
                state = "held-thermal"
            elif self._holds.battery and (self._need_resume or bool(self.pending)):
                state = "held-battery"
            elif self._holds.pressure >= 2 and not self.lexical_only:
                state = "deferred-memory"
        self._write_heartbeat(
            state=state,
            waiting=len(self.pending) if self.pending
            else (len(self.snapshot) if self._need_resume else 0),
        )

    # ------------------------------------------------------------------ scan

    def _scan(self) -> tuple[dict[str, tuple[int, float, str]], float, dict[str, int]]:
        """Tier-1 (D2): the same walk ingest uses, classification and skip
        policy inherited byte-for-byte; a scandir snapshot with no file reads.
        Returns (snapshot, duration_s, {dir-skip reason: count})."""
        t0 = self._clock()
        snap: dict[str, tuple[int, float, str]] = {}
        dir_skips: dict[str, int] = {}
        try:
            items = walk_files(
                [Path(r) for r in self.roots],
                code=self.code,
                exclude=list(self.exclude),
                max_file_bytes=int(self.cfg.ingest.max_file_mb) * (1 << 20),
            )
            for item in items:
                if isinstance(item, Candidate):
                    snap[str(item.path)] = (item.size, item.mtime, item.kind)
                elif isinstance(item, SkipNotice) and item.is_dir:
                    dir_skips[item.reason] = dir_skips.get(item.reason, 0) + 1
                # file-level SkipNotices (unsupported files) are NOT tracked:
                # the sweep tier owns them (spec T1)
        except Exception as exc:  # a scan must never kill the loop
            self._note(f"scan failed this tick: {type(exc).__name__}: {exc}")
        return snap, self._clock() - t0, dir_skips

    def _diff(self, old, new) -> tuple[list[str], list[str], bool]:
        added = [p for p in new if p not in old]
        changed = [p for p in new if p in old and old[p][:2] != new[p][:2]]
        gone = [p for p in old if p not in new]
        return added, changed, bool(gone)

    def _refresh_skip_notes(self) -> None:
        """Folder-level skip notices (TCC et al.) surface in the heartbeat
        notes — unreadable is never silent (walk.py F8 discipline)."""
        self._notes = [n for n in self._notes if not n.startswith("scan:")]
        for reason, count in sorted(self._dir_skips.items()):
            self._notes.append(f"scan: {count} folder(s) skipped: {reason}")

    def _note(self, text: str) -> None:
        if text not in self._notes:
            self._notes.append(text)

    # ------------------------------------------------------------ pass policy

    def _pass_allowed(self, kind: str, n_waiting: int, now: float) -> bool:
        """The gating policy (D6 + R-F1): thermal pauses everything;
        pressure defers model-bearing passes; battery batches + paces; the
        R-F1a gap applies to every embed-bearing pass; backoff after
        failures (R-F2). Returns False and names the hold in the state."""
        if now < self._next_pass_allowed_at:
            return False
        holds = self._holds
        if holds.thermal:
            return False
        if holds.pressure >= 2 and not self.lexical_only:
            return False
        if holds.battery:
            if kind in ("sweep", "resume"):
                return False  # sweep-class waits for AC (D6)
            if n_waiting > self.battery_max_batch:
                return False
            if now - self.last_pass_end < self.battery_pass_gap_s:
                return False
        if now - self.last_pass_end < self.min_pass_gap_s:
            return False
        return True

    def _sweep_due(self, now: float) -> bool:
        if self.last_sweep_at is None:
            return self._gone_detected
        since = now - self.last_sweep_at
        if self._gone_detected and since >= self.full_period_s / 2.0:
            return True
        return since >= self.full_period_s

    def _delta_roots(self) -> list[str]:
        """The delta pass root list: pending minus poisoned (R-F2) minus
        dataset-folder members evaluated against the WATCH roots + snapshot
        (R-F6 — the same context a full pass gives the detector)."""
        out: list[str] = []
        for path in sorted(self.pending):
            poison = self._poison.get(path)
            if poison is not None:
                try:
                    st = os.stat(path)
                except OSError:
                    continue  # vanished — sweep will prune the row
                if (st.st_size, st.st_mtime) == poison:
                    continue
            out.append(path)
        return self._exclude_datasets(out)

    def _exclude_datasets(self, paths: list[str]) -> list[str]:
        media = ("image", "audio", "video")
        if not any(self.snapshot.get(p, (0, 0, ""))[2] in media for p in paths):
            return paths
        files = [
            (str(Path(p).parent), Path(p).stem, kind)
            for p, (_s, _m, kind) in self.snapshot.items()
            if kind in media
        ]
        datasets = rules.dataset_folders(
            files, roots={str(Path(r)) for r in self.roots}
        )
        if not datasets:
            return paths
        return [p for p in paths if str(Path(p).parent) not in datasets]

    # -------------------------------------------------------------- the pass

    def _make_backend(self):
        if self._backend_factory is not None:
            return self._backend_factory()
        return create_backend(self.cfg, self.backend_name)

    def _run_pass(self, kind: str, roots: list[str]) -> bool:
        """One ingest pass (D2 tier-2: the pipeline unchanged). Returns True
        when the pipeline STARTED (the pending set may be cleared); False
        when it could not start (pending retained for retry after backoff).
        Never raises: F2's exception policy is the whole point."""
        from .ingest import IngestPipeline
        from .render import render_ingest_summary

        self._passes_started += 1
        self._write_heartbeat(state="ingesting", pass_kind=kind)  # R-F7
        t0 = self._clock()
        backend = None
        if not self.lexical_only:
            try:
                backend = self._make_backend()
            except BackendUnavailable as exc:
                self._pass_failed(f"backend unavailable: {exc}")
                return False
        pipeline = IngestPipeline(
            self.cfg, self.store, backend, lexical_only=self.lexical_only
        )
        self._pipeline = pipeline
        self._wrap_receipt(backend)
        try:
            report = pipeline.run(
                list(roots),
                code=self.code,
                exclude=list(self.exclude),
                include_datasets=self.include_datasets,
                update_roots=False,  # D3: watch passes never write scope
            )
        except FingerprintMismatch as exc:
            # R-F2: permanent refusal for this index+backend pair — render
            # the universal shape and stop with exit 3.
            self._fatal = ("fingerprint_mismatch", str(exc))
            return False
        except BackendStartupError as exc:
            self._pass_failed(f"backend failed to start: {exc}")
            return False
        except OSError as exc:
            self._pass_failed(f"pass I/O error: {exc}")
            return False
        except EmbeddingError as exc:
            self._poison_failed_paths(roots)
            self._pass_failed(f"embedding failed mid-pass: {exc}")
            return True  # started: pending clears, poison keeps the state
        except Exception as exc:  # unexpected: backoff, loud after N in a row
            self._unexpected_errors += 1
            self._pass_failed(f"unexpected pass error: {type(exc).__name__}: {exc}")
            if self._unexpected_errors >= MAX_UNEXPECTED_PASS_ERRORS:
                self._stop_reason = (
                    f"{self._unexpected_errors} consecutive unexpected pass errors"
                )
                self._stopping = True
                self._exit_code = 1
            return False
        finally:
            self._pipeline = None
            self._child_receipt = None
            self._write_heartbeat()  # clear the receipt from the heartbeat

        self._consecutive_failures = 0
        self._unexpected_errors = 0
        self._next_pass_allowed_at = 0.0
        duration = self._clock() - t0
        # R-F2 poison dedup: per-file embedding failures are failed rows in
        # the completed report (the pipeline fails THIS file and carries on),
        # so the poison scan runs on every pass — a failed path is not
        # re-attempted by a delta pass unless its size/mtime changed again.
        self._poison_failed_paths(roots)
        self._last_pass = {
            "at": self._wall(),
            "kind": kind,
            "duration_s": round(duration, 3),
            "scanned": report.scanned,
            "new": report.content_indexed,
            "metadata_only": report.metadata_only,
            "deleted": report.deleted,
            "failed": report.failed,
            "unchanged": report.unchanged,
        }
        self._files_indexed_total += report.content_indexed
        self.last_pass_end = self._clock()
        self.cadence.activity()
        self._print(
            f"watch pass ({kind}): scanned {report.scanned} · indexed "
            f"{report.content_indexed} · unchanged {report.unchanged} · "
            f"failed {report.failed} · deleted {report.deleted} · "
            f"{duration:.1f}s"
        )
        if self.json_mode:
            print(render_ingest_summary(report, json_mode=True), file=self.stream)
        return True

    def _pass_failed(self, why: str) -> None:
        """R-F2: heartbeat note + exponential backoff + continue — the watch
        loop never crashes on a pass failure."""
        self._consecutive_failures += 1
        backoff = min(
            self.min_pass_gap_s * (2 ** (self._consecutive_failures - 1)),
            PASS_BACKOFF_CAP_S,
        )
        self._next_pass_allowed_at = self._clock() + backoff
        self.last_pass_end = self._clock()
        self._note(f"last pass failed: {why.splitlines()[0][:200] if why else why}")
        self._print(f"goldmine watch: pass failed ({why}) — retrying after "
                    f"{backoff:.0f}s backoff")

    def _poison_failed_paths(self, roots: list[str]) -> None:
        """R-F2 poison dedup: paths UNDER this pass's roots whose store row
        is state='failed' (the pipeline records the row before re-raising)
        are poisoned at their current (size, mtime); delta passes skip them
        unless the content changes again."""
        assert self.store is not None
        file_roots = [str(Path(r)) for r in roots]

        def under_roots(path: str) -> bool:
            return any(
                path == r or path.startswith(r.rstrip("/") + "/")
                for r in file_roots
            )

        try:
            rows = self.store.db.execute(
                "SELECT path FROM files WHERE state = 'failed'"
            ).fetchall()
        except Exception:  # pragma: no cover - a poisoned read must not raise
            return
        for (path,) in rows:
            if not under_roots(path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            self._poison[path] = (st.st_size, st.st_mtime)

    def _wrap_receipt(self, backend) -> None:
        """R8-F3 discipline, reused: capture the pass backend's llama-server
        child receipt into the heartbeat IMMEDIATELY after Popen so a SIGKILL
        mid-pass leaves a sweepable receipt (verify-before-kill on the next
        start). llama.py itself is untouched."""
        if backend is None or not hasattr(backend, "_spawn"):
            return
        original_spawn = backend._spawn

        def spawn_with_receipt(port):
            proc = original_spawn(port)
            self._child_receipt = {
                "pid": proc.pid,
                "kind": "llama-server",
                "start_time": _ps_field(proc.pid, "lstart"),
                "comm": _ps_field(proc.pid, "comm"),
                "created_at": self._wall(),
            }
            try:
                self._write_heartbeat()
            except OSError:
                pass
            return proc

        backend._spawn = spawn_with_receipt  # type: ignore[method-assign]

    # -------------------------------------------------------------- heartbeat

    def _heartbeat_payload(self, state: str | None = None,
                           waiting: int | None = None) -> dict:
        holds = self._holds.payload()
        holds["waiting"] = self._waiting if waiting is None else waiting
        return {
            "proto": 1,
            "goldmine_version": __version__,
            "pid": os.getpid(),
            "start_time": self._self_start,
            "started_at": self.started_wall,
            "roots": list(self.roots),
            "state": state or self._state,
            "interval_s": round(self.cadence.interval, 2),
            "last_scan_s": round(self.last_scan_s, 3),
            "last_pass": self._last_pass,
            "files_indexed_total": self._files_indexed_total,
            "passes_started": self._passes_started,
            "holds": holds,
            "children": [self._child_receipt] if self._child_receipt else [],
            "notes": list(self._notes)[-8:],
        }

    def _write_heartbeat(self, state: str | None = None,
                         pass_kind: str | None = None,
                         waiting: int | None = None) -> None:
        if state is not None:
            self._state = state
        if waiting is not None:
            self._waiting = waiting
        payload = self._heartbeat_payload()
        if pass_kind and payload["last_pass"] is None:
            payload["pass_kind_pending"] = pass_kind
        self._atomic_write(self.heartbeat_path, payload)

    @staticmethod
    def _atomic_write(path: Path, payload: dict) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=1) + "\n")
        os.replace(tmp, path)  # atomic (D4)

    def _print(self, text: str) -> None:
        print(text, file=self.stream, flush=True)


# ---------------------------------------------------------------- commands


def _load_cfg(args):
    from .config import load

    config_flag = getattr(args, "config", None)
    return load(config_path=Path(config_flag) if config_flag else None)


def resolve_watch_roots(
    cfg: Config, roots_flag: list[str] | None
) -> tuple[list[str], list[str], list[str]]:
    """D1 scope resolution: `--roots` replaces the default (`cfg.init.roots`);
    URLs are filtered with a notice (watch follows folders); every root is
    stat-ed. Returns (resolved_file_roots, notes, missing_descriptions).
    Empty resolution = the caller refuses (exit 4)."""
    raw = [str(r) for r in (roots_flag if roots_flag else cfg.init.roots)]
    notes: list[str] = []
    urls = [r for r in raw if r.startswith(("http://", "https://"))]
    if urls:
        notes.append(
            f"{len(urls)} URL root(s) ignored — watch follows folders only"
        )
    resolved: list[str] = []
    missing: list[str] = []
    for r in raw:
        if r in urls:
            continue
        path = Path(r).expanduser().resolve()
        try:
            path.stat()
        except OSError as exc:
            missing.append(f"{path} ({exc})")
        else:
            resolved.append(str(path))
    resolved = list(dict.fromkeys(resolved))
    return resolved, notes, missing


def _render_refusal(kind: str, message: str, why: str, fixes: list[str],
                    json_mode: bool, stream=None) -> None:
    render_error(kind=kind, message=message, why=why, fixes=fixes,
                 doctor=True, json_mode=json_mode, stream=stream)


def _clamp_args(args, cfg) -> None:
    """Flag clamps (§6.2): violations are usage errors (exit 2)."""
    fast = getattr(args, "interval_fast", None)
    slow = getattr(args, "interval_slow", None)
    settle = getattr(args, "settle", None)
    if fast is not None and fast < 1:
        raise _usage(f"--interval-fast must be >= 1, got {fast}")
    if slow is not None and fast is not None and slow < fast:
        raise _usage(f"--interval-slow {slow} must be >= --interval-fast {fast}")
    if slow is not None and fast is None and slow < cfg.watch.interval_fast:
        raise _usage(
            f"--interval-slow {slow} must be >= interval_fast "
            f"{cfg.watch.interval_fast}"
        )
    if settle is not None and settle < 0:
        raise _usage(f"--settle must be >= 0, got {settle}")


def _usage(message: str) -> UserFacingError:
    return UserFacingError(
        kind="usage_error",
        message=message,
        why=message,
        fixes=["goldmine watch --help", "goldmine help watch"],
        doctor=False,
        exit_code=2,
    )


def cmd_watch(args) -> int:
    """`goldmine watch` — resolve scope, preflight the backend, run the
    daemon. Renders its own start-time refusals (R-F2)."""
    from .command_support import EXIT_FINGERPRINT, EXIT_PREREQ, _db_path, _g

    cfg = _load_cfg(args)
    json_mode = bool(_g(args, "json", False))
    try:
        _clamp_args(args, cfg)
    except UserFacingError as exc:
        _render_refusal(exc.kind, exc.message, exc.why, exc.fixes, json_mode)
        return exc.exit_code
    lexical_only = bool(_g(args, "lexical_only", False))
    backend_name = _g(args, "backend")

    resolved, notes, missing = resolve_watch_roots(cfg, list(_g(args, "roots", []) or []))
    if missing and not resolved:
        _render_refusal(
            "watch_no_roots",
            "watch refused: no folders to watch",
            "none of the given roots exist: " + "; ".join(missing),
            ["goldmine watch --roots <existing path>", "goldmine init"],
            json_mode,
        )
        return EXIT_PREREQ
    if not resolved:
        _render_refusal(
            "watch_no_roots",
            "watch refused: no folders to watch",
            "goldmine only follows folders you explicitly added — [init] "
            "roots is empty",
            ["goldmine init", "goldmine watch --roots <path>"],
            json_mode,
        )
        return EXIT_PREREQ
    if missing:
        notes.append(f"{len(missing)} root(s) missing — skipped: " + "; ".join(missing))

    # F2 start-time refusal: watch renders the backend-unavailable shape
    # itself (construction is lazy — no spawn here).
    if not lexical_only:
        try:
            backend = create_backend(cfg, backend_name)
            backend.close()
        except BackendUnavailable as exc:
            why = str(exc)
            if getattr(exc, "fix_hint", None):
                why += f" ({exc.fix_hint})"
            _render_refusal(
                "backend_unavailable",
                "watch refused: no embedding backend available",
                why,
                ["goldmine model check", "goldmine watch --lexical-only"],
                json_mode,
            )
            return EXIT_PREREQ

    daemon = WatchDaemon(
        cfg,
        _db_path(args, cfg),
        roots=resolved,
        startup_notes=notes,
        code=bool(_g(args, "code", False)),
        exclude=list(_g(args, "exclude", []) or []),
        include_datasets=bool(_g(args, "include_datasets", False)),
        lexical_only=lexical_only,
        backend_name=backend_name,
        interval_fast=_g(args, "interval_fast"),
        interval_slow=_g(args, "interval_slow"),
        settle_s=_g(args, "settle"),
        linger_s=_g(args, "linger"),
        forever=bool(_g(args, "forever", False)),
        json_mode=json_mode,
    )
    code = daemon.run()
    if daemon._fatal is not None:
        kind, why = daemon._fatal
        _render_refusal(
            kind,
            "watch stopped: the index was built with a different embedding "
            "fingerprint",
            why,
            ["goldmine reset --reindex", "goldmine search <query> --lexical"],
            json_mode,
        )
        return EXIT_FINGERPRINT
    return code


def cmd_watch_stop(args) -> int:
    """`goldmine watch stop` — the `cmd_serve_stop` mirror (D4): read the
    heartbeat -> pid liveness -> verified SIGTERM (start-time receipt match,
    never a blind kill) -> wait <= 10 s -> SIGKILL -> verify cleanup."""
    cfg = _load_cfg(args)
    data = read_heartbeat(cfg)
    pid = (data or {}).get("pid")
    pid = pid if isinstance(pid, int) else None
    if pid is None or not pid_alive(pid):
        print("watch: not running")
        return 0  # honest not-running is an answer, not an error
    if proc_matches_receipt(pid, data) is not True:
        print(
            "watch: could not verify the pid against the heartbeat receipt "
            "— not killed (a reused pid is never signalled blind)",
            file=sys.stderr,
        )
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"watch: SIGTERM failed: {exc}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    if pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        heartbeat_path_for(cfg).unlink()
    except OSError:
        pass
    print("watch: stopped")
    return 0


def cmd_watch_dispatch(args) -> int:
    """Route the `watch` group (start / stop) — imported lazily so a watch
    problem can never break the other commands."""
    from .command_support import _g

    verb = _g(args, "watch_verb")
    if verb == "stop":
        return cmd_watch_stop(args)
    return cmd_watch(args)


# --------------------------------------------------------------- doctor row


def check_watch(cfg: Config):
    """The R9 doctor row (D4 + R-F5): ok running / warn absent, stale,
    slow-scan or probe-degraded — with the honest why-copy. NEVER fail:
    watch is optional by construction (the serve row's contract)."""
    from .doctor import STATUS_OK, STATUS_WARN, CheckResult

    data = read_heartbeat(cfg)
    if data is None:
        return CheckResult(
            "watch", STATUS_WARN,
            "not running (optional — the index updates when you run "
            "goldmine ingest)",
            "start: goldmine watch   ·   stop: goldmine watch stop",
        )
    pid = data.get("pid")
    if not isinstance(pid, int) or not pid_alive(pid):
        return CheckResult(
            "watch", STATUS_WARN,
            f"stale heartbeat (dead pid {pid}) — self-heals on the next "
            "`goldmine watch` start, which also receipt-sweeps any orphaned "
            "llama-server child",
        )
    now = time.time()
    last = data.get("last_pass") or {}
    ago = _fmt_ago(now - last["at"]) if last.get("at") else "never"
    holds = data.get("holds") or {}
    state = str(data.get("state") or "idle")
    base = (
        f"running · pid {pid} · state {state} · last pass {ago} · "
        f"interval {data.get('interval_s')}s · "
        f"pressure {holds.get('pressure')} · waiting {holds.get('waiting', 0)}"
    )
    notes = list(data.get("notes") or [])
    scan_s = data.get("last_scan_s")
    slow = isinstance(scan_s, (int, float)) and scan_s > SCAN_COST_WARN_S
    if slow:
        detail = base + (
            f" · one scan takes {scan_s:.0f}s — consider narrower roots"
        )
        if notes:
            detail += " · " + "; ".join(notes)
        return CheckResult("watch", STATUS_WARN, detail)
    if state.startswith("held") or state == "deferred-memory":
        detail = (
            base + f" · held ({_hold_label(state, holds)}) — working as designed"
        )
    else:
        detail = base
    if notes:
        warn_notes = [
            n for n in notes
            if "unparseable" in n or "TCC" in n or "failed" in n
        ]
        if warn_notes:
            return CheckResult(
                "watch", STATUS_WARN, detail + " · " + "; ".join(warn_notes)
            )
    return CheckResult("watch", STATUS_OK, detail)
