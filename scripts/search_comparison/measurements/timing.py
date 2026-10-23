"""Finite attempt denominators; successful-only numbers never certify a run."""
from __future__ import annotations
import math

from answer_eval.protocol import EvaluationError
from .selection import verify_schedule


def nearest(values, p):
    if not values:
        return None
    if not 0 < p <= 1 or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values):
        raise EvaluationError("finite nonnegative timing values required")
    return sorted(values)[math.ceil(p * len(values)) - 1]


def distribution(values):
    return {"n": len(values), "p50_ms": nearest(values, .5), "p95_ms": nearest(values, .95), "max_ms": max(values) if values else None}


def summary(selection, slots, observations):
    verify_schedule(selection, slots)
    if (not isinstance(slots, list) or not isinstance(observations, list) or len(observations) != len(slots)
            or any(not isinstance(r, dict) for r in observations)):
        raise EvaluationError("every planned attempt requires one explicit disposition")
    for slot, row in zip(slots, observations):
        if row.get("ordinal") != slot["ordinal"] or row.get("id") != slot["id"]:
            raise EvaluationError("attempt order/identity differs from fixed schedule")
        if row.get("status") not in {"OK", "FAIL", "MISSING", "TIMEOUT", "PARSE_ERROR"}:
            raise EvaluationError("unknown attempt disposition")
        duration = row.get("wall_ms")
        if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0):
            raise EvaluationError("observed duration must be positive finite or explicit null")
        if row["status"] == "OK" and (duration is None or row.get("command") is None):
            raise EvaluationError("successful sample needs actual duration and raw reference")
        if row.get("resource_status") not in {"PASS", "FAIL", "MISSING"}:
            raise EvaluationError("every attempt needs its explicit resource disposition")
        if row["status"] == "OK" and row["resource_status"] == "FAIL":
            raise EvaluationError("an observed resource FAIL cannot be an OK sample")
        if slot["prime_ordinal"] is not None and row["status"] == "OK":
            if observations[slot["prime_ordinal"] - 1]["status"] != "OK":
                raise EvaluationError("warm sample lacks successful registered immediate prime")
    cells = {}
    for slot, row in zip(slots, observations):
        if slot["role"] == "measured":
            cells.setdefault((slot["view"], slot["arm"], slot["regime"]), []).append(row)
    result = []
    for (view, arm, regime), rows in sorted(cells.items()):
        if len(rows) != 30:
            raise EvaluationError("each regime/cell must retain30 fixed measured slots")
        good = [r["wall_ms"] for r in rows if r["status"] == "OK"]
        attempted = [r["wall_ms"] for r in rows if r["wall_ms"] is not None]
        result.append({"view": view, "arm": arm, "regime": regime, "n_fixed": 30,
                       "n_attempted": len(attempted), "n_success": len(good), "failed_ordinals": [r["ordinal"] for r in rows if r["status"] != "OK"],
                       "complete_service_latency": distribution(good) if len(good) == 30 else None,
                       "successful_only_diagnostic": distribution(good), "all_attempt_duration_including_errors": distribution(attempted)})
    # Authentication belongs to the source-bound command/resource replay. This
    # pure reconstruction cannot promote caller-supplied rows into real proof.
    resource_status = ("FAIL" if any(r["resource_status"] == "FAIL" for r in observations) else
                       "PASS" if all(r["resource_status"] == "PASS" for r in observations) else "MISSING")
    return {"status": "STRUCTURAL_SUMMARY_REQUIRES_AUTHENTICATED_RAW_REPLAY", "cells": result, "resource_status": resource_status,
            "quality_evidence": False, "release_eligible": False}
