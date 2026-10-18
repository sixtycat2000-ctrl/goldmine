#!/bin/bash
# Build the goldmine-ocr Vision helper into <home>/bin (docs/v2/02 §5.2).
#
# This is the DEV FALLBACK path — the user path is the prebuilt goldmine-ocr
# release asset (A5/B13: a stock Mac without Command Line Tools has no other
# fix path). goldmine never needs this script at runtime; the pipeline's
# compile-on-demand (goldmine/ocr.py) performs the same swiftc invocation.
#
# Usage: scripts/build_ocr_helper.sh [DEST]
#   DEST defaults to $GOLDMINE_HOME/bin/goldmine-ocr
#   (default home: ~/Library/Application Support/goldmine)
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
source_swift="$repo/tools/ocr/ocr_helper.swift"

home="${GOLDMINE_HOME:-$HOME/Library/Application Support/goldmine}"
dest="${1:-$home/bin/goldmine-ocr}"

if [ ! -f "$source_swift" ]; then
  echo "error: $source_swift not found (run from a goldmine checkout)" >&2
  exit 1
fi

if ! command -v swiftc >/dev/null 2>&1; then
  echo "error: swiftc not found — install Apple Command Line Tools first:" >&2
  echo "  xcode-select --install" >&2
  echo "(or use the prebuilt release asset: no dev tools needed)" >&2
  exit 1
fi

mkdir -p "$(dirname "$dest")"
tmp="$dest.tmp.$$"
trap 'rm -f "$tmp"' EXIT

# ~1.4 s on an M4 via CommandLineTools swiftc (02 §5.2 probe).
swiftc -O "$source_swift" -o "$tmp"
mv "$tmp" "$dest"
trap - EXIT

echo "$dest"
"$dest" --version
