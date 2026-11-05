#!/bin/sh
# Run relative to this file, including when the installation path has spaces.
set -eu
bundle=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
export SSL_CERT_FILE="${SSL_CERT_FILE:-$bundle/cacert.pem}"
exec "$bundle/python/bin/python3" -I -B -m goldmine "$@"
