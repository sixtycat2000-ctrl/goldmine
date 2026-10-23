"""Owned processes with raw streams and per-child, not parent, RSS."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

from .pipe_protocol import JsonLines
from .protocol import peak, ref, visible_env, write


def command(argv, directory: Path, *, cwd: Path, env: dict, timeout: float) -> dict:
    directory.mkdir(parents=True, exist_ok=False)
    began = time.monotonic_ns()
    timed_out, pid, usage, error = False, None, None, None
    with (directory / "stdout.log").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
            pid = proc.pid
            try:
                while True:
                    waited, status, usage = os.wait4(pid, os.WNOHANG)
                    if waited:
                        code = os.waitstatus_to_exitcode(status)
                        proc.returncode = code
                        break
                    if (time.monotonic_ns() - began) / 1e9 > timeout:
                        timed_out = True
                        proc.kill()
                        _, status, usage = os.wait4(pid, 0)
                        code = os.waitstatus_to_exitcode(status)
                        proc.returncode = code
                        break
                    time.sleep(.002)
            except BaseException:
                proc.kill()
                proc.wait()
                raise
        except OSError as exc:
            code, error = 127, f"{type(exc).__name__}: {exc}"
            stderr.write(error.encode())
    ended = time.monotonic_ns()
    result = {"schema": 1, "kind": "goldmine.scale.command", "argv": argv, "cwd": str(cwd.resolve()),
              "env": visible_env(env), "process_id": pid, "exit_code": code, "timed_out": timed_out,
              "started_ns": began, "finished_ns": ended, "wall_ms": (ended - began) / 1e6,
              "rss": peak(usage) if usage is not None else None, "spawn_error": error,
              "stdout": ref(directory, directory / "stdout.log"),
              "stderr": ref(directory, directory / "stderr.log")}
    write(directory / "receipt.json", result)
    return result


class Warm:
    """A case/arm-specific Python process; only imports persist between commands."""
    def __init__(self, argv, directory: Path, *, cwd: Path, env: dict, timeout: float):
        directory.mkdir(parents=True, exist_ok=False)
        self.directory, self.timeout = directory, timeout
        self.log = self.raw = self.proc = None
        self.ready = self.failure = None
        self.closed = False
        self.argv, self.env, self.cwd = argv, env, cwd
        self.started = time.monotonic_ns()
        try:
            self.log = (directory / "worker.stderr").open("wb")
            self.raw = (directory / "worker.stdout").open("wb")
            self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=self.log, bufsize=0)
            self.channel = JsonLines(self.proc.stdout, self.proc.stdin, self.raw)
            self.ready = self._read(self.started / 1e9 + timeout)
            if (not isinstance(self.ready, dict) or self.ready.get("kind") != "ready" or
                    self.ready.get("process_id") != self.proc.pid):
                raise ValueError("actual CLI warm worker did not become ready")
        except BaseException as exc:
            self.close(error=exc, phase="startup" if self.proc is not None else "spawn")
            raise

    def _read(self, deadline=None):
        return self.channel.receive(time.monotonic() + self.timeout if deadline is None else deadline)

    def call(self, args, directory: Path) -> dict:
        try:
            deadline = time.monotonic() + self.timeout
            self.channel.send({"args": args, "directory": str(directory.resolve())}, deadline)
            response = self._read(deadline)
            if response != {"kind": "completed", "directory": str(directory.resolve())}:
                raise ValueError("warm CLI response does not bind the requested command")
            from .output import json_value

            return json_value((directory / "receipt.json").read_bytes())
        except BaseException as exc:
            self.close(error=exc, phase="request")
            raise

    def close(self, *, error=None, phase=None):
        if self.closed:
            return
        self.closed = True
        proc = self.proc
        if error is not None:
            self.failure = {"phase": phase, "type": type(error).__name__, "reason": str(error)}
        try:
            if proc is not None and proc.poll() is None:
                if error is not None:
                    proc.kill()
                else:
                    proc.stdin.close()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1)
        finally:
            for stream in ((proc.stdin, proc.stdout) if proc is not None else ()) + (self.log, self.raw):
                if stream is not None and not stream.closed:
                    stream.close()
        # Even a Popen failure has an explicit empty raw stream and a durable
        # failed lifecycle receipt. No successful sample is invented.
        for name in ("worker.stdout", "worker.stderr"):
            if not (self.directory / name).exists():
                (self.directory / name).touch()
        write(self.directory / "process.json", {"schema": 1, "argv": self.argv, "cwd": str(self.cwd.resolve()),
            "env": visible_env(self.env), "process_id": proc.pid if proc is not None else None,
            "exit_code": proc.returncode if proc is not None else 127,
            "timed_out": isinstance(error, TimeoutError), "failure": self.failure,
            "reaped": proc is None or proc.returncode is not None,
            "started_ns": self.started, "finished_ns": time.monotonic_ns(), "ready": self.ready,
            "stdout": ref(self.directory, self.directory / "worker.stdout"),
            "stderr": ref(self.directory, self.directory / "worker.stderr")})
