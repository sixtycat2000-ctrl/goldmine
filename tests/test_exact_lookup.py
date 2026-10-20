"""Exact raw identifiers outrank aliases, with support for remaining words."""

from __future__ import annotations

import pytest

from goldmine.search import Searcher
from goldmine.store import FileRec, SegmentRec, Store


@pytest.fixture
def index(tmp_path):
    store = Store(tmp_path / "exact.sqlite")
    yield store
    store.close()


def add(store, path, text):
    fid = store.upsert_file(FileRec(path=path, state="done", capability="content_indexed"))
    store.replace_segments(fid, [
        SegmentRec(kind="name", modality="none", text_for_fts="explicit folder"),
        SegmentRec(kind="chunk", modality="none", loc=7, loc_end=7, text_for_fts=text),
    ])
    return fid


def hits(store, query):
    return Searcher(store, None).search(query, scorer="lexical", why=True)


@pytest.mark.parametrize("identifier", ["LH-2209", "K7Q2LM", "py-1.0.3", "key_value_store.sqlite", "77889900"])
def test_complete_identifier_returns_real_body_segment(index, identifier):
    add(index, "/explicit/source.txt", "approved identifier " + identifier)
    found = hits(index, identifier)
    assert found and found[0].segment == "chunk"
    assert found[0].loc == 7
    assert found[0].why["exact_source"] == "body"
    assert found[0].why["exact_match_type"] == "raw"
    assert identifier in index.segment_text(found[0].segment_id)
    assert found[0].cosine is None


def test_raw_original_beats_separator_alias_in_both_directions(index):
    add(index, "/explicit/separated.txt", "refund AB-12 approved")
    add(index, "/explicit/compact.txt", "refund AB12 approved")
    assert [h.path for h in hits(index, "AB12")] == ["/explicit/compact.txt", "/explicit/separated.txt"]
    assert [h.path for h in hits(index, "AB-12")] == ["/explicit/separated.txt", "/explicit/compact.txt"]
    assert hits(index, "AB12")[1].why["exact_match_type"] == "alias"


def test_anchor_does_not_hide_unmatched_query_words(index):
    add(index, "/explicit/source.txt", "refund AB-12 approved")
    assert hits(index, "refund AB-12")
    assert hits(index, "randomzz AB-12") == []
    assert hits(index, "AB123") == []


@pytest.mark.parametrize("path", [
    "/explicit/a b/Quarterly Report.md", "/explicit/中文/资料记录.txt",
    "/explicit/a_b/100%_data.sqlite",
])
def test_full_filename_and_path_lookup_are_literal(index, path):
    add(index, path, "unrelated document body")
    exact = hits(index, path)[0]
    assert exact.path == path
    assert exact.segment == "name"
    assert exact.why["exact_priority"] == 0
    basename = path.rsplit("/", 1)[-1]
    assert hits(index, basename)[0].path == path


def test_same_basename_remains_distinct_and_stably_ranked(index):
    add(index, "/explicit/a/report.md", "alpha")
    add(index, "/explicit/b/report.md", "beta")
    first = [h.path for h in hits(index, "report.md")]
    second = [h.path for h in hits(index, "report.md")]
    assert len(first) == 2 and first == second


def test_fullwidth_casefold_and_duplicate_import(index):
    fid = add(index, "/explicit/source.txt", "approved ＬＨ－２２０９")
    first = hits(index, "lh-2209")[0]
    assert first.path == "/explicit/source.txt"
    assert first.why["exact_match_type"] == "raw"
    assert first.why["exact_string"] == "ＬＨ－２２０９"
    before = index.db.execute("SELECT COUNT(*) FROM lookup_terms").fetchone()[0]
    index.reindex_lexical(); index.reindex_lexical()
    assert index.db.execute("SELECT COUNT(*) FROM lookup_terms").fetchone()[0] == before
    index.delete_file(fid)
    assert hits(index, "lh-2209") == []


def test_filename_only_identifier_never_claims_a_body_location(index):
    path = "/explicit/very/long/folder/words/make/name/row/longer/invoice-K7Q2LM.pdf"
    add(index, path, "short")
    first = hits(index, "K7Q2LM")[0]
    assert first.path == path
    assert first.segment == "name"
    assert first.loc is None and first.loc_end is None
    assert first.why["exact_source"] in ("basename", "path")
