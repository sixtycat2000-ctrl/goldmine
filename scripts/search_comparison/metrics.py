"""File-level relevance only; positions are not comparable across these CLIs."""

from __future__ import annotations

import math

from answer_eval.protocol import EvaluationError


def score_files(question, paths):
    if len(paths) != len(set(paths)):
        raise EvaluationError("file ranks must already be collapsed without duplicates")
    relevance = {}
    for expected in question["expect"]:
        relevance[expected["path"]] = max(relevance.get(expected["path"], 0), expected["grade"])
    grades = [relevance.get(path, 0) for path in paths]
    ideal = sorted(relevance.values(), reverse=True)[:10]
    dcg = sum((2 ** grade - 1) / math.log2(rank + 2) for rank, grade in enumerate(grades[:10]))
    idcg = sum((2 ** grade - 1) / math.log2(rank + 2) for rank, grade in enumerate(ideal))
    return {"success@1": int(any(grades[:1])), "success@5": int(any(grades[:5])),
            "MRR": next((1 / rank for rank, grade in enumerate(grades, 1) if grade > 0), 0),
            "nDCG@10": dcg / idcg if idcg else 0,
            "empty_result": int(not paths)}


def summarize(rows):
    """All eligible rows remain in the denominator, including CLI failures."""
    answerable = [row for row in rows if row["primary_class"] != "noise"]
    noise = [row for row in rows if row["primary_class"] == "noise"]

    def measured(group):
        return sum(row["status"] == "OK" for row in group)

    def means(group, names):
        result = {}
        for name in names:
            total = sum(row["metrics"].get(name, 0) for row in group if row["status"] == "OK")
            result[name] = {"sum": total, "n": len(group), "value": total / len(group) if group else None}
        return result

    classes = {}
    for name in sorted({row["primary_class"] for row in rows}):
        group = [row for row in rows if row["primary_class"] == name]
        classes[name] = {"n": len(group), "n_measured": measured(group),
                         "metrics": means(group, ("success@1", "success@5", "MRR", "nDCG@10", "empty_result"))}
    return {"n_queries": len(rows), "n_measured": measured(rows),
            "errors": [row["id"] for row in rows if row["status"] != "OK"],
            "answerable": {"n": len(answerable), "n_measured": measured(answerable),
                           "metrics": means(answerable, ("success@1", "success@5", "MRR", "nDCG@10"))},
            "noise": {"n": len(noise), "n_measured": measured(noise),
                      "metrics": means(noise, ("empty_result",))},
            "classes": classes}
