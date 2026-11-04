"""collect_body_units: one body MATCH per unit, results independent of batching.

The per-(unit, 400-rowid batch) rowid-IN shape re-evaluated the MATCH
constraint for every candidate rowid and multiplied statement cost ~100x on
long public queries (public-rootcause-fix-001). These tests pin the adopted
semantics: the posting set of each unit's body pattern intersected with the
recalled ids, for any batch partition.
"""

import sqlite3

import pytest

from goldmine.deadline import RequestTimeout, request_cancellation, request_timeout
from goldmine.lexical import query_units
from goldmine.retrieval_evidence import collect_body_units
from goldmine.search import Searcher
from goldmine.store import FileRec, SegmentRec, Store


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "body-units.sqlite")
    yield store
    store.close()


def add_chunks(store, path, texts):
    fid = store.upsert_file(FileRec(path=path, kind="doc", state="done",
                                    capability="content_indexed"))
    store.replace_segments(fid, [
        SegmentRec(kind="name", modality="none", text_for_fts=path),
        *[SegmentRec(kind="chunk", modality="none", loc=i + 1, loc_end=i + 1,
                     text_for_fts=text) for i, text in enumerate(texts)],
    ])
    return fid


def chunk_rows(store):
    return store.db.execute(
        "SELECT s.id,s.file FROM segments s WHERE s.kind!='name' ORDER BY s.id"
    ).fetchall()


def test_one_match_per_unit_covers_rows_across_batch_boundaries(store):
    # 900 recalled rows cross two 400-rowid batch boundaries; the last term
    # keeps a prefix pattern, exactly as production query_units builds it.
    texts = ["filler surrounding text"] * 899 + ["filler surrounding apple"]
    add_chunks(store, "/docs/many.txt", texts)
    units = query_units("apple", ["filler", "apple"], [])
    matched, segment_files = collect_body_units(store.db, units, chunk_rows(store))
    assert len(segment_files) == 900
    apple_unit = next(i for i, u in enumerate(units) if u.text == "apple")
    filler_unit = next(i for i, u in enumerate(units) if u.text == "filler")
    assert len(matched) == 900  # every row matches at least "filler"
    assert all(filler_unit in units_hit for units_hit in matched.values())
    last_row = max(segment_files)
    assert matched[last_row] == frozenset({filler_unit, apple_unit})
    assert all(apple_unit not in v for sid, v in matched.items() if sid != last_row)


def test_only_recalled_rows_are_reported_even_when_corpus_matches_elsewhere(store):
    add_chunks(store, "/docs/recalled.txt", ["alpha probe line"])
    add_chunks(store, "/docs/not-recalled.txt", ["alpha probe line", "beta probe line"])
    rows = [r for r in chunk_rows(store) if store.db.execute(
        "SELECT path FROM files WHERE id=?", (r[1],)).fetchone()[0] == "/docs/recalled.txt"]
    units = query_units("", ["alpha"], [])
    matched, _ = collect_body_units(store.db, units, rows)
    alpha = 0
    assert set(matched) == {sid for sid, _ in rows}
    assert all(v == frozenset({alpha}) for v in matched.values())


def test_cjk_units_still_verified_through_text_readback(store):
    texts = ["无光带说明"] + ["无关内容"] * 401
    add_chunks(store, "/docs/cjk.txt", texts)
    units = query_units("光带", [], [])
    matched, _ = collect_body_units(store.db, units, chunk_rows(store))
    cjk_unit = 0
    first_row = min(matched) if matched else None
    assert set(matched) == {first_row}
    assert matched[first_row] == frozenset({cjk_unit})


@pytest.mark.parametrize("query,terms,phrases", [
    ("alpha", ["alpha"], []),
    ("alpha beta", ["alpha", "beta"], []),
    ("中文检索", [], []),
    ("", [], ["exact phrase"]),
    ("", [], ["中文短语"]),
])
def test_empty_candidates_do_no_sql_for_any_valid_unit(query, terms, phrases):
    class NoDatabaseWork:
        def execute(self, *args):
            raise AssertionError("empty evidence candidates must not read postings or text")

    consumed = []

    def empty_rows():
        consumed.append(True)
        yield from ()

    units = query_units(query, terms, phrases)
    assert units
    assert collect_body_units(NoDatabaseWork(), units, empty_rows()) == ({}, {})
    assert consumed == [True]


def test_empty_candidates_still_observe_cancellation():
    class Cancelled(BaseException):
        pass

    cancelled = False

    def check():
        if cancelled:
            raise Cancelled()

    with request_cancellation(check):
        cancelled = True
        with pytest.raises(Cancelled):
            collect_body_units(None, [], [])


def test_empty_candidates_still_observe_expired_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("goldmine.deadline.time.monotonic", lambda: now[0])
    with request_timeout(1.0):
        now[0] = 2.0
        with pytest.raises(RequestTimeout):
            collect_body_units(None, [], [])
        now[0] = 0.0


def test_real_search_still_rejects_missing_modern_fts(store):
    add_chunks(store, "/docs/source.txt", ["alpha text"])
    store.db.execute("DROP TABLE fts")
    with pytest.raises(sqlite3.OperationalError, match="fts"):
        Searcher(store, None).search("alpha", scorer="lexical")
