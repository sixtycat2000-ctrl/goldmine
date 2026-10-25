"""Whole metadata eligibility before candidate bounds, without body reads."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import PurePath
import sqlite3

from .deadline import RequestTimeout, check_deadline
from .lexical import identifier_tokens, path_identifier_tokens, term_forms
from .query_obligations import literal_equal


def decode_info(info):
    value = json.loads(info) if info is not None else None
    if value is not None and not isinstance(value, dict):
        raise ValueError("invalid stored anchor metadata")
    return value


def metadata_fields(path, info):
    if not isinstance(path, str):
        raise ValueError("metadata pointer has no actual file path")
    value = decode_info(info)
    title = (value or {}).get("title")
    fields = {"path": path, "basename": PurePath(path).name}
    if isinstance(title, str):
        fields["title"] = title
    return fields


def derived_terms(field, value):
    if field in {"basename", "path"}:
        return {value, *path_identifier_tokens(value)}
    return set(identifier_tokens(value))


@contextmanager
def whole_metadata_predicate(db, allowed, *, derived):
    """A temporary connection function, not TEMP/database DDL or a source cache.

    SQL can examine extra metadata; only returned/materialized readback SIDs
    consume the Resolver's2000-row budget. SQL/request deadlines still apply.
    Pure actual-field parsing and derived-term sets are memoized for the
    lifetime of this ONE predicate invocation, keyed by the actual
    path/info/field bytes (SC-03); every row's pointer, file, source, raw,
    normalized, match-type and full-field checks are unchanged.
    """
    name = "_gm_full_metadata_v1"
    errors = []
    fields_cache = {}
    derived_cache = {}

    def eligible(sid, sid_file, kind, fid, path, info, lookup_file, field, raw, normalized, match_type, query):
        try:
            check_deadline()
            if sid is None or sid_file is None or fid is None or sid_file != fid:
                raise ValueError("anchor lookup points at a missing stored segment/file")
            if fid not in allowed:
                return 0
            if kind != "name":
                raise ValueError("metadata anchor pointer is not a name segment")
            fields_key = (path, info)
            if fields_key not in fields_cache:
                fields_cache[fields_key] = metadata_fields(path, info)
            fields = fields_cache[fields_key]
            if not derived:
                return int(any(literal_equal(query, value) for value in fields.values()))
            value = fields.get(field)
            derived_key = (field, value)
            if derived_key not in derived_cache:
                derived_cache[derived_key] = derived_terms(field, value) if isinstance(value, str) else None
            if (lookup_file != fid or value is None or (normalized, match_type) not in term_forms(raw)
                    or raw not in derived_cache[derived_key]):
                raise ValueError("anchor lookup pointer does not match its actual stored source")
            return int(raw == value and literal_equal(query, value))
        except (ValueError, TypeError, RequestTimeout) as error:
            if not errors:
                errors.append(error)
            raise

    db.create_function(name, 12, eligible, deterministic=True)
    try:
        yield name
    except sqlite3.Error:
        if errors:
            raise errors[0]
        raise
    finally:
        db.create_function(name, 12, None)
