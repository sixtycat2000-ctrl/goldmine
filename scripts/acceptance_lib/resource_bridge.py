"""Fixed same-PID exec bridge: set child-only NOFILE, prove it, await observer."""
from __future__ import annotations

import hashlib
import json
import os
import resource
import select
import sys


def main(argv):
    if len(argv) < 8 or argv[0] != "--policy" or argv[2] != "--start-fd" or argv[4] != "--ready-fd" or argv[6] != "--":
        raise ValueError("fixed resource bridge argv required")
    spec_path, start_fd, ready_fd, inner = argv[1], int(argv[3]), int(argv[5]), argv[7:]
    if min(start_fd, ready_fd) <= 2 or start_fd == ready_fd or not inner:
        raise ValueError("invalid inherited bridge descriptors/argv")
    with open(spec_path, "rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("bridge specification too large")
    spec = json.loads(raw)
    if (not isinstance(spec, dict) or set(spec) != {"schema", "nofile", "inner_argv", "nonce"}
            or spec["schema"] != 1 or spec["inner_argv"] != inner
            or type(spec["nofile"]) is not int or not 16 <= spec["nofile"] <= 512
            or not isinstance(spec["nonce"], str) or len(spec["nonce"]) != 64
            or any(c not in "0123456789abcdef" for c in spec["nonce"])):
        raise ValueError("invalid bridge specification")
    resource.setrlimit(resource.RLIMIT_NOFILE, (spec["nofile"], spec["nofile"]))
    ready = {"schema": 1, "pid": os.getpid(), "pgid": os.getpgrp(), "sid": os.getsid(0),
             "nofile": list(resource.getrlimit(resource.RLIMIT_NOFILE)),
             "spec_sha256": hashlib.sha256(raw).hexdigest(), "nonce": spec["nonce"]}
    frame = json.dumps(ready, sort_keys=True).encode() + b"\n"
    if os.write(ready_fd, frame) != len(frame):
        raise OSError("partial bridge ready frame")
    os.close(ready_fd)
    received = b""
    while b"\n" not in received:
        if not select.select([start_fd], [], [], 5)[0]:
            raise TimeoutError("parent observer did not release bridge")
        part = os.read(start_fd, 128)
        if not part or len(received) + len(part) > 128:
            raise ValueError("invalid bridge release frame")
        received += part
    os.close(start_fd)
    if received != spec["nonce"].encode() + b"\n":
        raise ValueError("bridge release nonce mismatch")
    os.execvpe(inner[0], inner, os.environ)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (OSError, ValueError, TimeoutError) as exc:
        print(f"resource bridge refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
