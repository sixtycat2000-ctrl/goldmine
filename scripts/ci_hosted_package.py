#!/usr/bin/env python3
"""Capture the existing build/install recipe in portable, source-bound receipts."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from acceptance_lib.evidence import InvalidEvidence, artifact, capture_command, read_json, verify_artifact, write_json
from acceptance_lib.hosted_contract import committed_source, hosted_context
from acceptance_lib.hosted_replay import successful


def build(root, out):
    before = committed_source(root)
    hosted = hosted_context("package", root)
    if hosted is None:
        raise InvalidEvidence("hosted package collection requires its actual GitHub Actions environment")
    if out != root / "reports/ci":
        raise InvalidEvidence("the registered hosted output is reports/ci")
    out.mkdir(parents=True, exist_ok=False)
    commands = {}

    def command(name, tail):
        directory = out / "package-commands" / name
        capture_command(directory, "ci.package." + name, [sys.executable, *tail], cwd=root,
                        identity=before["identity"], suite_id="ci.hosted.package.v1", timeout=900)
        successful(directory / "manifest.json", suite="ci.hosted.package.v1", identity=before["identity"])
        commands[name] = artifact(out, directory / "manifest.json")

    command("build", [str(root / "scripts/build_python.py"), "--python", sys.executable, "--out", str(out / "python")])
    built = read_json(out / "python/manifest.json")
    assets = {name: verify_artifact(out / "python", ref) for name, ref in built["assets"].items()}
    wheels, sdists = ([p for name, p in assets.items() if name.endswith(suffix)] for suffix in (".whl", ".tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise InvalidEvidence("expected exactly one source wheel and sdist")
    command("sdist-build", [str(root / "scripts/build_python.py"), "--python", sys.executable, "--sdist", str(sdists[0]), "--out", str(out / "sdist")])
    rebuilt = read_json(out / "sdist/manifest.json")
    rebuilt_wheels = [verify_artifact(out / "sdist", ref) for name, ref in rebuilt["assets"].items() if name.endswith(".whl")]
    if len(rebuilt_wheels) != 1:
        raise InvalidEvidence("expected one exact sdist-rebuilt wheel")
    command("twine", ["-m", "twine", "check", str(wheels[0]), str(sdists[0]), str(rebuilt_wheels[0])])
    command("wheelhouse", ["-m", "pip", "download", "--only-binary=:all:", "--dest", str(out / "wheelhouse"), "-r", str(root / "requirements/mcp.lock")])
    smokes = {}
    for name, wheel, profile in (("install-base", wheels[0], "base"), ("install-mcp", wheels[0], "mcp"), ("install-sdist", rebuilt_wheels[0], "base")):
        command(name, [str(root / "scripts/smoke_install.py"), "--wheel", str(wheel), "--wheelhouse", str(out / "wheelhouse"), "--profile", profile, "--out", str(out / name)])
        smokes[name] = artifact(out, out / name / "smoke.json")
    after = committed_source(root)
    if after != before:
        raise InvalidEvidence("committed source changed while building/installing artifacts")
    write_json(out / "package-hosted.json", {"schema": 1, "kind": "goldmine.hosted-package", "hosted": hosted,
        "before": before, "after": after, "commands": commands,
        "build": artifact(out, out / "python/manifest.json"), "sdist_build": artifact(out, out / "sdist/manifest.json"), "smokes": smokes})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        build(root, args.out.resolve())
        return 0
    except (InvalidEvidence, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"hosted package: MISSING — {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
