"""SQLite writer capability: committed databases always reject legacy writers.

Persistent triggers use RAISE(ROLLBACK), so an already-open old connection also
rolls back an earlier FTS delete in the same transaction. Current writers remove
the fences only inside their IMMEDIATE transaction, then restore before commit.
Other connections never observe the temporary removal. This avoids depending on
a missing SQL function, which aborts just one statement rather than the transaction.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar

from .deadline import check_deadline

_TABLES = ("files", "segments", "meta")
_ACTIONS = ("INSERT", "UPDATE", "DELETE")
_active: ContextVar[tuple[sqlite3.Connection, ...]] = ContextVar("goldmine_writers", default=())


def register_writer(db: sqlite3.Connection, supported_schema: int) -> None:
    # Capture the running build's version, never a later on-disk user_version.
    db.create_function("goldmine_writer_schema", 0, lambda: supported_schema)


def remove_fences(db: sqlite3.Connection) -> None:
    for table in _TABLES:
        for action in _ACTIONS:
            db.execute(f"DROP TRIGGER IF EXISTS gm_writer_{table}_{action.lower()}")


def install_fences(db: sqlite3.Connection) -> None:
    for table in _TABLES:
        for action in _ACTIONS:
            db.execute(
                f"CREATE TRIGGER IF NOT EXISTS gm_writer_{table}_{action.lower()} "
                f"BEFORE {action} ON {table} BEGIN SELECT RAISE(ROLLBACK, "
                "'index writer is obsolete or outside write_transaction; stop/restart goldmine writers'); END"
            )


@contextmanager
def writer_scope(db: sqlite3.Connection):
    """One atomic write scope; nested Store calls cannot commit their parent."""
    if db in _active.get():
        try:
            yield db
        except BaseException:
            db.rollback()
            raise
        return
    if db.in_transaction:
        raise RuntimeError("write_transaction must begin before raw SQLite mutations")
    check_deadline()
    db.execute("BEGIN IMMEDIATE")
    token = _active.set((*_active.get(), db))
    try:
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        supported = int(db.execute("SELECT goldmine_writer_schema()").fetchone()[0])
        if version != supported:
            raise RuntimeError(
                f"index schema v{version} needs a current writer (this connection supports v{supported}); "
                "stop/restart goldmine writers"
            )
        remove_fences(db)
        yield db
        check_deadline()
        if not db.in_transaction:
            raise RuntimeError("a nested write committed or rolled back the outer transaction")
        install_fences(db)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        _active.reset(token)
