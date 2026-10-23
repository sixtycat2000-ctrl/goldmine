"""Unicode lexical recall, phrase evidence and bounded malformed filters."""

from __future__ import annotations

import time

import pytest

from goldmine.lexical import cjk_runs, cjk_tokens, grams, normalize
from goldmine.search import Searcher, parse_filters
from goldmine.store import FileRec, SegmentRec, Store


def add(store, path, texts):
    fid = store.upsert_file(FileRec(path=path, state="done", capability="content_indexed"))
    store.replace_segments(fid, [SegmentRec(kind="name", modality="none", text_for_fts="explicit")] + [
        SegmentRec(kind="chunk", modality="none", loc=i+1, loc_end=i+1, text_for_fts=text)
        for i,text in enumerate(texts)
    ])
    return fid


def search(store, query, **kwargs):
    clean, filters = parse_filters(query)
    return Searcher(store, None).search(clean, scorer="lexical", filters=filters, **kwargs)


@pytest.fixture
def index(tmp_path):
    store = Store(tmp_path / "lexical.sqlite")
    yield store
    store.close()


@pytest.mark.parametrize("text,query", [
    ("中文知识库搜索为资料提供证据", "中文知识库搜索"),
    ("かなカナ検索資料を管理する", "カナ検索"),
    ("한국어지식검색자료관리", "지식검색"),
    ("检索字词", "字"),
    ("ＩＮＶＯＩＣＥ ２０２６ 中文管理", "invoice 2026"),
])
def test_lexical_recall_without_semantic_backend(index, text, query):
    add(index, "/explicit/source.txt", [text])
    hits = search(index, query)
    assert hits and hits[0].path == "/explicit/source.txt"
    assert index.segment_text(hits[0].segment_id) == text
    assert hits[0].cosine is None


def test_query_units_do_not_reward_each_cjk_gram_independently(index):
    add(index, "/explicit/source.txt", ["中文知识库搜索 invoice 2026"])
    hit = search(index, "中文知识库搜索 invoice 2026")[0]
    assert hit.coverage == pytest.approx(1)
    assert hit.score <= 4.0 + 1e-9  # lookup word weight, not one boost per gram
    assert len(grams("中文知识库搜索")) > 1


def test_cjk_phrase_requires_one_contiguous_stored_segment(index):
    add(index, "/explicit/good.txt", ["实践知识管理方法"])
    add(index, "/explicit/scattered.txt", ["知识 foo 识管 bar 管理"])
    add(index, "/explicit/cross.txt", ["知识", "识管", "管理"])
    hits = search(index, '"知识管理"')
    assert [h.path for h in hits] == ["/explicit/good.txt"]


def test_cjk_path_and_filename_indexed_without_changing_source_text(index):
    add(index, "/explicit/中文资料/报告.md", ["unrelated document body"])
    assert search(index, "中文资料")
    hit = search(index, "报告.md")[0]
    assert hit.segment == "name"
    assert index.segment_text(hit.segment_id) == "explicit"


def test_unicode_normalization_is_deterministic_and_has_no_emoji_tokens():
    assert normalize("ＡＢ１２") == "ab12"
    assert cjk_runs("管理 invoice 한글") == ["管理", "한글"]
    assert set(cjk_tokens("知识知识").split()) == {"知", "识", "知识", "识知"}
    assert cjk_tokens("😀  ") == ""


@pytest.mark.parametrize("query", ["😀", "   ", '""'])
def test_empty_units_return_empty(index, query):
    add(index, "/explicit/source.txt", ["正常正文"])
    assert search(index, query) == []


@pytest.mark.parametrize("value", ["999999999999999999m", "99999999d", "999999999999w", "9"*5000+"m"])
def test_hostile_relative_date_is_literal_and_never_loops(value):
    started = time.monotonic()
    clean, filters = parse_filters("needle after:" + value)
    assert filters.after is None
    assert "after:" in clean
    assert time.monotonic() - started < 0.5


def test_delete_replace_and_failed_cleanup_remove_derived_rows(index):
    fid = add(index, "/explicit/source.txt", ["旧中文知识", "编号 AB-12"])
    index.replace_segments(fid, [SegmentRec(kind="name", modality="none", text_for_fts="new")])
    assert search(index, "旧中文知识") == []
    assert index.db.execute("SELECT COUNT(*) FROM lookup_terms WHERE source='body'").fetchone()[0] == 0
    index.delete_content_segments(fid)
    index.delete_file(fid)
    assert index.db.execute("SELECT COUNT(*) FROM fts_cjk").fetchone()[0] == 0
    assert index.db.execute("SELECT COUNT(*) FROM lookup_terms").fetchone()[0] == 0
