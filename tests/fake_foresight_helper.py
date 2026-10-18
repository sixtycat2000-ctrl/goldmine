#!/usr/bin/env python3
"""Fake ForesightHelper: an in-process gRPC RagService for offline tests.

Serves the SAME generated proto stubs the real adapter uses
(goldmine/backends/_gen) over a Unix domain socket, with deterministic
blake2b-derived embeddings (fixed dim, default 768) — so foresight client code
is exercised end-to-end with NO live helper, NO network, NO model files.

The real helper's honest-degradation contract is mirrored: with no model
"loaded" here, Compute*Embedding still answer (the fake always has a pipeline),
but Encode/ChunkText/TruncateText return UNAVAILABLE like the live helper does
before its tokenizer is initialized (unused by goldmine; present for shape).
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

from goldmine.backends import _gen  # noqa: F401  (package presence check)
from goldmine.backends._gen import rag_service_pb2, rag_service_pb2_grpc


def deterministic_vector(seed_bytes: bytes, dim: int) -> list[float]:
    """blake2b counter-mode expansion -> dim floats in [-1, 1], L2-normalized."""
    digest = hashlib.blake2b(seed_bytes, digest_size=32).digest()
    raw = bytearray()
    counter = 0
    while len(raw) < dim * 4:
        raw.extend(
            hashlib.blake2b(digest + counter.to_bytes(4, "big"), digest_size=64).digest()
        )
        counter += 1
    ints = struct.unpack(f"<{dim}I", bytes(raw[: dim * 4]))
    vec = [(i / 0xFFFFFFFF) * 2.0 - 1.0 for i in ints]
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


class FakeForesightHelper(rag_service_pb2_grpc.RagServiceServicer):
    def __init__(self, version: str = "py-1.0.3-fake", dim: int = 768):
        self.version = version
        self.dim = dim

    def Ping(self, request, context=None):
        return rag_service_pb2.PingResponse(success=True, version=self.version)

    def ComputeEmbedding(self, request, context=None):
        vec = deterministic_vector(request.text.encode("utf-8"), self.dim)
        return rag_service_pb2.ComputeEmbeddingResponse(embedding=vec)

    def ComputeImageEmbedding(self, request, context=None):
        vec = deterministic_vector(b"img:" + request.image_bytes, self.dim)
        return rag_service_pb2.ComputeEmbeddingResponse(embedding=vec)

    def ComputeAudioEmbedding(self, request, context=None):
        vec = deterministic_vector(b"aud:" + request.audio_bytes, self.dim)
        return rag_service_pb2.ComputeEmbeddingResponse(embedding=vec)

    def Encode(self, request, context=None):
        if context is not None:
            context.set_code(5)  # UNAVAILABLE, mirrors the real pre-model helper
            context.set_details("Tokenizer not initialized")
        return rag_service_pb2.EncodeResponse()

    def ChunkText(self, request, context=None):
        if context is not None:
            context.set_code(5)
            context.set_details("Tokenizer not initialized")
        return rag_service_pb2.ChunkTextResponse()


def serve(socket_path: Path, version: str = "py-1.0.3-fake", dim: int = 768):
    """Bind a fake helper to a UDS path and start it. Returns (server, servicer).

    Tests stop it with server.stop(0). Uses grpc's ThreadPoolExecutor — grpcio
    only; the offline suite importorskips when grpc is absent.
    """
    import grpc
    from concurrent import futures

    socket_path = Path(socket_path)
    if socket_path.exists():
        socket_path.unlink()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    servicer = FakeForesightHelper(version=version, dim=dim)
    rag_service_pb2_grpc.add_RagServiceServicer_to_server(servicer, server)
    bound = server.add_insecure_port(f"unix:{socket_path}")
    if bound == 0:
        raise RuntimeError(f"could not bind UDS socket {socket_path}")
    server.start()
    return server, servicer
