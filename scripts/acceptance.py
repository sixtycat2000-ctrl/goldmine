#!/usr/bin/env python3
"""Run fixed acceptance profiles and revalidate their raw evidence."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from acceptance_lib.evidence import InvalidEvidence
from acceptance_lib.registry import PROFILES
from acceptance_lib.runner import exit_code, run, validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    verbs = parser.add_subparsers(dest="verb", required=True)
    execute = verbs.add_parser("run")
    execute.add_argument("--profile", choices=[*PROFILES, "release"], required=True)
    execute.add_argument("--out", type=Path, required=True)
    execute.add_argument("--task")
    execute.add_argument("--inputs", type=Path)
    execute.add_argument("--db", type=Path)
    check = verbs.add_parser("validate")
    check.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        result = run(root, args.out.resolve(), args.profile, task=args.task,
                     inputs_path=args.inputs, db=args.db) if args.verb == "run" else validate(root, args.report.resolve())
        print(f"acceptance: {result['status']}")
        return exit_code(result["status"])
    except (InvalidEvidence, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"acceptance: MISSING — {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
