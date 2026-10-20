#!/usr/bin/env bash
# Shared local/CI recipe: pinned source, exact patch, fresh static build and tar.
# OUT_DIR and GOLDMINE_SKIP_OCR=1 retain their existing meanings. Skipping OCR
# creates an explicitly dev-only asset that cannot satisfy release acceptance.
set -euo pipefail
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$repo_root/scripts/build_native.py" "$@"
