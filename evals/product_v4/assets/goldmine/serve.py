"""goldmine serve — the warm-search daemon (R8, D3/D5/D6 + review F1-F8).

One daemon, no index state: it owns exactly one LlamaServerBackend (lazy —
llama-server spawns on the first embed) and serves embed/count_tokens RPCs to
search clients over a line-JSON UDS. It NEVER opens SQLite; vectors stay in
client memory; search.py stays untouched.

Lifecycle (D3 as amended by §12): flock single-instance -> stale-state sweep
(verify-before-kill orphan receipts, F3) -> FRESH model sha256 (F2) -> bind the
0600 socket in a 0700 per-uid dir -> discovery file -> accept loop with a 1 s
tick (linger, guard) -> SIGTERM/SIGINT/shutdown op/linger expiry drain (<=10 s)
-> close backend (kills our child) -> unlink socket, delete serve.json, exit 0.
SIGKILL at any point leaves stale files + an orphaned child, all handled by the
next `serve start` (crash-only statelessness).

Memory guard (D3 + F4/F5): the kernel's kern.memorystatus_vm_pressure_level
(cached 30 s samples). Two warn-or-worse readings >= confirm window apart ->
UNLOAD: the backend is closed immediately (in-flight embeds fail — clients
retry-once then cold-fall back, F5), cooldown starts, and the model "steps
aside and stays aside until the system recovers". Two critical readings ->
graceful exit 0. A single warn reading never acts (no restart flapping).

This module imports goldmine.backends.serve (the wire codec/client); the
import is one-directional (daemon -> client module), no cycle. The cmd_*
handlers read config via .config.load directly — cli.py routes to them lazily.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import __version__
from .backends.base import PROMPT_SCHEME, BackendStartupError, EmbeddingError
from .backends.llama import VEC_DIM, LlamaServerBackend, resolve_model_path
from .backends.serve import (
    EMBED_MAX_TEXT_BYTES,
    EMBED_MAX_TEXTS,
    MAX_LINE,
    PROTO,
    SERVER_IDLE_TIMEOUT_S,
    SUN_PATH_MAX,
    LineReader,
    LineTooLong,
    ServeRequestError,
    one_shot_request,
    pid_alive,
    read_discovery,
    socket_path_for,
    discovery_path_for,
)
from .config import Config, resolve_home

logger = logging.getLogger("goldmine.serve")

DISCOVERY_FILE = "serve.json"
LOCK_FILE = "serve.lock"

PRESSURE_NAMES = {1: "normal", 2: "warn", 3: "critical"}

#: The honesty copy (03 §5.4 B15c): measured RSS class of the resident
#: embedding model on the dev mini; `serve status` and doctor say "~484 MB
#: class", never a fake exact number.
MODEL_RSS_CLASS_MB = 484


class ServeOpError(Exception):
    """An op failed; rendered as the wire error object (D2 vocabulary)."""

    def __init__(self, kind: str, message: str, tail: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.tail = tail


class ServeStartupRefusal(Exception):
    """The daemon refuses to start (socket path too long, dir mode wrong)."""


# ------------------------------------------------------------- ps helpers (F3)


def _ps_field(pid: int, field: str) -> str | None:
    """`ps -p PID -o <field>=` with a 5 s timeout; None when ps fails or the
    pid is gone. Two separate calls (lstart contains spaces — one combined
    call would be ambiguous to split)."""
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", f"{field}="],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", "replace").strip()
    return text or None


def proc_matches_receipt(pid: int, receipt: dict) -> bool | None:
    """F3 verify-before-kill: True when the process AT `pid` is verifiably the
    one the receipt recorded (start-time equality defeats pid reuse; procname
    is the fallback when the start-time is unavailable on either side), False
    when verifiably NOT, None when unverifiable (-> never kill)."""
    if not isinstance(pid, int) or pid <= 1:
        return None
    have_start = _ps_field(pid, "lstart")
    want_start = receipt.get("start_time")
    if have_start and want_start:
        return str(have_start) == str(want_start)
    have_comm = _ps_field(pid, "comm")
    want_comm = receipt.get("comm")
    if have_comm and want_comm:
        return Path(have_comm).name == Path(want_comm).name
    return None


def verify_and_kill(receipt: dict) -> str:
    """One F3 sweep step: kill a RECEIPTED child only after verification,
    SIGTERM -> 5 s -> SIGKILL by exact pid (never name-matching). Returns a
    short action string for the honest sweep report."""
    pid = receipt.get("pid") if isinstance(receipt, dict) else None
    if not isinstance(pid, int) or pid <= 1:
        return "skip (no usable pid)"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "already gone"
    except PermissionError:
        return "skip (owned by another user)"
    if proc_matches_receipt(pid, receipt) is not True:
        return "skip (could not verify the pid against the receipt — not killed)"
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return f"skip (SIGTERM failed: {exc})"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return "killed (SIGTERM)"
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return "gone (before SIGKILL)"
    return "killed (SIGKILL after 5s)"


# --------------------------------------------------------------- memory guard


def default_pressure_sampler() -> int:
    """The kernel's own memory-pressure level (D3 as amended; §12.3): 1 normal
    / 2 warn / 3 critical from kern.memorystatus_vm_pressure_level. Deliberately
    NOT the `memory_pressure -Q` free-percentage (B3's measured trap). Any read
    failure -> 1: the guard degrades to inert, honestly.

    GOLDMINE_SERVE_PRESSURE_LEVEL pins the reading (1|2|3) — a determinism hook
    for the guard tests and demos (on a 16 GB box level 2 is ORDINARY during a
    build, F1; tests must not depend on the machine's mood)."""
    forced = os.environ.get("GOLDMINE_SERVE_PRESSURE_LEVEL", "")
    if forced in ("1", "2", "3"):
        return int(forced)
    import shutil

    sysctl = shutil.which("sysctl") or "/usr/sbin/sysctl"
    try:
        proc = subprocess.run(
            [sysctl, "-n", "kern.memorystatus_vm_pressure_level"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=5.0,
        )
        if proc.returncode == 0:
            level = int(proc.stdout.split()[-1])
            return level if level in (1, 2, 3) else 1
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        pass
    return 1


class PressureGuard:
    """Warn x2 -> unload / critical x2 -> exit, with hysteresis and cooldown
    (D3). The sampler is injectable (offline tests script the level sequence);
    every threshold is constructor-injectable so tests run in real seconds."""

    def __init__(self, *, sampler=default_pressure_sampler,
                 sample_cache_s: float = 30.0, confirm_s: float = 30.0,
                 cooldown_s: float = 120.0, stream=None,
                 resident: Callable[[], bool] | None = None):
        self.sampler = sampler
        self.sample_cache_s = float(sample_cache_s)
        self.confirm_s = float(confirm_s)
        self.cooldown_s = float(cooldown_s)
        self.stream = stream if stream is not None else sys.stderr
        # F3: injectable residency — None reads as "resident" (standalone use);
        # the daemon wires `lambda: self.backend is not None` so a sustained
        # pressure episode with NOTHING resident starts the cooldown episode
        # but does not count (or print) a model unload that never happened.
        self.resident = resident
        self.level = 1
        self.sampled_at: float | None = None
        self.first_warn: float | None = None
        self.first_crit: float | None = None
        self.cooldown_until: float | None = None
        self.unloads = 0
        self.last_unload: float | None = None  # wall clock, for the F4 copy
        self.on_unload = None       # set by the daemon
        self.on_critical_exit = None

    @property
    def cooling(self) -> bool:
        """In cooldown = the model must stay aside. True while the cooldown
        window runs; after the window, still True while the level remains
        warn-or-worse (residency resumes ONLY on a normal reading, D3)."""
        if self.cooldown_until is None:
            return False
        if time.monotonic() < self.cooldown_until:
            return True
        return self.level >= 2

    def sample(self, now: float | None = None) -> int:
        """Cached sample (D3: a cold sysctl costs ~10-20 ms — never per op)."""
        now = time.monotonic() if now is None else now
        if self.sampled_at is None or now - self.sampled_at >= self.sample_cache_s:
            try:
                level = int(self.sampler())
            except Exception:
                level = 1
            self.level = level if level in (1, 2, 3) else 1
            self.sampled_at = now
        return self.level

    def check(self, now: float | None = None) -> int:
        """One hysteresis tick. Single warn reading -> NO action (no flapping);
        two warn-or-worse >= confirm_s apart -> ONE pressure episode: cooldown
        starts, and when a model IS resident it is unloaded (never re-fired
        while cooling — one unload per sustained-pressure episode, F3: with
        nothing resident the episode still starts but no unload is counted);
        two critical >= confirm_s apart -> on_critical_exit (graceful exit 0)."""
        now = time.monotonic() if now is None else now
        level = self.sample(now)
        if level == 1:
            self.first_warn = None
            self.first_crit = None
            # resume residency only after a NORMAL reading past cooldown (D3)
            if self.cooldown_until is not None and now >= self.cooldown_until:
                self.cooldown_until = None
            return level
        if level >= 2:
            if self.first_warn is None:
                self.first_warn = now
            elif now - self.first_warn >= self.confirm_s and not self.cooling:
                self.first_warn = None
                self.cooldown_until = now + self.cooldown_s
                is_resident = self.resident() if self.resident is not None else True
                if is_resident:
                    self.unloads += 1
                    self.last_unload = time.time()
                    print(
                        f"goldmine serve: memory pressure level {level} sustained "
                        f"— unloading the model (cooldown {self.cooldown_s:g}s)",
                        file=self.stream, flush=True,
                    )
                    if self.on_unload is not None:
                        self.on_unload(level)
                else:
                    # F3: the episode is real, the unload is not — nothing is
                    # resident to step aside; keep the hold, count nothing
                    print(
                        f"goldmine serve: memory pressure level {level} sustained "
                        f"— nothing resident to unload, holding aside "
                        f"(cooldown {self.cooldown_s:g}s)",
                        file=self.stream, flush=True,
                    )
        if level >= 3:
            if self.first_crit is None:
                self.first_crit = now
            elif now - self.first_crit >= self.confirm_s:
                self.first_crit = None
                if self.on_critical_exit is not None:
                    self.on_critical_exit()
        return level


# ------------------------------------------------------------------- the daemon


class ServeDaemon:
    """Foreground daemon: flock -> sweep -> fresh sha -> bind -> discovery ->
    accept loop. `serve_forever()` returns an int exit code (0 normally)."""

    def __init__(self, cfg: Config, *, linger_s: float | None = None,
                 stream=None, sock_path: str | Path | None = None,
                 sampler=None, guard_sample_cache_s: float = 30.0,
                 guard_confirm_s: float = 30.0, guard_cooldown_s: float = 120.0,
                 idle_tick_s: float = 1.0, drain_s: float = 10.0):
        self.cfg = cfg
        self.home = resolve_home(cfg)
        self.lock_path = self.home / LOCK_FILE
        self.discovery_path = discovery_path_for(cfg)
        self.sock_path = Path(sock_path) if sock_path else socket_path_for(cfg)
        self.linger_s = float(cfg.serve.linger if linger_s is None else linger_s)
        self.stream = stream if stream is not None else sys.stderr
        self.idle_tick_s = float(idle_tick_s)
        self.drain_s = float(drain_s)
        self.guard = PressureGuard(
            sampler=sampler or default_pressure_sampler,
            sample_cache_s=guard_sample_cache_s,
            confirm_s=guard_confirm_s,
            cooldown_s=guard_cooldown_s,
            stream=self.stream,
        )
        self.guard.on_unload = self._guard_unload
        # F3: the guard must know whether there is a model to unload, so a
        # sustained-pressure episode with nothing resident keeps the cooldown
        # hold without counting (or printing) a phantom unload
        self.guard.resident = lambda: self.backend is not None
        self.guard.on_critical_exit = (
            lambda: self.request_stop("memory pressure (critical level sustained)")
        )
        self.backend: LlamaServerBackend | None = None
        self.child_receipt: dict | None = None
        self.model_path: str = ""
        self.model_sha256: str | None = None
        self._fp_fields: dict = {}
        self.lock_fd: int | None = None
        self.listen_sock: socket.socket | None = None
        self.clients: dict[int, dict] = {}  # conn fileno -> registry entry (F8)
        self.started_at = time.time()
        self.started_mono = time.monotonic()
        self._self_start: str | None = None
        self._last_model_touch = time.monotonic()
        self._stopping = threading.Event()
        self._stop_reason: str | None = None
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._ready = threading.Event()

    # ------------------------------------------------------------- lifecycle

    @property
    def ready(self) -> threading.Event:
        """Set once the socket is bound and serve.json written (tests wait on it)."""
        return self._ready

    def serve_forever(self) -> int:
        if not self._acquire_lock():
            return 0
        try:
            self._sweep_stale_state()
            self._compute_model_identity()
            self._bind()
            self._write_discovery()
        except ServeStartupRefusal as exc:
            print(f"goldmine serve: {exc}", file=self.stream, flush=True)
            self._cleanup_files()
            self._release_lock()
            return 1
        self._install_signals()
        print(
            f"goldmine serve: pid {os.getpid()} · {self.sock_path} · "
            f"linger {self.linger_s:g}s · guard armed (warn→unload / critical→exit)",
            file=self.stream, flush=True,
        )
        self._ready.set()
        try:
            while not self._stopping.is_set():
                try:
                    conn, _addr = self.listen_sock.accept()
                except socket.timeout:
                    self._tick()
                    continue
                except OSError:
                    break  # listener closed (shutdown requested)
                conn.settimeout(SERVER_IDLE_TIMEOUT_S)
                self._track_conn(conn, add=True)
                threading.Thread(
                    target=self._serve_conn, args=(conn,), daemon=True,
                    name="goldmine-serve-conn",
                ).start()
        finally:
            logger.debug(
                "serve: accept loop exited (reason=%s)", self._stop_reason
            )
            self._shutdown()
        return 0

    def request_stop(self, reason: str) -> None:
        if self._stop_reason is None:
            self._stop_reason = reason
        self._stopping.set()
        sock = self.listen_sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _acquire_lock(self) -> bool:
        self.lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            self.lock_fd = None
            data = self._read_discovery_file()
            pid = data.get("pid") if data else None
            if isinstance(pid, int) and pid_alive(pid):
                print(f"already serving (pid {pid})", file=self.stream, flush=True)
                version = (data or {}).get("goldmine_version")
                if version and version != __version__:  # §11.1, answered: print it
                    print(
                        f"note: that daemon runs goldmine {version}, this is "
                        f"{__version__} — its clients will cold-fall back until "
                        "it is restarted",
                        file=self.stream, flush=True,
                    )
            else:
                print("already serving (lock held)", file=self.stream, flush=True)
            return False
        return True

    def _sweep_stale_state(self) -> None:
        """We hold the flock, so any previous daemon is gone: F3 receipt sweep
        (verify-before-kill), then stale socket + discovery removal (D3: swept
        by the next successful start, never by clients)."""
        data = self._read_discovery_file()
        if data:
            for child in data.get("children") or []:
                action = verify_and_kill(child)
                print(
                    f"goldmine serve: orphan sweep: child pid {child.get('pid')}: {action}",
                    file=self.stream, flush=True,
                )
        for path in (self.sock_path, self.discovery_path):
            try:
                path.unlink()
            except OSError:
                pass

    def _compute_model_identity(self) -> None:
        """F2: the daemon's model sha256 is computed FRESH from the file bytes
        at start (never a client-visible sidecar value) and is what info/,
        serve.json and `serve status` report. The ~0.5 s read lands in daemon
        startup, invisible to searches (§12.2)."""
        from .util import sha256_file

        model = resolve_model_path(self.cfg.llama)
        self.model_path = str(model)
        self.model_sha256 = sha256_file(model) if model.is_file() else "missing"
        self._fp_fields = {
            "model_id": model.stem,
            "model_sha256": self.model_sha256,
            "dim": VEC_DIM,
            "prompts": PROMPT_SCHEME,
            "pooling": "server:mean",  # spawned with --pooling mean (llama.py)
            "normalization": "l2_post",  # the backend re-normalizes defensively
        }

    def _bind(self) -> None:
        if len(os.fsencode(str(self.sock_path))) > SUN_PATH_MAX - 1:
            raise ServeStartupRefusal(
                f"socket path too long for a UDS (> {SUN_PATH_MAX - 1} bytes): "
                f"{self.sock_path} — set GOLDMINE_SERVE to a shorter path"
            )
        try:
            self.sock_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.sock_path.parent, 0o700)  # D5: created AND verified 0700
        except OSError as exc:
            raise ServeStartupRefusal(
                f"cannot prepare socket dir {self.sock_path.parent}: {exc}"
            ) from exc
        mode = os.stat(self.sock_path.parent).st_mode & 0o777
        if mode != 0o700:
            raise ServeStartupRefusal(
                f"socket dir {self.sock_path.parent} has mode {oct(mode)} — "
                "refusing (want 0700)"
            )
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self.sock_path))
        except OSError as exc:
            raise ServeStartupRefusal(f"cannot bind {self.sock_path}: {exc}") from exc
        os.chmod(self.sock_path, 0o600)  # D5
        sock.listen(16)
        sock.settimeout(self.idle_tick_s)
        self.listen_sock = sock

    def _discovery_payload(self) -> dict:
        return {
            "proto": PROTO,
            "goldmine_version": __version__,
            "pid": os.getpid(),
            "socket": str(self.sock_path),
            "started_at": self.started_at,
            "start_time": self._self_start_time(),
            "model_path": self.model_path,
            "model_sha256": self.model_sha256,
            "children": [self.child_receipt] if self.child_receipt else [],
        }

    def _self_start_time(self) -> str | None:
        if self._self_start is None:
            self._self_start = _ps_field(os.getpid(), "lstart")  # for stop's verify
        return self._self_start

    def _write_discovery(self) -> None:
        tmp = self.discovery_path.with_name(self.discovery_path.name + ".tmp")
        tmp.write_text(json.dumps(self._discovery_payload(), indent=1) + "\n")
        os.replace(tmp, self.discovery_path)  # atomic (D3)

    def _record_child(self, proc: subprocess.Popen, port: int) -> None:
        """F3: the spawn receipt is written IMMEDIATELY after Popen — before
        the health wait — so even a crash during startup leaves a sweepable
        receipt. start-time + procname make the later sweep verify-before-kill."""
        receipt = {
            "pid": proc.pid,
            "kind": "llama-server",
            "port": int(port),
            "start_time": _ps_field(proc.pid, "lstart"),
            "comm": _ps_field(proc.pid, "comm"),
            "created_at": time.time(),
        }
        self.child_receipt = receipt
        try:
            self._write_discovery()
        except OSError:
            pass

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
                pass  # not the main thread (in-process tests); subprocess runs get real signals

    def _tick(self) -> None:
        self.guard.check()
        if self._stopping.is_set():
            return
        idle = time.monotonic() - self._last_model_touch
        with self._inflight_lock:
            inflight = self._inflight
        if inflight == 0 and idle > self.linger_s:
            # F7: only model-touching ops refresh the timer, so a ping-only
            # client cannot keep an idle daemon alive
            self.request_stop(
                f"linger expired ({int(idle)}s since the last model op)"
            )

    def _shutdown(self) -> None:
        t0 = time.monotonic()
        # SIGTERM discipline: finish in-flight requests, bounded 10 s (D3).
        # (The guard's unload does NOT wait — that asymmetry is review F5.)
        deadline = time.monotonic() + self.drain_s
        while time.monotonic() < deadline:
            with self._inflight_lock:
                if self._inflight <= 0:
                    break
            time.sleep(0.05)
        logger.debug("serve: drain done in %.2fs", time.monotonic() - t0)
        backend, self.backend = self.backend, None
        if backend is not None:
            try:
                backend.close()  # kills OUR child: SIGTERM -> 5 s -> SIGKILL
            except Exception:  # pragma: no cover - shutdown must not raise
                pass
        self.child_receipt = None
        self._cleanup_files()
        if self.listen_sock is not None:
            try:
                self.listen_sock.close()
            except OSError:
                pass
            self.listen_sock = None
        self._release_lock()
        logger.debug("serve: shutdown completed in %.2fs", time.monotonic() - t0)
        print(
            f"goldmine serve: stopped ({self._stop_reason or 'shutdown'})",
            file=self.stream, flush=True,
        )

    def _cleanup_files(self) -> None:
        for path in (self.sock_path, self.discovery_path):
            try:
                path.unlink()
            except OSError:
                pass

    def _release_lock(self) -> None:
        if self.lock_fd is not None:
            try:
                os.close(self.lock_fd)  # releases the flock
            except OSError:
                pass
            self.lock_fd = None

    def _read_discovery_file(self) -> dict | None:
        try:
            data = json.loads(self.discovery_path.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _track_conn(self, conn: socket.socket, *, add: bool) -> None:
        try:
            key = conn.fileno()
        except OSError:
            return
        if add:
            self.clients[key] = {
                "pid": None,
                "connected_s": time.monotonic(),
                "last_req_s": time.monotonic(),
            }
        else:
            self.clients.pop(key, None)

    # --------------------------------------------------------- connection loop

    def _serve_conn(self, conn: socket.socket) -> None:
        entry = {
            "pid": None,
            "connected_s": time.monotonic(),
            "last_req_s": time.monotonic(),
        }
        try:
            key = conn.fileno()
            self.clients[key] = entry
        except OSError:
            key = None
        try:
            reader = LineReader(conn, MAX_LINE)
            while not self._stopping.is_set():
                try:
                    line = reader.readline()
                except LineTooLong:
                    break  # oversized line -> close that connection (D2)
                except (socket.timeout, OSError):
                    break  # 30 s recv idler / peer vanished (D2)
                if line is None:
                    break
                try:
                    request = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # malformed JSON -> bad_request, connection STAYS open (D2);
                    # a peer that already vanished just gets the quiet close
                    self._safe_error(conn, "malformed JSON line")
                    continue
                if not isinstance(request, dict):
                    self._safe_error(conn, "request is not a JSON object")
                    continue
                with self._inflight_lock:
                    self._inflight += 1
                try:
                    self._handle_line(conn, request)
                except OSError:
                    break  # peer vanished mid-response: close quietly
                except Exception as exc:  # a conn thread never dies noisily
                    logger.debug("serve: connection handler error: %s", exc)
                    break
                finally:
                    with self._inflight_lock:
                        self._inflight -= 1
        finally:
            if key is not None:
                self.clients.pop(key, None)
            try:
                conn.close()
            except OSError:
                pass

    def _respond(self, conn: socket.socket, ok: bool, op, req_id, payload=None,
                 error: dict | None = None) -> None:
        response: dict = {"ok": ok, "op": op}
        if req_id is not None:
            response["id"] = req_id
        if ok:
            response.update(payload or {})
        else:
            response["error"] = error or {"kind": "internal", "message": "error", "tail": ""}
        from .backends.serve import send_line

        send_line(conn, response)  # OSError (peer gone) -> conn thread exits

    def _safe_error(self, conn: socket.socket, message: str) -> None:
        """The pre-dispatch bad_request: never let a vanished peer turn the
        protocol error path into a thread exception."""
        try:
            self._respond(conn, False, None, None, error={
                "kind": "bad_request", "message": message, "tail": "",
            })
        except OSError:
            pass

    def _handle_line(self, conn: socket.socket, request: dict) -> None:
        op = request.get("op")
        req_id = request.get("id")
        # F8: every request may carry the client pid; the registry keeps the latest
        entry = self.clients.get(conn.fileno())
        if entry is not None:
            pid = request.get("pid")
            if isinstance(pid, int):
                entry["pid"] = pid
            entry["last_req_s"] = time.monotonic()
        try:
            if op == "ping":
                payload = {
                    "proto": PROTO,
                    "goldmine_version": __version__,
                    "pid": os.getpid(),
                    "uptime_s": round(time.monotonic() - self.started_mono, 3),
                }
            elif op == "info":
                payload = self._op_info()
            elif op == "embed":
                payload = self._op_embed(request)
            elif op == "count_tokens":
                payload = self._op_count_tokens(request)
            elif op == "status":
                payload = self._op_status()
            elif op == "shutdown":
                self._respond(conn, True, op, req_id, {"stopping": True})
                self.request_stop("shutdown op")
                return
            else:
                raise ServeOpError("bad_request", f"unknown op {op!r}")
        except ServeOpError as exc:
            self._respond(conn, False, op, req_id, error={
                "kind": exc.kind, "message": exc.message,
                "tail": (exc.tail or "")[:4000],
            })
            return
        except Exception as exc:  # never let one op kill the thread silently
            logger.exception("serve: op %r failed", op)
            self._respond(conn, False, op, req_id, error={
                "kind": "internal",
                "message": f"{type(exc).__name__}: {exc}",
                "tail": "",
            })
            return
        self._respond(conn, True, op, req_id, payload)

    # -------------------------------------------------------------------- ops

    @property
    def _resident(self) -> bool:
        backend = self.backend
        return bool(
            backend is not None
            and backend._started
            and backend._proc is not None
            and backend._proc.poll() is None
        )

    def _touch(self) -> None:
        """F7: the linger timer refreshes on model-touching ops ONLY."""
        self._last_model_touch = time.monotonic()

    def _op_info(self) -> dict:
        return {
            "fingerprint_fields": dict(self._fp_fields),
            "backend_id": "llama",
            "dim": VEC_DIM,
            "capability": {"text": True, "image": False, "audio": False},
            "max_input_tokens": int(self.cfg.llama.ctx),
            "model_path": self.model_path,
            "resident": self._resident,
            "child_pid": (self.child_receipt or {}).get("pid"),
            "goldmine_version": __version__,
        }

    def _not_resident_message(self, level: int, why: str) -> str:
        last = self.guard.last_unload
        return (
            f"model not resident ({why}) — under memory pressure the model "
            f"steps aside and stays aside until the system recovers "
            f"(last unload: {_fmt_wall(last) if last else 'never'}, "
            f"current level: {level})"
        )

    def _ensure_resident(self) -> None:
        """Guard-aware lazy spawn (D3 + F5): never spawn under warn-or-worse
        pressure (a one-shot's own child dies at command end — strictly better
        under pressure than residency); during cooldown answer model_not_resident
        so the client cold-falls back."""
        level = self.guard.sample()
        if self._resident:
            return
        now = time.monotonic()
        if (
            self.guard.cooldown_until is not None
            and now >= self.guard.cooldown_until
            and level == 1
        ):
            # a NORMAL reading past the cooldown window: residency may resume
            # (don't wait for the next 1 s tick to reach the same conclusion)
            self.guard.cooldown_until = None
        if self.guard.cooling:
            # F4: name the hold that is ACTIVELY refusing. "Cooling down after
            # an unload" is only true inside the window AND when a real unload
            # happened (F3: a nothing-resident episode opens the window but
            # unloads nothing); every other cooling shape is the pressure
            # level itself holding the model aside.
            in_unload_cooldown = (
                self.guard.cooldown_until is not None
                and now < self.guard.cooldown_until
                and self.guard.last_unload is not None
            )
            why = (
                "cooling down after a memory-pressure unload" if in_unload_cooldown
                else "held aside by sustained memory pressure"
            )
            raise ServeOpError(
                "model_not_resident",
                self._not_resident_message(level, why),
            )
        if level >= 2:
            raise ServeOpError(
                "model_not_resident",
                self._not_resident_message(level, "not spawning under memory pressure"),
            )
        if self.backend is None:
            backend = LlamaServerBackend(self.cfg.llama)
            self._wrap_spawn_for_receipt(backend)
            self.backend = backend
        try:
            self.backend.startup()  # idempotent lazy spawn (D6)
        except BackendStartupError as exc:
            self.backend = None
            raise ServeOpError(
                "embed_failed",
                f"llama-server failed to start: {str(exc).splitlines()[0][:400]}",
                (getattr(exc, "tail", "") or ""),
            ) from exc

    def _wrap_spawn_for_receipt(self, backend: LlamaServerBackend) -> None:
        """Inject the F3 receipt hook between llama.py's Popen and its health
        wait — llama.py itself is untouched."""
        original_spawn = backend._spawn

        def spawn_with_receipt(port: int):
            proc = original_spawn(port)
            self._record_child(proc, port)
            return proc

        backend._spawn = spawn_with_receipt  # type: ignore[method-assign]

    def _validate_texts(self, request: dict) -> list[str]:
        """F6 caps, in order: shape -> COUNT (64) -> per-text bytes (256 KiB).
        Count first means the count error wins when both are violated."""
        texts = request.get("texts")
        if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
            raise ServeOpError("bad_request", "embed needs {'texts': [str, ...]}")
        if len(texts) > EMBED_MAX_TEXTS:
            raise ServeOpError(
                "bad_request",
                f"embed: too many texts ({len(texts)} > {EMBED_MAX_TEXTS} cap)",
            )
        for text in texts:
            if len(text.encode("utf-8")) > EMBED_MAX_TEXT_BYTES:
                raise ServeOpError(
                    "bad_request",
                    f"embed: a text exceeds the {EMBED_MAX_TEXT_BYTES // 1024} KiB "
                    "per-text cap",
                )
        return texts

    def _op_embed(self, request: dict) -> dict:
        texts = self._validate_texts(request)
        self._ensure_resident()
        self._touch()
        try:
            vectors = self.backend.embed_texts(texts)
        except EmbeddingError as exc:
            raise ServeOpError(
                "embed_failed",
                str(exc).splitlines()[0][:400],
                (getattr(exc, "tail", "") or ""),
            ) from exc
        return {"vectors": vectors.tolist(), "dim": int(vectors.shape[1])}

    def _op_count_tokens(self, request: dict) -> dict:
        texts = request.get("texts")
        if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
            raise ServeOpError(
                "bad_request", "count_tokens needs {'texts': [str, ...]}"
            )
        self._ensure_resident()
        self._touch()
        counts = self.backend.count_tokens(texts)
        if counts is None:
            raise ServeOpError(
                "tokenize_failed", "resident backend could not count tokens"
            )
        return {"counts": [int(c) for c in counts]}

    def _op_status(self) -> dict:
        now = time.monotonic()
        guard = self.guard
        clients = sorted(self.clients.values(), key=lambda c: c["connected_s"])
        return {
            "pid": os.getpid(),
            "uptime_s": round(now - self.started_mono, 3),
            "goldmine_version": __version__,
            "proto": PROTO,
            "socket": str(self.sock_path),
            "model_path": self.model_path,
            "model_sha256": self.model_sha256,
            "resident": self._resident,
            "child_pid": (self.child_receipt or {}).get("pid"),
            "children": [self.child_receipt] if self.child_receipt else [],
            "clients": [
                {
                    "pid": c.get("pid"),
                    "connected_s": round(now - c["connected_s"], 1),
                    "last_req_s": round(now - c["last_req_s"], 1),
                }
                for c in clients
            ],
            "linger_remaining_s": round(
                max(0.0, self.linger_s - (now - self._last_model_touch)), 1
            ),
            "guard": {
                "level": guard.level,
                "sampled_age_s": (
                    round(now - guard.sampled_at, 1)
                    if guard.sampled_at is not None
                    else None
                ),
                "state": "cooldown" if guard.cooling else "normal",
                "unloads": guard.unloads,
                "last_unload_s": guard.last_unload,
                "cooldown_remaining_s": round(
                    max(0.0, (guard.cooldown_until or 0.0) - now), 1
                ),
            },
        }

    # ------------------------------------------------------------ guard action

    def _guard_unload(self, level: int) -> None:
        """F5: close the backend NOW — in-flight embeds fail mid-request and
        clients retry-once then cold-fall back. The daemon stays up; the model
        steps aside until the level returns to 1 past the cooldown."""
        backend, self.backend = self.backend, None
        self.child_receipt = None
        if backend is not None:
            try:
                backend.close()  # kills OUR child, even mid-request
            except Exception:  # pragma: no cover - unload must not raise
                pass
        try:
            self._write_discovery()
        except OSError:
            pass


# ------------------------------------------------------- rendering + commands


def _fmt_uptime(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def _fmt_wall(epoch: float | None) -> str:
    if not epoch:
        return "never"
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def _load_cfg(args):
    from .config import load

    config_flag = getattr(args, "config", None)
    return load(config_path=Path(config_flag) if config_flag else None)


def read_discovery_file(cfg: Config) -> dict | None:
    """Raw serve.json contents (dict) or None — for stop/doctor; read-only."""
    try:
        data = json.loads(discovery_path_for(cfg).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def cmd_serve(args) -> int:
    cfg = _load_cfg(args)
    daemon = ServeDaemon(cfg, linger_s=getattr(args, "linger", None))
    return daemon.serve_forever()


def _status_or_none(cfg: Config) -> dict | None:
    disc = read_discovery(cfg)
    if disc is None:
        return None
    if disc.pid is not None and not pid_alive(disc.pid):
        return None
    try:
        return one_shot_request(disc.socket, "status", timeout_s=5.0)
    except (OSError, ServeRequestError):
        return None


def _render_status_human(st: dict) -> str:
    guard = st.get("guard") or {}
    model_id = Path(str(st.get("model_path") or "?")).stem
    sha = str(st.get("model_sha256") or "?")
    sha_show = "missing" if sha == "missing" else f"{sha[:8]}…"
    lines = [
        f"serve: running · pid {st.get('pid')} · "
        f"up {_fmt_uptime(st.get('uptime_s') or 0)} · "
        f"goldmine {st.get('goldmine_version')}"
    ]
    if st.get("resident"):
        lines.append(
            f"  model: {model_id} (sha {sha_show}) · resident · "
            f"~{MODEL_RSS_CLASS_MB} MB class"
        )
    else:
        # F3 wording: unloads counts REAL unloads only — a pressure hold with
        # nothing resident is still a stepped-aside state, and the status says
        # which one it is instead of reading as plain laziness
        if (guard.get("unloads") or 0) >= 1:
            note = f"stepped aside (unload #{guard.get('unloads')})"
        elif guard.get("state") == "cooldown":
            note = "stepped aside under memory pressure (nothing was resident to unload)"
        else:
            note = "lazy — spawns on the first search"
        lines.append(f"  model: {model_id} (sha {sha_show}) · not resident · {note}")
    sampled_age = guard.get("sampled_age_s")
    age_note = (
        f", {sampled_age:g}s ago" if isinstance(sampled_age, (int, float)) else ""
    )
    lines.append(
        f"  clients: {len(st.get('clients') or [])} · "
        f"linger {_fmt_uptime(st.get('linger_remaining_s') or 0)} left · "
        f"pressure: {PRESSURE_NAMES.get(guard.get('level'), '?')} "
        f"(level {guard.get('level')}{age_note}) · unloads {guard.get('unloads', 0)}"
    )
    if not st.get("resident") and (guard.get("unloads") or 0) >= 1:
        # F4: the honest memory-yield copy, in words — only when the model was
        # actually UNLOADED (a lazy never-spawned daemon is normal, not a yield)
        lines.append(
            "  under memory pressure the model steps aside and stays aside until "
            f"the system recovers (last unload: {_fmt_wall(guard.get('last_unload_s'))}, "
            f"current level: {guard.get('level')})"
        )
    lines.append(
        f"  the model server ~{MODEL_RSS_CLASS_MB} MB; index pages live in "
        "reclaimable file cache"
    )
    return "\n".join(lines)


def cmd_serve_status(args) -> int:
    cfg = _load_cfg(args)
    json_mode = bool(getattr(args, "json", False))
    status = _status_or_none(cfg)
    if status is None:
        if json_mode:
            print(json.dumps(
                {"schema": "goldmine/1", "command": "serve-status", "running": False},
                indent=2, ensure_ascii=False,
            ))
        else:
            print("serve: not running")
            print(
                "start it: goldmine serve   (opt-in; searches work without it, "
                "just ~2s colder per query)"
            )
        return 0  # honest not-running is an answer, not an error (D6)
    if json_mode:
        print(json.dumps(
            {"schema": "goldmine/1", "command": "serve-status", **status},
            indent=2, ensure_ascii=False,
        ))
        return 0
    print(_render_status_human(status))
    return 0


def _daemon_receipt_verified(data: dict | None) -> bool:
    """F3 for the DAEMON pid in serve stop's SIGTERM fallback: the recorded
    start-time must still match. No start-time on either side -> not verified
    (report only, never a blind kill of a possibly-reused pid)."""
    if not isinstance(data, dict):
        return False
    pid = data.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return False
    return proc_matches_receipt(pid, data) is True


def cmd_serve_stop(args) -> int:
    cfg = _load_cfg(args)
    data = read_discovery_file(cfg)
    disc = read_discovery(cfg)
    pid = (data or {}).get("pid")
    pid = pid if isinstance(pid, int) else None

    def daemon_alive() -> bool:
        """A daemon is running iff its receipted pid lives OR its socket
        answers (covers the GOLDMINE_SERVE env case with no receipt file)."""
        if pid is not None and pid_alive(pid):
            return True
        if disc is not None:
            try:
                one_shot_request(disc.socket, "ping", timeout_s=2.0)
                return True
            except (OSError, ServeRequestError):
                return False
        return False

    if not daemon_alive():
        print("serve: not running")
        return 0  # honest not-running is an answer, not an error (D6)
    # 1) the polite path: the shutdown op (any same-uid process may call it — D5)
    polite = False
    if disc is not None:
        try:
            resp = one_shot_request(disc.socket, "shutdown", timeout_s=5.0)
            polite = bool(resp.get("stopping"))
        except (OSError, ServeRequestError):
            polite = False
    # 2) wait for exit; verified SIGTERM fallback when the op could not reach it
    sent_sigterm = False
    still = True
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if pid is None or not pid_alive(pid):
            still = False
            break
        if not polite and not sent_sigterm and _daemon_receipt_verified(data):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            sent_sigterm = True
        time.sleep(0.1)
    if still and pid is not None:
        still = pid_alive(pid)
    if still:
        print(
            f"serve: daemon pid {pid} did not exit — check `goldmine doctor` "
            "and `kill <pid>` manually",
            file=sys.stderr,
        )
        return 1
    # 3) verify discovery cleanup; stop MAY clean what a crashed daemon left
    if disc is not None:
        try:
            Path(disc.socket).unlink()
        except OSError:
            pass
    try:
        discovery_path_for(cfg).unlink()
    except OSError:
        pass
    print("serve: stopped (shutdown op)" if polite else "serve: stopped")
    return 0


# --------------------------------------------------------------- doctor row


def check_serve_daemon(cfg: Config):
    """The R8 doctor row (D6 + F4): ok running / warn absent, stale, skewed, or
    memory-yielded — with the honest why-copy. NEVER fail: the daemon is
    optional by construction."""
    from .doctor import STATUS_OK, STATUS_WARN, CheckResult

    disc = read_discovery(cfg)
    if disc is None:
        return CheckResult(
            "serve daemon", STATUS_WARN,
            "not running (optional — searches fall back to a cold ~2s path)",
            "start: goldmine serve   ·   stop: goldmine serve stop",
        )
    if disc.pid is not None and not pid_alive(disc.pid):
        return CheckResult(
            "serve daemon", STATUS_WARN,
            f"stale discovery file (dead pid {disc.pid}) — self-heals on the "
            "next `goldmine serve` start, which also receipt-sweeps any orphaned "
            "llama-server child",
        )
    try:
        st = one_shot_request(disc.socket, "status", timeout_s=3.0)
    except (OSError, ServeRequestError) as exc:
        return CheckResult(
            "serve daemon", STATUS_WARN,
            f"daemon at {disc.socket} not answering ({exc}) — searches fall "
            "back to cold",
        )
    guard = st.get("guard") or {}
    base = (
        f"running · pid {st.get('pid')} · up {_fmt_uptime(st.get('uptime_s') or 0)} · "
        + (
            f"model resident (~{MODEL_RSS_CLASS_MB} MB class)"
            if st.get("resident")
            else "model not resident"
        )
        + f" · pressure {PRESSURE_NAMES.get(guard.get('level'), '?')} "
        f"(level {guard.get('level')}) · unloads {guard.get('unloads', 0)}"
    )
    version = st.get("goldmine_version")
    if version != __version__:
        return CheckResult(
            "serve daemon", STATUS_WARN,
            f"{base} · version skew (daemon {version} != client {__version__}) — "
            "clients cold-fall back until it is restarted",
            "goldmine serve stop && goldmine serve",
        )
    if not st.get("resident") and (guard.get("unloads") or 0) >= 1:
        # F4: memory-YIELDED (an actual unload happened) — warn with the words.
        # A lazy never-spawned daemon (unloads == 0) is healthy, not a yield.
        return CheckResult(
            "serve daemon", STATUS_WARN,
            base + " · under memory pressure the model steps aside and stays "
            f"aside until the system recovers (last unload: "
            f"{_fmt_wall(guard.get('last_unload_s'))}, current level: "
            f"{guard.get('level')})",
        )
    return CheckResult("serve daemon", STATUS_OK, base)
