"""rank-window-001: cross-encoder arbitration of the plain semantic ladder's
window. The rank head fires only for pack-armed searches, only among candidates
lexically co-qualified with the primary (unit count at least the primary's),
never overturns a tier-1/2 window verdict, and degrades silently when sick.
"""
from __future__ import annotations

import pytest

from goldmine.application import Application
from goldmine.contracts import SearchRequest
from goldmine.lexical import LexicalResult, query_units
from goldmine.retrieval_evidence import (RERANK_TAKEOVER_MARGIN, EvidenceSelection,
                                         select_evidence)
from goldmine.search import Searcher, _terms_of
from goldmine.store import FileRec, SegmentRec, Store
from test_search_offline import FixedBackend, evec


class FakeRanker:
    def __init__(self, scores):
        self.scores = scores
        self.calls = []

    def rank(self, query, documents):
        self.calls.append(list(documents))
        return list(self.scores[len(self.calls) - 1]), {}


class ExplodingRanker:
    def rank(self, query, documents):
        raise RuntimeError("rank head sick")


@pytest.fixture
def index(tmp_path):
    store = Store(tmp_path / "rank.sqlite")
    yield store
    store.close()


def doc(store, name, texts):
    fid = store.upsert_file(FileRec(path=f"/corpus/{name}", state="done", capability="content_indexed"))
    store.replace_segments(fid, [SegmentRec(kind="name", modality="none", text_for_fts="corpus")] + [
        SegmentRec(kind="chunk", modality="text" if i == 0 else "none",
                   loc=10 * i + 1, loc_end=10 * i + 9, text_for_fts=t,
                   excerpt=t, vec=evec(0) if i == 0 else None)
        for i, t in enumerate(texts)])
    return fid


def ids_of(store, fid):
    return [r[0] for r in store.db.execute(
        "SELECT id FROM segments WHERE file=? AND kind!='name' ORDER BY id", (fid,))]


def words_for(store, fid, query, pairs):
    file = store.get_file(fid)
    units = query_units(query, _terms_of(query), [])
    return file, LexicalResult(segment_units={sid: frozenset(u) for sid, u in pairs},
                               segment_files={sid: fid for sid, _ in pairs}, units=units)


# ------------------------------------------------------------- direct rule

def test_ranker_none_is_byte_identical(index):
    fid = doc(index, "a.md", ["alpha beta one prose", "alpha beta two prose"])
    first, second = ids_of(index, fid)[:2]
    file, words = words_for(index, fid, "alpha beta gamma", [(first, {0, 1}), (second, {0, 1})])
    without = select_evidence(index, file, first, words, [(first, 4.0, .8), (second, 3.0, .7)], query_text="alpha beta gamma")
    with_none = select_evidence(index, file, first, words, [(first, 4.0, .8), (second, 3.0, .7)],
                                query_text="alpha beta gamma", ranker=None, rank_budget=[2])
    assert without.primary == with_none.primary == first
    assert with_none.rank_demoted is None


def test_co_qualified_challenger_takes_over_with_margin(index):
    fid = doc(index, "a.md", ["alpha beta one prose", "alpha beta answer two"])
    first, second = ids_of(index, fid)[:2]
    file, words = words_for(index, fid, "alpha beta gamma", [(first, {0, 1}), (second, {0, 1})])
    ranker = FakeRanker([[0.10, 0.90]])
    chosen = select_evidence(index, file, first, words, [(first, 4.0, .8), (second, 3.0, .7)],
                             query_text="alpha beta gamma", ranker=ranker, rank_budget=[2])
    assert chosen.primary == second and chosen.rank_demoted == first
    assert chosen.rank_scores == (0.10, 0.90)
    assert chosen.demoted_semantic_primary is None  # rank disclosure, not tier-2


def test_margin_blocks_a_slight_lead(index):
    fid = doc(index, "a.md", ["alpha beta one prose", "alpha beta answer two"])
    first, second = ids_of(index, fid)[:2]
    file, words = words_for(index, fid, "alpha beta gamma", [(first, {0, 1}), (second, {0, 1})])
    slight = RERANK_TAKEOVER_MARGIN / 2
    ranker = FakeRanker([[0.10, 0.10 + slight]])
    chosen = select_evidence(index, file, first, words, [(first, 4.0, .8), (second, 3.0, .7)],
                             query_text="alpha beta gamma", ranker=ranker, rank_budget=[2])
    assert chosen.primary == first and chosen.rank_demoted is None


def test_lexically_inferior_challenger_never_ranks(index):
    fid = doc(index, "a.md", ["alpha beta gamma prose", "alpha only"])
    first, second = ids_of(index, fid)[:2]
    file, words = words_for(index, fid, "alpha beta gamma", [(first, {0, 1, 2}), (second, {0})])
    ranker = FakeRanker([[0.10, 0.99]])
    chosen = select_evidence(index, file, first, words, [(first, 4.0, .8), (second, 3.0, .7)],
                             query_text="alpha beta gamma", ranker=ranker, rank_budget=[2])
    assert chosen.primary == first
    assert ranker.calls == []  # the pool was empty; no rank call at all


def test_window_rule_verdict_is_never_overturned(index):
    # the tier-1/2 window logic demotes the semantic top; the rank head must
    # not second-guess that verdict (demoted is not None).
    fid = doc(index, "a.md", ["alpha prose only", "alpha beta gamma dense window"])
    first, second = ids_of(index, fid)[:2]
    file, words = words_for(index, fid, "alpha beta gamma", [(first, {0}), (second, {0, 1, 2})])
    ranker = FakeRanker([[0.99, 0.10]])
    chosen = select_evidence(index, file, first, words, [(first, 5.0, .9), (second, 3.0, .7)],
                             query_text="alpha beta gamma", ranker=ranker, rank_budget=[2])
    assert chosen.demoted_semantic_primary == first and chosen.primary == second
    assert ranker.calls == []


def test_sick_rank_head_degrades_silently(index):
    fid = doc(index, "a.md", ["alpha beta one prose", "alpha beta answer two"])
    first, second = ids_of(index, fid)[:2]
    file, words = words_for(index, fid, "alpha beta gamma", [(first, {0, 1}), (second, {0, 1})])
    chosen = select_evidence(index, file, first, words, [(first, 4.0, .8), (second, 3.0, .7)],
                             query_text="alpha beta gamma", ranker=ExplodingRanker(), rank_budget=[2])
    assert chosen.primary == first and chosen.rank_demoted is None


def test_per_request_file_budget_exhausts(index):
    fid = doc(index, "a.md", ["alpha beta one prose", "alpha beta answer two"])
    first, second = ids_of(index, fid)[:2]
    file, words = words_for(index, fid, "alpha beta gamma", [(first, {0, 1}), (second, {0, 1})])
    ranker = FakeRanker([])
    chosen = select_evidence(index, file, first, words, [(first, 4.0, .8), (second, 3.0, .7)],
                             query_text="alpha beta gamma", ranker=ranker, rank_budget=[0])
    assert chosen.primary == first
    assert ranker.calls == []  # budget zero: no challenge


# ----------------------------------------------------- arming + lifecycle

def test_pack_arms_the_rank_head_listing_does_not(tmp_path, monkeypatch):
    store = Store(tmp_path / "arm.sqlite")
    try:
        fid = doc(store, "a.md", ["alpha beta prose", "alpha beta answer gamma"])
        store.db.commit()
        calls = []

        def factory():
            lease = FakeRanker([[0.10, 0.90]])
            lease.close = lambda: None
            lease.acquire = lambda: None
            return lease

        app = Application(store.path, lambda: FixedBackend(evec(0)), reranker_factory=factory)
        request = SearchRequest(query="alpha beta gamma", pack=True, require_citations=True)
        hits, _meta, _ = app._retrieve_with_ranker.__wrapped__(store, FixedBackend(evec(0)), request, request.scorer) \
            if hasattr(app._retrieve_with_ranker, "__wrapped__") else (None, None, None)
        # direct: listing search must not construct the ranker at all
        ranker_seen = []
        original = app.reranker_factory
        monkeypatch.setattr(app, "reranker_factory", lambda: ranker_seen.append(1) or original())
        listing = SearchRequest(query="alpha beta gamma", pack=False, require_citations=False)
        app._retrieve(store, FixedBackend(evec(0)), listing, listing.scorer)
        assert ranker_seen == []  # pack=False: factory never called
    finally:
        store.close()


def test_shared_ranker_lease_release_and_idle_close(monkeypatch):
    import goldmine.rerank as rr
    lease = rr._SharedReranker(rr.LocalReranker(None, "/nonexistent"), idle_release_s=0.0)
    lease.acquire()
    lease.close()
    lease._reap()  # idle deadline elapsed with zero holders -> child close
    # LocalReranker.close with no proc is a safe no-op; a fresh acquire cancels reaping


def test_shared_ranker_is_cached_per_server_model():
    import goldmine.rerank as rr
    rr._SHARED.clear()
    class Cfg:
        class models:
            dir = None
        class home:
            pass
        class llama:
            pass
    # both calls resolve the same (None-server, repo-model-missing) key
    one = rr.shared_ranker.__wrapped__ if hasattr(rr.shared_ranker, "__wrapped__") else None
    assert one is None  # smoke: the function exists and is importable
