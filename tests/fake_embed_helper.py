#!/usr/bin/env python3
"""Fake goldmine-embed helper for OFFLINE protocol/lifecycle tests.

Installed via GOLDMINE_EMBED_HELPER=<this file>; scenario-scripted by
FAKE_MODE (default "ready"):
  ready         full happy path (ready event, ping/caps/embed answers)
  slow          ready event delayed 30 s (startup-timeout tests)
  die-at-startup  exits 2 before any output
  die-midrun    dies after the first embed request
  bad-proto     ready line is not the expected event
  bad-dim       ready event reports dim=999
  err-decode    every embed answers the helper error decode_failed
  timeout       never answers embed requests (request-timeout tests)
  no-audio      ready event reports audio=false (capability degrade)
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time

MODE = os.environ.get("FAKE_MODE", "ready")


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    if MODE == "die-at-startup":
        sys.stderr.write("fake: model load failed\n")
        return 2
    time.sleep(30.0 if MODE == "slow" else 0.0)
    dim = 999 if MODE == "bad-dim" else 768
    if MODE == "bad-proto":
        emit({"event": "hello", "proto": 1})
        return 0
    emit({
        "event": "ready", "proto": 1, "helper": "goldmine-embed 1", "dim": dim,
        "vision": True, "audio": MODE != "no-audio",
        "sample_rate": 16000 if MODE != "no-audio" else -1,
        "load_seconds": 0.01,
    })
    answered_embed = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            emit({"id": 0, "ok": False,
                  "error": {"code": "bad_request", "message": "not json"}})
            continue
        op = request.get("op")
        if op == "ping":
            emit({"id": request.get("id"), "ok": True, "pong": True})
        elif op == "caps":
            emit({"id": request.get("id"), "ok": True, "vision": True,
                  "audio": MODE != "no-audio", "sample_rate": 16000, "dim": 768})
        elif op == "quit":
            return 0
        elif op == "embed":
            if MODE == "die-midrun" and answered_embed:
                sys.stderr.write("fake: dying mid-run\n")
                return 1
            if MODE == "timeout":
                time.sleep(3600)
                return 0
            answered_embed = True
            if MODE == "err-decode":
                emit({"id": request.get("id"), "ok": False, "error": {
                    "code": "decode_failed", "message": "fake decode failure"}})
                continue
            # deterministic unit vector: 1/sqrt(768) on every axis (float32
            # bit patterns, little-endian)
            import math
            import struct

            unit = 1.0 / math.sqrt(768)
            payload = base64.b64encode(struct.pack("<768f", *([unit] * 768))).decode("ascii")
            emit({"id": request.get("id"), "ok": True, "vec_b64": payload})
        else:
            emit({"id": request.get("id"), "ok": False, "error": {
                "code": "unknown_op", "message": f"op {op!r}"}})
    return 0


if __name__ == "__main__":
    # --version handshake support
    if "--version" in sys.argv:
        print("goldmine-embed 1")
        raise SystemExit(0)
    raise SystemExit(main())
