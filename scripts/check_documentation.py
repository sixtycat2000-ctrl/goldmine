#!/usr/bin/env python3
"""Check literal local installation docs; no publication, downloads or model calls."""
import argparse
import json
from pathlib import Path
import sys

from acceptance_lib.evidence import InvalidEvidence
from documentation_lib import capture, verify

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--distribution", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--validate", type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args(argv)
    try:
        if args.validate:
            result = verify(ROOT, args.validate)
        else:
            if args.wheelhouse is None or args.distribution is None or args.out is None:
                raise InvalidEvidence("provide --wheelhouse --distribution --out; missing prepared evidence is MISSING")
            result = capture(ROOT, args.out, args.wheelhouse, args.distribution, python=args.python)
        print(json.dumps({"status": "PASS", **result}, ensure_ascii=False))
        return 0
    except (InvalidEvidence, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "MISSING", "reason": str(error), "release_eligible": False}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
