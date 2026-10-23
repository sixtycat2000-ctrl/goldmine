"""Live, explicitly sampled counters for an anchored owned POSIX session.

No promise of lifetime-complete descendant discovery or kernel tree highwater
is made. Darwin NOTE_FORK without supported NOTE_TRACK leaves tree coverage
MISSING, even when useful descendants were observed. Default capture is inert.
"""
from __future__ import annotations

import errno
import json
import os
import secrets
import select
import subprocess
import sys
import time
from pathlib import Path

from .evidence import now, sha256, write_json
from .resource_policy import OUTPUTS, bridge_argv, check_run_location, digest, runtime_identity, validate_policy
from . import resource_tools


class Observer:
    def __init__(self, run_dir: Path, value: dict, inner: list[str], stdout, stderr):
        validate_policy(value)
        check_run_location(run_dir, value)
        if sys.platform != "darwin" or not hasattr(select, "kqueue"):
            raise OSError(errno.ENOTSUP, "resource observation currently requires Darwin ps/lsof/kqueue")
        if not Path(inner[0]).is_absolute():
            raise OSError(errno.EINVAL, "observed inner executable must be absolute")
        self.run_dir, self.policy, self.inner = run_dir, value, inner
        self.root = Path(value["owned_root"])
        st = self.root.stat()
        self.root_identity = (st.st_dev, st.st_ino)
        self.stdout, self.stderr = stdout, stderr
        self.runtime = runtime_identity()
        self.started, self.started_at = time.monotonic(), now()
        self.deadline = self.started + value["wall_seconds"]
        self.queue, self.proc, self.handshake, self.ready_at = None, None, None, None
        self.wait_finished, self.cleanup_finished = None, None
        self.target_exec_seen, self.target_exec_at = False, None
        self.previous, self.watched, self.gaps, self.violations = {}, set(), set(), []
        self.samples, self.fork_events, self.callback_seconds = 0, 0, 0.0
        self.tree_peak = self.root_peak = self.child_peak = self.fd_peak = 0
        self.disk_peak = self.stdout_peak = self.stderr_peak = 0
        self.fds, self.outer = [], None
        self.reserve = run_dir / "resource-observer-reserve.bin"
        self.reserve.write_bytes(bytes(32768))
        self.log = (run_dir / OUTPUTS["resource_samples"]).open("x", encoding="utf-8")
        self.spec_path = run_dir / "resource-bridge-policy.json"
        self.nonce = secrets.token_hex(32)
        write_json(self.spec_path, {"schema": 1, "nofile": value["limits"]["nofile"],
                                   "inner_argv": inner, "nonce": self.nonce})

    def prepare(self) -> tuple[list[str], dict]:
        start_read, start_write = os.pipe()
        self.fds.extend([start_read, start_write])
        ready_read, ready_write = os.pipe()
        self.fds.extend([ready_read, ready_write])
        self.start_read, self.start_write, self.ready_read, self.ready_write = start_read, start_write, ready_read, ready_write
        self.outer = bridge_argv(self.spec_path, start_read, ready_write, self.inner, runtime=self.runtime)
        return self.outer, {"pass_fds": (start_read, ready_write)}

    def binding(self) -> dict:
        return {"schema": 1, "policy": self.policy, "runtime": self.runtime,
                "inner_argv": self.inner, "outer_argv": self.outer}

    def _close_fd(self, fd):
        if fd in self.fds:
            os.close(fd)
            self.fds.remove(fd)

    def _record(self, value):
        self.log.write(json.dumps(value, sort_keys=True, allow_nan=False) + "\n")
        self.log.flush()

    def _watch(self, pid):
        if pid in self.watched:
            return
        event = select.kevent(pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                              fflags=select.KQ_NOTE_EXIT | select.KQ_NOTE_FORK | select.KQ_NOTE_EXEC)
        try:
            events = self.queue.control([event], 0, 0)
        except ProcessLookupError:
            self.gaps.add("process-exited-before-lifecycle-watch")
            return
        self.watched.add(pid)

    def _events(self, timeout=0):
        events = self.queue.control(None, 4096, timeout)
        if len(events) == 4096:
            self.gaps.add("lifecycle-event-batch-bound")
        rows = []
        observed = time.monotonic() - self.started
        for event in events:
            flags = int(event.fflags)
            if event.flags & select.KQ_EV_ERROR:
                self.gaps.add("lifecycle-observation-error")
            if flags & select.KQ_NOTE_FORK:
                self.fork_events += 1
                self.gaps.add("fork-descendant-lifetimes-not-atomically-tracked")
            if int(event.ident) == self.proc.pid and flags & select.KQ_NOTE_EXEC and self.ready_at is not None:
                if not self.target_exec_seen:
                    self.target_exec_at = observed
                self.target_exec_seen = True
            rows.append({"pid": int(event.ident), "flags": flags})
        if rows:
            self._record({"schema": 1, "kind": "lifecycle", "elapsed_seconds": observed,
                          "after_release": self.ready_at is not None, "utc": now(), "events": rows})
        return rows

    def _ready(self):
        data = b""
        while b"\n" not in data:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.ready_read], [], [], remaining)[0]:
                raise subprocess.TimeoutExpired(self.outer, self.policy["wall_seconds"])
            part = os.read(self.ready_read, 4096)
            if not part or len(data) + len(part) > 4096:
                raise OSError(errno.EINVAL, "missing/oversize bridge handshake")
            data += part
        try:
            ready = json.loads(data)
        except (ValueError, UnicodeError) as exc:
            raise OSError(errno.EINVAL, "invalid bridge handshake") from exc
        expected = {"schema": 1, "pid": self.proc.pid, "pgid": self.proc.pid, "sid": self.proc.pid,
                    "nofile": [self.policy["limits"]["nofile"]] * 2,
                    "spec_sha256": sha256(self.spec_path), "nonce": self.nonce}
        if ready != expected or os.getpgid(self.proc.pid) != self.proc.pid or os.getsid(self.proc.pid) != self.proc.pid:
            raise OSError(errno.EPERM, "bridge FD/session identity mismatch")
        self.handshake = ready
        self._close_fd(self.ready_read)

    def _violation(self, metric, observed, limit):
        if observed > limit:
            item = {"metric": metric, "observed": observed, "limit": limit, "overshoot": observed - limit}
            self.violations.append(item)
            return item
        return None

    def sample(self):
        self._events(0)
        start = time.monotonic()
        stage = "target" if self.target_exec_seen else "bridge"
        tools, gaps = [], []
        global_rows, raw = resource_tools.census(self.deadline)
        tools.append(raw)
        rows, gaps = resource_tools.owned_rows(global_rows, self.proc.pid, self.previous, leader_unreaped=self.proc.returncode is None)
        del global_rows  # Never emit the global census or unrelated identifiers.
        violations = []
        for row in rows:
            self.previous[row["pid"]] = row["birth"]
            self._watch(row["pid"])
            # Before and after native counters, prove membership against the
            # still-unreaped private session; a reused unrelated PID is excluded.
            count, receipt, gap = resource_tools.descriptors(row, self.deadline)
            if receipt:
                tools.append(receipt)
            try:
                same = os.getpgid(row["pid"]) == self.proc.pid and os.getsid(row["pid"]) == self.proc.pid
            except ProcessLookupError:
                same = (row["pid"] == self.proc.pid and self.proc.returncode is None and row["state"].startswith("Z"))
            if not same:
                count, gap = None, "ownership-lost-during-counter-read"
            row["fd_count"] = count
            if gap:
                gaps.append(gap)
            if count is not None:
                self.fd_peak = max(self.fd_peak, count)
                violation = self._violation("nofile", count, self.policy["limits"]["nofile"])
                if violation:
                    violations.append(violation)
        output = resource_tools.disk_usage(self.root, self.root_identity, self.deadline)
        if output["raced_entries"]:
            gaps.append("owned-output-census-race")
        output["stdout_bytes"] = os.fstat(self.stdout.fileno()).st_size
        output["stderr_bytes"] = os.fstat(self.stderr.fileno()).st_size
        for metric, observed in (("owned_bytes", max(output["logical_bytes"], output["allocated_bytes"])),
                                 ("stdout_bytes", output["stdout_bytes"]), ("stderr_bytes", output["stderr_bytes"])):
            violation = self._violation(metric, observed, self.policy["limits"][metric])
            if violation:
                violations.append(violation)
        tree = sum(row["rss_bytes"] for row in rows)
        root = sum(row["rss_bytes"] for row in rows if row["pid"] == self.proc.pid)
        self.tree_peak, self.root_peak = max(self.tree_peak, tree), max(self.root_peak, root)
        self.child_peak = max(self.child_peak, tree - root)
        self.disk_peak = max(self.disk_peak, output["logical_bytes"], output["allocated_bytes"])
        self.stdout_peak, self.stderr_peak = max(self.stdout_peak, output["stdout_bytes"]), max(self.stderr_peak, output["stderr_bytes"])
        self.gaps.update(gaps)
        duration = time.monotonic() - start
        self._record({"schema": 1, "kind": "sample", "sequence": self.samples,
                      "stage": stage, "started_elapsed_seconds": start - self.started,
                      "elapsed_seconds": time.monotonic() - self.started, "utc": now(),
                      "observation_seconds": duration, "processes": rows,
                      "tree_rss_bytes": tree, "root_rss_bytes": root, "children_rss_bytes": tree - root,
                      "output": output, "tools": tools, "gaps": sorted(set(gaps)), "violations": violations})
        self.samples += 1
        self.callback_seconds += duration
        if violations:
            raise OSError(errno.EFBIG, "observed resource budget exceeded; raw outputs retained")

    def wait(self, proc, timeout):
        from .owned_process import _wait_unreaped
        self.proc = proc
        self._close_fd(self.start_read)
        self._close_fd(self.ready_write)
        self.queue = select.kqueue()
        self._watch(proc.pid)
        self._ready()
        self.sample()  # Observed bridge baseline, before registered target exec.
        self._events(0)  # Drain all pre-release events before the target can exec.
        os.write(self.start_write, self.nonce.encode() + b"\n")
        self._close_fd(self.start_write)
        self.ready_at = time.monotonic() - self.started
        next_sample = time.monotonic()
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(self.outer, timeout)
            self._events(0)
            try:
                _wait_unreaped(proc, 0)
            except subprocess.TimeoutExpired:
                pass
            else:
                self.sample()
                self._events(0)
                return
            if time.monotonic() >= next_sample:
                self.sample()
                next_sample = time.monotonic() + self.policy["interval_seconds"]
            self._events(min(max(0, next_sample - time.monotonic()), max(0, self.deadline - time.monotonic())))

    def before_cleanup(self, proc):
        self.proc = proc
        self.wait_finished = time.monotonic() - self.started

    def after_cleanup(self):
        self.cleanup_finished = time.monotonic() - self.started

    def finish(self, exit_code, timed_out, error=None):
        """Always retain raw failure evidence; called after protected cleanup."""
        if self.queue is not None:
            try:
                self._events(0)
            except OSError:
                self.gaps.add("final-lifecycle-drain-unavailable")
            self.queue.close()
        for fd in list(self.fds):
            self._close_fd(fd)
        if self.handshake is None:
            self.gaps.add("bridge-handshake-missing")
        if not self.samples:
            self.gaps.add("no-owned-samples")
        if timed_out:
            self.gaps.add("wall-deadline-exceeded")
        if error:
            self.gaps.add("observer-or-command-incomplete")
        # Include stream bytes written during cleanup and final output census.
        try:
            end_disk = resource_tools.disk_usage(self.root, self.root_identity, time.monotonic() + 1)
            if end_disk["raced_entries"]:
                self.gaps.add("owned-output-census-race")
            end_stdout, end_stderr = os.fstat(self.stdout.fileno()).st_size, os.fstat(self.stderr.fileno()).st_size
            for metric, observed in (("owned_bytes", max(end_disk["logical_bytes"], end_disk["allocated_bytes"])),
                                     ("stdout_bytes", end_stdout), ("stderr_bytes", end_stderr)):
                self._violation(metric, observed, self.policy["limits"][metric])
            self.disk_peak = max(self.disk_peak, end_disk["logical_bytes"], end_disk["allocated_bytes"])
            self.stdout_peak, self.stderr_peak = max(self.stdout_peak, end_stdout), max(self.stderr_peak, end_stderr)
        except (OSError, subprocess.TimeoutExpired):
            end_disk, end_stdout, end_stderr = None, None, None
            self.gaps.add("final-output-census-incomplete")
        # Release a counted32KiB reserve before bounded final metadata replaces it.
        self.reserve.unlink()
        if self.wait_finished is None:
            self.wait_finished = time.monotonic() - self.started
        if self.cleanup_finished is None or self.cleanup_finished < self.wait_finished:
            self.cleanup_finished = self.wait_finished
        self._record({"schema": 1, "kind": "final", "elapsed_seconds": time.monotonic() - self.started,
                      "utc": now(), "output": end_disk, "stdout_bytes": end_stdout, "stderr_bytes": end_stderr,
                      "execution_finished_seconds": self.wait_finished, "cleanup_finished_seconds": self.cleanup_finished})
        self.log.close()
        observation = {"schema": 1, "kind": "goldmine.resource-observation", "binding_sha256": digest(self.binding()),
                       "started_at": self.started_at, "finished_at": now(), "root_pid": self.proc.pid if self.proc else None,
                       "bridge": self.handshake, "bridge_released_after_seconds": self.ready_at, "target_exec_observed_after_seconds": self.target_exec_at,
                       "exit_code": exit_code, "timed_out": timed_out, "error": error,
                       "execution_seconds": self.wait_finished, "cleanup_seconds": self.cleanup_finished - self.wait_finished,
                       "wall_deadline_overshoot_seconds": max(0, self.wait_finished - self.policy["wall_seconds"]),
                       "samples_sha256": sha256(self.run_dir / OUTPUTS["resource_samples"]), "sample_count": self.samples,
                       "fork_events": self.fork_events, "coverage_gaps": sorted(self.gaps),
                       "peak": {"sampled_tree_rss_bytes": self.tree_peak, "sampled_root_rss_bytes": self.root_peak,
                                "sampled_children_rss_bytes": self.child_peak, "sampled_per_process_fds": self.fd_peak,
                                "owned_output_bytes": self.disk_peak, "stdout_bytes": self.stdout_peak, "stderr_bytes": self.stderr_peak},
                       "violations": self.violations, "observer_callback_wall_seconds": self.callback_seconds,
                       "lifetime_complete": False, "kernel_tree_peak": False,
                       "model_child_roles": "unclassified; no command lines inspected"}
        if len(json.dumps(observation, ensure_ascii=False, indent=2).encode()) > 24576:
            raise OSError(errno.EFBIG, "resource final metadata exceeds reserved bound")
        write_json(self.run_dir / OUTPUTS["resource_observation"], observation)
        from .resource_publication import placeholder
        placeholder(self.run_dir)
