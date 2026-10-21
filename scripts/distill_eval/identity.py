"""Exact local source and complete stored-row identities for real distill evidence."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import UUID

from acceptance_lib.evidence import InvalidEvidence, sha256


def evaluator_identity(root):
    root = Path(root).resolve()
    paths = list((root / "goldmine").rglob("*.py"))
    for package in ("distill_eval", "product_eval", "answer_eval"):
        paths += list((root / "scripts" / package).rglob("*.py"))
    paths += [root / "scripts" / name for name in (
        "eval_distill.py", "capture_distill.py", "prepare_distill.py",
        "acceptance_lib/__init__.py", "acceptance_lib/distill_evidence.py",
        "acceptance_lib/evidence.py", "acceptance_lib/owned_process.py")]
    return {str(path.relative_to(root)): sha256(path) for path in sorted(set(paths))}


def _value(value):
    return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()} if isinstance(value, bytes) else value


def index_identity(path):
    """Read a closed standalone snapshot: all files/segments/FTS, not only vectors."""
    try:
        return _read_index(path)
    except sqlite3.Error as exc:
        raise InvalidEvidence(f"cannot read prepared SQLite snapshot: {exc}") from exc


def _read_index(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise InvalidEvidence("missing prepared index snapshot")
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise InvalidEvidence("prepared index must be a closed standalone snapshot without pending WAL/journal")
    before = sha256(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        meta = dict(db.execute("SELECT key,value FROM meta"))
        try:
            if str(UUID(meta["index_uuid"])) != meta["index_uuid"]:
                raise ValueError("noncanonical UUID")
        except (KeyError, ValueError) as exc:
            raise InvalidEvidence("prepared index lacks a canonical index UUID") from exc
        result = {"user_version": db.execute("PRAGMA user_version").fetchone()[0],
                  "meta": {key: meta.get(key) for key in (
                      "index_uuid", "fingerprint_id", "fingerprint_json", "embed_id", "schema_version")}}
        for table in ("files", "segments"):
            cursor = db.execute(f"SELECT * FROM {table} ORDER BY id")
            columns = [column[0] for column in cursor.description]
            result[table] = {str(row[0]): dict(zip(columns, map(_value, row))) for row in cursor}
        result["fts"] = {str(row[0]): [_value(v) for v in row[1:]] for row in db.execute(
            "SELECT rowid,name,body,original FROM fts ORDER BY rowid")}
    if sha256(path) != before:
        raise InvalidEvidence("prepared snapshot changed during read-only identity audit")
    if any(str(row["file"]) not in result["files"] for row in result["segments"].values()) or set(result["fts"]) - set(result["segments"]):
        raise InvalidEvidence("prepared snapshot contains orphan segments/FTS evidence")
    return result


def assert_extension(base, extended):
    for key in ("user_version", "meta"):
        if base[key] != extended[key]:
            raise InvalidEvidence("+10 did not preserve the same base index/schema/embedding identity")
    for table in ("files", "segments", "fts"):
        if any(extended[table].get(key) != row for key, row in base[table].items()):
            raise InvalidEvidence(f"+10 changed existing {table} IDs/content/provenance")
    old_files = set(base["files"])
    if any(str(row["file"]) in old_files for sid, row in extended["segments"].items()
           if sid not in base["segments"]):
        raise InvalidEvidence("+10 inserted replacement/additional segments into an original file")


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def assert_prepared_copies(base, extended, proof):
    if (canonical_sha(base) != proof["base_identity_sha256"] or
            canonical_sha(extended) != proof["incremental_identity_sha256"]):
        raise InvalidEvidence("actual evaluation copies differ from the bound prepared snapshot rows")
    assert_extension(base, extended)
