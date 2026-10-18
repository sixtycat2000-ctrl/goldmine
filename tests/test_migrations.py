"""Tier-2: the v2 migration ladder (migrations.py) — B2 binding rule fixture,
purely-additive v1->v2, snapshot, refusal, backups, doctor --deep.

The v1 fixture is GENERATED at test time (REDESIGN §9 Q2's leaning): a real
index is built with the current store + mock backend, then stripped to the v1
shape (new columns removed, user_version=1, v2 meta keys deleted) so the
ladder runs against an honest pre-v2 file.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from goldmine import fingerprints, migrations
from goldmine.backends.mock import MockBackend
from goldmine.chunker import CHUNKER_VERSION
from goldmine.config import load
from goldmine.ingest import IngestPipeline
from goldmine.store import Store

# ------------------------------------------------------------------ helpers

_V1_FILES_DDL = """
CREATE TABLE files_v1(
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
    indexed_at        REAL
);
"""

_V2_ONLY_META_KEYS = ("embed_id", "embed_identity_json", "migration_history_json")


def _strip_to_v1(db_path: Path) -> None:
    """Simulate a v1 index: rebuild `files` without the v2 columns, stamp
    user_version=1, drop v2-only meta keys."""
    con = sqlite3.connect(str(db_path))
    try:
        con.executescript(_V1_FILES_DDL)
        con.execute(
            "INSERT INTO files_v1 SELECT id, path, source, kind, size, mtime, "
            "sha256, state, capability, detail, info, extractor_version, "
            "indexed_at FROM files"
        )
        con.execute("DROP TABLE files")
        con.execute("ALTER TABLE files_v1 RENAME TO files")
        con.execute("CREATE INDEX IF NOT EXISTS files_state ON files(state, kind)")
        con.execute("CREATE INDEX IF NOT EXISTS files_capability ON files(capability)")
        for key in _V2_ONLY_META_KEYS:
            con.execute("DELETE FROM meta WHERE key = ?", (key,))
        con.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )
        con.execute("PRAGMA user_version = 1")
        con.commit()
    finally:
        con.close()


def _corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    root.mkdir(parents=True, exist_ok=True)
    (root / "notes.txt").write_text(
        "The scheduler evicts a pod when a node is under pressure.\n"
        "Tolerations keep workloads running.\n",
        encoding="utf-8",
    )
    (root / "guide.md").write_text(
        "# Sourdough Guide\n\nFeed the starter daily.\n\nBulk ferment.\n",
        encoding="utf-8",
    )
    (root / "numbers.txt").write_text("4815162342 9988776655\n", encoding="utf-8")
    (root / "secret.pem").write_text("-----BEGIN KEY-----\n", encoding="utf-8")
    return root


def _ingest(db_path: Path, root: Path, *, backend: MockBackend | None = None):
    cfg = load()
    store = Store(db_path)
    try:
        pipeline = IngestPipeline(cfg, store, backend or MockBackend(dim=768))
        return pipeline.run([str(root)])
    finally:
        store.close()


def _rows(db) -> dict[str, tuple]:
    return {
        r[0]: (r[1], r[2], r[3], r[4])
        for r in db.execute(
            "SELECT path, capability, chunker_version, embed_id, extractor_version "
            "FROM files"
        ).fetchall()
    }


# ------------------------------------------------------------ fresh databases


def test_fresh_store_creates_current_schema_directly(tmp_path):
    """Rule 3: fresh DBs create at TARGET via the store DDL — no ladder rows,
    no migration history noise."""
    db = tmp_path / "fresh.sqlite"
    store = Store(db)
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == migrations.TARGET
        columns = {r[1] for r in store.db.execute("PRAGMA table_info(files)")}
        assert {"chunker_version", "embed_id", "content_date"} <= columns
        # v3 shape ships in the fresh DDL too
        indexes = {
            r[0] for r in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert {"segments_vec_file", "files_kind_cap"} <= indexes
        assert store.meta("migration_history_json") is None
    finally:
        store.close()
    # reopening is a no-op
    store = Store(db)
    try:
        assert migrations.migrate(store.db) == []
    finally:
        store.close()


def test_newer_schema_refusal_shape_unchanged(tmp_path):
    db = tmp_path / "future.sqlite"
    store = Store(db)
    store.close()
    con = sqlite3.connect(str(db))
    con.execute(f"PRAGMA user_version = {migrations.TARGET + 1}")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="newer than this build"):
        Store(db)
    # direct ladder call refuses identically
    con = sqlite3.connect(str(db))
    try:
        with pytest.raises(RuntimeError, match="upgrade goldmine"):
            migrations.migrate(con)
    finally:
        con.close()


# ------------------------------------------------------ the v1 -> v2 payload


def test_v1_index_migrates_purely_additively(tmp_path):
    db = tmp_path / "v1.sqlite"
    _ingest(db, _corpus(tmp_path))
    store = Store(db)
    before_files = store.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    before_segments = store.db.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    before_vecs = store.db.execute(
        "SELECT id, vec FROM segments WHERE vec IS NOT NULL ORDER BY id"
    ).fetchall()
    before_fp = store.meta("fingerprint_json")
    store.close()

    _strip_to_v1(db)
    store = Store(db)  # reopening runs the ladder
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == migrations.TARGET
        columns = {r[1] for r in store.db.execute("PRAGMA table_info(files)")}
        assert {"chunker_version", "embed_id", "content_date"} <= columns

        # purely additive: nothing dropped, nothing re-embedded
        assert store.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == before_files
        assert store.db.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == before_segments
        after_vecs = store.db.execute(
            "SELECT id, vec FROM segments WHERE vec IS NOT NULL ORDER BY id"
        ).fetchall()
        assert [(i, bytes(v)) for i, v in after_vecs] == [
            (i, bytes(v)) for i, v in before_vecs
        ]
        # legacy fingerprint kept verbatim; schema meta caught up
        assert store.meta("fingerprint_json") == before_fp
        assert store.meta("schema_version") == str(migrations.TARGET)

        # backfill: the V1 chunker id everywhere (NOT the current constant —
        # the D3 re-chunk must actually happen for upgraded indexes),
        # embed_id exactly on vec-bearing files
        rows = store.db.execute(
            "SELECT chunker_version, embed_id, "
            "(id IN (SELECT DISTINCT file FROM segments WHERE vec IS NOT NULL)) "
            "FROM files"
        ).fetchall()
        assert rows and all(r[0] == migrations.V1_CHUNKER_VERSION for r in rows)
        assert migrations.V1_CHUNKER_VERSION != CHUNKER_VERSION  # the whole point
        assert all((r[1] is not None) == bool(r[2]) for r in rows)
        # the backfill note names the constant + the re-chunk consequence
        assert migrations.V1_CHUNKER_VERSION in (store.meta("chunker_backfill_note") or "")

        # history recorded: every applied step (v1->v2 AND v2->v3)
        history = json.loads(store.meta("migration_history_json") or "[]")
        assert [e["to"] for e in history] == [2, migrations.TARGET]
        assert all("app_version" in e and "ms" in e for e in history)
    finally:
        store.close()


def test_v1_lexical_only_index_backfills_no_embed_ids(tmp_path):
    """Risk 05 §7-1: a --lexical-only v1 index has no fingerprint — the
    backfill must leave embed_id NULL everywhere (vectorless files cannot be
    embed-stale) while chunker bookkeeping still catches up."""
    db = tmp_path / "lex.sqlite"
    cfg = load()
    store = Store(db)
    try:
        IngestPipeline(cfg, store, None, lexical_only=True).run(
            [str(_corpus(tmp_path))]
        )
        assert store.meta("fingerprint_id") is None
    finally:
        store.close()
    _strip_to_v1(db)
    store = Store(db)
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == migrations.TARGET
        assert store.db.execute(
            "SELECT COUNT(*) FROM files WHERE embed_id IS NOT NULL"
        ).fetchone()[0] == 0
        assert store.meta("embed_id") is None
        report = fingerprints.staleness_report(store, None)
        assert report["embed_stale"] == 0  # no vec-bearing files at all
        assert report["vec_files"] == 0
        # the chunk.v1 backfill is chunker-stale BY DESIGN (REDESIGN §6): the
        # content rows must show as stale so the next plain ingest re-chunks
        content = store.db.execute(
            "SELECT COUNT(*) FROM files WHERE capability = 'content_indexed'"
        ).fetchone()[0]
        assert content > 0
        assert report["chunker_stale"] == content
        assert report["stale_files"] == content
    finally:
        store.close()


def test_migration_failure_rolls_back_completely(tmp_path):
    """One transaction per open: a step that dies mid-way leaves the v1 file
    exactly v1 (no columns, no stamp, no history)."""
    db = tmp_path / "crash.sqlite"
    _ingest(db, _corpus(tmp_path))
    store = Store(db)
    store.close()
    _strip_to_v1(db)

    original = migrations.MIGRATIONS[2]

    def exploding(db_con, from_version):
        db_con.execute(
            "ALTER TABLE files ADD COLUMN chunker_version TEXT"
        )  # partial work inside the transaction...
        raise RuntimeError("boom")  # ...then die

    migrations.MIGRATIONS[2] = exploding
    try:
        con = sqlite3.connect(str(db))
        try:
            with pytest.raises(RuntimeError, match="boom"):
                migrations.migrate(con, snapshot_threshold=1 << 40)
            version = con.execute("PRAGMA user_version").fetchone()[0]
            columns = {r[1] for r in con.execute("PRAGMA table_info(files)")}
            history = con.execute(
                "SELECT value FROM meta WHERE key = 'migration_history_json'"
            ).fetchone()
        finally:
            con.close()
    finally:
        migrations.MIGRATIONS[2] = original

    assert version == 1
    assert "chunker_version" not in columns
    assert history is None
    # and a clean retry after the bug is fixed still migrates
    store = Store(db)
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == migrations.TARGET
    finally:
        store.close()


# ------------------------------------------------- v3 step + DDL ordering (03)


def test_v2_index_migrates_to_v3_partial_indexes(tmp_path):
    """v2 -> v3 (storage critique 03 F1): an honest v2 file gains ONLY the two
    indexes — no row changes, history records the step, and the vec-file
    subquery actually uses the partial index afterwards."""
    db = tmp_path / "v2to3.sqlite"
    _ingest(db, _corpus(tmp_path))
    store = Store(db)
    try:
        n_files = store.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        n_vecs = store.db.execute(
            "SELECT COUNT(*) FROM segments WHERE vec IS NOT NULL"
        ).fetchone()[0]
    finally:
        store.close()
    # make it an honest v2 file: drop the v3 indexes, stamp v2, trim the tail
    con = sqlite3.connect(str(db))
    con.execute("DROP INDEX segments_vec_file")
    con.execute("DROP INDEX files_kind_cap")
    con.execute("DELETE FROM meta WHERE key = 'migration_history_json'")
    con.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
    con.execute("PRAGMA user_version = 2")
    con.commit()
    con.close()

    store = Store(db)  # reopen = ladder runs the v3 step
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == migrations.TARGET
        indexes = {
            r[0] for r in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert {"segments_vec_file", "files_kind_cap"} <= indexes
        # indexes only: no rows touched, no backfill
        assert store.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == n_files
        assert store.db.execute(
            "SELECT COUNT(*) FROM segments WHERE vec IS NOT NULL"
        ).fetchone()[0] == n_vecs
        history = json.loads(store.meta("migration_history_json") or "[]")
        assert [e["to"] for e in history] == [migrations.TARGET]
        # the index is not just created but USED by the status workload
        plan = store.db.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM segments WHERE vec IS NOT NULL"
        ).fetchall()
        assert any("segments_vec_file" in r[3] for r in plan)
    finally:
        store.close()


def _raw_v1_db(db_path: Path) -> None:
    """A minimal honest pre-v2 file: v1 files/segments/meta, NO fts, NO cards,
    NO card_chunks — the shape whose DDL the old Store.__init__ pre-created
    before the ladder (03 F5)."""
    con = sqlite3.connect(str(db_path))
    con.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE files(
            id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL DEFAULT 'file', kind TEXT NOT NULL,
            size INTEGER NOT NULL, mtime REAL, sha256 TEXT,
            state TEXT NOT NULL DEFAULT 'pending', capability TEXT,
            detail TEXT, info TEXT, extractor_version TEXT, indexed_at REAL);
        CREATE INDEX files_state ON files(state, kind);
        CREATE INDEX files_capability ON files(capability);
        CREATE TABLE segments(
            id INTEGER PRIMARY KEY,
            file INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, modality TEXT NOT NULL, loc REAL, loc_end REAL,
            excerpt TEXT, vec BLOB);
        CREATE INDEX segments_file ON segments(file);
    """)
    con.execute("INSERT INTO meta VALUES('schema_version', '1')")
    con.execute("INSERT INTO files(path, kind, size) VALUES('/old/a.txt', 'doc', 3)")
    con.execute("INSERT INTO segments(file, kind, modality) VALUES(1, 'name', 'none')")
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()


def test_failed_migration_leaves_no_current_shaped_ddl(tmp_path, monkeypatch):
    """03 F5 ordering fix: Store reads user_version BEFORE any DDL, so a FAILED
    migration leaves the v1 file v1-clean — no v2/v3 tables, no v3 indexes
    pre-created around the rolled-back ladder."""
    db = tmp_path / "order.sqlite"
    _raw_v1_db(db)

    def exploding(db_con, from_version):
        db_con.execute("ALTER TABLE files ADD COLUMN chunker_version TEXT")
        raise RuntimeError("boom")

    monkeypatch.setitem(migrations.MIGRATIONS, 2, exploding)
    with pytest.raises(RuntimeError, match="boom"):
        Store(db)

    con = sqlite3.connect(str(db))
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = {r[1] for r in con.execute("PRAGMA table_info(files)")}
        assert "chunker_version" not in columns
        tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert tables == {"meta", "files", "segments"}  # no fts/cards/card_chunks
        indexes = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "segments_vec_file" not in indexes
        assert "files_kind_cap" not in indexes
    finally:
        con.close()


def test_future_schema_refusal_happens_before_any_ddl(tmp_path):
    """03 F5: an old build opening a future schema must refuse BEFORE DDL —
    the refusal must not re-create tables the future schema dropped."""
    db = tmp_path / "future-shape.sqlite"
    Store(db).close()
    con = sqlite3.connect(str(db))
    con.execute("DROP TABLE cards")  # imagine the future schema dropped it
    con.execute(f"PRAGMA user_version = {migrations.TARGET + 1}")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="newer than this build"):
        Store(db)
    con = sqlite3.connect(str(db))
    try:
        tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        con.close()
    assert "cards" not in tables  # DDL did not run before the refusal


def test_migration_survives_sigkill_mid_step(tmp_path):
    """03 F3, empirically: SIGKILL a real migration mid-step (child process
    inside a slowed v3 step, mid-transaction) — the one-transaction ladder
    rolls back completely and a plain retry reaches TARGET."""
    db = tmp_path / "kill.sqlite"
    _ingest(db, _corpus(tmp_path))
    Store(db).close()  # bring to current, then make it an honest v2 file again
    con = sqlite3.connect(str(db))
    con.execute("DROP INDEX segments_vec_file")
    con.execute("DROP INDEX files_kind_cap")
    con.execute("DELETE FROM meta WHERE key = 'migration_history_json'")
    con.execute("PRAGMA user_version = 2")
    con.commit()
    con.close()

    repo_root = Path(__file__).resolve().parents[1]
    child = (
        "import sys, time; sys.path.insert(0, %r); "
        "from goldmine import migrations; "
        "orig = migrations.MIGRATIONS[3]; "
        "def slow(db_con, v): time.sleep(30); return orig(db_con, v); "
        "migrations.MIGRATIONS[3] = slow; "
        "from pathlib import Path; "
        "from goldmine.store import Store; "
        "Store(Path(%r))" % (str(repo_root), str(db))
    )
    proc = subprocess.Popen([sys.executable, "-c", child])
    time.sleep(3.0)  # child is now inside the slowed step, mid-transaction
    proc.kill()
    proc.wait()

    con = sqlite3.connect(str(db))
    try:
        # rollback, not partial application
        assert con.execute("PRAGMA user_version").fetchone()[0] == 2
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        indexes = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "segments_vec_file" not in indexes
    finally:
        con.close()
    store = Store(db)  # plain retry finishes the job
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == migrations.TARGET
        assert store.db.execute(
            "SELECT COUNT(*) FROM files"
        ).fetchone()[0] > 0
    finally:
        store.close()


def test_pre_migration_snapshot_only_when_large(tmp_path):
    db = tmp_path / "big.sqlite"
    _ingest(db, _corpus(tmp_path))
    store = Store(db)
    store.close()
    _strip_to_v1(db)

    # tiny index + default threshold: no snapshot
    Store(db).close()
    assert not Path(str(db) + f".pre-v{migrations.TARGET}.sqlite").exists()

    # forced threshold: snapshot taken, verified, and reusable
    _strip_to_v1(db)
    con = sqlite3.connect(str(db))
    try:
        applied = migrations.migrate(con, snapshot_threshold=1)
    finally:
        con.close()
    assert applied == [2, migrations.TARGET]
    snapshot = Path(str(db) + f".pre-v{migrations.TARGET}.sqlite")
    assert snapshot.is_file()
    assert migrations.verify_backup(snapshot) == "ok"
    # the snapshot is the PRE-migration state (v1)
    snap_con = sqlite3.connect(str(snapshot))
    try:
        assert snap_con.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        snap_con.close()


# ------------------------------------------------- THE B2 zero-stale fixture


def test_migrated_index_matches_fresh_v2_index_ids(tmp_path):
    """B2 binding rule, normative fixture (REDESIGN §3 C2 / 05 §4.1): build a
    v1-format index, run the ladder, and prove (a) a FRESH v2 index produces
    IDENTICAL per-file embed_id values for the same model constants, and (b)
    staleness reports ZERO stale files when the live backend matches."""
    corpus = _corpus(tmp_path)

    v1_db = tmp_path / "migrated.sqlite"
    backend = MockBackend(dim=768)
    _ingest(v1_db, corpus, backend=backend)
    _strip_to_v1(v1_db)
    migrated = Store(v1_db)  # open = migrate

    fresh_db = tmp_path / "fresh.sqlite"
    _ingest(fresh_db, corpus, backend=MockBackend(dim=768))
    fresh = Store(fresh_db)

    try:
        m_rows, f_rows = _rows(migrated.db), _rows(fresh.db)
        assert set(m_rows) == set(f_rows)
        for path in m_rows:
            m_cap, m_chunker, m_embed, m_extractor = m_rows[path]
            f_cap, f_chunker, f_embed, f_extractor = f_rows[path]
            # embed identity + extractor + capability: migrated == fresh
            # (B2 zero-re-embed); the chunker differs BY DESIGN — the
            # migration stamps 'chunk.v1' so the D3 re-chunk happens
            assert (m_cap, m_embed, m_extractor) == (f_cap, f_embed, f_extractor), (
                f"identity mismatch for {path}: migrated {m_rows[path]} vs "
                f"fresh {f_rows[path]}"
            )
            assert m_chunker == migrations.V1_CHUNKER_VERSION
            assert f_chunker == CHUNKER_VERSION
        # meta identities agree too — and the embed id is NEVER the old
        # combined fingerprint_id (that hash still covers preprocessing)
        live = fingerprints.live_embed_id(MockBackend(dim=768))
        assert migrated.meta("embed_id") == fresh.meta("embed_id") == live
        assert live != migrated.meta("fingerprint_id")
        assert migrated.meta("fingerprint_id") == fresh.meta("fingerprint_id")

        # (b) embed/extractor ZERO stale on the migrated index when the live
        # backend matches — but every content row IS chunker-stale (F2: the
        # upgraded index must actually re-chunk)
        report = fingerprints.staleness_report(migrated, live)
        assert report["embed_stale"] == 0
        assert report["extractor_stale"] == 0
        content = migrated.db.execute(
            "SELECT COUNT(*) FROM files WHERE capability = 'content_indexed'"
        ).fetchone()[0]
        assert content > 0
        assert report["chunker_stale"] == content
        assert report["stale_files"] == content
        # a DIFFERENT live identity (other seed) flags the vec-bearing files
        other = fingerprints.live_embed_id(MockBackend(seed=7, dim=768))
        report2 = fingerprints.staleness_report(migrated, other)
        assert report2["embed_stale"] == report["vec_files"] > 0
    finally:
        migrated.close()
        fresh.close()


def test_migrated_index_rechunks_on_the_next_plain_ingest(tmp_path):
    """F2 / REDESIGN §6 (05:152/264): the 'chunk.v1' backfill makes migrated
    rows chunker-stale per staleness_report, and ONE plain ingest over the
    same roots re-chunks them to the current CHUNKER_VERSION (D3 skip-fix) —
    vectors are re-derived (fresh ids unchanged), staleness drops to zero,
    and the backfill note stays as the audit record."""
    corpus = _corpus(tmp_path)
    db = tmp_path / "rechunk.sqlite"
    backend = MockBackend(dim=768)
    _ingest(db, corpus, backend=backend)
    _strip_to_v1(db)
    live = fingerprints.live_embed_id(MockBackend(dim=768))

    migrated = Store(db)  # open = migrate
    try:
        report = fingerprints.staleness_report(migrated, live)
        content = migrated.db.execute(
            "SELECT COUNT(*) FROM files WHERE capability = 'content_indexed'"
        ).fetchone()[0]
        assert report["chunker_stale"] == content > 0
        before_vecs = migrated.db.execute(
            "SELECT id, vec FROM segments WHERE vec IS NOT NULL ORDER BY id"
        ).fetchall()
    finally:
        migrated.close()

    _ingest(db, corpus, backend=MockBackend(dim=768))  # plain, no flags
    store = Store(db)
    try:
        assert store.db.execute(
            "SELECT COUNT(*) FROM files WHERE capability = 'content_indexed' "
            "AND chunker_version != ?",
            (CHUNKER_VERSION,),
        ).fetchone()[0] == 0
        after = fingerprints.staleness_report(
            store, fingerprints.live_embed_id(MockBackend(dim=768))
        )
        assert after["stale_files"] == 0
        # segment identities may be rewritten, but the vector payload count
        # is stable and every content file keeps the same embed id
        after_vecs = store.db.execute(
            "SELECT id, vec FROM segments WHERE vec IS NOT NULL ORDER BY id"
        ).fetchall()
        assert len(after_vecs) == len(before_vecs)
        assert store.meta("chunker_backfill_note")  # audit record kept
    finally:
        store.close()


def test_migrated_index_passes_the_v1_gate_with_same_backend(tmp_path):
    """Migration changes no vectors, so the unchanged index-level gate must
    still pass with the same backend (search works immediately)."""
    from goldmine.backends.mock import MockBackend as MB

    corpus = _corpus(tmp_path)
    db = tmp_path / "gate.sqlite"
    _ingest(db, corpus, backend=MB(dim=768))
    _strip_to_v1(db)
    store = Store(db)
    try:
        fingerprints.gate(store.meta, fingerprints.build(MB(dim=768)))  # no raise
    finally:
        store.close()


# ------------------------------------------------------------------ backups


def _fake_backups(home: Path, stem: str, n: int) -> list[Path]:
    bdir = home / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(n):
        p = bdir / f"{stem}-2025010{i + 1}-000000.sqlite"
        p.write_bytes(b"old backup")
        paths.append(p)
    return paths


def test_create_backup_verifies_and_prunes(tmp_path):
    home = tmp_path / "home"
    db = tmp_path / "default.sqlite"
    _ingest(db, _corpus(tmp_path))
    _fake_backups(home, "default", 7)

    info = migrations.create_backup(db, keep=5, home=home)
    backup = Path(info["path"])
    assert backup.is_file()
    assert info["integrity"] == "ok"
    assert backup.parent == home / "backups"
    assert backup.name.startswith("default-")
    # 7 fake + 1 new = 8, keep 5 -> 3 oldest pruned
    assert len(info["pruned"]) == 3
    remaining = migrations.backups_of(db, home)
    assert len(remaining) == 5
    assert backup in remaining
    assert not (home / "backups" / "default-20250101-000000.sqlite").exists()
    # the backup is a real, queryable index copy
    con = sqlite3.connect(str(backup))
    try:
        assert con.execute("SELECT COUNT(*) FROM files").fetchone()[0] > 0
    finally:
        con.close()


def test_create_backup_explicit_out_skips_pruning(tmp_path):
    home = tmp_path / "home"
    db = tmp_path / "default.sqlite"
    _ingest(db, _corpus(tmp_path))
    _fake_backups(home, "default", 2)
    out = tmp_path / "elsewhere" / "copy.sqlite"
    info = migrations.create_backup(db, out=out, keep=5, home=home)
    assert info["path"] == str(out)
    assert info["pruned"] == []
    assert len(migrations.backups_of(db, home)) == 2  # untouched


def test_verify_backup_detects_corruption(tmp_path):
    db = tmp_path / "default.sqlite"
    _ingest(db, _corpus(tmp_path))
    info = migrations.create_backup(db, out=tmp_path / "b.sqlite")
    good = Path(info["path"])
    assert migrations.verify_backup(good) == "ok"
    # corrupt the file header — the copy must be detected as unusable
    data = bytearray(good.read_bytes())
    data[20] ^= 0xFF  # header: change the file change counter / page area
    data[1] ^= 0xFF   # and the magic itself
    (tmp_path / "bad.sqlite").write_bytes(bytes(data))
    with pytest.raises(RuntimeError):
        migrations.verify_backup(tmp_path / "bad.sqlite")


def test_cli_backup_command(tmp_home, tmp_path, capsys):
    from goldmine.cli import main

    db = tmp_home / "gmhome" / "default.sqlite"
    assert main(["--backend", "mock", "--db", str(db), "ingest",
                 str(_corpus(tmp_path))]) == 0
    capsys.readouterr()
    assert main(["--db", str(db), "backup", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["integrity"] == "ok"
    assert Path(payload["path"]).is_file()
    assert payload["bytes"] > 0

    assert main(["--db", str(db), "backup"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("backup: ") and "integrity ok" in out

    # missing index -> honest prerequisite error, nothing created
    assert main(["--db", str(tmp_path / "nope.sqlite"), "backup"]) == 4


# ------------------------------------------------------------- doctor --deep


def test_doctor_deep_rows(tmp_home, tmp_path, capsys):
    from goldmine.cli import main

    db = tmp_home / "gmhome" / "default.sqlite"
    assert main(["--backend", "mock", "--db", str(db), "ingest",
                 str(_corpus(tmp_path))]) == 0
    capsys.readouterr()
    assert main(["--db", str(db), "doctor", "--deep", "--json"]) in (0, 4)
    payload = json.loads(capsys.readouterr().out)
    by_name = {c["name"]: c for c in payload["checks"]}
    assert set(by_name) >= {
        "index integrity", "staleness", "cards fingerprint", "backups"
    }
    assert by_name["index integrity"]["status"] == "ok"
    assert "integrity_check ok" in by_name["index integrity"]["detail"]
    assert by_name["staleness"]["status"] == "ok"
    assert "0 of" in by_name["staleness"]["detail"]
    assert by_name["cards fingerprint"]["status"] == "ok"  # no cards yet
    # no backup yet -> honest warn with the fix hint
    assert by_name["backups"]["status"] == "warn"
    assert "goldmine backup" in (by_name["backups"]["fix_hint"] or "")

    # after a backup the row turns ok
    assert main(["--db", str(db), "backup"]) == 0
    capsys.readouterr()
    assert main(["--db", str(db), "doctor", "--deep", "--json"]) in (0, 4)
    payload = json.loads(capsys.readouterr().out)
    by_name = {c["name"]: c for c in payload["checks"]}
    assert by_name["backups"]["status"] == "ok"
    assert "days ago" in by_name["backups"]["detail"]


def test_doctor_deep_flags_staleness_and_cards_drift(tmp_home, tmp_path, capsys):
    from goldmine.cli import main

    db = tmp_home / "gmhome" / "default.sqlite"
    assert main(["--backend", "mock", "--db", str(db), "ingest",
                 str(_corpus(tmp_path))]) == 0
    # age the index: stale chunker + stale extractor + stale embed id + old cards
    store = Store(db)
    try:
        store.db.execute(
            "UPDATE files SET chunker_version = 'chunk.v0', extractor_version = "
            "'txt-stdlib.v0' WHERE capability = 'content_indexed'"
        )
        store.db.execute("UPDATE files SET embed_id = 'deadbeefdeadbeef'")
        store.db.commit()
        store.db.execute(
            "INSERT INTO cards(title_terms, size, n_files, fingerprint_id, "
            "distill_algo, created_at) VALUES('[]', 1, 1, 'oldfp', 'a', 0.0)"
        )
        store.db.commit()
    finally:
        store.close()
    capsys.readouterr()
    assert main(["--db", str(db), "doctor", "--deep", "--json"]) in (0, 4)
    payload = json.loads(capsys.readouterr().out)
    by_name = {c["name"]: c for c in payload["checks"]}
    assert by_name["staleness"]["status"] == "warn"
    assert "re-run" in (by_name["staleness"]["fix_hint"] or "")
    assert by_name["cards fingerprint"]["status"] == "warn"
    assert "distill" in (by_name["cards fingerprint"]["fix_hint"] or "")
    assert by_name["index integrity"]["status"] == "ok"


def test_doctor_deep_without_index_is_a_warn_not_a_crash(tmp_home, capsys):
    from goldmine.cli import main

    capsys.readouterr()
    assert main(["doctor", "--deep", "--json"]) in (0, 4)
    payload = json.loads(capsys.readouterr().out)
    by_name = {c["name"]: c for c in payload["checks"]}
    assert "index integrity" in by_name
    assert by_name["index integrity"]["status"] == "warn"
