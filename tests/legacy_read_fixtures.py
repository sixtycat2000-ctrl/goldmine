"""Synthetic4/5 fixtures using the existing genuine legacy schema producer.

The independent report additionally invokes the exact frozen original Store.
This portable fixture uses test_lexical_migration's native4 DDL, old NFC write
semantics and real4→5 migration; no current index is merely relabelled4/5.
"""
from contextlib import closing, contextmanager
import json
import sqlite3
import unicodedata

import pytest

from goldmine import migrations
from goldmine.store import Store
from test_anchor_contract import add
from test_lexical_migration import legacy_index

UUID = "6f230a79-0bc1-47a7-9fa9-a6f6c9291ded"


def build(path, version, docs, *, body_only=False, uuid=UUID):
    if version == "current":
        # the live CURRENT schema (P1-3 v7): Store-created at TARGET
        with closing(Store(path)) as store:
            if uuid is None:
                store.set_meta("index_uuid", None)
            for doc in docs:
                add(store, doc.get("text", []), path=doc.get("path", "/owned/source.txt"),
                    kind=doc.get("kind", "doc"), title=doc.get("title"), vector=doc.get("vector"))
        return path
    legacy_index(path, rows=0)
    with closing(sqlite3.connect(path)) as db:
        if uuid is not None:
            db.execute("INSERT INTO meta VALUES('index_uuid',?)", (uuid,))
        for number, doc in enumerate(docs, 1):
            path_value = doc.get("path", "/owned/source.txt")
            db.execute("INSERT INTO files(id,path,kind,size,state,capability,info) "
                       "VALUES(?,?,?,1,'done','content_indexed',?)", (number, path_value, doc.get("kind", "doc"),
                        json.dumps({"title": doc["title"]}) if "title" in doc else None))
            texts = doc.get("text", [])
            texts = [texts] if isinstance(texts, str) else texts
            for index, text in enumerate([doc.get("folder", "owned folder"), *texts]):
                kind = "name" if index == 0 else "chunk"
                loc = None if index == 0 else 6 + index
                sid = db.execute("INSERT INTO segments(file,kind,modality,loc,loc_end,excerpt) "
                                 "VALUES(?,?,'none',?,?,?)", (number, kind, loc, loc, text)).lastrowid
                body = unicodedata.normalize("NFC", text)
                db.execute("INSERT INTO fts(rowid,name,body,original) VALUES(?,?,?,?)",
                           (sid, path_value.rsplit("/", 1)[-1], body, text if text != body else None))
        db.commit()
        if body_only:
            assert not db.execute("SELECT rowid FROM fts WHERE original IS NOT NULL").fetchall()
            rows = db.execute("SELECT rowid,name,body FROM fts").fetchall()
            sql = db.execute("SELECT sql FROM sqlite_schema WHERE name='fts'").fetchone()[0]
            sql = sql.replace("name, body, original UNINDEXED", "name, body")
            db.execute("DROP TABLE fts")
            db.execute(sql)
            db.executemany("INSERT INTO fts(rowid,name,body) VALUES(?,?,?)", rows)
            db.commit()
    if version in (5, 6):
        # legacy shapes: migrate EXACTLY to the requested version (a true
        # v5/v6 db, not the live TARGET — P1-3 v7 made 6 a legacy version)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(migrations, "TARGET", version)
            Store(path).close()
    return path


def logical(db):
    return list(db.iterdump())


@contextmanager
def fixture_writer(path, version):
    """Use actual writer fences while retaining a native5 fixture's version."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(migrations, "TARGET", version)
        with closing(Store(path)) as store:
            yield store
