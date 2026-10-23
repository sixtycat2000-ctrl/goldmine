"""Derived CJK/identifier indexes; all text comes from stored rows, never files."""

from __future__ import annotations

import json
from pathlib import PurePath
from uuid import uuid4

from .deadline import check_deadline
from .lexical import (
    LEXICAL_CJK_VERSION, LEXICAL_VERSION, cjk_tokens, identifier_tokens,
    normalize, path_identifier_tokens, term_forms,
)

CJK_DDL = "CREATE VIRTUAL TABLE IF NOT EXISTS fts_cjk USING fts5(tokens)"
LOOKUP_DDL = """CREATE TABLE IF NOT EXISTS lookup_terms(
    normalized_term TEXT NOT NULL,
    raw_term        TEXT NOT NULL,
    file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    segment_id      INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    source          TEXT NOT NULL CHECK(source IN ('basename','path','title','body')),
    match_type      TEXT NOT NULL CHECK(match_type IN ('raw','alias')),
    UNIQUE(normalized_term, raw_term, file_id, segment_id, source, match_type)
)"""


def _meta(db, key, value):
    db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key,value))


def create_derived(db, *, exact: bool) -> None:
    db.execute(CJK_DDL)
    if exact:
        db.execute(LOOKUP_DDL)
        db.execute("CREATE INDEX IF NOT EXISTS lookup_normalized ON lookup_terms(normalized_term)")
        db.execute("CREATE INDEX IF NOT EXISTS lookup_segment ON lookup_terms(segment_id)")
    db.execute("DROP TRIGGER IF EXISTS gm_lexical_delete")
    cleanup = "DELETE FROM lookup_terms WHERE segment_id=old.id;" if exact else ""
    db.execute(
        "CREATE TRIGGER gm_lexical_delete AFTER DELETE ON segments BEGIN "
        "DELETE FROM fts_cjk WHERE rowid=old.id; " + cleanup + " END"
    )


def insert_derived(db, segment_id: int, file_id: int, kind: str, name: str, text: str, *, exact: bool = True):
    path, info_json = db.execute("SELECT path,info FROM files WHERE id=?", (file_id,)).fetchone()
    cjk_text = name + " " + text + (" " + path if kind == "name" else "")
    db.execute("INSERT INTO fts_cjk(rowid,tokens) VALUES(?,?)", (segment_id, cjk_tokens(cjk_text)))
    if not exact:
        return
    terms = []
    if kind == "name":
        basename = PurePath(path).name
        terms.extend([(basename, "basename"), (path, "path")])
        terms.extend((token, "basename") for token in path_identifier_tokens(basename))
        terms.extend((token, "path") for token in path_identifier_tokens(path))
        info = json.loads(info_json) if info_json else {}
        title = info.get("title") if isinstance(info, dict) else None
        if isinstance(title, str) and title.strip():
            terms.extend((term, "title") for term in identifier_tokens(title))
    else:
        terms.extend((term, "body") for term in identifier_tokens(text))
    rows = {
        (normalized, raw, file_id, segment_id, source, match_type)
        for raw, source in terms for normalized, match_type in term_forms(raw)
    }
    db.executemany(
        "INSERT OR IGNORE INTO lookup_terms(normalized_term,raw_term,file_id,segment_id,source,match_type) "
        "VALUES(?,?,?,?,?,?)", sorted(rows),
    )


def rebuild_derived(db, *, exact: bool) -> dict:
    """Rebuild from stored original/body and paths, preserving segment/vector IDs."""
    create_derived(db, exact=exact)
    db.execute("DELETE FROM fts_cjk")
    if exact:
        db.execute("DELETE FROM lookup_terms")
    last_id, count = 0, 0
    while True:
        check_deadline()
        # Keep the primary-key range outermost; FTS-first batches rescan/sort.
        rows = db.execute(
            "SELECT s.id,s.file,s.kind,t.name,t.body,t.original FROM segments s "
            "CROSS JOIN fts t ON t.rowid=s.id WHERE s.id>? ORDER BY s.id LIMIT 256", (last_id,),
        ).fetchall()
        if not rows:
            break
        for sid, fid, kind, name, body, original in rows:
            text = original if original is not None else body
            normalized_name, normalized_body = normalize(name), normalize(text)
            stored_original = original if original is not None else (text if normalized_body != text else None)
            if (name, body, original) != (normalized_name, normalized_body, stored_original):
                db.execute("DELETE FROM fts WHERE rowid=?", (sid,))
                db.execute("INSERT INTO fts(rowid,name,body,original) VALUES(?,?,?,?)",
                           (sid, normalized_name, normalized_body, stored_original))
            insert_derived(db, sid, fid, kind, normalized_name, text, exact=exact)
            count += 1
        last_id = rows[-1][0]
    if db.execute("SELECT value FROM meta WHERE key='index_uuid'").fetchone() is None:
        _meta(db, "index_uuid", str(uuid4()))
    version = LEXICAL_VERSION if exact else LEXICAL_CJK_VERSION
    _meta(db, "lexical_version", version)
    return {"fts_rows":count, "lexical_version":version}
