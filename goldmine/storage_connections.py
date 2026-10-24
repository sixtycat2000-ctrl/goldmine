"""SQLite connection policy, schema initialization and consistent read snapshots.

Store owns records and SQL operations; this module owns opening a connection.
Read-only opening never creates files, applies DDL, or runs a migration.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import migrations
from .deadline import check_deadline, remaining_timeout
from .storage_writes import install_fences, register_writer

_DDL = """
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files(
    id                INTEGER PRIMARY KEY,
    path              TEXT NOT NULL UNIQUE,
    source            TEXT NOT NULL DEFAULT 'file',
    kind              TEXT NOT NULL,
    size              INTEGER NOT NULL,
    mtime             REAL,
    sha256            TEXT,
    state             TEXT NOT NULL DEFAULT 'pending',
    capability        TEXT,
    detail            TEXT,
    info              TEXT,
    extractor_version TEXT,
    chunker_version   TEXT,
    embed_id          TEXT,
    content_date      REAL,
    indexed_at        REAL
);
CREATE INDEX IF NOT EXISTS files_state ON files(state, kind);
CREATE INDEX IF NOT EXISTS files_capability ON files(capability);
CREATE INDEX IF NOT EXISTS files_kind_cap ON files(kind, capability);

CREATE TABLE IF NOT EXISTS segments(
    id       INTEGER PRIMARY KEY,
    file     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL,
    modality TEXT NOT NULL,
    loc      REAL,
    loc_end  REAL,
    excerpt  TEXT,
    vec      BLOB
);
CREATE INDEX IF NOT EXISTS segments_file ON segments(file);
CREATE INDEX IF NOT EXISTS segments_vec_file ON segments(file) WHERE vec IS NOT NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    name, body, original UNINDEXED,
    tokenize = "porter unicode61 remove_diacritics 2 categories 'L* N* Co M*'"
);

CREATE TABLE IF NOT EXISTS distill_runs(
    id          TEXT PRIMARY KEY,     -- sha256(algo|tau|scope_json|corpus_hash|mode)
    algo        TEXT NOT NULL,        -- "ens-coassoc.v3" (v2-era rows keep their historical string)
    tau         REAL NOT NULL,
    tau_source  TEXT NOT NULL,        -- fixed|manual|inherited (v3); historical rows may carry otsu/floor/ceil/percentile
    scope_json  TEXT NOT NULL,        -- canonical: {under, kinds, after}
    corpus_hash TEXT NOT NULL,        -- audit identity (D9; never gates reuse, F4)
    mode        TEXT NOT NULL,        -- full|incremental
    stats_json  TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cards(
    id             INTEGER PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES distill_runs(id),
    view_id        TEXT NOT NULL,     -- D7: sha256(algo + canonical scope_json)[:16]
    title          TEXT NOT NULL,
    title_kind     TEXT NOT NULL DEFAULT 'terms',  -- terms|sentence|llm
    terms          TEXT NOT NULL,     -- JSON array (renamed from title_terms)
    excerpts       TEXT NOT NULL,     -- JSON [{text,path,kind,loc,loc_end,role}]
    size           INTEGER NOT NULL,
    n_files        INTEGER NOT NULL,
    parent_id      INTEGER REFERENCES cards(id),    -- header card row (status='header')
    centroid       BLOB,              -- 768-dim fp16 (1536 B) via util.pack_fp16
    status         TEXT NOT NULL DEFAULT 'active',  -- active|dissolved|header
    llm_title      TEXT,
    llm_summary    TEXT,
    llm_model      TEXT,
    fingerprint_id TEXT NOT NULL,
    distill_algo   TEXT NOT NULL,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS card_chunks(
    card   INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    chunk  INTEGER NOT NULL,      -- NO segments FK (DEVIATION from R6 §3.1):
                                  -- PRAGMA foreign_keys=ON + this FK would make
                                  -- ANY segment deletion fail once cards exist;
                                  -- §4.4 assigns tombstone cleanup to the
                                  -- dissolve sweep, which requires tombstones
                                  -- to be storable. Pinned shape contradiction
                                  -- resolved in favor of §4.4's sweep design.
    weight REAL NOT NULL,
    UNIQUE(card, chunk)
);
CREATE INDEX IF NOT EXISTS card_chunks_card ON card_chunks(card);
CREATE INDEX IF NOT EXISTS cards_view ON cards(view_id, status);

CREATE TABLE IF NOT EXISTS distill_views(
    name        TEXT PRIMARY KEY,
    scope_json  TEXT NOT NULL,
    params_json TEXT NOT NULL,        -- {threshold: float|null, min_members: int}
    created_at  REAL NOT NULL
);
"""



def connect_index(
    path: Path | str, *, ro: bool = False, timeout_s: float = 60.0
) -> sqlite3.Connection:
    """The ONE sqlite3.connect for the package (storage critique 03 F6): every
    caller — Store, doctor's deep checks, migrations' backup verification —
    goes through here so the pragmas are set exactly once and identically.

    Read-write: WAL (persistent), busy_timeout 60 s, foreign_keys ON,
    synchronous=NORMAL. The index is derived, rebuildable data — WAL+NORMAL is
    SQLite's recommended pairing for that case (commits ~4.4x cheaper than the
    FULL default, measured 23.9 -> 5.4 ms per small commit; an app crash still
    preserves every committed transaction, only a power loss may drop the last
    one).

    Read-only (``ro=True``): a mode=ro URI — never creates the file, never
    writes, no journal/WAL pragmas. For short-lived inspection connections
    (doctor --deep, backup verification).

    DELIBERATE EXCEPTION — migrations._vacuum_into keeps a plain connect: its
    connection must stay OUT of WAL mode so the VACUUM INTO copy is written as
    a plain single-file (rollback-journal) database — a WAL-flagged copy is
    exactly what read-only verification chokes on (03 F4).
    """
    path = Path(path)
    timeout = remaining_timeout(timeout_s)
    if ro:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout)
        con.execute(f"PRAGMA busy_timeout = {max(0, int(timeout * 1000))}")
        con.execute("PRAGMA query_only = ON")
        return con
    con = sqlite3.connect(str(path), timeout=timeout)
    register_writer(con, migrations.TARGET)
    # busy_timeout 60000 per the ARCHITECTURE §6 DDL. (Build note said 5000;
    # the binding DDL wins — see DEVIATION notes in the build report.)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute(f"PRAGMA busy_timeout = {max(0, int(timeout * 1000))}")
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA synchronous = NORMAL")
    return con



def open_index(path: Path, *, ro: bool = False, timeout_s: float = 60.0) -> sqlite3.Connection:
    path = Path(path)
    if not ro:
        path.parent.mkdir(parents=True, exist_ok=True)
    db = connect_index(path, ro=ro, timeout_s=timeout_s)
    # ORDERING (storage critique 03 F5): user_version is read BEFORE any
    # DDL. The old order — executescript(_DDL) then version check — meant
    # (a) a FAILED migration left a v1 file with pre-created v2-shaped
    # tables (the DDL ran in autocommit before the ladder rolled back),
    # and (b) an old build opening a future schema re-created any table
    # that future schema had dropped, before refusing. Now: refuse future
    # schemas first, hand pre-TARGET files straight to the ladder (the
    # ladder owns EVERY schema write for them), and only fresh (0) /
    # already-current files see the idempotent _DDL.
    version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if version > migrations.TARGET:
        db.close()
        raise RuntimeError(
            f"index {path} has schema v{version}, newer than this build "
            f"(v{migrations.TARGET}) — upgrade goldmine"
        )
    if ro:
        # Inspection never creates tables, stamps meta, or upgrades old
        # schemas. Application operations report absent capabilities.
        pass
    elif 0 < version < migrations.TARGET:
        # The tested one-per-version ladder (migrations.py): existing
        # pre-TARGET files are upgraded in ONE additive transaction per
        # open, with a pre-migration snapshot when the file is large.
        # NO _DDL before this — a failed migration must leave the file
        # exactly at its old version, with no current-shaped tables or
        # indexes pre-created around it.
        try:
            migrations.migrate(db, path=path)
        except Exception:
            db.close()
            raise
    else:
        # Fresh database (version 0, no tables) or already-current file:
        # create/ensure the full current shape, idempotently. Fresh dbs are
        # then stamped at TARGET by the ladder's fresh-db rule (migrations
        # rule 3) — no ladder rows touched, no history entry.
        db.executescript(_DDL)
        if version == 0:
            try:
                from .lexical_store import rebuild_derived

                rebuild_derived(db, exact=migrations.TARGET >= 6)
                migrations.migrate(db, path=path)
            except Exception:
                db.close()
                raise
    if not ro:
        # Both fresh DDL and the migration ladder leave a committed database
        # fenced. Reinstall atomically if a current-format fixture omitted it.
        db.execute("BEGIN IMMEDIATE")
        install_fences(db)
    db.commit()
    return db


@contextmanager
def read_snapshot(db: sqlite3.Connection):
    """Pin all reads for an evidence request to one WAL generation."""
    if db.in_transaction:
        raise RuntimeError("read_snapshot requires an idle Store connection")
    check_deadline()
    db.execute("BEGIN")
    try:
        # A deferred BEGIN needs its first read to pin the snapshot.
        db.execute("SELECT count(*) FROM sqlite_schema").fetchone()
        db.set_progress_handler(_check_read_deadline, 1000)
        yield
        check_deadline()
    finally:
        db.set_progress_handler(None, 0)
        db.rollback()


def _check_read_deadline() -> int:
    try:
        check_deadline()
    except TimeoutError:
        return 1
    return 0
