"""Read-only stored-text shapes for supported legacy/current SQLite snapshots."""
from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3
import unicodedata


from .deadline import check_deadline
from .lexical import LEXICAL_CJK_VERSION, LEXICAL_VERSION, normalize


def _columns(db, table):
    check_deadline()
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _require(db, table, columns):
    if not set(columns) <= _columns(db, table):
        raise sqlite3.OperationalError(f"required {table} stored columns are missing")


# Per-connection projection memo (SC-03): a connection pins one snapshot, so
# its shape cannot go stale. sqlite3.Connection objects are not
# weak-referenceable, so entries hold a strong reference inside a small FIFO;
# eviction drops the reference and only costs one recomputed PRAGMA read.
# Different connections (new request/snapshot) never share an entry.
_PROJECTION_MEMO: dict[int, tuple[object, str]] = {}
_PROJECTION_MEMO_MAX = 8


def _fts_shape(db):
    key = id(db)
    entry = _PROJECTION_MEMO.get(key)
    if entry is not None and entry[0] is db:
        return entry[1]
    columns = _columns(db, "fts")
    if not {"name", "body"} <= columns:
        raise sqlite3.OperationalError("required fts stored columns are missing")
    if "original" in columns:
        shape = "original"
    else:
        check_deadline()
        if db.execute("PRAGMA user_version").fetchone()[0] != 4:
            raise sqlite3.OperationalError("required fts.original column is missing")
        shape = "schema4-body"
    if len(_PROJECTION_MEMO) >= _PROJECTION_MEMO_MAX:
        _PROJECTION_MEMO.pop(next(iter(_PROJECTION_MEMO)))
    _PROJECTION_MEMO[key] = (db, shape)
    return shape


def fts_projection(db, alias=""):
    """Fixed internal SQL aliases only; no views or writes.

    The PRAGMA shape is memoized per live connection — identical pure work is
    not repeated per 400-row read batch — and a connection's snapshot keeps
    the memo valid by construction; nothing survives the connection.
    """
    if alias and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("invalid internal FTS alias")
    prefix = alias + "." if alias else ""
    if _fts_shape(db) == "original":
        return f"coalesce({prefix}original,{prefix}body)", prefix + "original"
    return prefix + "body", "NULL"


@dataclass(frozen=True)
class ReadSourceProfile:
    schema: int
    original: bool
    cjk: bool
    exact: bool

    def validate_original(self, original, body):
        if original is not None:
            expected = unicodedata.normalize("NFC", original) if self.schema == 4 else normalize(original)
            if expected != body:
                raise ValueError("stored original/body normalization is inconsistent")

    def diagnostics(self):
        return {"schema_version": self.schema, "adapter_version": "stored-source.v1",
                "original_column": self.original, "cjk_index": self.cjk, "exact_index": self.exact,
                "exact_recall": "derived" if self.exact else "bounded_legacy_source",
                "metadata_recall": "whole_field_filtered_derived" if self.exact else "whole_field_filtered_legacy_fts",
                "original_provenance": "stored_original_or_body" if self.original else "stored_body_only"}


def read_source_profile(db):
    """Call inside the request transaction. Errors never choose a lower profile."""
    check_deadline()
    schema = db.execute("PRAGMA user_version").fetchone()[0]
    if schema not in (4, 5, 6, 7):
        raise sqlite3.OperationalError("source verification needs supported schema4/5/6/7")
    _require(db, "files", ("id", "path", "kind", "info"))
    _require(db, "segments", ("id", "file", "kind"))
    _require(db, "meta", ("key", "value"))
    # The fts column checks share the per-snapshot projection memo (SC-03);
    # the shape reports the stored-original column presence with the same
    # required-column error behavior as the previous direct column check.
    fts_shape = _fts_shape(db)
    if schema >= 5 and fts_shape != "original":
        raise sqlite3.OperationalError("required fts stored columns are missing")
    lexical = db.execute("SELECT value FROM meta WHERE key='lexical_version'").fetchone()
    lexical = lexical[0] if lexical else None
    if schema == 4:
        if lexical is not None:
            raise sqlite3.OperationalError("schema4 claims an inconsistent derived lexical version")
        return ReadSourceProfile(4, fts_shape == "original", False, False)
    expected = LEXICAL_CJK_VERSION if schema == 5 else LEXICAL_VERSION
    if lexical != expected:
        raise sqlite3.OperationalError(f"schema{schema} has an inconsistent derived lexical version")
    _require(db, "fts_cjk", ("tokens",))
    if schema >= 6:
        _require(db, "lookup_terms", ("normalized_term", "raw_term", "file_id", "segment_id", "source", "match_type"))
    # exact derived index exists from v6 onward (schema bumps above 6 are
    # additive and keep it; P1-3 v7)
    return ReadSourceProfile(schema, True, True, schema >= 6)
