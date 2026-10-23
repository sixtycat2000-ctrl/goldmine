"""Suite identity and stored-content preflight for the historical media set.

No ingestion, source file reads, model creation, or tool discovery is needed to
verify that an already-built index contains the promised evidence.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

SUITE_IDS = {"legacy": "legacy.v3", "full-media": "media-full.v3"}


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _stored_capability(con, path: str, need: str, exp: dict) -> tuple[bool, str]:
    row = con.execute("SELECT id,kind,info,extractor_version FROM files WHERE path=?",
                      (path,)).fetchone()
    if row is None:
        return False, "file is absent from index"
    file_id, kind, raw_info, extractor = row
    info = json.loads(raw_info) if raw_info else {}
    segments = con.execute(
        "SELECT s.kind,s.modality,s.loc,s.loc_end,coalesce(f.original,f.body) "
        "FROM segments s LEFT JOIN fts f ON f.rowid=s.id WHERE s.file=?",
        (file_id,)).fetchall()
    if need == "asr":
        ok = kind in {"audio", "video"} and any(
            sk == "audio" and modality == "text" and text and text.strip()
            for sk, modality, _, _, text in segments)
        return bool(ok), "actual ASR text segment required"
    if need == "ocr":
        if kind == "image":
            return any(sk == "ocr" and text and text.strip() for sk, _, _, _, text in segments), \
                "actual OCR text segment required"
        pages = set(info.get("ocr_pages") or [])
        if "pages" in exp:
            start, end = exp["pages"]
            pages &= set(range(int(start), int(end) + 1))
        ok = kind == "pdf" and any(sk in {"page", "chunk"} and loc in pages and text and text.strip()
                                  for sk, _, loc, _, text in segments)
        return bool(ok), "stored OCR page provenance and nonempty page text required"
    if need == "exif":
        # Name/path matches never stand in for embedded photo metadata.
        available = {key: val for key, val in info.items()
                     if key in {"taken", "camera", "make", "model", "gps", "exif"} and val}
        return bool(available), "stored EXIF date/camera/GPS fields required"
    return False, f"unknown required media capability: {need}"


def inspect_index(db: Path, queries: list[dict], fixture_root: Path) -> dict:
    """Read one explicit SQLite snapshot, including all indexed-file identities."""
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        version = con.execute("PRAGMA user_version").fetchone()[0]
        metadata = dict(con.execute("SELECT key,value FROM meta"))
        corpus = con.execute("SELECT path,source,sha256,size,extractor_version,chunker_version,embed_id "
                             "FROM files ORDER BY path,source").fetchall()
        checks = []
        for query in queries:
            for need in sorted(set(query.get("needs") or [])):
                expected = [exp for exp in query.get("expect", []) if exp.get("grade", 0) >= 2]
                candidates = []
                for exp in expected:
                    path = Path(exp["path"])
                    path = path if path.is_absolute() else fixture_root / path
                    ok, detail = _stored_capability(con, str(path.resolve()), need, exp)
                    candidates.append({"path": exp["path"], "present": ok, "detail": detail})
                checks.append({"id": query["id"], "capability": need,
                               "present": any(item["present"] for item in candidates),
                               "evidence": candidates})
        fp = json.loads(metadata.get("fingerprint_json") or "{}")
        return {"schema": 1, "index_schema": version,
                "index_identity": {key: metadata.get(key) for key in
                                   ("index_uuid", "embed_id", "chunker_version", "lexical_version")},
                "indexed_corpus_sha256": hashlib.sha256(_canonical(corpus)).hexdigest(),
                "queries_sha256": hashlib.sha256(_canonical(queries)).hexdigest(),
                "eligible_ids": [q["id"] for q in queries],
                "answerable": sum(q["tag"] != "noise" for q in queries),
                "noise": sum(q["tag"] == "noise" for q in queries),
                "capabilities": checks,
                "missing": [item for item in checks if not item["present"]],
                "photo_visual_probe": {"status": "unsupported" if fp.get("backend") == "llama"
                                       else "not_measured",
                                       "reason": "llama adapter embeds text; photo names/EXIF are separate"}}
    finally:
        if con.in_transaction:
            con.rollback()
        con.close()
