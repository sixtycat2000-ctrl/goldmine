"""MockBackend — deterministic hash embeddings, in-process. Offline-first enabler.

Bag-of-token hashing via blake2b (NEVER Python hash() — it is salted per process),
L2-normalized. Deterministic across runs and machines so the whole suite runs
offline and the relevance eval is reproducible (ARCHITECTURE §4).

Task asymmetry comes from the prompts: callers embed "task: search result | query: X"
vs "title: T | text: X", and different strings hash to different vectors —
consistently, deterministically. The backend itself stays prompt-agnostic (D7).

Media (image/audio) is OPT-IN via `multimodal=True`: the DEFAULT mock is
text-only so offline tests pin media as metadata-only and the pipeline's
non-multimodal path stays exercised. DEVIATION from SPEC §7's table row
("mock yes/yes/yes"): the default constructor is text-only; the fake
image/audio embedding exists only behind the flag (it hashes raw bytes —
determinism only, zero quality claim).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import numpy as np

from .base import Capability, EmbeddingBackend, NotSupported, DOCUMENT_PROMPT_TEMPLATE

_TOKEN = re.compile(r"[a-z0-9]+")

#: Fingerprint field recorded when multimodal is on — flipping the flag changes
#: which modalities an index was built with, so it must force a reindex. The
#: default (flag off) fingerprint stays byte-identical to pre-multimodal builds.
MEDIA_FINGERPRINT = "bytes.v1"


class MockBackend(EmbeddingBackend):
    backend_id = "mock"
    fingerprint_without_startup = True
    model_spawn_count = 0
    embedding_http_requests = 0

    #: same split doc-prompt constant as llama (the mock hashes whatever framed
    #: text it receives, so the prompt strings are fingerprint identity only)
    prompts_doc_template = DOCUMENT_PROMPT_TEMPLATE

    def __init__(self, seed: int = 0, dim: int = 768, *, multimodal: bool = False):
        # dim configurable so tests can exercise other dimensionalities; the dim is
        # part of the fingerprint, so indexes built with different dims never mix.
        # (MODULE_MAP pins DIM=768 as the default; the orchestrator spec asked for a
        # configurable dim with 256 as default — 768 kept to match MODULE_MAP.
        # DEVIATION noted in the build report.)
        self.seed = seed
        self._dim = int(dim)
        self.multimodal = bool(multimodal)

    @property
    def dim(self) -> int:
        return self._dim

    def capability(self) -> Capability:
        if self.multimodal:
            return Capability(text=True, image=True, audio=True)
        return Capability(text=True, image=False, audio=False)

    def fingerprint_fields(self) -> dict:
        fields = {
            "model_id": f"mock-v1(seed={self.seed})",
            "model_sha256": "n/a",
            "dim": self._dim,
            "prompts": "v1:q='task: search result | query: {}' d='title: {} | text: {}'",
            "pooling": "none",
            "normalization": "l2",
        }
        if self.multimodal:
            fields["media"] = MEDIA_FINGERPRINT
        return fields

    def describe(self) -> dict:
        return {
            "id": self.backend_id,
            "available": True,
            "dim": self._dim,
            "capability": {
                "text": True,
                "image": self.multimodal,
                "audio": self.multimodal,
            },
            "source": "in-process",
            "note": "deterministic hash vectors; tests only — not a quality claim"
            + ("" if self.multimodal else " (media: text-only default; "
               "multimodal=True for fake image/audio)"),
        }

    # -- internals ---------------------------------------------------------

    def _embed_text(self, text: str) -> np.ndarray:
        vec = np.zeros(self._dim, dtype=np.float32)
        for tok in _TOKEN.findall(text.lower()):
            digest = hashlib.blake2b(
                f"{self.seed}:{tok}".encode("utf-8"), digest_size=16
            ).digest()
            # 2 dims per token: (index, sign) twice, from one digest.
            i1 = int.from_bytes(digest[0:4], "big") % self._dim
            i2 = int.from_bytes(digest[4:8], "big") % self._dim
            vec[i1] += 1.0 if digest[8] & 1 else -1.0
            vec[i2] += 1.0 if digest[8] & 2 else -1.0
        norm = float(np.linalg.norm(vec))
        if norm > 0.0:
            vec /= norm
        return vec

    def _embed_bytes(self, data: bytes) -> np.ndarray:
        # Content-hash vector in the same space; cross-modal similarity is not
        # meaningful for the mock — only determinism is.
        digest = hashlib.shake_256(f"{self.seed}:".encode("utf-8") + data).digest(
            self._dim * 2
        )
        arr = np.frombuffer(digest, dtype="<u2").astype(np.float32)
        arr = (arr - 32767.5) / 32767.5
        norm = float(np.linalg.norm(arr))
        if norm > 0.0:
            arr /= norm
        return arr

    # -- EmbeddingBackend --------------------------------------------------

    def count_tokens(self, texts: list[str]) -> list[int] | None:
        """The deterministic offline estimator (len//4), so mock/offline runs
        exercise the same kind-aware fallback path as a backend without
        /tokenize (01 §5.5 'Lexical-only / mock')."""
        return [len(t) // 4 for t in texts]

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        return np.stack([self._embed_text(t) for t in texts])

    def embed_image(self, path: Path) -> np.ndarray:
        if not self.multimodal:
            raise NotSupported("default mock is text-only; construct with multimodal=True")
        return self._embed_bytes(Path(path).read_bytes())

    def embed_audio_wav(self, wav: bytes) -> np.ndarray:
        if not self.multimodal:
            raise NotSupported("default mock is text-only; construct with multimodal=True")
        return self._embed_bytes(wav)

    def embed_image_bytes(self, data: bytes) -> np.ndarray:
        """Convenience for tests: embed raw image bytes without a file."""
        return self._embed_bytes(data)

    def close(self) -> None:  # nothing owned, nothing to kill
        pass
