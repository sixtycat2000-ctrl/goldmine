"""Bounded cleanup for an explicitly owned, newly spawned POSIX session only."""
from __future__ import annotations

from contextlib import closing
import os
import errno
import json
import select
import signal
import subprocess
import sys
import threading
import time


class ProcessInterrupted(Exception):
    def __init__(self, signum):
        self.exit_code = 128 + signum
        super().__init__(f"owned command interrupted by signal {signum}")


def _no_live_group(proc, diagnostics):
    """Darwin EPERM needs both unreaped completion and a bounded group census."""
    if sys.platform != "darwin":
        return False
    argv = ["/bin/ps", "-g", str(proc.pid), "-o", "pid=,pgid=,stat="]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=1, check=False)
        diagnostics(json.dumps({"owned_group_audit": {"argv": argv, "exit_code": result.returncode,
            "stdout": result.stdout[:4096].decode(errors="replace"), "stderr": result.stderr[:4096].decode(errors="replace")}}))
        if result.returncode or result.stderr or not result.stdout or len(result.stdout) > 4096:
            return False
        rows = [line.split() for line in result.stdout.decode("ascii").splitlines() if line.strip()]
        return (any(len(row) == 3 and int(row[0]) == proc.pid for row in rows) and
                all(len(row) == 3 and int(row[1]) == proc.pid and row[2].startswith("Z") for row in rows))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        diagnostics(f"owned group census incomplete: {type(exc).__name__}: {exc}")
        return False


def _signal_group(proc, signum, diagnostics):
    if proc.returncode is not None:
        raise OSError(errno.ECHILD, "owned leader already reaped; refusing any group signal")
    try:
        os.killpg(proc.pid, signum)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        # Darwin returns EPERM for a group containing only exited children.
        # Confirm completion independently without releasing our leader PID.
        try:
            _wait_unreaped(proc, 0)
        except (OSError, subprocess.TimeoutExpired):
            raise exc
        if not _no_live_group(proc, diagnostics):
            raise exc
        return False
    return True


def _stop(proc, *, diagnostics):
    # Keep the leader unreaped until the final group signal, preventing reuse.
    try:
        if _signal_group(proc, signal.SIGTERM, diagnostics):
            time.sleep(.25)
            _signal_group(proc, signal.SIGKILL, diagnostics)
    except BaseException as group_error:
        # If group cleanup is refused, reap this exact direct child if possible;
        # the original group failure still makes the command incomplete.
        try:
            proc.kill()
            proc.wait(timeout=1)
        except BaseException as direct_error:
            group_error.add_note(f"direct child cleanup failed: {type(direct_error).__name__}: {direct_error}")
        raise group_error
    proc.wait(timeout=1)


def _wait_unreaped(proc, timeout):
    if hasattr(os, "waitid"):
        deadline = time.monotonic() + timeout
        while True:
            result = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
            if result is not None and result.si_pid:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout)
            time.sleep(min(.01, remaining))
    else:
        # Darwin's Python exposes kqueue, but not waitid. NOTE_EXIT observes
        # completion while leaving the owned leader waitable and unreused.
        with closing(select.kqueue()) as queue:
            event = select.kevent(proc.pid, filter=select.KQ_FILTER_PROC,
                                 flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT)
            try:
                events = queue.control([event], 1, timeout)
            except ProcessLookupError:
                return  # The child exited before registration; it remains unreaped.
            if not events:
                raise subprocess.TimeoutExpired(proc.args, timeout)
            for result in events:
                if result.flags & select.KQ_EV_ERROR and result.data not in (0, errno.ESRCH):
                    raise OSError(result.data, "owned process completion observation failed")


def run_owned(argv, *, cwd, env, stdout, stderr, timeout, observer=None):
    if os.name != "posix" or threading.current_thread() is not threading.main_thread():
        raise OSError(errno.ENOTSUP, "owned session capture requires the POSIX main thread")
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL or not (hasattr(os, "waitid") or hasattr(select, "kqueue")):
        raise OSError(errno.ENOTSUP, "owned session capture requires unreaped child ownership and waitid/kqueue")
    pending_signal, acquiring = None, True
    def interrupted(signum, _frame):
        nonlocal pending_signal
        if acquiring:
            if pending_signal is None:
                pending_signal = signum
            return
        raise ProcessInterrupted(signum)
    old = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    def diagnostics(message):
        try:
            stderr.write((message + "\n").encode())
        except (OSError, AttributeError):
            pass
    proc, problem = None, None
    try:
        for sig in old:
            signal.signal(sig, interrupted)
        if pending_signal is not None:
            raise ProcessInterrupted(pending_signal)
        spawn_argv, extra = (observer.prepare() if observer is not None else (argv, {}))
        proc = subprocess.Popen(spawn_argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                                stdin=subprocess.DEVNULL, start_new_session=True, **extra)
        acquiring = False
        if pending_signal is not None:
            raise ProcessInterrupted(pending_signal)
        if observer is None:
            _wait_unreaped(proc, timeout)
        else:
            observer.wait(proc, timeout)
    except BaseException as exc:
        problem = exc
        raise
    finally:
        try:
            for sig in old:
                signal.signal(sig, signal.SIG_IGN)
            if proc is not None:
                try:
                    try:
                        if observer is not None:
                            observer.before_cleanup(proc)
                    finally:
                        _stop(proc, diagnostics=diagnostics)
                except BaseException as cleanup:
                    diagnostics(f"owned cleanup failed: {type(cleanup).__name__}: {cleanup}")
                    for note in getattr(cleanup, "__notes__", ()):
                        diagnostics(note)
                    if problem is not None:
                        raise problem from cleanup
                    raise
                finally:
                    if observer is not None:
                        observer.after_cleanup()
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)
    return subprocess.CompletedProcess(argv, proc.returncode)
