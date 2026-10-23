"""Read-only Goldmine audit with all known sources, including stale rows."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

from answer_eval.protocol import EvaluationError, canonical_sha256, parse_json, sha256_file


def _text(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise EvaluationError("actual query-index text is not UTF8 text/SQL NULL")
    raw = value.encode("utf-8")
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _query_part(db, part):
    terms = [dict(r) for r in db.execute("SELECT normalized_term,raw_term,source,match_type FROM lookup_terms WHERE segment_id=? ORDER BY normalized_term,raw_term,source,match_type", (part["id"],))]
    cjk = [r[0] for r in db.execute("SELECT tokens FROM fts_cjk WHERE rowid=?", (part["id"],))]
    if len(cjk) > 1:
        raise EvaluationError("duplicate actual CJK index row")
    return {"anchor": {k: part[k] for k in ("kind", "modality", "loc", "loc_end", "vector")},
            "excerpt": _text(part.pop("excerpt")), "fts_present": bool(part.pop("fts_present")),
            "fts": {k: _text(part.pop("fts_" + k)) for k in ("name", "body", "original")},
            "cjk": [_text(v) for v in cjk], "lookup": {"rows": len(terms), "sha256": canonical_sha256(terms)}}


def goldmine(path, corpus, known, embedding):
    path, root = Path(path), Path(corpus)
    before = sha256_file(path)
    expected = {str(root / d["view_path"]): d for d in known}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON"); db.execute("BEGIN")
        schema = db.execute("PRAGMA user_version").fetchone()[0]
        if schema != 6:
            raise EvaluationError("update audit requires exact Goldmine schema6")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"files", "segments", "fts", "fts_cjk", "lookup_terms", "meta"} <= tables:
            raise EvaluationError("current hybrid query indexes are absent from the actual schema6 snapshot")
        if (db.execute("SELECT 1 FROM segments s LEFT JOIN files f ON f.id=s.file WHERE f.id IS NULL LIMIT 1").fetchone()
                or db.execute("SELECT 1 FROM fts t LEFT JOIN segments s ON s.id=t.rowid WHERE s.id IS NULL LIMIT 1").fetchone()
                or db.execute("SELECT 1 FROM fts_cjk t LEFT JOIN segments s ON s.id=t.rowid WHERE s.id IS NULL LIMIT 1").fetchone()
                or db.execute("SELECT 1 FROM lookup_terms t LEFT JOIN segments s ON s.id=t.segment_id WHERE s.id IS NULL OR s.file!=t.file_id LIMIT 1").fetchone()):
            raise EvaluationError("actual index has orphan/misattributed source/segment/FTS/CJK/lookup rows")
        meta = {r["key"]: r["value"] for r in db.execute("SELECT key,value FROM meta")}
        fp = parse_json(meta.get("fingerprint_json", "null"), "actual embedding fingerprint")
        if not isinstance(fp, dict) or fp.get("backend") != "llama" or fp.get("model_sha256") != embedding:
            raise EvaluationError("update audit backend/model differs from the fixed engine")
        rows = [dict(r) for r in db.execute("SELECT * FROM files ORDER BY id")]
        if len({r["path"] for r in rows}) != len(rows) or any(r["path"] not in expected for r in rows):
            raise EvaluationError("duplicate or unregistered actual indexed source")
        sources, query_index = {}, {}
        for row in rows:
            parts = []
            material = []
            for part in db.execute("SELECT s.id,s.kind,s.modality,s.loc,s.loc_end,s.excerpt,s.vec,t.name AS fts_name,t.body AS fts_body,t.original AS fts_original,(t.rowid IS NOT NULL) AS fts_present,coalesce(t.original,t.body) AS text FROM segments s LEFT JOIN fts t ON t.rowid=s.id WHERE s.file=? ORDER BY s.id", (row["id"],)):
                value = dict(part)
                vector = value.pop("vec")
                value["vector"] = None if vector is None else {"bytes": len(vector), "sha256": hashlib.sha256(vector).hexdigest()}
                material.append(_query_part(db, value))
                parts.append(value)
            query_index[expected[row["path"]]["source_path"]] = sorted(material, key=canonical_sha256)
            sources[expected[row["path"]]["source_path"]] = {"file": row, "parts": parts,
                "status": "OBSERVED_INDEXED_TEXT" if any(p["kind"] != "name" and p["text"] for p in parts) else "METADATA_ONLY"}
        db.rollback()
    if sha256_file(path) != before:
        raise EvaluationError("read-only update audit observed a changing database")
    stable = {"schema": schema, "meta": meta, "sources": sources, "query_index": query_index}
    return {"stable": stable, "stable_sha256": canonical_sha256(stable), "observed_files": {str(path): before}}
