"""Fixed JSONL stdin bridge. exec keeps the capture owner's PID/process group."""
from __future__ import annotations

import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    if not args.input.is_absolute() or not args.binary.is_absolute() or args.input.is_symlink():
        parser.error("only explicit local owned input and pinned binary paths are allowed")
    with args.input.open("rb") as source:
        os.dup2(source.fileno(), 0)
        os.set_inheritable(0, True)
        os.execv(str(args.binary), [str(args.binary)])


if __name__ == "__main__":
    main()
