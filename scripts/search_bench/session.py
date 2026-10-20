"""Own one disposable serve daemon and retain every CLI command receipt."""
from __future__ import annotations

import json
import os
from contextlib import closing
from pathlib import Path
import platform
import sqlite3
import subprocess
import tempfile
import time

from acceptance_lib.evidence import artifact, capture_command, read_command_run, sha256


def pressure() -> dict:
    command = ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=5)
        return {"argv": command, "exit_code": proc.returncode, "raw": proc.stdout.strip(),
                "level": int(proc.stdout.strip()) if proc.returncode == 0 else None}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"argv": command, "level": None, "error": type(exc).__name__}


def package_identity(source: Path) -> dict:
    return {str(p.relative_to(source)): sha256(p) for p in sorted((source / "goldmine").rglob("*.py"))}


class Session:
    def __init__(self, out, source, model, server, python, profile):
        self.out, self.source = Path(out), Path(source)
        self.python = str(Path(python).absolute())
        self.identity = {"source_files": package_identity(self.source), "model_sha256": sha256(model),
                         "server_sha256": sha256(server), "profile": profile}
        self.home = self.out / "state"
        self.home.mkdir(parents=True)
        self.config = self.home / "config.toml"
        self.config.write_text("[serve]\nlinger = 3600\n")
        self.socket_dir = tempfile.TemporaryDirectory(prefix="gm-bench-")
        self.socket = str(Path(self.socket_dir.name) / "s.sock")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GOLDMINE_") and k not in ("PYTHONPATH", "PYTHONHOME")}
        self.env.update({"GOLDMINE_HOME": str(self.home), "GOLDMINE_CONFIG": str(self.config),
                         "GOLDMINE_MODEL": str(Path(model).resolve()), "GOLDMINE_LLAMA_SERVER": str(Path(server).resolve()),
                         "GOLDMINE_SERVE": self.socket, "PYTHONPATH": str(self.source),
                         "GOLDMINE_OCR_AUTOCOMPILE": "0"})
        if profile == "controlled":
            self.env["GOLDMINE_SERVE_PRESSURE_LEVEL"] = "1"
        self.proc = None
        self.streams = []
        self.db = self.home / "index.sqlite"

    def prepare(self, db: Path, *, upgrade: bool):
        self.identity["source_index_sha256"] = sha256(db)
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as src, closing(sqlite3.connect(self.db)) as target:
            src.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
            target.commit()
        if upgrade:
            receipt, _, _, _, _ = self.command("prepare-lexical", ["reindex", "lexical", "--db", str(self.db), "--json"])
            if receipt["exit_code"]:
                raise ValueError("explicit upgrade of disposable benchmark index failed")
        self.identity["index_sha256"] = sha256(self.db)
        from goldmine.backends.serve import one_shot_request

        for name in ("daemon.stdout", "daemon.stderr"):
            self.streams.append((self.out / name).open("wb"))
        self.proc = subprocess.Popen([self.python, "-m", "goldmine.cli", "serve"], cwd=self.out,
                env=self.env, stdin=subprocess.DEVNULL, stdout=self.streams[0], stderr=self.streams[1])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise ValueError("owned serve exited during startup; inspect daemon.stderr")
            try:
                one_shot_request(self.socket, "ping", timeout_s=1)
                return
            except (OSError, RuntimeError):
                time.sleep(.1)
        raise ValueError("owned serve was not ready within30s")

    def command(self, name, arguments, *, timeout=120):
        directory = self.out / "commands" / name
        capture_command(directory, f"latency.{name}", [self.python, "-m", "goldmine.cli", *arguments],
                        cwd=self.out, env=self.env, identity=self.identity, suite_id="latency.sample.v1", timeout=timeout)
        _, receipt, stdout = read_command_run(directory / "manifest.json")
        try:
            rows = json.loads(stdout.read_text())
            valid = isinstance(rows, list)
        except ValueError:
            rows, valid = None, False
        return receipt, valid, rows, (directory / "stderr.log").read_text(), artifact(self.out, directory / "manifest.json")

    def status(self):
        from goldmine.backends.serve import one_shot_request

        return one_shot_request(self.socket, "status", timeout_s=5)

    def close(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        for stream in self.streams:
            stream.close()
        self.socket_dir.cleanup()


def hardware() -> dict:
    return {"os": platform.system(), "architecture": platform.machine(), "python": platform.python_version(),
            "cpu_count": os.cpu_count(), "pressure": pressure()}
