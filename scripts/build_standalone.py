#!/usr/bin/env python3
"""Build a self-contained macOS arm64 CLI from explicitly prepared local inputs."""
import argparse
from pathlib import Path
import subprocess
import sys
import tarfile

from native_build_lib.source import BuildError
from standalone.build import build


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "wheel", "wheelhouse", "native-tar", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        asset = build(Path(__file__).resolve().parents[1], args.runtime.resolve(), args.wheel.resolve(),
                      args.wheelhouse.resolve(), args.native_tar.resolve(), args.out.resolve())
        print(f"Standalone build: PASS — {asset}")
        return 0
    except (BuildError, OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(f"Standalone build: FAIL — {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
