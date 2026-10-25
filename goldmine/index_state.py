"""Shared index transitions and source scope policy.

All mutations use the caller's fenced transaction. Model identity is published
before vectors, and invalidation removes evidence and its card memberships.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from . import fingerprints
from .chunker import CHUNKER_VERSION


def canonical_source(value: str) -> str:
    if value.startswith(("http://", "https://")):
        from .ingest.extract_html import canonicalize

        return canonicalize(value)
    return str(Path(value).expanduser().resolve())


def contains(root: str, path: str) -> bool:
    """Literal path boundary, with file and URL namespaces kept separate."""
    if root.startswith(("http://", "https://")) != path.startswith(("http://", "https://")):
        return False
    root = root.rstrip("/")
    return path.rstrip("/") == root or path.startswith(root + "/")


def json_list(store, key: str) -> list[str]:
    value = json.loads(store.meta(key) or "[]")
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"invalid stored {key}")
    return value


def forgotten(store, path: str) -> bool:
    return any(contains(root, path) for root in json_list(store, "forgotten_sources_json"))


def select_files(store, targets=()):
    from .store import _SELECT_FILE, _file_row

    roots = [canonical_source(value) for value in targets]
    predicates, values = [], []
    for root in roots:
        root = root.rstrip("/")
        predicates.append("(path=? OR substr(path,1,?)=?)")
        values.extend((root, len(root) + 1, root + "/"))
    where = " WHERE " + " OR ".join(predicates) if predicates else ""
    return [_file_row(row) for row in store.db.execute(_SELECT_FILE + where + " ORDER BY path", values)]


def validate_identity(store, live: dict, *, embed_id=None) -> None:
    fingerprints.gate(store.meta, live)
    if store.meta("fingerprint_id") is None or store.meta("identity_protocol") != "1":
        expected = embed_id or fingerprints.embed_id_from_fingerprint(live)
        identities = {row[0] for row in store.db.execute(
            "SELECT DISTINCT f.embed_id FROM files f JOIN segments s ON s.file=f.id WHERE s.vec IS NOT NULL"
        )}
        if identities - {expected}:
            raise fingerprints.FingerprintMismatch(
                "stored vectors have an unknown or different model identity; "
                "rebuild all recorded roots with goldmine ingest --reindex"
            )


def publish_identity(store, live: dict, *, replace: bool = False, identity=None) -> None:
    """Atomic identity transition; replacement invalidates every old segment."""
    with store.write_transaction():
        if replace:
            for (fid,) in store.db.execute("SELECT id FROM files WHERE capability IS NULL OR capability<>'unsupported'").fetchall():
                store.invalidate_file(fid, state="pending", capability=None, detail="model changed; rebuild pending")
        else:
            validate_identity(store, live, embed_id=fingerprints._embed_identity_id(identity) if identity else None)
        store.set_meta("fingerprint_id", fingerprints.fingerprint_id(live))
        store.set_meta("fingerprint_json", fingerprints.canonical(live).decode())
        identity = identity or fingerprints.embed_identity(live)
        store.set_meta("embed_id", fingerprints._embed_identity_id(identity))
        store.set_meta("embed_identity_json", fingerprints.canonical(identity).decode())
        store.set_meta("identity_protocol", "1")


def record_roots(store, roots) -> None:
    with store.write_transaction():
        exclusions = json_list(store, "forgotten_sources_json")
        merged = list(dict.fromkeys([*json_list(store, "roots_json"), *roots]))
        store.set_meta("roots_json", json.dumps([root for root in merged
            if not any(contains(excluded, root) for excluded in exclusions)]))


def update_reasons(store, rec, *, embed_id=None, extractors=None) -> list[str]:
    """Stored-state repair reasons, shared by ingest and the repair planner."""
    reasons = []
    if rec.state != "done":
        reasons.append("failed" if rec.state == "failed" else "pending")
    if rec.capability == "content_indexed":
        if not store.db.execute("SELECT 1 FROM segments WHERE file=? AND kind<>'name' LIMIT 1", (rec.id,)).fetchone():
            reasons.append("missing_content")
        if rec.chunker_version != CHUNKER_VERSION:
            reasons.append("chunker_changed")
        if extractors is not None and rec.extractor_version not in extractors:
            reasons.append("extractor_changed")
    if embed_id is not None and rec.embed_id is not None and rec.embed_id != embed_id:
        reasons.append("model_changed")
    return reasons


def missing_text_segments(store, rec, *, image_capable=False, ocr_text_vector=True):
    """Text whose extraction policy permits an embedding, retaining originals."""
    from .ingest.extract_pdf import THIN_PAGE_CHARS

    if rec.state != "done" or rec.capability != "content_indexed":
        return []
    rows = store.db.execute(
        "SELECT s.id,s.kind,coalesce(t.original,t.body) FROM segments s "
        "JOIN fts t ON t.rowid=s.id WHERE s.file=? AND s.vec IS NULL AND s.kind<>'name' ORDER BY s.id",
        (rec.id,),
    )
    result = []
    for sid, kind, text in rows:
        if not text or not any(char.isalpha() for char in text):
            continue
        if kind == "ocr" and (image_capable or not ocr_text_vector):
            continue
        if rec.kind == "pdf" and len(text.strip()) < THIN_PAGE_CHARS:
            continue
        result.append((sid, text))
    return result


def install_membership_cleanup(db) -> None:
    # Persistent so older writers also cannot retarget a membership when
    # SQLite reuses a deleted segment ID. This is additive to schema v7.
    db.execute("CREATE TRIGGER IF NOT EXISTS gm_card_segment_delete AFTER DELETE ON segments BEGIN "
               "DELETE FROM card_chunks WHERE chunk=old.id; END")
    db.execute("CREATE INDEX IF NOT EXISTS card_chunks_chunk ON card_chunks(chunk)")


def next_segment_id(store) -> int:
    """A durable high-water mark also preserves citation identity across reuse."""
    current = int(store.meta("segment_id_high_water") or 0)
    maximum = store.db.execute("SELECT coalesce(max(id),0) FROM segments").fetchone()[0]
    if not current:
        maximum = max(maximum, store.db.execute("SELECT coalesce(max(chunk),0) FROM card_chunks").fetchone()[0])
    value = max(current, maximum) + 1
    if value >= (1 << 63):
        raise ValueError("segment identity space exhausted")
    store.set_meta("segment_id_high_water", str(value))
    return value


@contextmanager
def lexical_scope(db, allowed):
    """Push file eligibility inside FTS LIMIT without an SQL variable ceiling."""
    if allowed is None:
        yield ""
        return
    db.create_function("_gm_lexical_allowed", 1, lambda fid: int(fid in allowed), deterministic=True)
    try:
        yield " AND _gm_lexical_allowed(s.file)"
    finally:
        db.create_function("_gm_lexical_allowed", 1, None)
