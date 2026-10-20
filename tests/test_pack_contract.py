"""Adversarial bounds apply even when no evidence records can be emitted."""
from __future__ import annotations

import pytest

from goldmine.pack import BudgetTooSmall, PackSettings, build_pack_from_hits
from goldmine.store import Store


@pytest.mark.parametrize("budget", [512, 1000, 4000, 32768])
@pytest.mark.parametrize("context", [0, 1, 3])
@pytest.mark.parametrize("body", [
    "中文证据：退款期限为四十五天。\n" * 12,
    "| item | value |\n| copper | 431 |\n" * 12,
    "def calculate(x):\n    return x * 431\n" * 12,
    "large indivisible source " * 3000,
], ids=["chinese", "table", "code", "oversized"])
def test_complete_cited_envelope_fits_or_honestly_omits_whole_record(tmp_path, budget, context, body):
    from goldmine.application import Application
    from goldmine.contracts import ContextRequest, SearchRequest
    from goldmine.store import FileRec, SegmentRec

    store = Store(tmp_path / "index.sqlite")
    path = "/explicit/" + "中文长目录/" * 12 + "record.md"
    fid = store.upsert_file(FileRec(path=path, state="done", capability="content_indexed"))
    store.replace_segments(fid, [SegmentRec(kind="chunk", modality="none", loc=1, loc_end=30,
                                           text_for_fts=body)])
    store.close()
    request = SearchRequest("source" if "source" in body else "431" if "431" in body else "退款",
                            scorer="lexical", lexical_only=True, pack=True, budget=budget, context=context)
    app = Application(store.path)
    first, second = app.search(request), app.search(request)
    assert first.pack_text == second.pack_text
    from goldmine.chunker import est_tokens
    assert first.pack_meta["used_tokens"] == est_tokens(first.pack_text) <= budget
    for citation in first.citations:
        # Search citations may refer to a record omitted for budget. Every ID
        # actually emitted in the context must nevertheless round-trip.
        if citation.citation_id in first.pack_text:
            returned = (app.context(ContextRequest([citation.citation_id], neighbors=0, budget=32768))
                        if len(body) > 24000 else app.get(citation.citation_id, budget=8000))
            assert citation.citation_id in returned.context_text


def test_discontinuous_pdf_evidence_keeps_exact_pages_and_round_trip_ids(tmp_path):
    from goldmine.application import Application
    from goldmine.citations import read_evidence
    from goldmine.contracts import SearchRequest
    from goldmine.search import Searcher
    from goldmine.store import FileRec, SegmentRec

    store = Store(tmp_path / "index.sqlite")
    fid = store.upsert_file(FileRec(path="/explicit/neutral.pdf", kind="pdf", state="done", capability="content_indexed"))
    store.replace_segments(fid, [
        SegmentRec(kind="chunk", modality="none", loc=page, loc_end=page, text_for_fts=text)
        for page, text in [(3, "needle warranty lasts 45 days"), (17, "needle payment due 18 days")]
    ])
    hits = Searcher(store, None).search("warranty", scorer="lexical")
    other_sid = store.db.execute("SELECT id FROM segments WHERE loc=17").fetchone()[0]
    hits[0].supporting_segment_ids = [other_sid]
    text, meta = build_pack_from_hits(store, None, PackSettings(query="warranty payment", context=0), hits,
                                     citation_id_of=lambda sid: read_evidence(store, sid).citation.citation_id)
    assert "45 days" in text and "18 days" in text
    assert "p.3" in text and "p.17" in text
    assert "p.3–17" not in text
    ids = [read_evidence(store, sid).citation.citation_id for sid in [hits[0].segment_id, other_sid]]
    store.close()
    for cid in ids:
        assert cid in text
        assert cid in Application(store.path).get(cid).context_text
    assert len(meta["records"]) == 1 and len(meta["records"][0]["segments"]) == 2


@pytest.mark.parametrize("backend", [None, "exact"])
def test_empty_pack_with_maximum_query_cannot_exceed_budget(tmp_path, backend):
    class Counter:
        def count_tokens(self, texts):
            return [len(text.encode("utf-8")) // 4 for text in texts]

    store = Store(tmp_path / "index.sqlite")
    try:
        with pytest.raises(BudgetTooSmall, match="budget_too_small"):
            build_pack_from_hits(store, Counter() if backend else None,
                                 PackSettings(query="x" * 8192, budget=512), [])
    finally:
        store.close()


def test_empty_pack_fits_and_counts_whole_envelope(tmp_path):
    store = Store(tmp_path / "index.sqlite")
    try:
        text, meta = build_pack_from_hits(store, None, PackSettings(query="ordinary", budget=512), [])
        assert meta["used_tokens"] <= 512
        assert meta["fixpoint_converged"]
        assert "ordinary" in text
    finally:
        store.close()


@pytest.mark.parametrize("first_succeeds", [True, False])
def test_tokenizer_availability_never_mixes_exact_and_estimate(tmp_path, first_succeeds):
    from goldmine.chunker import est_tokens

    class Flaky:
        calls = 0
        def count_tokens(self, texts):
            self.calls += 1
            if (self.calls == 1) == first_succeeds:
                return [1]
            return None
    backend = Flaky()
    store = Store(tmp_path / "index.sqlite")
    try:
        text, meta = build_pack_from_hits(store, backend, PackSettings(query="ordinary", budget=512), [])
        assert meta["token_mode"] == "estimate" and "mode=estimate" in text
        assert meta["used_tokens"] == est_tokens(text)
        if not first_succeeds:
            assert backend.calls == 1  # fixed estimator, even if SDK later recovers
    finally:
        store.close()


def test_pdf_neighbor_pages_are_not_displaced_by_same_page_chunks(tmp_path):
    from goldmine.pack import NeighborIndex
    from goldmine.search import Hit
    from goldmine.store import FileRec, SegmentRec

    store = Store(tmp_path / "pages.sqlite")
    try:
        fid = store.upsert_file(FileRec(path="/explicit/multiblock.pdf", kind="pdf", state="done"))
        store.replace_segments(fid, [SegmentRec(kind="chunk", modality="none", loc=page, loc_end=page,
                    text_for_fts=f"stored source block {i}") for i, page in enumerate([2, 3, 3, 3, 4, 17])])
        ids = store.db.execute("SELECT id FROM segments ORDER BY id").fetchall()
        hit = Hit(1, 1, None, None, 1, "pdf", "/explicit/multiblock.pdf", 3, 3,
                  "chunk", None, "content_indexed", "lexical", segment_id=ids[2][0])
        neighbor = NeighborIndex(store)
        parts, _ = neighbor.parts_for(hit, 1)
        spans, _ = neighbor.spans_for(hit, 1)
        assert [p["loc"] for p in parts] == [2, 3, 3, 3, 4]
        assert {p["loc"] for p in spans} == {2, 3, 4}
    finally:
        store.close()


def test_exact_duplicate_pack_keeps_each_original_source_and_citation(tmp_path):
    from goldmine.application import Application
    from goldmine.contracts import SearchRequest
    from goldmine.store import FileRec, SegmentRec

    store = Store(tmp_path / "duplicates.sqlite")
    for name in ("a", "b"):
        fid = store.upsert_file(FileRec(path=f"/explicit/{name}.txt", state="done", capability="content_indexed"))
        store.replace_segments(fid, [SegmentRec(kind="chunk", modality="none", loc=1, loc_end=1,
                                               text_for_fts="needle original shared source")])
    store.close()
    app = Application(store.path)
    result = app.search(SearchRequest("needle", scorer="lexical", lexical_only=True, pack=True, context=0))
    assert len(result.pack_meta["records"]) == 1 and result.pack_meta["duplicates_folded"] == 1
    for citation in result.citations:
        assert citation.path in result.pack_text and citation.citation_id in result.pack_text
        assert "original shared source" in app.get(citation.citation_id).context_text


def test_record_cap_accounts_for_every_admitted_hit(tmp_path):
    from goldmine.application import Application
    from goldmine.contracts import SearchRequest
    from goldmine.store import FileRec, SegmentRec

    store = Store(tmp_path / "many.sqlite")
    for number in range(50):
        fid = store.upsert_file(FileRec(path=f"/explicit/{number}.txt", state="done", capability="content_indexed"))
        store.replace_segments(fid, [SegmentRec(kind="chunk", modality="none", text_for_fts=f"needle unique source {number}")])
    store.close()
    result = Application(store.path).search(SearchRequest("needle", scorer="lexical", lexical_only=True,
                                                         limit=50, pack=True, budget=32768))
    meta = result.pack_meta
    assert len(meta["records"]) == 24 and meta["skipped_by_record_limit"] == 26
    assert len(meta["records"]) + meta["also_folded"] + meta["duplicates_folded"] + meta["skipped_over_budget"] + meta["skipped_by_record_limit"] == 50
    assert "26 skipped (record limit)" in result.pack_text
