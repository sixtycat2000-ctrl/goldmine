"""Compound expansion must preserve entities and use only source evidence."""
from dataclasses import asdict, replace
import json

import pytest

from goldmine.contracts import SearchRequest
from goldmine.quality import merge_queries, select_results
from goldmine.query_plan import plan_query
from goldmine.search import Hit, QueryFilters


@pytest.mark.parametrize("query", [
    "Research and Development", 'find "alpha; beta"', '"比较A与B在性能"',
    "under:/some/path;a.md", "alpha beta;", "alpha; beta", "中文；测试",
    "alpha beta; ALPHA BETA", "one two; three four; five six", "unclosed \"quote; more words",
])
def test_unverified_grammar_never_expands(query):
    assert [q.text for q in plan_query(query)] == [query]


@pytest.mark.parametrize("query, expected", [
    ("alpha latency; beta memory", ["alpha latency", "beta memory"]),
    ("采购付款周期；售后保修政策", ["采购付款周期", "售后保修政策"]),
    ("compare Alpha and Beta on memory usage", ["Alpha memory usage", "Beta memory usage"]),
    ("比较苹果公司与微软公司在研发投入", ["苹果公司 研发投入", "微软公司 研发投入"]),
])
def test_plan_is_deterministic_and_every_added_word_has_source_span(query, expected):
    plan = plan_query(query)
    assert [q.text for q in plan[1:]] == expected
    assert [(q.id, q.weight) for q in plan] == [("q0", 2), ("q1", 1), ("q2", 1)]
    assert json.dumps([asdict(q) for q in plan]) == json.dumps([asdict(q) for q in plan_query(query)])
    for q in plan[1:]:
        assert q.text == " ".join(query[a:b].strip() for a, b in q.spans)


def hit(sid, path):
    return Hit(1, 9, None, None, 1, "doc", path, 1, 1, "chunk", "source", "content_indexed", "lexical", segment_id=sid)


def test_multiquery_shared_filters_dedupe_identity_and_round_robin():
    shared = QueryFilters(under=["/explicit"])
    request = SearchRequest("alpha latency; beta memory", filters=shared, limit=1, mode="quality", multi_query=True)
    class Search:
        def search(self, query, **kwargs):
            assert kwargs["filters"] is shared
            assert kwargs["limit"] == 20
            return [hit(1, "/explicit/a")] if "alpha" in query else [hit(2, "/explicit/b")]
    hits, metadata = merge_queries(Search(), request, {"filters": shared})
    assert len(hits) == 2 and metadata["merged_segments"] == 2
    assert hits[0].query_ids == ["q0", "q1"]
    selected = select_results(hits, limit=2, balance_topics=True)
    assert [h.segment_id for h in selected] == [1, 2]
    assert selected[0].query_ids == ["q0", "q1"]
    assert select_results(hits, limit=0, balance_topics=True) == []


def test_failed_subquery_is_explicit_and_cancellation_stops_later_queries():
    from goldmine.deadline import RequestTimeout
    request = SearchRequest("alpha latency; beta memory", mode="quality", multi_query=True)
    class Search:
        calls = []
        def search(self, query, **kwargs):
            self.calls.append(query)
            if query == "alpha latency":
                raise ValueError("fixture failure")
            return [hit(1, "/explicit/a")]
    search = Search()
    hits, metadata = merge_queries(search, request, {})
    assert hits and metadata["subquery_failures"] == [{"query_id": "q1", "reason": "ValueError"}]
    def cancelled(query, **kwargs):
        raise RequestTimeout("expired")
    search.search = cancelled
    with pytest.raises(RequestTimeout):
        merge_queries(search, request, {})
