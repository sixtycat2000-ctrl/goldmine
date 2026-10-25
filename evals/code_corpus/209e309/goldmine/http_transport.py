"""Bounded localhost HTTP for owned model processes; no redirects or proxies.

The HTTP parser reads headers and bodies through the same deadline-aware raw
reader. Socket inactivity timeouts alone cannot bound a peer that keeps sending
one byte at a time. Cancellation is checked at most every 100ms of blocked I/O.
"""

from __future__ import annotations

import http.client
import io
import ipaddress
import socket
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from .deadline import RequestTimeout, check_deadline, remaining_timeout, request_timeout

_POLL_S = 0.1
_CHUNK_BYTES = 64 * 1024
DEFAULT_MAX_BYTES = 64 * 1024 * 1024


class ResponseTooLarge(ValueError):
    """A model response exceeds the explicit memory bound."""


class _Reader(io.RawIOBase):
    def __init__(self, sock):
        super().__init__()
        self.sock = sock
        # HTTPConnection may relinquish a Connection: close socket as soon as
        # headers arrive. Keep the normal makefile ownership reference alive
        # while performing recv_into ourselves (SocketIO permanently poisons
        # its reader after an ordinary polling timeout).
        self._owner = sock.makefile("rb", buffering=0)

    def close(self):
        try:
            self._owner.close()
        finally:
            super().close()

    def readable(self):
        return True

    def readinto(self, buffer):
        while True:
            check_deadline()
            self.sock.settimeout(remaining_timeout(_POLL_S))
            try:
                count = self.sock.recv_into(buffer)
            except socket.timeout:
                check_deadline()
                continue
            check_deadline()
            return count


class _Response(http.client.HTTPResponse):
    def __init__(self, sock, *args, **kwargs):
        class ReadSocket:
            def makefile(self, mode):
                return io.BufferedReader(_Reader(sock))

        super().__init__(ReadSocket(), *args, **kwargs)


class _Connection(http.client.HTTPConnection):
    response_class = _Response

    def connect(self):
        check_deadline()
        super().connect()
        check_deadline()

    def send(self, data):
        # This transport accepts only in-memory bytes; the model clients never
        # submit streams, files or iterable request bodies.
        check_deadline()
        if self.sock is None:
            self.connect()
        view = memoryview(data)
        while view:
            check_deadline()
            self.sock.settimeout(remaining_timeout(_POLL_S))
            try:
                count = self.sock.send(view[:_CHUNK_BYTES])
            except socket.timeout:
                check_deadline()
                continue
            if count == 0:
                raise ConnectionError("localhost HTTP socket closed during send")
            view = view[count:]
        check_deadline()


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes


def request(url, *, data=None, headers=None, timeout_s=10.0, max_bytes=DEFAULT_MAX_BYTES):
    """Read one bounded response, preserving urllib HTTPError status handling.

    A per-call timeout without an expired outer request remains a conventional
    socket timeout, preserving clients' retry policy. An exhausted caller budget
    or cancellation propagates intact and must never trigger a retry/fallback.
    """
    parsed = urlsplit(url)
    try:
        local = parsed.hostname is not None and ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        local = False
    if parsed.scheme != "http" or not local or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("model HTTP transport requires a literal loopback HTTP address")
    if not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("HTTP response byte limit must be nonnegative")
    try:
        with request_timeout(timeout_s):
            connection = _Connection(parsed.hostname, parsed.port, timeout=remaining_timeout(_POLL_S))
            try:
                path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
                connection.request("POST" if data is not None else "GET", path, body=data, headers=headers or {})
                with connection.getresponse() as response:
                    if response.status >= 400:
                        # Error bodies also obey the request deadline. The
                        # caller receives an in-memory bounded HTTPError body.
                        detail = response.read(2000)
                        raise HTTPError(url, response.status, response.reason, response.headers, io.BytesIO(detail))
                    if response.length is not None and response.length > max_bytes:
                        raise ResponseTooLarge(f"model HTTP response exceeds {max_bytes} bytes")
                    chunks, size = [], 0
                    while True:
                        check_deadline()
                        part = response.read(min(_CHUNK_BYTES, max_bytes + 1 - size))
                        if not part:
                            break
                        size += len(part)
                        if size > max_bytes:
                            raise ResponseTooLarge(f"model HTTP response exceeds {max_bytes} bytes")
                        chunks.append(part)
                    check_deadline()
                    return Response(response.status, b"".join(chunks))
            except http.client.HTTPException as exc:
                check_deadline()
                raise URLError(exc) from exc
            finally:
                connection.close()
    except RequestTimeout as exc:
        # The nested per-call scope has been removed here. Only a still-live
        # outer deadline may permit the existing transient retry policy.
        check_deadline()
        raise socket.timeout("localhost HTTP call timed out") from exc
