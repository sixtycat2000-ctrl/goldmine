"""Fixed, bounded native counters. Never persist a global process inventory."""
from __future__ import annotations

import errno
import hashlib
import os
import selectors
import signal
import stat
import subprocess
import time
from pathlib import Path

PS = ["/bin/ps", "-axo", "pid=,ppid=,pgid=,rss=,lstart=,stat="]
MAX_BYTES, MAX_ROWS, MAX_FILES = 256 * 1024, 4096, 200000


def tool(argv: list[str], deadline: float) -> tuple[bytes, dict]:
    """Capture at most256KiB/stream and enforce an absolute <=1s tool deadline.

    These are fixed trusted native counter programs, not an arbitrary command
    runner. Only this newly spawned direct counter process can be killed here.
    """
    if argv != PS and not (len(argv) == 7 and argv[:6] == ["/usr/sbin/lsof", "-nP", "-w", "-a", "-p", argv[5]]
                            and argv[5].isdigit() and argv[6:] == ["-Fpf"]):
        raise OSError(errno.EINVAL, "noncanonical resource tool argv")
    started = time.monotonic()
    end = min(deadline, started + 1)
    if end <= started:
        raise subprocess.TimeoutExpired(argv, 0)
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={"PATH": "/usr/bin:/bin:/usr/sbin", "LC_ALL": "C", "TZ": "UTC"})
    buffers = {"out": bytearray(), "err": bytearray()}
    try:
        with selectors.DefaultSelector() as selector:
            for name, stream in (("out", proc.stdout), ("err", proc.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, end - started)
                for key, _ in selector.select(remaining):
                    part = os.read(key.fd, 65536)
                    if not part:
                        selector.unregister(key.fileobj)
                    elif len(buffers[key.data]) + len(part) > MAX_BYTES:
                        raise OSError(errno.EFBIG, "resource tool response exceeds bound")
                    else:
                        buffers[key.data].extend(part)
            proc.wait(timeout=max(.001, end - time.monotonic()))
    finally:
        if proc.returncode is None:
            proc.kill()
            proc.wait(timeout=1)
        proc.stdout.close()
        proc.stderr.close()
    raw, err = bytes(buffers["out"]), bytes(buffers["err"])
    # Global numeric census bytes are ephemeral. Only digest/counts and filtered
    # owned counters are persisted; no process command lines are requested.
    receipt = {"argv": argv, "elapsed_seconds": time.monotonic() - started, "exit_code": proc.returncode,
               "stdout_bytes": len(raw), "stderr_bytes": len(err),
               "stdout_sha256": hashlib.sha256(raw).hexdigest(), "stderr_sha256": hashlib.sha256(err).hexdigest()}
    if proc.returncode != 0 or err:
        raise OSError(errno.EIO, f"resource tool incomplete (exit {proc.returncode}, stderr bytes {len(err)})")
    return raw, receipt


def _ascii(raw):
    try:
        return raw.decode("ascii").splitlines()
    except UnicodeError as exc:
        raise OSError(errno.EINVAL, "non-ASCII native resource counters") from exc


def _integer(value):
    if not value.isdigit() or len(value) > 20:
        raise OSError(errno.EINVAL, "invalid bounded native integer")
    return int(value)


def census(deadline: float) -> tuple[list[dict], dict]:
    raw, receipt = tool(PS, deadline)
    rows = []
    for line in _ascii(raw):
        fields = line.split()
        if len(fields) != 10 or not all(field.isdigit() for field in fields[:4]):
            raise OSError(errno.EINVAL, "malformed numeric process census")
        pid, ppid, pgid, kib = map(_integer, fields[:4])
        rows.append({"pid": pid, "ppid": ppid, "pgid": pgid, "rss_bytes": kib * 1024,
                     "birth": " ".join(fields[4:9]), "state": fields[9]})
    if not rows or len(rows) > MAX_ROWS or len({r["pid"] for r in rows}) != len(rows):
        raise OSError(errno.EFBIG, "ambiguous/oversize process census")
    return rows, receipt


def owned_rows(rows: list[dict], root_pid: int, previous: dict[int, str], *, leader_unreaped: bool = False) -> tuple[list[dict], list[str]]:
    """Prove each reported counter belongs to the still-anchored private session.

    No PID-derived signal is sent here. Escape/identity uncertainty fails closed;
    only the existing protected leader group cleanup is allowed to signal.
    """
    by_pid = {row["pid"]: row for row in rows}
    if root_pid not in by_pid:
        raise OSError(errno.ESRCH, "unreaped owned leader absent from census")
    descendants = {root_pid}
    while True:
        found = {row["pid"] for row in rows if row["ppid"] in descendants}
        expanded = descendants | found
        if expanded == descendants:
            break
        descendants = expanded
    candidates = {r["pid"] for r in rows if r["pgid"] == root_pid} | descendants
    candidates |= {pid for pid, birth in previous.items() if pid in by_pid and by_pid[pid]["birth"] == birth}
    selected, gaps = [], []
    for pid in sorted(candidates):
        row = by_pid[pid]
        try:
            pgid, sid = os.getpgid(pid), os.getsid(pid)
        except ProcessLookupError:
            # Darwin may hide getpgid/getsid of an exited but still waitable
            # direct child. Its unreaped PID and actual ps group row are bound;
            # no numeric PID is signalled or reused by accepting this zero row.
            if pid == root_pid and leader_unreaped and row["state"].startswith("Z") and row["pgid"] == root_pid:
                selected.append(row)
                continue
            gaps.append("process-disappeared-before-ownership-check")
            continue
        if pgid != root_pid or sid != root_pid or row["pgid"] != root_pid:
            gaps.append("observed-owned-descendant-escaped-private-group")
            continue
        selected.append(row)
    return selected, sorted(set(gaps))


def descriptors(row: dict, deadline: float) -> tuple[int | None, dict | None, str | None]:
    if row["state"].startswith("Z"):
        return 0, None, None
    argv = ["/usr/sbin/lsof", "-nP", "-w", "-a", "-p", str(row["pid"]), "-Fpf"]
    try:
        raw, receipt = tool(argv, deadline)
    except OSError:
        # An exiting process is not evidence of a zero-FD process.
        return None, None, "fd-census-unavailable-or-process-exited"
    pid, fds = None, set()
    for line in _ascii(raw):
        if line.startswith("p") and line[1:].isdigit() and pid is None:
            pid = _integer(line[1:])
        elif line.startswith("f"):
            if line[1:].isdigit():
                number = _integer(line[1:])
                if number in fds:
                    raise OSError(errno.EINVAL, "duplicate native descriptor")
                fds.add(number)
            elif line[1:] not in {"cwd", "txt", "rtd", "mem", "DEL", "NOFD"}:
                raise OSError(errno.EINVAL, "unexpected native descriptor field")
        else:
            raise OSError(errno.EINVAL, "unexpected lsof field")
    if pid != row["pid"] or not fds:
        return None, receipt, "fd-census-empty-or-wrong-process"
    return len(fds), receipt, None


def disk_usage(root: Path, identity: tuple[int, int], deadline: float) -> dict:
    started = time.monotonic()
    top = root.lstat()
    if (top.st_dev, top.st_ino) != identity or not stat.S_ISDIR(top.st_mode):
        raise OSError(errno.EINVAL, "owned output root replaced")
    logical, allocated, entries, races = top.st_size, top.st_blocks * 512, 0, 0
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    initial = os.open(root, flags)
    actual = os.fstat(initial)
    if (actual.st_dev, actual.st_ino) != identity:
        os.close(initial)
        raise OSError(errno.EINVAL, "owned root changed during open")
    stack, seen = [initial], set()
    try:
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(directory) as children:
                    for child in children:
                        if time.monotonic() >= deadline:
                            raise subprocess.TimeoutExpired(["owned-output-census"], 0)
                        entries += 1
                        if entries > MAX_FILES:
                            raise OSError(errno.EFBIG, "owned output census exceeds entry bound")
                        try:
                            info = child.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            races += 1
                            continue
                        if info.st_dev != identity[0]:
                            raise OSError(errno.ENOTSUP, "cross-filesystem output cannot prove exclusive ownership")
                        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                            raise OSError(errno.ENOTSUP, "symlink/special file in owned output scope")
                        if stat.S_ISDIR(info.st_mode):
                            try:
                                nested = os.open(child.name, flags, dir_fd=directory)
                            except FileNotFoundError:
                                races += 1
                                continue
                            stack.append(nested)
                            if os.fstat(nested).st_ino != info.st_ino:
                                raise OSError(errno.EINVAL, "owned directory replaced during census")
                        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                            raise OSError(errno.ENOTSUP, "hardlink output cannot prove exclusive owned bytes")
                        inode = (info.st_dev, info.st_ino)
                        if inode not in seen:
                            seen.add(inode)
                            logical += info.st_size
                            allocated += info.st_blocks * 512
            finally:
                os.close(directory)
    finally:
        for directory in stack:
            os.close(directory)
    return {"logical_bytes": logical, "allocated_bytes": allocated, "entries": entries,
            "raced_entries": races, "elapsed_seconds": time.monotonic() - started}
