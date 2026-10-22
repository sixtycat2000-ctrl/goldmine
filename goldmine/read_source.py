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


def fts_projection(db, alias=""):
    """Fixed internal SQL aliases only; no views, writes or retained cache."""
    if alias and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("invalid internal FTS alias")
    prefix = alias + "." if alias else ""
    columns = _columns(db, "fts")
    if not {"name", "body"} <= columns:
        raise sqlite3.OperationalError("required fts stored columns are missing")
    if "original" in columns:
        return f"coalesce({prefix}original,{prefix}body)", prefix + "original"
    check_deadline()
    if db.execute("PRAGMA user_version").fetchone()[0] != 4:
        raise sqlite3.OperationalError("required fts.original column is missing")
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
    if schema not in (4, 5, 6):
        raise sqlite3.OperationalError("source verification needs supported schema4/5/6")
    _require(db, "files", ("id", "path", "kind", "info"))
    _require(db, "segments", ("id", "file", "kind"))
    _require(db, "meta", ("key", "value"))
    columns = _columns(db, "fts")
    if not {"name", "body"} <= columns or schema >= 5 and "original" not in columns:
        raise sqlite3.OperationalError("required fts stored columns are missing")
    lexical = db.execute("SELECT value FROM meta WHERE key='lexical_version'").fetchone()
    lexical = lexical[0] if lexical else None
    if schema == 4:
        if lexical is not None:
            raise sqlite3.OperationalError("schema4 claims an inconsistent derived lexical version")
        return ReadSourceProfile(4, "original" in columns, False, False)
    expected = LEXICAL_CJK_VERSION if schema == 5 else LEXICAL_VERSION
    if lexical != expected:
        raise sqlite3.OperationalError(f"schema{schema} has an inconsistent derived lexical version")
    _require(db, "fts_cjk", ("tokens",))
    if schema == 6:
        _require(db, "lookup_terms", ("normalized_term", "raw_term", "file_id", "segment_id", "source", "match_type"))
    return ReadSourceProfile(schema, True, True, schema == 6)
