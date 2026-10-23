"""Opaque citations bound to stored text, location and source metadata."""

from __future__ import annotations

import hashlib
import json
import re
from uuid import UUID

from .contracts import ApplicationError, Citation, StoredEvidence
from .read_source import fts_projection

_CITATION = re.compile(r"gm1:([0-9a-f-]{36}):([1-9][0-9]{0,18}):([0-9a-f]{16})\Z")
_MAX_SEGMENT_ID = (1 << 63) - 1


def _valid_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def index_uuid(store, *, required: bool = True) -> str | None:
    value = store.meta("index_uuid")
    if value is not None and _valid_uuid(value):
        return value
    if not required:
        return None
    raise ApplicationError(
        "missing_capability",
        "opaque citations need an index UUID; run goldmine reindex lexical --db "
        f"{str(store.path)!r}",
        exit_code=4,
    )


def read_evidence(store, segment_id: int, *, uuid: str | None = None) -> StoredEvidence:
    """Read and hash one joined row within the caller's request snapshot."""
    if isinstance(segment_id, bool) or not isinstance(segment_id, int) or not 0 < segment_id <= _MAX_SEGMENT_ID:
        raise ApplicationError("invalid_params", "invalid citation segment ID", exit_code=2)
    uuid = index_uuid(store) if uuid is None else uuid
    text_sql, _ = fts_projection(store.db, "t")
    row = store.db.execute(
        "SELECT f.id, f.path, f.source, f.sha256, f.kind, f.info, f.capability, "
        f"s.id, s.kind, s.modality, s.loc, s.loc_end, {text_sql} "
        "FROM segments s JOIN files f ON f.id=s.file "
        "LEFT JOIN fts t ON t.rowid=s.id WHERE s.id=?",
        (segment_id,),
    ).fetchone()
    if row is None:
        raise ApplicationError("stale_citation", "citation segment no longer exists")
    (fid, path, source, sha, kind, info, capability, sid, segment_kind,
     modality, loc, loc_end, text) = row
    try:
        metadata = json.loads(info) if info is not None else None
        evidence_type = (
            "name_metadata" if segment_kind == "name" else
            "vector_only" if text is None else "content"
        )
        payload = [
            "gm-citation/v1", fid, path, source, sha, sid, segment_kind,
            modality, loc, loc_end, evidence_type, metadata, text,
        ]
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise ApplicationError("invalid_index", "citation has invalid stored metadata or location") from exc
    digest = hashlib.sha256(canonical).hexdigest()
    citation = Citation(
        citation_id=f"gm1:{uuid}:{sid}:{digest[:16]}", index_uuid=uuid,
        segment_id=sid, file_id=fid, path=path, source=source, kind=kind,
        segment_kind=segment_kind, modality=modality, loc=loc, loc_end=loc_end,
        evidence_type=evidence_type, content_sha256=digest,
    )
    return StoredEvidence(citation, text, metadata, capability)


def resolve_citation(store, citation_id: str) -> StoredEvidence:
    if not isinstance(citation_id, str):
        raise ApplicationError("invalid_params", "citation_id must be an opaque string", exit_code=2)
    match = _CITATION.fullmatch(citation_id)
    if match is None:
        raise ApplicationError("invalid_params", "invalid citation ID", exit_code=2)
    uuid, sid, _ = match.groups()
    if not _valid_uuid(uuid):
        raise ApplicationError("invalid_params", "invalid citation index UUID", exit_code=2)
    if int(sid) > _MAX_SEGMENT_ID:
        raise ApplicationError("invalid_params", "invalid citation segment ID", exit_code=2)
    if uuid != index_uuid(store):
        raise ApplicationError("stale_citation", "citation belongs to another index")
    evidence = read_evidence(store, int(sid), uuid=uuid)
    if evidence.citation.citation_id != citation_id:
        raise ApplicationError("stale_citation", "citation text, location or metadata changed")
    return evidence
