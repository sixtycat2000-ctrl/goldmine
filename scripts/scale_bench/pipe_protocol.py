"""Bounded JSON-line I/O under one absolute request/startup deadline."""
from __future__ import annotations

import json
import os
import select
import time

from .output import json_value

MAX_LINE_BYTES = 1 << 20


class JsonLines:
    def __init__(self, stdout, stdin, log):
        self.stdout, self.stdin, self.log = stdout, stdin, log
        self.pending = bytearray()
        os.set_blocking(stdout.fileno(), False)
        os.set_blocking(stdin.fileno(), False)

    @staticmethod
    def remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("owned warm CLI worker timed out before a complete response")
        return remaining

    def receive(self, deadline):
        while True:
            self.remaining(deadline)
            end = self.pending.find(b"\n")
            if end >= 0:
                if end + 1 > MAX_LINE_BYTES:
                    raise ValueError("warm CLI protocol line exceeds its fixed byte bound")
                line = bytes(self.pending[:end + 1])
                del self.pending[:end + 1]
                return json_value(line)
            if len(self.pending) >= MAX_LINE_BYTES:
                raise ValueError("warm CLI protocol line exceeds its fixed byte bound")
            if not select.select([self.stdout], [], [], self.remaining(deadline))[0]:
                raise TimeoutError("owned warm CLI worker timed out before a complete response")
            try:
                chunk = os.read(self.stdout.fileno(), min(65536, MAX_LINE_BYTES - len(self.pending)))
            except BlockingIOError:
                continue
            if not chunk:
                raise ValueError("owned warm CLI worker exited before a complete response")
            self.log.write(chunk)
            self.log.flush()
            self.pending.extend(chunk)

    def send(self, value, deadline):
        raw = (json.dumps(value) + "\n").encode()
        if len(raw) > MAX_LINE_BYTES:
            raise ValueError("warm CLI request exceeds its fixed byte bound")
        sent = 0
        while sent < len(raw):
            if not select.select([], [self.stdin], [], self.remaining(deadline))[1]:
                raise TimeoutError("owned warm CLI worker timed out while sending request")
            try:
                count = os.write(self.stdin.fileno(), raw[sent:])
            except BlockingIOError:
                continue
            if count == 0:
                raise BrokenPipeError("owned warm CLI worker closed its request pipe")
            sent += count
