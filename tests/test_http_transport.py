"""A peer sending frequent bytes still cannot extend a request deadline."""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from goldmine.backends.llama import LlamaServerBackend
from goldmine.config import LlamaCfg
from goldmine.deadline import RequestTimeout, request_cancellation, request_timeout
from goldmine.http_transport import ResponseTooLarge, request
from goldmine.rerank import LocalReranker


class Peer(ThreadingHTTPServer):
    daemon_threads = True
    mode = "normal"
    body = b'{"tokens": [1, 2]}'
    status = 200


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.serve()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.serve()

    def drip(self, data):
        for char in data:
            self.connection.sendall(bytes([char]))
            time.sleep(.02)

    def serve(self):
        self.server.calls += 1
        body, mode = self.server.body, self.server.mode
        header = (f"HTTP/1.1 {self.server.status} Fixture\r\n"
                  + ("Transfer-Encoding: chunked\r\n" if mode == "chunked" else f"Content-Length: {len(body)}\r\n")
                  + "Connection: close\r\n\r\n").encode()
        try:
            if mode == "headers":
                self.drip(header)
                self.connection.sendall(body)
            else:
                self.connection.sendall(header)
                if mode == "body":
                    self.drip(body)
                elif mode == "chunked":
                    for char in body:
                        self.connection.sendall(b"1\r\n" + bytes([char]) + b"\r\n")
                        time.sleep(.02)
                    self.connection.sendall(b"0\r\n\r\n")
                else:
                    self.connection.sendall(body)
        except (BrokenPipeError, ConnectionResetError):
            self.server.disconnected.set()


@pytest.fixture
def peer():
    server = Peer(("127.0.0.1", 0), Handler)
    server.calls = 0
    server.disconnected = threading.Event()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    yield server, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=1)
    assert not thread.is_alive()


@pytest.mark.parametrize("phase", ["headers", "body", "chunked"])
def test_outer_absolute_deadline_bounds_slow_drip_and_closes_socket(peer, phase):
    server, url = peer
    server.mode = phase
    started = time.monotonic()
    with pytest.raises(RequestTimeout):
        with request_timeout(.08):
            request(url, data=b"{}", timeout_s=2)
    assert time.monotonic() - started < .3
    assert server.disconnected.wait(.4)
    assert server.calls == 1


@pytest.mark.parametrize("phase", ["headers", "body", "chunked"])
def test_per_call_limit_without_outer_budget_preserves_socket_timeout(peer, phase):
    server, url = peer
    server.mode = phase
    started = time.monotonic()
    with pytest.raises(socket.timeout) as caught:
        request(url, timeout_s=.08)
    assert not isinstance(caught.value, RequestTimeout)
    assert time.monotonic() - started < .3
    assert server.disconnected.wait(.4)


@pytest.mark.parametrize("phase", ["headers", "body", "chunked"])
def test_cooperative_cancel_interrupts_parser_and_leaves_no_blocked_worker(peer, phase):
    class Cancelled(BaseException):
        pass

    server, url = peer
    server.mode = phase
    cancelled = threading.Event()

    def check():
        if cancelled.is_set():
            raise Cancelled

    timer = threading.Timer(.06, cancelled.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(Cancelled):
            with request_cancellation(check):
                request(url, timeout_s=2)
    finally:
        timer.cancel()
        timer.join(timeout=1)
    assert time.monotonic() - started < .3
    assert server.disconnected.wait(.4)
    assert server.calls == 1


def test_success_error_status_body_limit_and_request_scope_reset(peer):
    server, url = peer
    server.body = json.dumps({"text": "中文資料"}, ensure_ascii=False).encode()
    assert json.loads(request(url).body) == {"text": "中文資料"}
    with pytest.raises(ResponseTooLarge):
        request(url, max_bytes=1)
    server.status, server.body = 503, b"temporary model error"
    with pytest.raises(HTTPError) as caught:
        request(url)
    assert caught.value.code == 503 and caught.value.read() == b"temporary model error"
    server.status = 200
    # A previous timeout or protocol error cannot poison the next operation.
    assert request(url).status == 200


@pytest.mark.parametrize("url", ["https://127.0.0.1:1/", "http://example.com/", "http://127.0.0.1:1/#fragment"])
def test_nonlocal_redirect_or_proxy_destinations_are_not_model_endpoints(url):
    with pytest.raises(ValueError, match="loopback"):
        request(url)


@pytest.mark.parametrize("phase", ["headers", "body"])
@pytest.mark.parametrize("operation", ["embedding", "tokenize", "rerank", "health"])
def test_all_model_paths_use_total_deadline_without_retry_or_late_spawn(peer, phase, operation):
    server, url = peer
    server.mode = phase
    backend = LlamaServerBackend(LlamaCfg(request_timeout=2))
    backend._port, backend._started = server.server_port, True
    existing_process = SimpleNamespace(poll=lambda: None)
    backend._proc = existing_process
    ranker = LocalReranker(None, Path("/nonexistent-model"))
    ranker._port = server.server_port
    started = time.monotonic()
    with pytest.raises(RequestTimeout):
        with request_timeout(.08):
            if operation == "embedding":
                backend.embed_texts(["needle"])
            elif operation == "tokenize":
                backend.count_tokens(["needle"])
            elif operation == "health":
                backend._port_has_foreign_server(server.server_port)
            else:
                ranker._post("v1/rerank", {})
    assert time.monotonic() - started < .3
    assert server.calls == 1 and backend._proc is existing_process and ranker._proc is None
