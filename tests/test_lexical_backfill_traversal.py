"""Bounded keyset traversal mechanics; no elapsed-time or capacity speed claim."""
from __future__ import annotations

import json
import sqlite3
import unicodedata

import pytest

from goldmine import lexical_store, migrations
from goldmine.deadline import RequestTimeout
from goldmine.storage_connections import _DDL
from goldmine.store import Store


class RecordingConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        if sql.startswith("SELECT s.id,s.file,s.kind,t.name,t.body,t.original FROM segments s "):
            self.keyset_calls.append((sql, tuple(parameters)))
        return super().execute(sql, parameters)


def native_fixture(path=":memory:", *, rows=600, sparse=False):
    db = sqlite3.connect(path, factory=RecordingConnection)
    db.keyset_calls = []
    db.executescript(_DDL)
    db.execute("PRAGMA user_version=4")
    db.execute("INSERT INTO meta(key,value) VALUES('index_uuid','fixed-owned-lexical-proof')")
    db.execute("INSERT INTO files(id,path,kind,size,state,capability,info) VALUES(1,?,'doc',1,'done','content_indexed',?)",
               ("/not-a-real-source/中文/Invoice-AB-12.md", json.dumps({"title": "ＸＹ－９８"})))
    ids = [-5, 0, *(i * 19 if sparse else i for i in range(1, rows + 1)), 2**40, 2**63 - 1]
    matched = []
    for i, sid in enumerate(ids):
        db.execute("INSERT INTO segments(id,file,kind,modality,loc,loc_end,excerpt,vec) VALUES(?,1,?,'text',2,3,?,?)",
                   (sid, "name" if sid == 1 else "chunk", "Kept original excerpt ＡＢ－１２", b"\x00\x3c" * 768))
        if i % 11 == 0:  # real missing-FTS holes, including negative ID
            continue
        original = (None, "", "Original 中文 ＡＢ－１２ Straße")[i % 3]
        name, body = "Invoice-ＡＢ－１２.MD", "Body 中文 ＸＹ－９８ Straße"
        db.execute("INSERT INTO fts(rowid,name,body,original) VALUES(?,?,?,?)", (sid, name, body, original))
        if sid > 0:
            matched.append((sid, name, body, original))
    db.execute("INSERT INTO fts(rowid,name,body,original) VALUES(-999,'orphan','Not a segment',NULL)")
    db.commit()
    return db, matched


def snapshot_truth(db):
    return (db.execute("SELECT * FROM files ORDER BY id").fetchall(),
            db.execute("SELECT * FROM segments ORDER BY id").fetchall())


def derived(db, exact):
    return (db.execute("SELECT rowid,tokens FROM fts_cjk ORDER BY rowid").fetchall(),
            db.execute("SELECT * FROM lookup_terms ORDER BY normalized_term,raw_term,file_id,segment_id,source,match_type").fetchall() if exact else None)


@pytest.mark.parametrize("exact", [False, True])
@pytest.mark.parametrize("sparse", [False, True])
def test_sparse_holes_original_normalization_and_all_stored_identity_are_preserved(exact, sparse):
    db, expected = native_fixture(sparse=sparse)
    try:
        truth = snapshot_truth(db)
        unmatched = db.execute("SELECT rowid,name,body,original FROM fts WHERE rowid<=0 ORDER BY rowid").fetchall()
        with db:
            result = lexical_store.rebuild_derived(db, exact=exact)
        assert result == {"fts_rows": len(expected), "lexical_version": "lex.v2-exact" if exact else "lex.v1-cjk2"}
        assert snapshot_truth(db) == truth  # includes complete vector BLOBs, IDs, locations/excerpts
        assert db.execute("SELECT value FROM meta WHERE key='index_uuid'").fetchone()[0] == "fixed-owned-lexical-proof"
        assert db.execute("SELECT rowid,name,body,original FROM fts WHERE rowid<=0 ORDER BY rowid").fetchall() == unmatched
        actual_ids = [r[0] for r in db.execute("SELECT rowid FROM fts_cjk ORDER BY rowid")]
        assert actual_ids == [r[0] for r in expected]  # inner-join omissions stay omitted, never filled
        for sid, name, body, original in expected:
            text = original if original is not None else body
            normalized = unicodedata.normalize("NFKC", text).casefold()
            kept = original if original is not None else (text if normalized != text else None)
            assert db.execute("SELECT name,body,original FROM fts WHERE rowid=?", (sid,)).fetchone() == (
                unicodedata.normalize("NFKC", name).casefold(), normalized, kept)
        before = derived(db, exact)
        with db:
            assert lexical_store.rebuild_derived(db, exact=exact) == result
        assert derived(db, exact) == before
        assert snapshot_truth(db) == truth
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4  # rebuild itself never migrates
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


def production_query():
    db, _ = native_fixture(rows=1)
    try:
        lexical_store.rebuild_derived(db, exact=False)
        queries = {sql for sql, _ in db.keyset_calls}
        assert len(queries) == 1
        return queries.pop()
    finally:
        db.close()


def traversal(db, query):
    instructions = [0]
    def step():
        instructions[0] += 1
        return 0
    db.set_progress_handler(step, 1)
    try:
        last, ids, batches = 0, [], 0
        while True:
            rows = db.execute(query, (last,)).fetchall()
            batches += 1
            if not rows:
                break
            ids.extend(row[0] for row in rows)
            last = rows[-1][0]
        return instructions[0], ids, batches
    finally:
        db.set_progress_handler(None, 0)


def test_actual_production_query_uses_pk_range_and_rowid_lookups_without_sort():
    query = production_query()
    db, expected = native_fixture()
    try:
        plan = [row[3].upper() for row in db.execute("EXPLAIN QUERY PLAN " + query, (0,))]
        assert "SEARCH S USING INTEGER PRIMARY KEY (ROWID>?)" in plan[0]
        assert any("VIRTUAL TABLE INDEX" in row and "=" in row for row in plan[1:])
        assert not any("TEMP B-TREE" in row for row in plan)
        _, actual, batches = traversal(db, query)
        assert actual == [row[0] for row in expected]
        assert batches == (len(expected) + 255) // 256 + 1
    finally:
        db.close()


def test_complete_keyset_traversal_vm_work_grows_linearly_with_doubled_rows():
    query = production_query()
    counts = []
    for rows in (512, 1024, 2048):
        db, expected = native_fixture(rows=rows, sparse=True)
        try:
            steps, actual, _ = traversal(db, query)
            assert actual == [row[0] for row in expected]
            # Includes FTS point lookup bookkeeping and missing-FTS holes.
            assert steps < 80 * (rows + 4) + 1000
            counts.append(steps)
        finally:
            db.close()
    assert all(larger <= 2.15 * smaller for smaller, larger in zip(counts, counts[1:]))


def test_timeout_after_written_first_batch_rolls_back_current_reindex(tmp_path, monkeypatch):
    path = tmp_path / "current.sqlite"
    db, _ = native_fixture(path)
    db.close()
    store = Store(path)
    try:
        before = list(store.db.iterdump())
        calls = [0]
        def expired():
            calls[0] += 1
            if calls[0] == 2:
                raise RequestTimeout("owned test deadline after one completed batch")
        monkeypatch.setattr(lexical_store, "check_deadline", expired)
        with pytest.raises(RequestTimeout, match="completed batch"):
            store.reindex_lexical()
        assert calls[0] == 2 and not store.db.in_transaction
        assert list(store.db.iterdump()) == before
        with pytest.raises(sqlite3.IntegrityError, match="obsolete"):
            store.db.execute("UPDATE meta SET value='forged' WHERE key='index_uuid'")
    finally:
        store.close()


def test_timeout_during_v4_backfill_keeps_schema4_and_original_rows(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    db, _ = native_fixture(path)
    before = list(db.iterdump())
    db.close()
    calls = [0]
    def expired():
        calls[0] += 1
        if calls[0] == 2:
            raise RequestTimeout("owned migration deadline after one completed batch")
    monkeypatch.setattr(lexical_store, "check_deadline", expired)
    with pytest.raises(RequestTimeout, match="completed batch"):
        Store(path)
    check = sqlite3.connect(path)
    try:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 4
        assert list(check.iterdump()) == before
        assert not check.execute("SELECT name FROM sqlite_schema WHERE name='fts_cjk'").fetchall()
    finally:
        check.close()
