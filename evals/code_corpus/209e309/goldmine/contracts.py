"""Typed values shared by CLI and MCP; no transport or model lifecycle code."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .search import Hit, QueryFilters
    from .store import StatusCounts

EvidenceType = Literal["content", "name_metadata", "vector_only"]


class ApplicationError(RuntimeError):
    """An expected application failure, rendered by the calling adapter."""

    def __init__(self, kind: str, message: str, *, exit_code: int = 1):
        super().__init__(message)
        self.kind = kind
        self.exit_code = exit_code


class BudgetTooSmall(ApplicationError):
    def __init__(self, message: str = "budget cannot fit the required source information"):
        super().__init__("budget_too_small", message, exit_code=2)


@dataclass(frozen=True)
class SearchRequest:
    query: str
    filters: QueryFilters | None = None
    scorer: str = "hybrid"
    z_gate: float = 3.0
    limit: int = 20
    kinds: set[str] | None = None
    snippet_width: int | None = 240
    why: bool = False
    pack: bool = False
    budget: int = 4000
    context: int = 0
    lexical_only: bool = False
    require_citations: bool = False
    mode: str = "fast"
    rerank: str = "off"
    multi_query: bool = False
    timeout_s: float | None = None


@dataclass(frozen=True)
class Citation:
    citation_id: str
    index_uuid: str
    segment_id: int
    file_id: int
    path: str
    source: str
    kind: str
    segment_kind: str
    modality: str
    loc: float | None
    loc_end: float | None
    evidence_type: EvidenceType
    content_sha256: str


@dataclass(frozen=True)
class StoredEvidence:
    citation: Citation
    text: str | None
    metadata: dict | None
    file_status: str | None


@dataclass(frozen=True)
class SegmentContext:
    parts: list[dict]
    note: str | None


@dataclass(frozen=True)
class SearchResult:
    hits: list[Hit]
    citations: list[Citation] = field(default_factory=list)
    contexts: dict[int, SegmentContext] = field(default_factory=dict)
    pack_text: str | None = None
    pack_meta: dict | None = None
    settings: dict = field(default_factory=dict)
    via: str = "cold"
    fallback_reason: str | None = None
    stale: bool = False
    supporting_citations: dict[int, list[Citation]] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextRequest:
    citation_ids: list[str]
    neighbors: int = 1
    budget: int = 4000
    timeout_s: float | None = None


@dataclass(frozen=True)
class ContextResult:
    context_text: str
    citations: list[Citation]
    used_tokens: int
    token_mode: str
    tokenizer: str
    skipped: int = 0
    truncated: bool = False
    evidence_type: EvidenceType | None = None
    metadata: dict | None = None
    file_status: str | None = None


@dataclass(frozen=True)
class IndexStatus:
    schema_version: int
    index_uuid: str | None
    counts: StatusCounts
    lexical_version: str | None
    fingerprint_id: str | None
    capabilities: dict[str, bool]
    staleness: dict
    details: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
