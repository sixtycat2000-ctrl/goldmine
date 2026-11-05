#!/bin/sh
# Goldmine: verified user-space install; no system Python or Homebrew required.
set -eu
fail() { printf 'Goldmine: %s\n' "$*" >&2; exit 1; }
usage() {
    cat <<'EOF'
Goldmine installer — macOS 14+ / Apple Silicon
  sh install.sh [--model /path/to/pinned.gguf] [--skip-model] [--no-path]
  sh install.sh --archive /path/to/goldmine-macos-arm64.tar.gz --sha256 HEX [options]
  sh install.sh --uninstall

The default install prepares the 310 MB embedding model, checks the native
engine, and adds goldmine to PATH for new terminals. --skip-model is keyword-only.
--no-path leaves shell profiles unchanged. Existing config/indexes are preserved.
GOLDMINE_VERSION selects a release tag; otherwise use the latest public release.
GOLDMINE_INSTALL_DIR / GOLDMINE_BIN_DIR override the user-space install locations.
EOF
}
archive= expected= model= skip= no_path= uninstall=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --archive|--sha256|--model)
            [ "$#" -ge 2 ] || fail "$1 requires a value"
            case "$1" in --archive) archive=$2 ;; --sha256) expected=$2 ;; --model) model=$2 ;; esac
            shift 2 ;;
        --skip-model) skip=1; shift ;;
        --no-path) no_path=1; shift ;;
        --uninstall) uninstall=1; shift ;;
        *) fail "unknown option: $1 (see --help)" ;;
    esac
done
[ "$(uname -s)" = Darwin ] || fail "requires macOS 14+ on Apple Silicon"
[ "$(uname -m)" = arm64 ] || fail "requires Apple Silicon; Intel/Rosetta is unsupported"
major=$(/usr/bin/sw_vers -productVersion | /usr/bin/cut -d. -f1)
[ "$major" -ge 14 ] || fail "requires macOS 14 or newer"
[ -z "$model" ] || [ -z "$skip" ] || fail "choose --model or --skip-model"
prefix=${GOLDMINE_INSTALL_DIR:-$HOME/.local/share/goldmine}
if [ -n "$uninstall" ]; then
    [ -f "$prefix/current/install.py" ] || fail "no managed installation at $prefix"
    exec "$prefix/current/python/bin/python3" -I -B "$prefix/current/install.py" --uninstall
fi
# A prepared download folder is installable with just `sh install.sh`.
if [ -z "$archive" ] && [ -z "$expected" ] && [ -f "$0" ]; then
    installer_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
    if [ -f "$installer_dir/goldmine-macos-arm64.tar.gz" ] && [ -f "$installer_dir/SHA256SUMS" ]; then
        archive=$installer_dir/goldmine-macos-arm64.tar.gz
        expected=$(/usr/bin/awk '$2 == "goldmine-macos-arm64.tar.gz" {print $1}' "$installer_dir/SHA256SUMS")
    fi
fi
stage=$(mktemp -d "${TMPDIR:-/tmp}/goldmine-install.XXXXXX")
trap 'rm -rf "$stage"' EXIT HUP INT TERM
if [ -z "$archive" ]; then
    [ -z "$expected" ] || fail "--sha256 requires --archive"
    version=${GOLDMINE_VERSION:-latest}
    case "$version" in *[!a-zA-Z0-9._-]*|'') fail "invalid GOLDMINE_VERSION" ;; esac
    if [ "$version" = latest ]; then route=latest/download; else route=download/$version; fi
    base=https://github.com/sixtycat2000-ctrl/goldmine/releases/$route
    printf 'Downloading Goldmine from %s\n' "$base"
    /usr/bin/curl --fail --show-error --location --proto '=https' --proto-redir '=https' \
        "$base/SHA256SUMS" -o "$stage/SHA256SUMS" || fail "release not available; use a prepared local archive (see docs/INSTALL.md)"
    expected=$(/usr/bin/awk '$2 == "goldmine-macos-arm64.tar.gz" { print $1 }' "$stage/SHA256SUMS")
    archive=$stage/goldmine-macos-arm64.tar.gz
    /usr/bin/curl --fail --show-error --location --proto '=https' --proto-redir '=https' \
        "$base/goldmine-macos-arm64.tar.gz" -o "$archive"
fi
[ -f "$archive" ] || fail "archive not found: $archive"
[ "${#expected}" -eq 64 ] || fail "provide one 64-character --sha256 from the build's SHA256SUMS"
case "$expected" in *[!0-9a-f]*) fail "invalid SHA256" ;; esac
actual=$(/usr/bin/shasum -a 256 "$archive")
actual=${actual%% *}
[ "$actual" = "$expected" ] || fail "archive SHA256 mismatch; nothing installed"
# Reject unexpected top-level paths before extraction. bsdtar also rejects traversal.
/usr/bin/tar -tzf "$archive" > "$stage/members"
/usr/bin/awk '$0 !~ /^goldmine(\/|$)/ || $0 ~ /(^|\/)\.\.(\/|$)/ { bad=1 } END { exit bad }' "$stage/members" \
    || fail "archive contains an unsafe path"
/usr/bin/tar -xzf "$archive" --no-same-owner -p -C "$stage"
set --
[ -z "$model" ] || set -- "$@" --model "$model"
[ -z "$skip" ] || set -- "$@" --skip-model
[ -z "$no_path" ] || set -- "$@" --no-path
"$stage/goldmine/python/bin/python3" -I -B "$stage/goldmine/install.py" "$@"
