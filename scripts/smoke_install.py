#!/usr/bin/env python3
"""Install an actual wheel outside the checkout using a prepared local wheelhouse."""
import argparse
from pathlib import Path
import sys

from distribution_lib import smoke
from native_build_lib.source import BuildError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--profile", choices=("base", "mcp"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--native-tar", type=Path)
    parser.add_argument("--model", type=Path)
    args = parser.parse_args(argv)
    try:
        result = smoke(args.wheel.resolve(), args.wheelhouse.resolve(), args.out.resolve(),
                       args.profile, native_tar=args.native_tar, model=args.model)
        print(f"installation smoke: PASS ({result['profile']})")
        return 0
    except (BuildError, OSError, ValueError) as exc:
        print(f"installation smoke: FAIL — {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
