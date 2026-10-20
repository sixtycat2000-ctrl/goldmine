"""Rank metrics and complete frozen fact coverage from actually emitted spans."""

from __future__ import annotations

import math
from collections import defaultdict

from answer_eval.protocol import EvaluationError
from answer_eval.statistics import paired_group_bootstrap
from .schema import CLASSES, MEDIA, NOISE


def _covers(evidence, expected):
    if evidence.get("readback_verified") is not True or not evidence.get("citation_id"):
        return False
    if (evidence["path"] != expected["path"] or evidence["view_id"] != expected["view_id"]
            or evidence["evidence_type"] != expected["evidence_type"]):
        return False
    a, b = evidence["location"], expected["location"]
    if (a is None) != (b is None):
        return False
    if b is not None and (a["unit"] != b["unit"] or a["end"] < b["start"] or a["start"] > b["end"]):
        return False
    span = expected["span"]
    if not evidence["start_char"] <= span["start_char"] <= span["end_char"] <= evidence["end_char"]:
        return False
    offset = span["start_char"] - evidence["start_char"]
    return evidence["text"][offset:offset + len(span["text"])] == span["text"]


def score_query(query, hits, emitted):
    """hits are unique document rows with verified spans; emitted is pack-only.

    A file hit or supported neighboring citation does not imply that a frozen
    fact reached the consumer. The runner constructs emitted spans from actual
    pack body and read-back source segments, never from query annotations.
    """
    paths = [h["path"] for h in hits]
    if len(paths) != len(set(paths)):
        raise EvaluationError("duplicate file ranking would inflate document metrics")
    expected = query["expect"]
    gains = []
    relevant = []
    for hit in hits:
        matches = [e for e in expected if e["path"] == hit["path"]]
        exact = [e["grade"] for e in matches if any(_covers(span, e) for span in hit["spans"])]
        gain = max(exact, default=max((max(0, e["grade"] - 1) for e in matches), default=0))
        gains.append(gain)
        relevant.append(any(e["grade"] >= 2 and any(_covers(span, e) for span in hit["spans"]) for e in matches))
    by_path = defaultdict(int)
    for e in expected:
        by_path[e["path"]] = max(by_path[e["path"]], e["grade"])
    ideal = sorted(by_path.values(), reverse=True)[:10]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains[:10]))
    covered = {e["evidence_id"] for e in expected if any(_covers(span, e) for span in emitted)}
    facts = {f["fact_id"]: any(set(alternative) <= covered for alternative in f["sufficient_evidence"])
             for f in query["required_facts"]}
    noise = query["primary_class"] == "noise"
    return {"success@1": int(bool(relevant) and relevant[0]),
            "success@5": int(any(relevant[:5])),
            "MRR": next((1 / (i + 1) for i, ok in enumerate(relevant) if ok), 0),
            "nDCG@10": dcg / idcg if idcg else None,
            "noise_correct_empty": int(not hits) if noise else None,
            "all_required_facts@pack": int(all(facts.values())) if facts else None,
            "covered_evidence_ids": sorted(covered), "fact_results": facts, "n_hits": len(hits)}


def summarize(rows):
    def stats(subset):
        answerable = [r for r in subset if r["primary_class"] != "noise"]
        noise = [r for r in subset if r["primary_class"] == "noise"]
        values = {"n": len(subset), "n_answerable": len(answerable),
                  "n_source_groups": len({r["source_group"] for r in subset})}
        for metric in ("success@1", "success@5", "MRR", "nDCG@10", "all_required_facts@pack"):
            measured = [r["metrics"][metric] for r in answerable if r["metrics"][metric] is not None]
            values[metric] = {"value": sum(measured) / len(measured) if measured else None,
                              "n": len(measured), "sum": sum(measured)}
        measured = [r["metrics"]["noise_correct_empty"] for r in noise]
        values["noise_correct_empty"] = {"value": sum(measured) / len(measured) if measured else None,
                                          "n": len(measured), "sum": sum(measured)}
        return values
    return {"overall": stats(rows),
            "classes": {c: stats([r for r in rows if r["primary_class"] == c]) for c in CLASSES},
            "media": {m: stats([r for r in rows if r["primary_class"] == "media-text" and m in r["tags"]]) for m in MEDIA},
            "noise_subtypes": {n: stats([r for r in rows if r["primary_class"] == "noise" and n in r["tags"]]) for n in NOISE}}


def paired_changes(reference_rows, candidate_rows, *, seed=20261021, reps=10000):
    base, candidate = ({r["id"]: r for r in rows} for rows in (reference_rows, candidate_rows))
    if len(base) != len(reference_rows) or len(candidate) != len(candidate_rows) or set(base) != set(candidate):
        raise EvaluationError("paired product comparison requires complete, unique equal query sets")
    result = {}
    for metric in ("success@1", "success@5", "MRR", "nDCG@10", "all_required_facts@pack", "noise_correct_empty"):
        deltas, groups = {}, {}
        for qid in sorted(base):
            if base[qid]["source_group"] != candidate[qid]["source_group"]:
                raise EvaluationError("source groups differ between paired runs")
            a, b = base[qid]["metrics"][metric], candidate[qid]["metrics"][metric]
            if (a is None) != (b is None):
                raise EvaluationError("metric denominator changed")
            if a is not None:
                deltas[qid], groups[qid] = b - a, base[qid]["source_group"]
        result[metric] = paired_group_bootstrap(deltas, groups, seed=seed, reps=reps) if deltas else None
    return result
