"""Explicitly built, opt-in vector cache; SQLite always remains the truth.

This additive protocol does not change the schema version. Current v6 writers
drop/reinstall only ``gm_writer_*`` fences, so the persistent epoch triggers
survive their write scopes. Readers require their exact definitions, not merely
a metadata flag. No automatic build, migration, or generation deletion occurs.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
import re
import sqlite3
from uuid import UUID, uuid4

import numpy as np

from ._vector_cache_files import (DIMENSION, publish_manifest, read_arrays,
                                 read_manifest, write_generation)
from .deadline import RequestTimeout, check_deadline

PROTOCOL = 1
_TRIGGERS = {
    f"gm_vector_epoch_{action.lower()}": (
        f"CREATE TRIGGER gm_vector_epoch_{action.lower()} AFTER {action} ON segments BEGIN "
        "UPDATE meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='vector_epoch' "
        "AND value<>'' AND value NOT GLOB '*[^0-9]*' AND length(value)<19; "
        "SELECT CASE WHEN changes()<>1 THEN RAISE(ROLLBACK, 'vector epoch missing or invalid; rebuild vector cache protocol') END; END"
    ) for action in ("INSERT", "UPDATE", "DELETE")
}


class _MappedSequence(Sequence):
    """Python scalar iteration for SQLite, zero-copy arrays for vector search."""

    def __init__(self, array, scalar):
        self._array, self._scalar = array, scalar

    def __len__(self):
        return len(self._array)

    def __getitem__(self, index):
        value = self._array[index]
        if isinstance(value, np.ndarray):
            return _MappedSequence(value, self._scalar)
        return self._scalar(value)

    def __array__(self, dtype=None, copy=None):
        if copy is False and dtype is not None and self._array.dtype != np.dtype(dtype):
            raise ValueError("requested dtype cannot be provided without a copy")
        return np.array(self._array, dtype=dtype, copy=True) if copy else np.asarray(self._array, dtype=dtype)

    def __eq__(self, other):
        return bool(np.array_equal(self._array, np.asarray(other)))


def cache_directory(store) -> Path:
    return store.path.with_name(store.path.name + ".vectors")


def _triggers_match(db) -> bool:
    actual = dict(db.execute("SELECT name, sql FROM sqlite_schema WHERE type='trigger' AND name LIKE 'gm_vector_epoch_%'"))
    return actual == _TRIGGERS


def _epoch(value) -> int:
    if not isinstance(value, str) or re.fullmatch(r"0|[1-9][0-9]{0,17}", value) is None:
        raise ValueError("missing or invalid vector epoch")
    return int(value)


def install_vector_cache_protocol(store) -> None:
    """Install/repair epoch tracking inside one fenced writer transaction."""
    if store.ro:
        raise ValueError("building vector cache requires a writable Store")
    with store.write_transaction():
        version = store.meta("vector_cache_protocol")
        if version not in (None, str(PROTOCOL)):
            raise ValueError("unknown vector-cache protocol; upgrade before building")
        epoch = store.meta("vector_epoch")
        valid = (version == str(PROTOCOL) and epoch is not None
                 and store.meta("vector_cache_epoch_uuid") is not None and _triggers_match(store.db))
        if valid:
            _epoch(epoch)
            return
        # Any repair changes the generation identity. Never reset a surviving
        # counter, which could revalidate an old manifest after a lost trigger.
        value = str(_epoch(epoch) + 1) if epoch is not None else "0"
        store.db.execute("INSERT INTO meta(key,value) VALUES('vector_epoch',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))
        store.db.execute("INSERT INTO meta(key,value) VALUES('vector_cache_protocol',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(PROTOCOL),))
        store.db.execute("INSERT INTO meta(key,value) VALUES('vector_cache_epoch_uuid',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(uuid4()),))
        for name, sql in _TRIGGERS.items():
            store.db.execute(f"DROP TRIGGER IF EXISTS {name}")
            store.db.execute(sql)


def _identity(store) -> dict:
    if store.meta("vector_cache_protocol") != str(PROTOCOL) or not _triggers_match(store.db):
        raise ValueError("vector epoch tracking is unavailable or changed")
    index_uuid = store.meta("index_uuid")
    if not index_uuid or str(UUID(index_uuid)) != index_uuid:
        raise ValueError("vector-cache index UUID missing or invalid")
    epoch_uuid = store.meta("vector_cache_epoch_uuid")
    if not epoch_uuid or str(UUID(epoch_uuid)) != epoch_uuid:
        raise ValueError("vector-cache epoch protocol UUID missing or invalid")
    return {"index_uuid": index_uuid, "fingerprint_id": store.meta("fingerprint_id"),
            "fingerprint_json": store.meta("fingerprint_json"), "vector_epoch": _epoch(store.meta("vector_epoch")),
            "epoch_uuid": epoch_uuid, "schema": int(store.db.execute("PRAGMA user_version").fetchone()[0]), "protocol": PROTOCOL}


def build_vector_cache(store) -> dict:
    """Build one complete snapshot and atomically publish; do not enable it.

    A concurrent ingest can make the new generation immediately obsolete. Its
    epoch then fails validation and the next reader safely uses SQLite instead.
    """
    install_vector_cache_protocol(store)
    root = cache_directory(store)
    if root.is_symlink():
        raise ValueError("vector-cache root must not be a symbolic link")
    with nullcontext() if store.db.in_transaction else store.read_snapshot():
        identity = _identity(store)
        count = store.db.execute("SELECT count(*) FROM segments WHERE vec IS NOT NULL AND length(vec)=?", (DIMENSION * 2,)).fetchone()[0]
        rows = store.db.execute("SELECT id,file,modality,vec FROM segments WHERE vec IS NOT NULL AND length(vec)=? ORDER BY id", (DIMENSION * 2,))
        generation, arrays = write_generation(root, rows, count)
        manifest = {"version": PROTOCOL, "generation": generation, "identity": identity,
                    "rows": count, "dimension": DIMENSION, "arrays": arrays}
        digest = publish_manifest(root, manifest)
    return {"status": "BUILT", "cache_root": str(root.resolve()), "manifest_sha256": digest,
            "generation": generation, "rows": count, "identity": identity,
            "enabled": store.meta("vector_cache_enabled") == "1"}


def load_vector_cache(store, *, verify_checksums=False):
    """Return an immutable VectorTable or None on any cache mismatch.

    Request deadlines/cancellation propagate. The surrounding request may own a
    read snapshot already; in that case all checks use that exact snapshot.
    ``verify_checksums=True`` additionally hashes the whole matrix for audits.
    """
    arrays = None
    transferred = False
    try:
        check_deadline()
        with nullcontext() if store.db.in_transaction else store.read_snapshot():
            manifest = read_manifest(cache_directory(store))
            if set(manifest) != {"version", "generation", "identity", "rows", "dimension", "arrays"}:
                raise ValueError("vector-cache manifest shape invalid")
            if (type(manifest["version"]) is not int or manifest["version"] != PROTOCOL
                    or type(manifest["dimension"]) is not int or manifest["dimension"] != DIMENSION
                    or type(manifest["rows"]) is not int or manifest["rows"] < 0
                    or not isinstance(manifest["identity"], dict)
                    or any(type(manifest["identity"].get(key)) is not int for key in ("schema", "protocol", "vector_epoch"))
                    or manifest["identity"] != _identity(store)):
                raise ValueError("vector-cache identity/epoch mismatch")
            arrays = read_arrays(cache_directory(store), manifest, verify_checksums=verify_checksums)
            check_deadline()
            from .store import VectorTable

            table = VectorTable(ids=_MappedSequence(arrays["ids"], int), file_ids=_MappedSequence(arrays["file_ids"], int),
                                modalities=_MappedSequence(arrays["modalities"], str), matrix=arrays["matrix"])
        transferred = True
        return table
    except RequestTimeout:
        raise
    except (OSError, ValueError, KeyError, TypeError, OverflowError, EOFError, sqlite3.Error):
        check_deadline()
        return None
    finally:
        if arrays is not None and not transferred:
            for array in arrays.values():
                array._mmap.close()


def set_vector_cache_enabled(store, enabled: bool) -> None:
    """Explicit opt-in after a measured performance gate; never build here."""
    if type(enabled) is not bool:
        raise ValueError("vector-cache enable flag must be boolean")
    if enabled and load_vector_cache(store) is None:
        raise ValueError("build a current valid vector cache before enabling it")
    store.set_meta("vector_cache_enabled", "1" if enabled else "0")
    store._vec_cache = None
    store._vec_cache_key = None
