"""Native-schema fixtures with identical vectors and genuine stored FTS bodies."""
from __future__ import annotations

from contextlib import closing
import hashlib
import math
from pathlib import Path
import sqlite3

from .protocol import BLOCK, DIM, SEED, body, canonical, db_snapshot, file_id, file_path, peak


def logical_identity(db: Path) -> dict:
    """Stream the common SQLite truth; no native-schema metadata is equated."""
    digest, counts = hashlib.sha256(), {"files": 0, "segments": 0, "fts": 0, "vectors": 0}
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        for row in con.execute("SELECT id,path,source,kind,size,sha256,state,capability FROM files ORDER BY id"):
            digest.update(canonical(list(row)) + b"\n")
            counts["files"] += 1
        for row in con.execute("SELECT s.id,s.file,s.kind,s.modality,s.loc,s.loc_end,s.excerpt,s.vec,"
                               "t.name,t.body,t.original FROM segments s LEFT JOIN fts t ON t.rowid=s.id ORDER BY s.id"):
            sid, fid, kind, modality, loc, end, excerpt, vec, name, text, original = row
            digest.update(canonical([sid, fid, kind, modality, loc, end, excerpt, name, text, original]) + b"\n")
            digest.update(vec or b"")
            counts["segments"] += 1
            counts["fts"] += text is not None
            counts["vectors"] += vec is not None
        version = con.execute("PRAGMA user_version").fetchone()[0]
    return {"logical_sha256": digest.hexdigest(), "counts": counts, "schema_version": version}


def build(db: Path, rows: int, per_file: int) -> dict:
    # This function runs in an exited, source-selected builder process.
    from bench_scale import build_fixture
    from goldmine.backends.mock import MockBackend
    from goldmine import fingerprints
    from goldmine.chunker import CHUNKER_VERSION
    from goldmine.store import Store

    vector = build_fixture(db, rows, per_file)
    store = Store(db)
    backend = MockBackend()
    fp = fingerprints.build(backend)
    try:
        scope = store.write_transaction() if hasattr(store, "write_transaction") else store.db
        with scope:
            # Bulk writes keep generation memory bounded. The English-only source
            # is NFC already; derived lexical indexes are rebuilt explicitly below.
            for start in range(1, rows + 1, BLOCK):
                ids = range(start, min(start + BLOCK, rows + 1))
                store.db.executemany("UPDATE segments SET excerpt=? WHERE id=?", ((body(sid), sid) for sid in ids))
                store.db.executemany("INSERT INTO fts(rowid,name,body,original) VALUES(?,?,?,NULL)",
                    ((sid, f"{file_id(sid, rows, per_file)}.txt", body(sid)) for sid in ids))
            store.db.execute("UPDATE files SET extractor_version=?,chunker_version=?,embed_id=?",
                (fingerprints.EXTRACTOR_VERSIONS["txt"], CHUNKER_VERSION, fingerprints.live_embed_id(backend)))
        store.set_meta("fingerprint_id", fingerprints.fingerprint_id(fp))
        store.set_meta("fingerprint_json", fingerprints.canonical(fp).decode())
        if hasattr(store, "reindex_lexical"):
            store.reindex_lexical()
        index_uuid = store.meta("index_uuid")
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        backend.close()
        store.close()
    logical = logical_identity(db)
    expected = {"files": math.ceil(rows / per_file), "segments": rows, "fts": rows, "vectors": rows}
    if logical["counts"] != expected:
        raise ValueError("native fixture has incomplete file/vector/FTS content")
    return {"schema": 2, "kind": "synthetic-capacity-text/v2", "rows": rows, "segments_per_file": per_file,
            "dim": DIM, "seed": SEED, "generation_block_rows": BLOCK, "generation_dtype": "float32",
            "stored_dtype": "<f2", "vector_logical_sha256": vector["logical_sha256"], **logical,
            "snapshot": db_snapshot(db), "fixture_peak_rss": peak(scope="fixture-builder-only"),
            "embedding_fingerprint": fp, "index_uuid": index_uuid, "quality_claim": False}


def readback(db: Path, locations: list[tuple[int, int]], rows: int, per_file: int) -> list[dict]:
    proofs = []
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        con.execute("PRAGMA query_only=ON")
        for fid, sid in sorted(set(locations)):
            row = con.execute("SELECT f.path,s.file,s.kind,s.loc,s.loc_end,t.body,s.vec FROM segments s "
                              "JOIN files f ON f.id=s.file JOIN fts t ON t.rowid=s.id WHERE s.id=?", (sid,)).fetchone()
            if row is None or row[:6] != (file_path(fid), fid, "chunk", float(sid), None, body(sid)):
                raise ValueError("CLI result does not read back to the actual deterministic FTS body")
            if fid != file_id(sid, rows, per_file) or len(row[6] or b"") != DIM * 2:
                raise ValueError("CLI result has the wrong vector/file mapping")
            proofs.append({"file_id": fid, "segment_id": sid, "path": row[0], "loc": row[3], "body": row[5],
                           "vector_sha256": hashlib.sha256(row[6]).hexdigest()})
    return proofs
