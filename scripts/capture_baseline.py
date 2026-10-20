#!/usr/bin/env python3
"""Capture immutable historical inputs and the current local environment.

The default operation never starts a model, downloads data, or opens an index.
Outputs are staged beside the destination then renamed into place.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs/execution-plan/baseline-20261021.json"


def sha256(file: Path) -> str:
    digest = hashlib.sha256()
    with file.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command(argv: list[str], cwd: Path) -> dict:
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=60)
    return {"argv": argv, "cwd": str(cwd), "exit_code": proc.returncode,
            "stdout": proc.stdout.decode("utf-8", "replace"),
            "stderr": proc.stderr.decode("utf-8", "replace")}


def capture(out: Path, *, root: Path = ROOT, spec: Path = SPEC,
            include_live: bool = False, collect: bool = True) -> dict:
    out = out.resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError(f"destination must be absent or empty: {out}")
    frozen = json.loads(spec.read_text(encoding="utf-8"))
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{out.name}-", dir=out.parent))
    try:
        files, missing, drift = [], [], []
        for name, expected in frozen["sha256"].items():
            source = root / name
            if not source.is_file():
                missing.append(name)
                continue
            target = stage / "history" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            actual = sha256(source)
            if sha256(target) != actual:
                raise ValueError(f"copy changed while capturing: {name}")
            files.append({"source": name, "path": str(target.relative_to(stage)),
                          "sha256": actual, "historical_sha256": expected,
                          "matches_history": actual == expected})
            if actual != expected:
                drift.append(name)
        versions = {}
        for package in ("numpy", "pytest"):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = None
        commit = command(["git", "rev-parse", "HEAD"], root)
        diff = command(["git", "diff", "--binary", "HEAD"], root)
        tracked = command(["git", "diff", "--name-only", "-z", "HEAD"], root)
        untracked = command(["git", "ls-files", "--others", "--exclude-standard", "-z"], root)
        changed = set(tracked["stdout"].split("\0") + untracked["stdout"].split("\0"))
        working_files = {name: sha256(root / name) for name in sorted(changed)
                         if name and (root / name).is_file()
                         and (root / name).resolve().is_relative_to(root.resolve())}
        (stage / "working-tree.diff").write_text(diff["stdout"], encoding="utf-8")
        result = {"schema": 1, "kind": "baseline_capture",
                  "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "git_commit": commit["stdout"].strip() if commit["exit_code"] == 0 else None,
                  "dirty_diff_sha256": sha256(stage / "working-tree.diff"),
                  "working_files": working_files,
                  "historical_source_commit": frozen["source_commit"],
                  "platform": {"os": platform.platform(), "arch": platform.machine(),
                               "python": platform.python_version(),
                               "sqlite": sqlite3.sqlite_version, **versions},
                  "include_live": include_live, "files": files,
                  "missing": missing, "drift": drift, "commands": [],
                  "model_files": [], "status": "NOT_COMPARABLE" if missing or drift else "PASS"}
        if collect:
            collection = command([sys.executable, "-m", "pytest", "tests/", "--collect-only",
                                  "-q", "-m", "not live"], root)
            (stage / "collection.log").write_text(collection.pop("stdout"), encoding="utf-8")
            (stage / "collection.stderr").write_text(collection.pop("stderr"), encoding="utf-8")
            nodeids = [line for line in (stage / "collection.log").read_text().splitlines()
                       if line.startswith("tests/") and "::" in line]
            (stage / "nodeids.json").write_text(json.dumps(nodeids, indent=2) + "\n")
            result["commands"].append({**collection,
                                       "log_sha256": sha256(stage / "collection.log"),
                                       "collected_nodeids": len(nodeids)})
            if collection["exit_code"] or not nodeids:
                result["missing"].append("pytest collection")
                result["status"] = "NOT_COMPARABLE"
        if include_live:
            # Explicit opt-in probes only; inference is a separate measured run.
            server = root / "vendor/llama.cpp/build/bin/llama-server"
            if server.is_file():
                result["commands"].append(command([str(server), "--version"], root))
            for model in sorted((root / "models").glob("*.gguf")):
                result["model_files"].append({"path": str(model.relative_to(root)),
                                              "bytes": model.stat().st_size,
                                              "sha256": sha256(model)})
        result["artifacts"] = {str(f.relative_to(stage)): sha256(f)
                               for f in sorted(stage.rglob("*")) if f.is_file()}
        (stage / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        if out.exists():
            out.rmdir()  # only the checked empty destination may be replaced
        os.replace(stage, out)
        return result
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--include-live", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = capture(args.out, include_live=args.include_live)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"status": result["status"], "out": str(args.out),
                      "missing": result["missing"], "drift": result["drift"]}))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
