"""Label-blind deterministic selection and the complete finite slot schedule."""
from __future__ import annotations
from collections import Counter
import hashlib

from answer_eval.protocol import EvaluationError, canonical_sha256, fields
from product_eval.schema import CLASSES
from search_comparison.engine_capture.protocol import MANDATORY, CONTROLS, VIEWS

SEED = "goldmine-p2-08-measurements-v1-20261022"
ARMS = MANDATORY + CONTROLS
KINDS = {"doc": 41, "code": 19, "pdf": 12, "screenshot": 20, "image": 10, "audio": 20}
ADDITIONS = {"doc": 2, "code": 2, "pdf": 2, "screenshot": 2, "audio": 1, "image": 1}


def key(domain, identifier):
    return hashlib.sha256((SEED + "\0" + domain + "\0" + identifier).encode()).hexdigest(), identifier


def select_sources(sources):
    if (not isinstance(sources, dict) or len(sources) != 122 or any(not isinstance(s, dict) or s.get("path") != p for p, s in sources.items())
            or Counter(s.get("kind") for s in sources.values()) != Counter(KINDS)):
        raise EvaluationError("exact122 frozen source-only identities/kinds required")
    additional = []
    for kind, count in ADDITIONS.items():
        rows = sorted((p for p, s in sources.items() if s["kind"] == kind), key=lambda p: key("addition-" + kind, p))
        additional.extend(rows[:count])
    additional.sort(key=lambda p: key("addition-order", p))
    base = sorted(set(sources) - set(additional), key=lambda p: key("base-order", p))
    docs = sorted((p for p in base if sources[p]["kind"] == "doc"), key=lambda p: key("lifecycle-doc", p))
    chosen = {"base112": base, "add10": additional, "remove": docs[0], "replace_target": docs[1], "replace_donor": docs[2]}
    if len(base) != 112 or len(additional) != 10 or set(base) & set(additional) or set(base) | set(additional) != set(sources):
        raise EvaluationError("source cohort denominator changed")
    return {"seed": SEED, **chosen, "sha256": canonical_sha256(chosen)}


def select_requests(requests):
    if not isinstance(requests, list) or len(requests) != 210:
        raise EvaluationError("exact210 fixed dev requests required")
    for r in requests:
        fields(r, {"id", "q", "primary_class", "source_group"}, "dev measurement request")
        if (any(not isinstance(r[k], str) or not r[k] or "\x00" in r[k] for k in ("id", "q", "source_group"))
                or r["primary_class"] not in CLASSES):
            raise EvaluationError("invalid dev measurement request")
    if len({r["id"] for r in requests}) != 210 or Counter(r["primary_class"] for r in requests) != Counter({c: 30 for c in CLASSES}):
        raise EvaluationError("dev ID/class denominator changed")
    selected = []
    for cls in CLASSES:
        selected.extend(sorted((r for r in requests if r["primary_class"] == cls), key=lambda r: key("query-class-" + cls, r["id"]))[:4])
    ids = {r["id"] for r in selected}
    selected.extend(sorted((r for r in requests if r["id"] not in ids), key=lambda r: key("query-extra", r["id"]))[:2])
    selected.sort(key=lambda r: key("query-order", r["id"]))
    return {"seed": SEED, "requests": selected, "sha256": canonical_sha256(selected),
            "selection_rule": "four-per-class-plus-two-hash-remaining/v1"}


def schedule(selection):
    fields(selection, {"seed", "requests", "sha256", "selection_rule"}, "registered query selection")
    if (selection["seed"] != SEED or selection["sha256"] != canonical_sha256(selection["requests"])
            or selection["selection_rule"] != "four-per-class-plus-two-hash-remaining/v1"):
        raise EvaluationError("registered query selection identity differs")
    rows = selection["requests"]
    if len(rows) != 30 or len({r["id"] for r in rows}) != 30:
        raise EvaluationError("exact30 unique selected dev requests required")
    cells = [(view, arm) for view in VIEWS for arm in ARMS]
    cells.sort(key=lambda c: key("cell-order", "/".join(c)))
    slots = []
    def add(cell, request, role, regime, *, prime=None):
        slot = {"ordinal": len(slots) + 1, "view": cell[0], "arm": cell[1], "id": request["id"],
                "role": role, "regime": regime, "restore_pre_query_snapshot": regime == "cold_cli", "prime_ordinal": prime}
        slots.append(slot)
        return slot["ordinal"]
    for n in range(3):
        rotated = cells[n:] + cells[:n]
        for cell in rotated:
            for regime in ("cold_cli", "warm_cli"):
                add(cell, rows[n], "excluded_warmup", regime)
    for qn, request in enumerate(rows):
        shift = qn % len(cells)
        rotated = cells[shift:] + cells[:shift]
        for cn, cell in enumerate(rotated):
            regimes = ("cold_cli", "warm_cli") if (qn + cn) % 2 == 0 else ("warm_cli", "cold_cli")
            for regime in regimes:
                prime = add(cell, request, "cache_prime", regime) if regime == "warm_cli" else None
                add(cell, request, "measured", regime, prime=prime)
    return slots


def verify_schedule(selection, slots):
    if slots != schedule(selection):
        raise EvaluationError("fixed ordered slot schedule omitted/reordered/changed attempts")
    return {"cells": len(VIEWS) * len(ARMS), "samples_per_regime_per_cell": 30,
            "measured_slots": 1080, "cache_primes": 540, "excluded_warmups": 108,
            "all_slots": 1728, "sha256": canonical_sha256(slots)}
