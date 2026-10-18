"""ForesightBackend — gRPC-over-UDS adapter to the LIVE ForesightHelper.

Optional experimental backend (ARCHITECTURE §4): we are a CLIENT of a helper we
do not own — no lifecycle management, no spawn, Ping() for readiness+identity.

Proto: research.xeno.labs.foresight.launch_agent.proto/RagService (recovered
from the helper binary; generated stubs are committed under
goldmine/backends/_gen — regenerate per the docstring there). We use 4 of the
11 RPCs: Ping, ComputeEmbedding, ComputeImageEmbedding, ComputeAudioEmbedding.

Identity: the helper's model files cannot be hashed by us, so `Ping().version`
takes the model-identity slot in the fingerprint (family
'foresight-gemma-embedding'); a helper upgrade => fingerprint change =>
reindex, by design. Fingerprint isolation means foresight vectors can NEVER
share an index with llama vectors — the gate refuses (exit 3).

grpcio is imported lazily: absence = backend unavailable (honest probe),
never a crash. Socket resolution: env GOLDMINE_FORESIGHT_SOCKET > config
`foresight.socket` > $TMPDIR/foresight_helper.sock (SPEC §10/§11).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

from ..ingest.media import MEDIA_PREPROCESSING
from .base import Capability, EmbeddingBackend, EmbeddingError

logger = logging.getLogger("goldmine.backends.foresight")

BACKEND_CLASS: type["ForesightBackend"]  # registry hook (backends/__init__)

FAMILY = "foresight-gemma-embedding"
REQUEST_TIMEOUT_S = 60.0
CONNECT_TIMEOUT_S = 5.0

#: The prompts fingerprint value + the embed-identity `prompts_doc` (F1): the
#: helper applies its own prompts internally — the client sends framed text
#: verbatim, so there is NO d='…' slot to parse out of this string. Recorded
#: as-is in both places; it must stay one single constant.
PROMPTS_DOC = "helper-internal (client sends framed text verbatim)"

#: Verified live facts (foresight-rea docs, 2026-10-11): embeddings are 768-dim;
#: the helper tags queries internally with EmbedData.TaskType=6 (documents use
#: the helper's default); Part.Kind=3 for image parts. Recorded in the
#: fingerprint so a helper that changes its mapping forces a reindex.
TASK_TYPE_MAPPING = {"query": 6, "document": "helper-default"}
DIM_LIVE_VERIFIED = 768


def _load_grpc():
    """(grpc, pb2, pb2_grpc) or None — lazily; never raises at import time."""
    try:
        import grpc
        from . import _gen
        from ._gen import rag_service_pb2, rag_service_pb2_grpc
    except ImportError as exc:
        logger.debug("foresight: grpc stack unavailable (%s)", exc)
        return None
    return grpc, rag_service_pb2, rag_service_pb2_grpc


def grpcio_available() -> bool:
    return _load_grpc() is not None


def default_socket() -> Path:
    return Path(os.environ.get("TMPDIR", "/tmp")) / "foresight_helper.sock"


class ForesightBackend(EmbeddingBackend):
    backend_id = "foresight"

    #: no client-side document prompt — the marker string IS the identity value
    prompts_doc_template = PROMPTS_DOC

    def __init__(self, cfg):
        self.cfg = cfg
        self._channel = None
        self._stub = None
        self._version: str | None = None
        self._dim: int | None = None
        self._pb2 = None
        self._pb2_grpc = None
        self._grpc = None

    # ------------------------------------------------------------- identity

    @property
    def dim(self) -> int:
        return self._dim if self._dim is not None else DIM_LIVE_VERIFIED

    def capability(self) -> Capability:
        return Capability(text=True, image=True, audio=True)

    def socket_path(self) -> Path:
        raw = self.cfg.socket or os.environ.get("GOLDMINE_FORESIGHT_SOCKET") or ""
        return Path(raw).expanduser() if raw else default_socket()

    def fingerprint_fields(self) -> dict:
        return {
            "family": FAMILY,
            "model_id": f"helper@{self._version or 'unknown'}",
            "model_sha256": "n/a (helper-managed; identity = Ping().version)",
            "dim": self.dim,
            "prompts": PROMPTS_DOC,
            "pooling": "helper-default",
            "normalization": "l2_post",
            "task_type": TASK_TYPE_MAPPING,
            # media preprocessing the PIPELINE applies before these bytes reach
            # the helper (30s/25s 16 kHz mono WAV windows, raw image bytes) —
            # recorded so a windowing change forces a reindex. Media vectors
            # carry no prompt; this is their fingerprint identity instead.
            "media": MEDIA_PREPROCESSING,
        }

    # ------------------------------------------------------------- lifecycle

    def _ensure_grpc(self) -> bool:
        if self._grpc is None:
            stack = _load_grpc()
            if stack is None:
                return False
            self._grpc, self._pb2, self._pb2_grpc = stack
        return True

    def ping(self) -> str:
        """RagService.Ping -> version. Connects lazily; raises EmbeddingError
        when the helper is unreachable (callers treat that as honest data)."""
        if not self._ensure_grpc():
            raise EmbeddingError(
                "grpcio is not installed — the foresight backend is unavailable",
                "pip install --user grpcio (or use --backend llama/mock)",
            )
        socket_path = self.socket_path()
        if not socket_path.exists():
            raise EmbeddingError(
                f"ForesightHelper socket not found at {socket_path}",
                "start the Foresight app (the helper owns the socket), or pick "
                "another backend",
            )
        if self._channel is None:
            # absolute path => "unix:" + path (grpcio UDS target syntax)
            self._channel = self._grpc.insecure_channel(f"unix:{socket_path}")
            self._stub = self._pb2_grpc.RagServiceStub(self._channel)
        try:
            response = self._stub.Ping(
                self._pb2.PingRequest(), timeout=CONNECT_TIMEOUT_S
            )
        except Exception as exc:  # grpc.RpcError and friends
            raise EmbeddingError(
                f"ForesightHelper Ping failed on {socket_path}: {exc}",
            ) from exc
        if not response.success:
            raise EmbeddingError(
                f"ForesightHelper Ping returned success=false (version "
                f"{response.version!r})",
            )
        self._version = response.version
        expected = getattr(self.cfg, "expect_version", "") or ""
        if expected and expected != response.version:
            logger.warning(
                "foresight: helper version %s != pinned %s — fingerprint will "
                "change and force a reindex",
                response.version, expected,
            )
        return response.version

    def startup(self) -> None:
        """Ping + one probe embed (records dim for the fingerprint). Idempotent."""
        if self._stub is not None and self._version is not None and self._dim:
            return
        self.ping()
        probe = self._embed_raw_text("goldmine startup dim probe")
        self._dim = int(probe.shape[0])
        logger.info("foresight: helper %s ready, dim %d", self._version, self._dim)

    def close(self) -> None:
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:  # pragma: no cover - close is best effort
                pass
        self._channel = None
        self._stub = None

    # ------------------------------------------------------------- embedding

    def _embed_raw_text(self, text: str) -> np.ndarray:
        """One ComputeEmbedding RPC. Preconditions: startup() ran (stub + dim)."""
        if self._stub is None:
            raise EmbeddingError("foresight backend not started")
        try:
            response = self._stub.ComputeEmbedding(
                self._pb2.ComputeEmbeddingRequest(text=text),
                timeout=REQUEST_TIMEOUT_S,
            )
        except Exception as exc:
            raise EmbeddingError(f"ComputeEmbedding failed: {exc}") from exc
        vector = np.asarray(response.embedding, dtype=np.float32).reshape(-1)
        if vector.size == 0:
            raise EmbeddingError(
                "ComputeEmbedding returned an empty vector — helper pipeline "
                "not initialized? (see the helper's model state)"
            )
        if self._dim is not None and vector.size != self._dim:
            raise EmbeddingError(
                f"helper returned dim {vector.size}, expected {self._dim}"
            )
        return vector

    def _normalize(self, matrix: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        return matrix / norms

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        self.startup()  # records version + dim for the fingerprint
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            out[i] = self._embed_raw_text(text)  # one RPC per text (no batch RPC)
        return self._normalize(out)

    def _embed_bytes_rpc(self, rpc, request) -> np.ndarray:
        """Preconditions: startup() ran (stub connected)."""
        if self._stub is None:
            raise EmbeddingError("foresight backend not started")
        try:
            response = rpc(request, timeout=REQUEST_TIMEOUT_S)
        except Exception as exc:
            raise EmbeddingError(f"embedding RPC failed: {exc}") from exc
        vector = np.asarray(response.embedding, dtype=np.float32).reshape(-1)
        if vector.size == 0:
            raise EmbeddingError("helper returned an empty vector")
        return vector

    def embed_image(self, path: Path) -> np.ndarray:
        self.startup()
        path = Path(path)
        if not path.is_file():
            raise EmbeddingError(f"image not found: {path}")
        vector = self._embed_bytes_rpc(
            self._stub.ComputeImageEmbedding,
            self._pb2.ComputeImageEmbeddingRequest(image_bytes=path.read_bytes()),
        )
        return self._normalize(vector.reshape(1, -1))[0]

    def embed_audio_wav(self, wav: bytes) -> np.ndarray:
        self.startup()
        if not wav:
            raise EmbeddingError("empty audio payload")
        vector = self._embed_bytes_rpc(
            self._stub.ComputeAudioEmbedding,
            self._pb2.ComputeAudioEmbeddingRequest(audio_bytes=wav),
        )
        return self._normalize(vector.reshape(1, -1))[0]

    # --------------------------------------------------------------- report

    def describe(self) -> dict:
        socket_path = self.socket_path()
        available = grpcio_available() and socket_path.exists()
        info: dict = {
            "id": self.backend_id,
            "available": available,
            "dim": self.dim if self._version else None,
            "capability": {"text": True, "image": True, "audio": True},
            "socket": str(socket_path),
            "version": self._version,
            "experimental": True,
            "note": "client of a shared helper we do not own; identity = Ping version",
        }
        if not grpcio_available():
            info["fix_hint"] = "pip install --user grpcio (foresight extra)"
        elif not socket_path.exists():
            info["fix_hint"] = (
                f"start the Foresight app (socket {socket_path} appears when the "
                "helper runs)"
            )
        return info
