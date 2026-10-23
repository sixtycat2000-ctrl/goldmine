#!/usr/bin/env python3
"""Bind exact distribution build/install reports and recheck their raw evidence."""
import argparse
from pathlib import Path
import sys

from acceptance_lib.delivery import verify_bundle
from acceptance_lib.evidence import InvalidEvidence, capture_command, read_json, sha256, verify_artifact, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("build", "sdist-build", "base", "mcp", "rebuilt", "upgrade", "native", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable), help="prepared build/twine virtualenv executable")
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error("output must not overwrite earlier evidence")
    refs = {name: {"manifest_path": str(getattr(args, name).resolve()), "sha256": sha256(getattr(args, name))}
            for name in ("build", "sdist_build", "base", "mcp", "rebuilt", "upgrade", "native")}
    artifacts = []
    for manifest_path in (args.build, args.sdist_build):
        manifest = read_json(manifest_path)
        artifacts.extend(str(verify_artifact(manifest_path.parent, ref)) for ref in manifest["assets"].values())
    directory = args.out.resolve().parent / (args.out.stem + "-twine")
    capture_command(directory, "distribution.twine", [str(args.python.absolute()), "-m", "twine", "check", *artifacts],
                    cwd=args.out.resolve().parent, suite_id="distribution.twine.v1")
    refs["twine"] = {"manifest_path": str(directory / "manifest.json"), "sha256": sha256(directory / "manifest.json")}
    write_json(args.out, {"schema": 1, "kind": "goldmine.distribution-bundle", "evidence": refs})
    try:
        result = verify_bundle(args.out)
        print(f"distribution: PASS ({result['version']})")
        return 0
    except (InvalidEvidence, ValueError, OSError, KeyError, TypeError) as exc:
        print(f"distribution: MISSING — {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
