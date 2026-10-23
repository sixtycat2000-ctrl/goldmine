#!/usr/bin/env python3
"""Build wheel/sdist from a bounded source snapshot, leaving checkout build files out."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

from acceptance_lib.evidence import (artifact, capture_command, read_command_run,
                                     sha256, write_json)
from acceptance_lib.runner import code_identity


def snapshot(root: Path, destination: Path) -> dict:
    files = [root / name for name in ("pyproject.toml", "README.md", "LICENSE")]
    files += sorted((root / "goldmine").rglob("*.py"))
    files = [p for p in files if "__pycache__" not in p.parts]
    before = {str(p.relative_to(root)): sha256(p) for p in files}
    destination.mkdir(parents=True)
    for source in files:
        target = destination / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    copied = {name: sha256(destination / name) for name in before}
    after = {name: sha256(root / name) for name in before}
    if before != copied or before != after:
        raise ValueError("source changed while preparing build snapshot; retry after edits finish")
    return copied


def snapshot_revision(root: Path, revision: str, destination: Path) -> dict:
    commit = subprocess.run(["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
                            cwd=root, check=True, capture_output=True, text=True).stdout.strip()
    data = subprocess.run(["git", "archive", "--format=tar", commit,
                           "pyproject.toml", "README.md", "LICENSE", "goldmine"],
                          cwd=root, check=True, capture_output=True).stdout
    import io

    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(destination, filter="data")
    return {"commit": commit, "files": {str(p.relative_to(destination)): sha256(p)
            for p in sorted(destination.rglob("*")) if p.is_file()}}


def build(out: Path, python: Path, *, sdist: Path | None = None,
          revision: str | None = None) -> dict:
    root = Path(__file__).resolve().parents[1]
    if sdist is not None and revision is not None:
        raise ValueError("select either an explicit source revision or an sdist")
    if out.exists() and any(out.iterdir()):
        raise ValueError("build output must be new or empty")
    out.mkdir(parents=True, exist_ok=True)
    identity = code_identity(root, out)
    with tempfile.TemporaryDirectory(prefix="goldmine-python-build-") as temp:
        stage = Path(temp) / "source"
        if sdist:
            stage.mkdir()
            with tarfile.open(sdist) as archive:
                archive.extractall(stage, filter="data")
            children = list(stage.iterdir())
            if len(children) != 1 or not (children[0] / "pyproject.toml").is_file():
                raise ValueError("sdist must have one project root")
            stage = children[0]
            copied = {"sdist_sha256": sha256(sdist)}
        elif revision:
            copied = snapshot_revision(root, revision, stage)
            identity = {"commit": copied["commit"], "source": "git-archive", "dirty": False}
        else:
            copied = snapshot(root, stage)
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
        env.update({"PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
        # A virtualenv executable is commonly a symlink. Resolving it escapes
        # the virtualenv and silently discards the explicitly selected tools.
        argv = [str(python.absolute()), "-m", "build", "--no-isolation", "--outdir", str(out / "dist")]
        if sdist:
            argv.append("--wheel")
        capture_command(out / "command", "distribution.python_build", argv, cwd=stage,
                        env=env, identity=identity, suite_id="distribution.build.v1", timeout=300)
        _, receipt, _ = read_command_run(out / "command/manifest.json")
        if receipt["exit_code"]:
            raise ValueError(f"build failed; see {out / 'command'}")
    assets = {p.name: artifact(out, p) for p in sorted((out / "dist").iterdir()) if p.is_file()}
    if len([name for name in assets if name.endswith(".whl")]) != 1:
        raise ValueError("build did not produce exactly one wheel")
    manifest = {"schema": 1, "kind": "goldmine.python-build", "identity": identity,
                "source_snapshot": copied, "from_sdist": sdist is not None,
                "command": artifact(out, out / "command/manifest.json"), "assets": assets}
    write_json(out / "manifest.json", manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--sdist", type=Path)
    parser.add_argument("--revision", help="explicit committed source, for upgrade compatibility probes")
    args = parser.parse_args(argv)
    try:
        result = build(args.out.resolve(), args.python, sdist=args.sdist, revision=args.revision)
        print(f"Python package build: PASS ({len(result['assets'])} assets)")
        return 0
    except (ValueError, OSError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print(f"Python package build: FAIL — {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
