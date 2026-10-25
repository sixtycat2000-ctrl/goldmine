#!/bin/bash
# Build the goldmine-embed media helper into vendor/llama.cpp/build-static/bin
# (digup-plan-002 design §7.2 + amendments B1/B2).
#
# Dev-checkout build only — the user path is the prebuilt release asset, exactly like
# scripts/build_ocr_helper.sh. Idempotent: configures cmake only when the cache is missing
# or a flag differs, rebuilds the archives only when they are stale, then always
# recompiles/links/codesigns the helper from tools/embed/goldmine-embed.c.
#
# Amendment B2 (rev2, the measured recipe): GGML_BLAS=OFF, 7 archives (no libggml-blas.a),
# Accelerate present because ggml-cpu uses vDSP unconditionally on macOS.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
src="$repo/tools/embed/goldmine-embed.c"
vc="$repo/vendor/llama.cpp"
build="$vc/build-static"
out="$build/bin/goldmine-embed"

if [ ! -f "$src" ]; then
  echo "error: $src not found (run from a goldmine checkout)" >&2
  exit 1
fi
for tool in clang clang++; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "error: $tool not found — install Apple Command Line Tools first:" >&2
    echo "  xcode-select --install" >&2
    exit 1
  fi
done

# cmake from PATH, or a uv venv fallback (DigUp build-llama.sh:34-40 convention).
if ! command -v cmake >/dev/null 2>&1; then
  if [ ! -x "$repo/vendor/cmake-venv/bin/cmake" ]; then
    uv venv --quiet "$repo/vendor/cmake-venv" --python 3.12
    uv pip install --quiet --python "$repo/vendor/cmake-venv/bin/python" cmake
  fi
  export PATH="$repo/vendor/cmake-venv/bin:$PATH"
fi

# Amendment B1: no hardcoded -G Ninja; DigUp build-llama.sh:41's conditional generator.
GENERATOR="$(command -v ninja >/dev/null 2>&1 && echo Ninja || echo 'Unix Makefiles')"

flags=(
  -DCMAKE_BUILD_TYPE=Release
  -DCMAKE_OSX_DEPLOYMENT_TARGET=14.0
  -DBUILD_SHARED_LIBS=OFF
  -DGGML_BACKEND_DL=OFF
  -DGGML_METAL=ON
  -DGGML_METAL_EMBED_LIBRARY=ON
  -DGGML_BLAS=OFF
  -DGGML_NATIVE=OFF
  -DLLAMA_BUILD_MTMD=ON
  -DLLAMA_BUILD_COMMON=OFF
  -DLLAMA_BUILD_TOOLS=OFF
  -DLLAMA_BUILD_SERVER=OFF
  -DLLAMA_BUILD_EXAMPLES=OFF
  -DLLAMA_BUILD_TESTS=OFF
  -DLLAMA_BUILD_APP=OFF
  -DLLAMA_BUILD_UI=OFF
  -DLLAMA_OPENSSL=OFF
  -DLLAMA_CURL=OFF
)

# Configure only when the cache is missing or disagrees with the frozen flag set.
configure=0
if [ ! -f "$build/CMakeCache.txt" ]; then
  configure=1
else
  if ! grep -q "^CMAKE_GENERATOR:INTERNAL=$GENERATOR\$" "$build/CMakeCache.txt"; then
    configure=1
  fi
  for f in "${flags[@]}"; do
    key="${f%%=*}"
    key="${key#-D}"   # cache lines carry no -D prefix
    value="${f#*=}"
    if ! grep -q "^$key:.*=$value\$" "$build/CMakeCache.txt"; then
      configure=1
      break
    fi
  done
fi
if [ "$configure" = 1 ]; then
  cmake -S "$vc" -B "$build" -G "$GENERATOR" "${flags[@]}"
fi

# The 7 static archives of amendment B2 (any stale libggml-blas.a in the tree is ignored).
archives=(
  "$build/src/libllama.a"
  "$build/tools/mtmd/libmtmd.a"
  "$build/vendor/hash/libvendor-hash.a"
  "$build/ggml/src/libggml.a"
  "$build/ggml/src/libggml-base.a"
  "$build/ggml/src/libggml-cpu.a"
  "$build/ggml/src/ggml-metal/libggml-metal.a"
)

# Rebuild only when an archive is missing or a cmake input is newer than the oldest one.
stale=0
for a in "${archives[@]}"; do
  if [ ! -f "$a" ]; then stale=1; break; fi
done
if [ "$stale" = 0 ] && [ "$configure" = 0 ]; then
  oldest="${archives[0]}"
  for a in "${archives[@]}"; do
    if [ "$a" -ot "$oldest" ]; then oldest="$a"; fi
  done
  if [ -n "$(find "$vc" -name CMakeLists.txt -newer "$oldest" -not -path "$build/*" -print -quit 2>/dev/null)" ]; then
    stale=1
  fi
fi
if [ "$stale" = 1 ] || [ "$configure" = 1 ]; then
  cmake --build "$build" --config Release -j "$(sysctl -n hw.ncpu)" --target llama mtmd
fi

# Compile the C separately (clang, not clang++), link the .a set with clang++ (the libs are
# C++/libc++; Accelerate is required by ggml-cpu's unconditional vDSP on macOS).
mkdir -p "$build/bin"
clang -O2 -std=c11 -c "$src" \
  -I "$vc/tools/mtmd" -I "$vc/include" -I "$vc/ggml/include" \
  -o "$build/goldmine-embed.o"
clang++ -O2 "$build/goldmine-embed.o" -o "$out" \
  "${archives[@]}" \
  -framework Foundation -framework Metal -framework Accelerate -framework CoreFoundation

# Ad-hoc codesign + the doctor handshake (same pattern as build_ocr_helper.sh).
codesign --force --sign - "$out" >/dev/null
codesign --verify --strict "$out"
"$out" --version
echo "$out"
