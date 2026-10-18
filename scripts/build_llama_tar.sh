#!/usr/bin/env bash
# Build the pinned llama-server into a release tar (v2 item 4 / A10, 05 §4.7).
#
# ONE recipe shared by CI (.github/workflows/build-release.yml) and local dev
# builds so the two can never drift. What it does:
#   1. obtain llama.cpp at the pinned tag (clone, or reuse an existing checkout
#      — the repo's vendor/llama.cpp by default)
#   2. apply scripts/patches/llama.cpp-no-logits.patch (EmbeddingGemma's
#      non-causal embedding context must not reserve logits: ~1 MiB/token of
#      Gemma's 262k vocab otherwise — copied from DigUp, kept in-repo so the
#      workflow is self-contained)
#   3. cmake Release + Metal, server only (no tests/examples/tools)
#   4. ad-hoc codesign, smoke-run --version
#   5. compile the goldmine-ocr Vision helper (swiftc -O; a macos-14 runner
#      has the CLT) into the same dist/ staging dir (review F8)
#   6. tar.xz llama-server + goldmine-ocr to
#      dist/llama-server-macos-arm64-<tag>.tar.xz
#
# Usage:
#   scripts/build_llama_tar.sh [workdir]
#     workdir  llama.cpp checkout dir (default: <repo>/vendor/llama.cpp —
#              building there also produces vendor/llama.cpp/build/bin/
#              llama-server, the path llama.py's "vendor" resolution expects;
#              CI passes a scratch dir instead)
# Env:
#   LLAMA_TAG          tag to build        (default b11461 — pinned lineage)
#   LLAMA_REPO         source repo         (default ggml-org/llama.cpp)
#   GOLDMINE_SKIP_OCR  =1 skips the goldmine-ocr compile (local-only escape;
#                      a RELEASE tar must contain the helper — stock Macs
#                      without Command Line Tools have no other fix path)

set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
tag="${LLAMA_TAG:-b11461}"
repo="${LLAMA_REPO:-https://github.com/ggml-org/llama.cpp}"
workdir="${1:-$repo_root/vendor/llama.cpp}"
patch_file="$repo_root/scripts/patches/llama.cpp-no-logits.patch"
out_dir="${OUT_DIR:-$repo_root/dist}"

echo "== goldmine llama-server build: tag $tag, workdir $workdir" >&2

if [ ! -f "$patch_file" ]; then
  echo "FATAL: patch missing: $patch_file" >&2
  exit 1
fi

mkdir -p "$(dirname "$workdir")"
if [ ! -d "$workdir/.git" ]; then
  git clone --depth 1 --branch "$tag" "$repo" "$workdir"
else
  current="$(git -C "$workdir" describe --tags --always 2>/dev/null || echo unknown)"
  if [ "$current" != "$tag" ]; then
    echo "WARNING: existing checkout at $workdir is $current, not $tag — continuing with it" >&2
  fi
fi

# Apply the no-logits patch (idempotent: skip when already applied).
if git -C "$workdir" apply --check "$patch_file" 2>/dev/null; then
  git -C "$workdir" apply "$patch_file"
  echo "== patch applied: llama.cpp-no-logits" >&2
else
  echo "== patch not applicable (already applied?) — continuing" >&2
fi

cmake -S "$workdir" -B "$workdir/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_METAL=ON \
  -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_BUILD_EXAMPLES=OFF \
  -DLLAMA_BUILD_SERVER=ON \
  -DLLAMA_CURL=OFF
cmake --build "$workdir/build" --target llama-server -- -j"$(sysctl -n hw.ncpu)"

bin="$workdir/build/bin/llama-server"
if [ ! -x "$bin" ]; then
  echo "FATAL: build did not produce $bin" >&2
  exit 1
fi

# Ad-hoc signature (05 §4.7: no identity, we have none to sign with). Absence
# of codesign (non-macOS) is not fatal.
codesign --force --sign - "$bin" 2>/dev/null || echo "== codesign skipped" >&2

"$bin" --version || true

# ---- goldmine-ocr (v2 item B4 / review F8) --------------------------------
# Compile the Vision OCR helper into the same staging dir so ONE release tar
# carries both binaries. A stock Mac without Command Line Tools has no other
# fix path (A5/B13) — the prebuilt helper in this tar IS that fix, so a
# release build refuses to produce a tar without it (GOLDMINE_SKIP_OCR=1 is
# the local-only escape hatch).
ocr_src="$repo_root/tools/ocr/ocr_helper.swift"
staging="$out_dir/staging"
mkdir -p "$staging"
cp "$bin" "$staging/llama-server"

if [ -f "$ocr_src" ] && command -v swiftc >/dev/null 2>&1; then
  echo "== building goldmine-ocr (Vision OCR helper)" >&2
  # ~1.4 s on an M4 via CommandLineTools swiftc (02 §5.2 probe); the exact
  # invocation goldmine/ocr.py uses for the compile-on-demand path
  swiftc -O "$ocr_src" -o "$staging/goldmine-ocr"
  codesign --force --sign - "$staging/goldmine-ocr" 2>/dev/null \
    || echo "== codesign skipped (goldmine-ocr)" >&2
  "$staging/goldmine-ocr" --version >&2
elif [ "${GOLDMINE_SKIP_OCR:-0}" = "1" ]; then
  echo "== goldmine-ocr SKIPPED (GOLDMINE_SKIP_OCR=1) — tar ships without it" >&2
else
  echo "FATAL: swiftc not found — cannot build goldmine-ocr." >&2
  echo "       Install Apple Command Line Tools (xcode-select --install), or" >&2
  echo "       set GOLDMINE_SKIP_OCR=1 for a llama-server-only dev build." >&2
  exit 1
fi

mkdir -p "$out_dir"
out="$out_dir/llama-server-macos-arm64-$tag.tar.xz"
# tar from the staging dir so the archive contains plain top-level entries:
# llama-server + goldmine-ocr
entries=(llama-server)
if [ -f "$staging/goldmine-ocr" ]; then
  entries+=(goldmine-ocr)
fi
tar -C "$staging" -cJf "$out" "${entries[@]}"
echo "== wrote $out ($(du -h "$out" | cut -f1); entries: ${entries[*]})" >&2
echo "$out"
