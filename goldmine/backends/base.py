"""EmbeddingBackend ABC: the only boundary through which strings become vectors.

Backends NEVER apply EmbeddingGemma prompts themselves — the pipeline applies the
document prompt and the CLI/search apply the query prompt before calling embed_texts.
The verbatim prompt scheme lives here as shared constants so fingerprints can record
it (D7: prompts are part of the fingerprint; changing one is a reindex).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# EmbeddingGemma 2 model-card prompts, VERBATIM (SPEC §3, D7). Versioned "v1" in
# the fingerprint; changing either string forces a reindex.
QUERY_PROMPT = "task: search result | query: {text}"
DOCUMENT_PROMPT = "title: {title} | text: {text}"
PROMPT_SCHEME = (
    "v1:q='task: search result | query: {}' d='title: {} | text: {}'"
)

#: The DOCUMENT-prompt template as a SPLIT, structured constant (v2 review F1):
#: the LIVE embed identity is computed from this — never by parsing the
#: combined PROMPT_SCHEME, whose d='…' slot is a llama/mock artifact (the
#: foresight scheme deliberately has no d-slot because the helper frames text
#: internally). Byte-identical to PROMPT_SCHEME's d-slot for llama/mock, so
#: the B2 binding rule holds: migrated v1 embed ids and fresh v2 ids agree.
DOCUMENT_PROMPT_TEMPLATE = "title: {} | text: {}"


def query_prompt(text: str) -> str:
    return QUERY_PROMPT.format(text=text)


def document_prompt(title: str | None, text: str) -> str:
    return DOCUMENT_PROMPT.format(title=title if title is not None else "none", text=text)


@dataclass(frozen=True)
class Capability:
    text: bool
    image: bool
    audio: bool


class NotSupported(RuntimeError):
    """The backend cannot embed this modality (e.g. images under llama)."""


class EmbeddingError(RuntimeError):
    """A backend failed at runtime; carries a bounded tool/log tail for the message."""

    def __init__(self, message: str, tail: str = ""):
        super().__init__(message if not tail else f"{message}\n{tail}")
        self.tail = tail


class BackendStartupError(EmbeddingError):
    """The backend could not start at all (binary missing, readiness timeout,
    model hash mismatch, dim probe failure).

    Exit-code contract (ARCHITECTURE §8, binding scope pass 6): a STARTUP
    failure maps to exit 2 with a 'backend failed to start' detail, distinct
    from a mid-ingest backend crash (exit 1) and from per-file failures
    (exit 5 partial). DEVIATION note: SPEC §1.1 lists 2 as 'usage error';
    ARCHITECTURE §8 pins llama-server-fails-to-start to exit 2 and the scope
    resolves the conflict in ARCHITECTURE's favor.
    """


class EmbeddingBackend(ABC):
    """Contract: embed_texts returns an (n, 768) float32 matrix, L2-normalized.

    DEVIATION from MODULE_MAP (`embed_texts -> list[np.ndarray]`): returns a single
    (n, dim) np.ndarray — the orchestrator's build spec states "(n,768) L2-normalized
    float32", and every consumer (fp16 packing, matrix search) wants a matrix anyway.
    """

    backend_id: str = "abstract"

    #: The document-prompt template this backend declares — one axis of the
    #: embed identity (fingerprints.live_embed_identity). The default is the
    #: shared llama/mock template; a backend that applies no client-side
    #: document prompt (foresight: the helper frames text internally) overrides
    #: it with its own marker string, used verbatim in the identity.
    prompts_doc_template: str = DOCUMENT_PROMPT_TEMPLATE

    @property
    @abstractmethod
    def dim(self) -> int:
        """Embedding dimension (768 for the real backends)."""

    # The orchestrator build spec calls this "name"; MODULE_MAP calls it backend_id.
    @property
    def name(self) -> str:  # pragma: no cover - trivial alias
        return self.backend_id

    @abstractmethod
    def capability(self) -> Capability: ...

    def max_input_tokens(self) -> int | None:
        """Server-side per-input token ceiling for text embeds, or None.

        Backends with a hard ceiling (llama-server context) override this so
        the pipeline shrinks chunks up front instead of losing text to server
        truncation or a 400 exceed_context error.
        """
        return None

    def count_tokens(self, texts: list[str]) -> list[int] | None:
        """Exact per-text token counts as the EMBED path would see them
        (specials included), or None when counting is unavailable — callers
        fall back to the kind-aware `est_tokens` estimator (01 §5.5 / C6).

        llama implements this via POST /tokenize; the mock returns len//4
        (the deterministic offline estimator).
        """
        return None

    def token_pieces(self, text: str) -> "list[bytes] | None":
        """Exact token PIECES of `text` as utf-8 bytes (one piece = one token;
        concatenated they reproduce the text), or None when unavailable —
        the chunker then uses its estimator path. Pieces power token-boundary
        chunk cuts and the ctx-guard SPLIT-not-fail backstop (D3)."""
        return None

    @abstractmethod
    def fingerprint_fields(self) -> dict:
        """Model id/sha (or helper version), dim, prompts, pooling, normalization."""

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> np.ndarray:  # (n, dim) float32, L2
        ...

    @abstractmethod
    def embed_image(self, path: Path) -> np.ndarray:
        """Raises NotSupported when the backend is text-only."""

    @abstractmethod
    def embed_audio_wav(self, wav: bytes) -> np.ndarray:
        """Raises NotSupported when the backend is text-only."""

    @abstractmethod
    def describe(self) -> dict:
        """Availability, version, source of truth, fix hints — for status/doctor."""

    def startup(self) -> None:
        """Acquire resources (spawn llama-server, ping helper). Default: nothing."""

    def shutdown(self) -> None:
        """Alias of close() for use as a symmetric lifecycle pair."""

    def close(self) -> None:
        """Release resources. Kill ONLY processes this backend spawned."""

    def __enter__(self) -> "EmbeddingBackend":
        self.startup()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
