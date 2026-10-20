#!/usr/bin/env python3
"""The same fixed acceptance entry used locally and by hosted CI jobs."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from acceptance_lib.evidence import InvalidEvidence, artifact, write_json
from acceptance_lib.ci_evidence import source_files
from acceptance_lib.runner import exit_code, run, validate

PROFILES = {"base": "offline", "native": "native", "mcp": "optional-mcp",
            "foresight": "optional-foresight", "live": "local_rc"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--inputs", type=Path)
    args = parser.parse_args(argv)
    if args.profile == "live" and (args.inputs is None or not args.inputs.is_file()):
        print("live CI: MISSING — provide the configured reference machine's pinned inputs file", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[1]
    try:
        before = source_files(root)
        report = run(root, args.out.resolve(), PROFILES[args.profile], inputs_path=args.inputs)
        validate(root, args.out.resolve() / "manifest.json")
        report_path = args.out.resolve() / "manifest.json"
        if report_path.is_file():
            write_json(args.out.resolve() / "ci-context.json", {"schema": 1, "profile": args.profile,
                "before": before, "after": source_files(root),
                "report": artifact(args.out.resolve(), report_path)})
        elif report["status"] == "PASS":
            raise InvalidEvidence("successful CI result lacks its raw report")
        print(f"CI {args.profile}: {report['status']}")
        return exit_code(report["status"])
    except (InvalidEvidence, OSError, ValueError, KeyError) as exc:
        print(f"CI {args.profile}: MISSING — {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
