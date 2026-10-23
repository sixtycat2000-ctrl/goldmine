"""Owned subprocess groups, bounded output/CPU/time, and raw argv receipts."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import time


def _limits():
    import resource

    for name, value in (("RLIMIT_CPU", 30), ("RLIMIT_FSIZE", 16 * 1024 * 1024), ("RLIMIT_NOFILE", 256)):
        option = getattr(resource, name)
        soft, hard = resource.getrlimit(option)
        ceiling = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(option, (ceiling, ceiling))


def run_process(argv, *, cwd, env, raw_prefix, timeout=30):
    """No shell or user startup files. A timeout kills only this owned group."""
    raw_prefix = Path(raw_prefix)
    raw_prefix.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timed_out = False
    with raw_prefix.with_suffix(".stdout").open("xb") as stdout, raw_prefix.with_suffix(".stderr").open("xb") as stderr:
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                                       start_new_session=True, preexec_fn=_limits if os.name == "posix" else None)
        except OSError as error:
            return {"argv": argv, "cwd": str(cwd), "exit_code": None, "error": str(error), "timed_out": False,
                    "wall_seconds": time.monotonic() - started}
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()
    return {"argv": argv, "cwd": str(cwd), "exit_code": process.returncode, "timed_out": timed_out,
            "wall_seconds": time.monotonic() - started,
            "stdout": raw_prefix.with_suffix(".stdout").name, "stderr": raw_prefix.with_suffix(".stderr").name}
