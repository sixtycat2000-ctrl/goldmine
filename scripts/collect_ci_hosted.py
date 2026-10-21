#!/usr/bin/env python3
"""Read-only collection/replay of one explicitly selected actual GitHub CI run."""
import argparse
import json
from pathlib import Path
import sys

from acceptance_lib.evidence import InvalidEvidence
from acceptance_lib.hosted_ci import collect, verify
from acceptance_lib.runner import exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    collect_parser = modes.add_parser("collect")
    collect_parser.add_argument("--repository", required=True)
    collect_parser.add_argument("--run-id", type=int, required=True)
    collect_parser.add_argument("--run-attempt", type=int, required=True)
    collect_parser.add_argument("--head-sha", required=True)
    collect_parser.add_argument("--out", type=Path, required=True)
    replay = modes.add_parser("verify")
    replay.add_argument("--manifest", type=Path, required=True)
    replay.add_argument("--offline", action="store_true", help="structural replay only; hosted result remains MISSING")
    for command in (collect_parser, replay):
        command.add_argument("--token-env", choices=("GH_TOKEN", "GITHUB_TOKEN"), default="GH_TOKEN")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        if args.mode == "collect":
            selected = {"repository": args.repository, "run_id": args.run_id, "run_attempt": args.run_attempt, "head_sha": args.head_sha}
            manifest = collect(root, selected, args.out, token_env=args.token_env)
            # Collection and actual hosted verification have separate outcomes.
            print(json.dumps({"collection": "COMPLETE", "manifest": str(manifest), "hosted_verification": "MISSING"}))
            return 0
        status, details = verify(root, args.manifest.resolve(), online=not args.offline, token_env=args.token_env)
        print(json.dumps({"status": status, "details": details}, sort_keys=True))
        return exit_code(status)
    except (InvalidEvidence, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"hosted CI: MISSING — {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
