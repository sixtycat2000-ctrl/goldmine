"""Pure stored-row upgrades and hard fencing of already-open legacy writers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from goldmine import migrations
from goldmine.storage_connections import _DDL
from goldmine.storage_writes import register_writer, writer_scope
from goldmine.store import FileRec, SegmentRec, Store


def legacy_index(path, *, rows=1):
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_DDL)
    con.execute("PRAGMA user_version=4")
    vec = np.zeros(768, dtype="<f2"); vec[0] = 1
    for i in range(rows):
        fid, name_sid, body_sid = i+1, i*2+1, i*2+2
        con.execute("INSERT INTO files(id,path,kind,size,state,capability,info) "
                    "VALUES(?,?,'doc',1,'done','content_indexed',?)",
                    (fid,f"/explicit/旧资料-{i}.md",json.dumps({"title":"AB-12"})))
        con.execute("INSERT INTO segments(id,file,kind,modality) VALUES(?,?,'name','none')", (name_sid,fid))
        con.execute("INSERT INTO segments(id,file,kind,modality,loc,loc_end,vec) "
                    "VALUES(?,?,'chunk','text',1,1,?)", (body_sid,fid,vec.tobytes()))
        con.execute("INSERT INTO fts(rowid,name,body) VALUES(?,?,?)", (name_sid,"旧资料.md","explicit"))
        con.execute("INSERT INTO fts(rowid,name,body) VALUES(?,?,?)",
                    (body_sid,"旧资料.md",f"中文知识库 INVOICE ＡＢ－１２ {i}"))
    con.commit()
    con.close()


def vector_digest(db):
    digest = hashlib.sha256()
    for sid, blob in db.execute("SELECT id,vec FROM segments WHERE vec IS NOT NULL ORDER BY id"):
        digest.update(str(sid).encode()); digest.update(blob)
    return digest.hexdigest()


def derived_rows(store):
    return (
        store.db.execute("SELECT rowid,tokens FROM fts_cjk ORDER BY rowid").fetchall(),
        store.db.execute("SELECT * FROM lookup_terms ORDER BY normalized_term,raw_term,segment_id,source,match_type").fetchall(),
    )


def test_v4_to_v5_to_v6_preserves_vectors_text_and_uuid(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    legacy_index(path)
    con = sqlite3.connect(path)
    before = vector_digest(con)
    original = con.execute("SELECT body FROM fts WHERE rowid=2").fetchone()[0]
    con.close()
    monkeypatch.setattr(migrations, "TARGET", 5)
    v5 = Store(path)
    try:
        assert v5.db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert v5.meta("lexical_version") == "lex.v1-cjk2"
        assert vector_digest(v5.db) == before
        assert v5.segment_text(2) == original
        uuid = v5.meta("index_uuid")
    finally:
        v5.close()
    monkeypatch.setattr(migrations, "TARGET", 6)
    v6 = Store(path)
    try:
        assert v6.db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert v6.meta("lexical_version") == "lex.v2-exact"
        assert v6.meta("index_uuid") == uuid
        assert vector_digest(v6.db) == before
        assert v6.segment_text(2) == original
        assert v6.db.execute("SELECT COUNT(*) FROM lookup_terms WHERE normalized_term='ab-12'").fetchone()[0]
        assert v6.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert v6.db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        v6.close()


@pytest.mark.parametrize("mutation", ["upsert", "replace", "delete", "failed_cleanup", "meta"])
def test_already_open_v4_writer_rolls_back_even_earlier_fts_delete(tmp_path, mutation):
    path = tmp_path / "legacy.sqlite"
    legacy_index(path)
    old = sqlite3.connect(path)  # opened before migration, no new writer function
    current = Store(path)
    before = derived_rows(current)
    try:
        # Match the dangerous old delete_content_segments sequence, which did
        # not own a Python connection context and whose close would commit.
        old.execute("DELETE FROM fts WHERE rowid=2")
        statements = {
            "upsert":"UPDATE files SET state='pending' WHERE id=1",
            "replace":"INSERT INTO segments(file,kind,modality) VALUES(1,'chunk','none')",
            "delete":"DELETE FROM files WHERE id=1",
            "failed_cleanup":"DELETE FROM segments WHERE file=1 AND kind!='name'",
            "meta":"INSERT INTO meta(key,value) VALUES('fingerprint_id','forged')",
        }
        with pytest.raises(sqlite3.IntegrityError, match="obsolete"):
            old.execute(statements[mutation])
        assert not old.in_transaction
        old.commit()  # legacy close/commit cannot publish the first FTS delete
        assert current.db.execute("SELECT body FROM fts WHERE rowid=2").fetchone() is not None
        assert derived_rows(current) == before
        assert current.meta("fingerprint_id") is None
    finally:
        old.close(); current.close()


def test_v5_scope_refuses_upgrade_that_happened_after_connection_open(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    legacy_index(path)
    monkeypatch.setattr(migrations, "TARGET", 5)
    v5 = Store(path)
    monkeypatch.setattr(migrations, "TARGET", 6)
    v6 = Store(path)
    try:
        with pytest.raises(RuntimeError, match="supports v5"):
            with writer_scope(v5.db):
                v5.db.execute("DELETE FROM files")
        assert v6.counts().files == 1
    finally:
        v5.close(); v6.close()


def test_nested_failure_rolls_back_parent_and_restores_all_fences(tmp_path):
    store = Store(tmp_path / "nested.sqlite")
    before = list(store.db.iterdump())
    try:
        with pytest.raises(ValueError, match="abort"):
            with store.write_transaction():
                fid = store.upsert_file(FileRec(path="/explicit/new.txt"))
                store.replace_segments(fid, [SegmentRec(kind="chunk", modality="none", text_for_fts="中文 AB-12")])
                store.set_meta("new", "value")
                raise ValueError("abort")
        assert list(store.db.iterdump()) == before
        with pytest.raises(sqlite3.IntegrityError, match="obsolete"):
            store.db.execute("UPDATE meta SET value='forged' WHERE key='index_uuid'")
    finally:
        store.close()


def test_reindex_uses_stored_rows_is_idempotent_and_needs_no_source(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    legacy_index(path)
    store = Store(path)
    try:
        before_vec = vector_digest(store.db)
        before_derived = derived_rows(store)
        monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("read a source/model file"))
        store.reindex_lexical(); store.reindex_lexical()
        assert derived_rows(store) == before_derived
        assert vector_digest(store.db) == before_vec
    finally:
        store.close()


def test_killed_v5_backfill_rolls_back_then_retries(tmp_path):
    path, ready = tmp_path / "kill.sqlite", tmp_path / "ready"
    legacy_index(path, rows=150)
    con = sqlite3.connect(path)
    before = vector_digest(con); con.close()
    script = '''
import sys,time
from pathlib import Path
from goldmine import lexical_store
from goldmine.store import Store
original=lexical_store.insert_derived
count=0
def pause(*a,**kw):
    global count
    original(*a,**kw)
    count+=1
    if count==30:
        Path(sys.argv[2]).write_text('ready')
        time.sleep(30)
lexical_store.insert_derived=pause
Store(Path(sys.argv[1]))
'''
    child = subprocess.Popen([sys.executable, "-c", script, str(path), str(ready)])
    try:
        end = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < end and child.poll() is None:
            time.sleep(0.01)
        assert ready.exists(), "migration never reached the injected in-transaction pause"
        child.kill(); child.wait(timeout=5)
        con = sqlite3.connect(path)
        assert con.execute("PRAGMA user_version").fetchone()[0] == 4
        assert vector_digest(con) == before
        assert not con.execute("SELECT name FROM sqlite_schema WHERE name='fts_cjk'").fetchall()
        con.close()
        recovered = Store(path)
        try:
            assert recovered.db.execute("PRAGMA user_version").fetchone()[0] == 6
            assert vector_digest(recovered.db) == before
            assert recovered.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            recovered.close()
    finally:
        if child.poll() is None:
            child.kill(); child.wait(timeout=5)
