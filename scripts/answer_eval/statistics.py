"""Stability diagnostics and paired resampling at the independent source unit."""

from __future__ import annotations

import math
import random
from collections import defaultdict


def compute_score_variance(records: dict[str, list[int]]) -> dict:
    """Four grades describe two answers, not four independent answer samples.

    The Wilson interval describes the observed question-level flag only. It
    cannot be used to infer a run-level false positive bound, especially when
    questions share a source or a model run.
    """
    if not isinstance(records, dict) or not records:
        raise ValueError("score diagnostics require nonempty answerable records")
    for qid, scores in records.items():
        if (not qid or not isinstance(scores, list) or len(scores) != 4
                or any(type(s) is not int or s not in (0, 1, 2) for s in scores)):
            raise ValueError(f"{qid}: require exactly four integer scores in 0..2")
    n = len(records)
    changed = sum(len(set(scores)) > 1 for scores in records.values())
    p = changed / n
    z = 1.959963984540054
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return {
        "n_answerable": n,
        "score_variation_count": changed,
        "p_score": p,
        "p_score_interval": [max(0.0, center - radius), min(1.0, center + radius)],
        "interval_method": "question-flag Wilson 95%; diagnostic, dependence not corrected",
        "round_means": [sum(s[i] for s in records.values()) / n for i in range(4)],
        "calibration_status": "uncalibrated",
        "false_positive_bound": None,
        "note": "Two answers, two judgments each; no run-level false positive guarantee.",
    }


def paired_group_bootstrap(deltas: dict[str, float], groups: dict[str, str], *,
                           seed: int = 20261021, reps: int = 10000) -> dict:
    """Sample G source groups with replacement and recompute the query mean.

    Unequal groups retain their original query counts. We do not average group
    means: that would change the estimand away from the reported query mean.
    """
    if not isinstance(deltas, dict) or not isinstance(groups, dict) or not deltas or set(deltas) != set(groups):
        raise ValueError("bootstrap requires complete, equal nonempty query/group sets")
    if type(seed) is not int or type(reps) is not int or not 1000 <= reps <= 100000:
        raise ValueError("bootstrap seed must be an integer, reps must be 1000..100000")
    grouped: dict[str, list[float]] = defaultdict(list)
    for qid, delta in deltas.items():
        if (type(delta) not in (int, float) or not math.isfinite(delta)
                or not isinstance(groups[qid], str) or not groups[qid]):
            raise ValueError(f"{qid}: invalid delta or empty source group")
        grouped[groups[qid]].append(delta)
    aggregates = [(sum(grouped[g]), len(grouped[g])) for g in sorted(grouped)]
    rng = random.Random(seed)
    samples = []
    for _ in range(reps):
        total, count = 0.0, 0
        for _ in aggregates:
            group_sum, group_count = aggregates[rng.randrange(len(aggregates))]
            total += group_sum
            count += group_count
        samples.append(total / count)
    samples.sort()
    lo = samples[int(0.025 * reps)]
    hi = samples[min(reps - 1, int(0.975 * reps))]
    return {
        "mean_delta": sum(deltas.values()) / len(deltas),
        "ci95": [lo, hi],
        "n_queries": len(deltas),
        "n_source_groups": len(aggregates),
        "unit": "source_group",
        "algorithm": "paired-cluster-query-mean/percentile-v1",
        "seed": seed,
        "reps": reps,
        "significant_improvement": len(aggregates) >= 12 and lo > 0,
        "inference_status": "eligible" if len(aggregates) >= 12 else "insufficient_groups",
    }
