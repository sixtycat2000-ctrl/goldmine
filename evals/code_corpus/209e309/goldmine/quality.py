"""Bounded optional retrieval composition after ordinary candidate admission."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace

from .deadline import RequestTimeout, check_deadline
from .query_plan import plan_query


def merge_queries(searcher, request, kwargs):
    plan = plan_query(request.query)
    if len(plan) == 1:
        hits = searcher.search(request.query, **kwargs)
        for hit in hits:
            hit.query_ids = ["q0"]
        return hits, {"queries": 1, "expanded": False, "subquery_failures": [], "merged_segments": len(hits)}
    by_id, values, topics, failures = {}, {}, {}, []
    for query in plan:
        check_deadline()
        try:
            hits = searcher.search(query.text, **{**kwargs, "limit": 20 if request.limit > 0 else 0})
        except RequestTimeout:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            if query.id == "q0":
                raise
            check_deadline()
            failures.append({"query_id": query.id, "reason": type(exc).__name__})
            continue
        for rank, hit in enumerate(hits, 1):
            if hit.segment_id is None:
                continue
            sid = hit.segment_id
            by_id.setdefault(sid, hit)
            values[sid] = values.get(sid, 0.) + query.weight / (60 + rank)
            topics.setdefault(sid, []).append(query.id)
    ordered = sorted(by_id, key=lambda sid: (-values[sid], by_id[sid].path, sid))[:60]
    hits = []
    for sid in ordered:
        hit = replace(by_id[sid], score=values[sid])
        hit.query_ids = topics[sid]
        if request.why:
            hit.why = {**(hit.why or {}), "query_ids": topics[sid], "fusion": "weighted_rrf_k60"}
        hits.append(hit)
    return hits, {"queries": len(plan), "expanded": len(plan) > 1,
                  "subquery_failures": failures, "merged_segments": len(hits)}


def rerank_candidates(store, query, hits, factory, *, why):
    """Only the admitted top twenty can move; failures preserve original order."""
    from .rerank import RerankError, validate_scores

    if not hits:
        return hits, {"executed": False, "reason": "no_admitted_candidates"}, None
    if factory is None:
        return hits, {"executed": False}, "local_reranker_not_configured"
    selected = hits[:20]
    try:
        texts = [store.segment_text(hit.segment_id) or "" for hit in selected]
        with closing(factory()) as backend:
            scores, metadata = backend.rank(query, texts)
        # Validate injected backends as strictly as the wire implementation.
        scores = validate_scores({"results": [{"index": i, "relevance_score": score}
                                              for i, score in enumerate(scores)]}, len(selected))
        ordered = sorted(zip(selected, scores), key=lambda item: (-item[1], item[0].segment_id))
        result = []
        for hit, score in ordered:
            replacement = replace(hit)
            replacement.rerank_score = score
            if why:
                replacement.why = {**(hit.why or {}), "rerank_score": score}
            result.append(replacement)
        return result + hits[20:], {"executed": True, "candidates": len(selected), **metadata}, None
    except RequestTimeout:
        raise
    except (RerankError, OSError, ValueError) as exc:
        check_deadline()
        return hits, {"executed": False}, f"reranker_failed:{type(exc).__name__}"


def select_results(hits, *, limit, balance_topics):
    """Pack rotates through real matched themes; outward list remains file-deduped."""
    if balance_topics:
        selected, used = [], set()
        for topic in ("q1", "q2", "q0"):
            hit = next((h for h in hits if h.segment_id not in used and topic in getattr(h, "query_ids", [])), None)
            if hit is not None:
                selected.append(hit)
                used.add(hit.segment_id)
        hits = selected + [h for h in hits if h.segment_id not in used]
    files, output = set(), []
    for hit in hits:
        if hit.path in files:
            # Other segments of a file are retained as pack-only supporting
            # evidence. The public one-file/one-hit retrieval contract remains.
            continue
        files.add(hit.path)
        supporting = list(dict.fromkeys(hit.supporting_segment_ids + [
            other.segment_id for other in hits
            if other.path == hit.path and other.segment_id != hit.segment_id
            and other.segment != "name" and other.segment_id is not None
        ]))[:2]
        # A subquery's/local reranker's old display ranks no longer identify
        # the final list. Pack verifies exact material equality independently.
        output.append(replace(hit, rank=len(output) + 1, supporting_segment_ids=supporting, duplicate_of=None))
        if len(output) >= limit:
            break
    return output if limit > 0 else []
