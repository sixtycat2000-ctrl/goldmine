"""Request/snapshot-local reuse of identical pure work (SC-01/SC-03).

These tests reproduce the static scaling exposures from
reports/execution/P0-06/public-c1-scaling-static-review-001 and pin the
semantics-preserving reuse: pure remainder units prepared once per clause,
whole-metadata derived sets computed once per distinct field value inside one
predicate invocation, and the FTS projection memoized per pinned snapshot.
None of them may change returned IDs, witnesses, errors or bounds.
"""
from __future__ import annotations

import sqlite3

import pytest

import goldmine.anchor_resolution as anchor_resolution
import goldmine.metadata_prelookup as metadata_prelookup
import goldmine.read_source as read_source
from goldmine.anchor_admission import resolve_obligations
from goldmine.anchor_resolution import Resolver
from goldmine.search import _terms_of
from goldmine.store import FileRec, SegmentRec, Store


@pytest.fixture
def index(tmp_path):
    store = Store(tmp_path / "reuse.sqlite")
    yield store
    store.close()


def add(store, path, text, *, title=None):
    fid = store.upsert_file(FileRec(path=path, kind="doc", state="done", capability="content_indexed",
                                    info={"title": title} if title is not None else None))
    store.replace_segments(fid, [SegmentRec(kind="name", modality="none", text_for_fts=path)] + [
        SegmentRec(kind="chunk", modality="text", loc=7 + i, loc_end=7 + i, text_for_fts=t, excerpt=t)
        for i, t in enumerate([text] if isinstance(text, str) else text)])
    return fid


def all_files(store):
    return {row[0] for row in store.db.execute("SELECT id FROM files WHERE state='done'")}


def test_sc01_remainder_units_prepared_once_per_clause(index, monkeypatch):
    """E eligible files must not rebuild the identical clause units E times."""
    # anchor.v2-intent: ambient _CODE anchors never bind, so the remainder
    # preparation is reached with a structural identity instead.
    for i in range(12):
        add(index, f"/docs/owner-{i:02d}.txt", "approved TX_84 with beacon harbor oversight")
    unit_calls = {"n": 0}
    real_units = anchor_resolution.query_units
    def counted_units(text, terms, phrases):
        unit_calls["n"] += 1
        return real_units(text, terms, phrases)
    monkeypatch.setattr(anchor_resolution, "query_units", counted_units)
    decision = resolve_obligations(index, "TX_84 beacon harbor oversight", (), all_files(index), _terms_of)
    assert decision.owners and not decision.reason
    # one clause → one pure unit preparation, independent of owner count
    assert unit_calls["n"] == 1, unit_calls


def test_sc01_terms_of_runs_once_per_clause(index, monkeypatch):
    for i in range(12):
        add(index, f"/docs/owner-{i:02d}.txt", "approved ZT-84 with beacon harbor oversight")
    calls = {"n": 0}
    def terms_of(text):
        calls["n"] += 1
        return _terms_of(text)
    decision = resolve_obligations(index, "ZT-84 beacon harbor oversight", (), all_files(index), terms_of)
    assert decision.owners
    assert calls["n"] == 1, calls


def test_sc03_whole_metadata_derived_terms_once_per_distinct_value(index, monkeypatch):
    """M files sharing one title value must not recompute the same pure
    derived-term set per callback row."""
    from goldmine.query_obligations import Anchor
    for i in range(10):
        add(index, f"/docs/meta-{i:02d}.txt", f"body filler {i}", title="AB-12")
    calls = {"n": 0}
    real_terms = metadata_prelookup.derived_terms
    def counted(field, value):
        calls["n"] += 1
        return real_terms(field, value)
    monkeypatch.setattr(metadata_prelookup, "derived_terms", counted)
    resolver = Resolver(index, all_files(index))
    bindings = resolver.lookup(Anchor(0, 0, 0, 0, "AB-12", "identity"), whole_metadata=True)
    assert bindings and len(bindings) == 10, "fixture must reach the callback for all ten files"
    # ten callback rows, one shared title value → the pure set is derived once
    assert calls["n"] == 1, calls


def test_sc03_fts_projection_memoized_per_snapshot(index, monkeypatch):
    """A multi-batch read must not re-run PRAGMA table_info per 400-row batch."""
    add(index, "/docs/big.txt", [f"filler row {i} beacon" for i in range(500)])
    pragma_calls = {"n": 0}
    real_columns = read_source._columns
    def counted_columns(db, table):
        if table == "fts":
            pragma_calls["n"] += 1
        return real_columns(db, table)
    monkeypatch.setattr(read_source, "_columns", counted_columns)
    resolver = Resolver(index, all_files(index))
    sids = [row[0] for row in index.db.execute(
        "SELECT id FROM segments WHERE kind!='name' ORDER BY id")]
    assert len(sids) > 400  # two read batches
    rows = resolver.read(sids)
    assert len(rows) == len(sids)
    assert pragma_calls["n"] == 1, pragma_calls


def test_sc01_file_scoped_body_ids_sql_equivalence(index):
    """The file-first (segments-outer) body_ids shape returns identical IDs in
    the same order for every (pattern, file) pair, and both shapes raise the
    same FTS errors on malformed patterns."""
    add(index, "/docs/alpha.txt", ["approved ZT-84 beacon", "second ZT-84 harbor line"])
    add(index, "/docs/beta.txt", ["nothing relevant here"])
    add(index, "/docs/empty.txt", [])
    resolver = Resolver(index, all_files(index))
    files = sorted(all_files(index))
    patterns = ['body : ("zt")', 'body : ("84")', 'body : ("zt" AND "beacon")', 'body : ("zzz")']
    for pattern in patterns:
        for fid in files:
            old_sql = ("SELECT DISTINCT s.id FROM fts JOIN segments s ON s.id=fts.rowid "
                       "WHERE fts MATCH ? AND s.kind!='name' AND s.file=? ORDER BY s.id")
            new_sql = ("SELECT DISTINCT s.id FROM segments s CROSS JOIN fts ON fts.rowid=s.id "
                       "WHERE s.file=? AND s.kind!='name' AND fts MATCH ? ORDER BY s.id")
            old = resolver.db.execute(old_sql + " LIMIT 2001", (pattern, fid)).fetchall()
            new = resolver.db.execute(new_sql + " LIMIT 2001", (fid, pattern)).fetchall()
            assert old == new, (pattern, fid, old, new)
    for sql, params in (
            ("SELECT DISTINCT s.id FROM fts JOIN segments s ON s.id=fts.rowid "
             "WHERE fts MATCH ? AND s.kind!='name' AND s.file=? ORDER BY s.id", ('body : (', files[0])),
            ("SELECT DISTINCT s.id FROM segments s CROSS JOIN fts ON fts.rowid=s.id "
             "WHERE s.file=? AND s.kind!='name' AND fts MATCH ? ORDER BY s.id", (files[0], 'body : ('))):
        with pytest.raises(sqlite3.OperationalError):
            resolver.db.execute(sql + " LIMIT 2001", params)
