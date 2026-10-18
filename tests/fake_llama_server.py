#!/usr/bin/env python3
"""Fake llama-server: stdlib http.server mimicking the subset of the real
llama-server HTTP API that goldmine's client uses (tier-2 offline tests).

Endpoints:
  GET  /health          -> 200 {"status":"ok"} once serving; 503 for the first
                           --health-fail N requests (startup-timeout path)
  GET  /stats           -> JSON counters (embed_requests, embed_texts, ...)
  POST /v1/embeddings   -> OpenAI-style {"data":[{"index","embedding"},...]};
                           embeddings derive deterministically from blake2b of
                           the input text (fixed --dim, default 768); the first
                           --embed-fail N requests return 500 (error path)
  POST /tokenize        -> {"tokens":[ids...]} or, with with_pieces, pieces
                           exactly like the real b11461 handler (verified live:
                           SPM-style pieces where a leading space belongs to
                           the FOLLOWING token; byte-fallback groups come back
                           as [b0,b1,...] arrays). add_special=true prepends
                           id 2 and appends id 1 (gemma BOS/EOS). The first
                           --tokenize-fail N requests return 500 (fallback
                           path).

It ACCEPTS the llama-server flags goldmine spawns with (--embeddings --pooling
mean -m <model> -c <ctx> -np <n> --host <h> --port <p>) and ignores the ones it
does not care about, so tests can point LlamaCfg.server at this file and
exercise the real spawn/health/batching code path. Extra test-only flags:
  --dim N            embedding width (default 768)
  --wrong-dim N      shorthand for --dim N != 768 (dim-assertion-at-start path)
  --health-fail N    first N /health requests return 503
  --embed-fail N     first N /v1/embeddings requests return 500
  --tokenize-fail N  first N /tokenize requests return 500
  --delay SECONDS    sleep before each embeddings response (deadline path)
  --crash-on-start   print a message to stderr and exit(1) immediately

No network beyond 127.0.0.1; no real model; no llama.cpp.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS: dict = {}
STATS: dict = {"health": 0, "embed_requests": 0, "embed_texts": 0,
               "embed_failures": 0, "tokenize_requests": 0,
               "tokenize_failures": 0}
EMBED_FAILS_LEFT: list[int] = [0]
TOKENIZE_FAILS_LEFT: list[int] = [0]


def deterministic_embedding(text: str, dim: int) -> list[float]:
    """blake2b-of-text -> `dim` floats in [-1, 1], L2-normalized. Stable across
    runs and machines (never Python hash())."""
    seed = hashlib.blake2b(text.encode("utf-8"), digest_size=32).digest()
    raw = bytearray()
    counter = 0
    while len(raw) < dim * 4:
        raw.extend(
            hashlib.blake2b(seed + counter.to_bytes(4, "big"), digest_size=64).digest()
        )
        counter += 1
    ints = struct.unpack(f"<{dim}I", bytes(raw[: dim * 4]))
    vec = [(i / 0xFFFFFFFF) * 2.0 - 1.0 for i in ints]
    norm = sum(v * v for v in vec) ** 0.5
    if norm > 0.0:
        vec = [v / norm for v in vec]
    return vec


def fake_tokenize(content: str) -> list:
    """Deterministic SPM-shaped pieces, mimicking the real /tokenize response
    (verified live against the vendored b11461 server on the pinned model):

    - ASCII runs split into <=5-char pieces; a space glues to the FOLLOWING
      piece ("hello world" -> ["hello", " world"])
    - newline runs are their own piece ("\n", "\n\n")
    - non-ASCII chars come back as BYTE-ARRAY pieces ([233,155,136]) exactly
      like the server's invalid-utf8 fallback for byte tokens

    Concatenating the pieces reproduces the content byte for byte.
    """
    pieces: list = []
    cur = ""
    i = 0
    while i < len(content):
        ch = content[i]
        if ch == "\n":
            if cur:
                pieces.append(cur)
                cur = ""
            j = i
            while j < len(content) and content[j] == "\n":
                j += 1
            pieces.append(content[i:j])
            i = j
            continue
        if ord(ch) > 127:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(list(ch.encode("utf-8")))
            i += 1
            continue
        cur += ch
        if len(cur) >= 5:
            pieces.append(cur)
            cur = ""
        i += 1
    if cur:
        pieces.append(cur)
    return pieces


def _piece_id(piece, index: int) -> int:
    data = piece.encode("utf-8") if isinstance(piece, str) else bytes(piece)
    digest = hashlib.blake2b(data + index.to_bytes(4, "big"), digest_size=8).digest()
    return 10 + int.from_bytes(digest, "big") % 200000


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence per-request stderr noise
        pass

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            STATS["health"] += 1
            if ARGS["health_fail"] and STATS["health"] <= ARGS["health_fail"]:
                self._send_json(503, {"error": {"message": "loading model"}})
                return
            self._send_json(200, {"status": "ok"})
            return
        if self.path == "/stats":
            self._send_json(200, STATS)
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path == "/control":
            # test-fixture control plane: inject faults into the RUNNING server
            # (the startup dim-probe embed has already happened by then, so
            # mid-run fault tests are deterministic)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                control = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": {"message": "bad json"}})
                return
            if "embed_fail" in control:
                EMBED_FAILS_LEFT[0] = int(control["embed_fail"])
            if "delay" in control:
                ARGS["delay"] = float(control["delay"])
            self._send_json(200, STATS)
            return
        if self.path == "/tokenize":
            STATS["tokenize_requests"] += 1
            if TOKENIZE_FAILS_LEFT[0] > 0:
                TOKENIZE_FAILS_LEFT[0] -= 1
                STATS["tokenize_failures"] += 1
                self._send_json(500, {"error": {"message": "synthetic tokenize failure"}})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                request = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": {"message": "bad json"}})
                return
            content = str(request.get("content", ""))
            add_special = bool(request.get("add_special", False))
            with_pieces = bool(request.get("with_pieces", False))
            pieces = fake_tokenize(content)
            if with_pieces:
                tokens = [
                    {
                        "id": _piece_id(p, i),
                        "piece": p if isinstance(p, str) else list(p),
                    }
                    for i, p in enumerate(pieces)
                ]
                if add_special:  # mimic gemma BOS/EOS pieces (never used by the client)
                    tokens = [{"id": 2, "piece": "<bos>"}] + tokens + [{"id": 1, "piece": "<eos>"}]
            else:
                tokens = [_piece_id(p, i) for i, p in enumerate(pieces)]
                if add_special:
                    tokens = [2] + tokens + [1]  # gemma BOS=2, EOS=1
            self._send_json(200, {"tokens": tokens})
            return
        if self.path != "/v1/embeddings":
            self._send_json(404, {"error": {"message": "not found"}})
            return
        STATS["embed_requests"] += 1
        if EMBED_FAILS_LEFT[0] > 0:
            EMBED_FAILS_LEFT[0] -= 1
            STATS["embed_failures"] += 1
            self._send_json(500, {"error": {"message": "synthetic embed failure"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": {"message": "bad json"}})
            return
        texts = request.get("input", [])
        if isinstance(texts, str):
            texts = [texts]
        reject = ARGS.get("reject_substr")
        if reject and any(reject in str(t) for t in texts):
            self._send_json(400, {
                "error": {
                    "type": "exceed_context_size_error",
                    "message": "input is larger than the max context size. skipping",
                }
            })
            return
        if ARGS["delay"] > 0:
            time.sleep(ARGS["delay"])
        STATS["embed_texts"] += len(texts)
        data = [
            {
                "object": "embedding",
                "index": i,
                "embedding": deterministic_embedding(str(text), ARGS["dim"]),
            }
            for i, text in enumerate(texts)
        ]
        self._send_json(
            200,
            {
                "object": "list",
                "model": "fake-embedding-model",
                "data": data,
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    # mimic llama-server's version banner (real one prints to stderr, exit 0)
    parser.add_argument(
        "--version", action="version",
        version="version: 0.0.1 (build 0, commit fake) — fake_llama_server",
    )
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--dim", type=int, default=768)
    parser.add_argument("--wrong-dim", type=int, default=None)
    parser.add_argument("--health-fail", type=int, default=0)
    parser.add_argument("--embed-fail", type=int, default=0)
    parser.add_argument("--tokenize-fail", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--crash-on-start", action="store_true")
    parser.add_argument("--reject-substr", default="",
                        help="reject /v1/embeddings whose input contains this "
                             "substring with a 400 exceed_context error")
    # llama-server flags goldmine passes; parsed so help/abbrev never trips,
    # then ignored.
    parser.add_argument("--embeddings", action="store_true")
    parser.add_argument("--pooling", default=None)
    parser.add_argument("-m", "--model", default=None)
    parser.add_argument("-c", "--ctx-size", dest="ctx", default=None)
    parser.add_argument("-np", "--parallel", dest="parallel", default=None)
    args, _unknown = parser.parse_known_args()

    ARGS.update({
        "health_fail": args.health_fail,
        "dim": args.wrong_dim if args.wrong_dim is not None else args.dim,
        "delay": args.delay,
        "reject_substr": args.reject_substr,
    })
    EMBED_FAILS_LEFT[0] = args.embed_fail
    TOKENIZE_FAILS_LEFT[0] = args.tokenize_fail

    if args.crash_on_start:
        print("fake llama-server: crash-on-start requested", file=sys.stderr)
        return 1

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"fake llama-server listening on {args.host}:{args.port}", file=sys.stderr)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
