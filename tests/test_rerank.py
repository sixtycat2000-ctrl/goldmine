"""Optional ranker must never alter admission, source text or fast execution."""
from dataclasses import replace

import pytest

from goldmine.application import Application
from goldmine.contracts import SearchRequest
from goldmine.deadline import RequestTimeout
from goldmine.quality import rerank_candidates
from goldmine.rerank import LocalReranker, RerankError, validate_scores
from goldmine.store import FileRec, SegmentRec, Store


@pytest.mark.parametrize("rows", [
    [], [{"index": 0, "relevance_score": .5}] * 2,
    [{"index": True, "relevance_score": .5}, {"index": 1, "relevance_score": .2}],
    [{"index": 0, "relevance_score": float("nan")}, {"index": 1, "relevance_score": .2}],
    [{"index": 0, "relevance_score": 1.01}, {"index": 1, "relevance_score": .2}],
    [{"index": 0, "relevance_score": .5}, {"index": 2, "relevance_score": .2}],
])
def test_incomplete_duplicate_nonfinite_or_wrong_id_confidences_rejected(rows):
    with pytest.raises(RerankError):
        validate_scores({"results": rows}, 2)


def test_wire_order_is_mapped_by_document_index():
    assert validate_scores({"results": [{"index": 1, "relevance_score": .8},
                                         {"index": 0, "relevance_score": .2}]}, 2) == [.2, .8]


def test_exact_token_cap_is_verified_against_original_unicode(tmp_path):
    backend = LocalReranker(None, tmp_path / "missing")
    calls = []
    def post(endpoint, payload):
        text = payload["content"]
        calls.append(text)
        return {"tokens": [{"piece": c} for c in text] if payload.get("with_pieces") else list(range(len(text)))}
    backend._post = post
    clipped, count = backend._bounded_text("中文资料" * 200, 384)
    assert clipped == ("中文资料" * 200)[:384] and count == 384
    backend._post = lambda *a: {"tokens": [{"piece": "invented"}]}
    with pytest.raises(RerankError, match="round-trip"):
        backend._bounded_text("actual source", 384)


def test_fast_zero_factory_quality_fallback_and_cancellation_cleanup(tmp_path):
    store = Store(tmp_path / "index.sqlite")
    for name in ("a", "b"):
        fid = store.upsert_file(FileRec(path=f"/explicit/{name}.txt", state="done", capability="content_indexed"))
        store.replace_segments(fid, [SegmentRec(kind="chunk", modality="none", text_for_fts=f"needle {name}")])
    store.close()
    calls = []
    class Ranker:
        def rank(self, query, docs):
            calls.append((query, docs))
            return [float("nan")] * len(docs), {}
        def close(self):
            calls.append("closed")
    app = Application(store.path, reranker_factory=Ranker)
    request = SearchRequest("needle", scorer="lexical", lexical_only=True)
    fast = app.search(request)
    assert calls == []
    # A lexical scorer may use an independent ranker, but explicit --lexical
    # remains a strict zero-model mode and therefore refuses this combination.
    quality = replace(request, mode="quality", rerank="local", lexical_only=False)
    result = app.search(quality)
    assert [h.segment_id for h in result.hits] == [h.segment_id for h in fast.hits]
    assert result.fallback_reason == "reranker_failed:RerankError"
    assert calls[-1] == "closed" and not result.settings["reranker"]["executed"]
    class Cancelled(Ranker):
        def rank(self, *args):
            raise RequestTimeout("cancelled")
    app.reranker_factory = Cancelled
    with pytest.raises(Exception) as error:
        app.search(quality)
    assert error.value.kind == "timeout" and calls[-1] == "closed"


def test_only_twenty_admitted_segments_and_ties_use_stable_id():
    from goldmine.search import Hit
    hits = [Hit(i+1, 2, None, None, 1, "doc", f"/{i}", 1, 1, "chunk", None, "content_indexed",
                "lexical", segment_id=30-i) for i in range(25)]
    class Stored:
        def segment_text(self, sid):
            return f"stored {sid}"
    class Ranker:
        closed = False
        def rank(self, query, docs):
            assert len(docs) == 20 and docs[0] == "stored 30"
            return [.5] * 20, {}
        def close(self):
            self.closed = True
    ranker = Ranker()
    result, meta, fallback = rerank_candidates(Stored(), "q", hits, lambda: ranker, why=True)
    assert ranker.closed and meta["executed"] and fallback is None
    assert [h.segment_id for h in result[:20]] == list(range(11, 31))
    assert result[20:] == hits[20:] and all(h.why["rerank_score"] == .5 for h in result[:20])


def test_quality_reordering_cannot_fold_the_only_surviving_result(tmp_path):
    import numpy as np
    from goldmine.pack import PackSettings, build_pack_from_hits
    from goldmine.quality import select_results
    from goldmine.search import Searcher

    store = Store(tmp_path / "index.sqlite")
    try:
        vector = np.zeros(768, np.float32); vector[0] = 1
        for name, days in [("a", 18), ("b", 45)]:
            fid = store.upsert_file(FileRec(path=f"/explicit/{name}.txt", state="done", capability="content_indexed"))
            store.replace_segments(fid, [SegmentRec(kind="chunk", modality="text", loc=1, loc_end=1,
                        text_for_fts=f"needle warranty lasts {days} days", vec=vector)])
        from goldmine.backends.mock import MockBackend

        hits = Searcher(store, MockBackend()).search("needle", scorer="hybrid")
        assert hits[1].duplicate_of == 1
        final = select_results(list(reversed(hits)), limit=1, balance_topics=False)
        assert final[0].rank == 1 and final[0].duplicate_of is None
        text, meta = build_pack_from_hits(store, None, PackSettings(query="needle", context=0), final)
        assert len(meta["records"]) == 1 and "45 days" in text
        # Fast packs also preserve different facts despite similar vectors.
        text, meta = build_pack_from_hits(store, None, PackSettings(query="needle", context=0), hits)
        assert len(meta["records"]) == 2 and "18 days" in text and "45 days" in text
    finally:
        store.close()
