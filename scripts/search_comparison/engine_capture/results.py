"""Preserve engine order/raw material; file ranks never invent source positions."""
from __future__ import annotations

import base64
import binascii
import math
import re

from answer_eval.protocol import EvaluationError, fields, parse_json
from search_comparison.source_views.coverage import file_window


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _goldmine(data, arm, mapping):
    quality = arm in ("goldmine.quality-rerank", "goldmine.quality-multi")
    treatment = {"status": "NOT_REQUESTED"}
    settings, fallback = None, None
    if quality:
        fields(data, {"hits", "settings", "fallback_reason"}, "Goldmine quality JSON")
        settings, fallback = data["settings"], data["fallback_reason"]
        if not isinstance(settings, dict) or fallback is not None and not isinstance(fallback, str):
            raise EvaluationError("invalid actual quality execution metadata")
        expected = {"scorer": "hybrid", "mode": "quality", "rerank": "local" if arm.endswith("rerank") else "off",
                    "multi_query": arm.endswith("multi"), "limit": 100}
        if any(settings.get(k) != v or type(settings.get(k)) is not type(v) for k, v in expected.items()):
            raise EvaluationError("Goldmine actual quality mode differs from fixed treatment")
        if arm.endswith("rerank"):
            executed = settings.get("reranker")
            if not isinstance(executed, dict) or type(executed.get("executed")) is not bool:
                raise EvaluationError("local reranker execution proof is absent", status="MISSING")
            if executed["executed"] and (type(executed.get("candidates")) is not int or not 1 <= executed["candidates"] <= 20 or not executed.get("model")):
                raise EvaluationError("invalid actual local reranker candidate/model proof")
            treatment = {"status": "DEGRADED" if fallback else "EXECUTED" if executed["executed"] else "NO_ADMITTED_CANDIDATES",
                         "observations": executed}
        else:
            plan = settings.get("query_plan")
            if (not isinstance(plan, dict) or type(plan.get("queries")) is not int or plan["queries"] < 1
                    or type(plan.get("expanded")) is not bool or plan["expanded"] != (plan["queries"] > 1)
                    or not isinstance(plan.get("subquery_failures"), list) or any(not isinstance(v, dict) for v in plan["subquery_failures"])):
                raise EvaluationError("multi-query actual execution proof is absent/contradictory", status="MISSING")
            treatment = {"status": "DEGRADED" if fallback else "EXECUTED" if plan["expanded"] else "UNEXPANDED", "observations": plan}
        data = data["hits"]
    if not isinstance(data, list) or len(data) > 100:
        raise EvaluationError("Goldmine output is not the bounded actual hit list")
    rows = []
    for i, r in enumerate(data, 1):
        if (not isinstance(r, dict) or type(r.get("rank")) is not int or r["rank"] != i or not isinstance(r.get("path"), str)
                or not _finite(r.get("score"))):
            raise EvaluationError("Goldmine raw rank/path/score is malformed")
        rows.append({"source_path": mapping.file_path(r["path"]), "engine_rank": i, "engine_id": r.get("citation", r.get("segment_id")),
                     "score": r["score"], "raw": r, "position_status": "NOT_COMPARABLE_UNVERIFIED_ORIGINAL_COORDINATES"})
    if quality and treatment["status"] == "NO_ADMITTED_CANDIDATES" and rows:
        raise EvaluationError("reranker claims no admitted candidates but returns hits")
    return rows, {"settings": settings, "fallback_reason": fallback, "treatment": treatment}


def _qmd(data, index):
    if not isinstance(data, list) or len(data) > 100:
        raise EvaluationError("QMD output must contain at most100 raw rows")
    by_uri = {r["uri"]: r for r in index["documents"]}
    if len(by_uri) != len(index["documents"]):
        raise EvaluationError("QMD indexed URI registry collides")
    result = []
    for i, r in enumerate(data, 1):
        if not isinstance(r, dict) or not {"docid", "score", "file", "title"} <= r.keys() or not _finite(r["score"]) or not isinstance(r["title"], str):
            raise EvaluationError("QMD actual formatter fields differ")
        doc = by_uri.get(r["file"])
        if doc is None or r["docid"] != "#" + doc["hash"][:6]:
            raise EvaluationError("QMD native URI/hash does not match actual prepared content")
        if "line" in r and (type(r["line"]) is not int or r["line"] < 1):
            raise EvaluationError("QMD snippet line is malformed")
        result.append({"source_path": doc["source_path"], "engine_rank": i, "engine_id": r["docid"], "score": r["score"], "raw": r,
                       "position_status": "NOT_COMPARABLE_UNVERIFIED_ORIGINAL_COORDINATES"})
    return result, {"treatment": {"status": "ENGINE_NATIVE_PIPELINE"}}


def _recoll(text, mapping):
    lines = text.splitlines()
    if len(lines) < 2 or not lines[0].startswith("Recoll query: "):
        raise EvaluationError("Recoll query/count framing differs")
    match = re.fullmatch(r"([0-9]+) results(?: \(printing  100 max\):)?", lines[1])
    if match is None:
        raise EvaluationError("Recoll result-count framing differs")
    total, entries = int(match[1]), [v.split() for v in lines[2:] if v.strip()]
    if len(entries) != min(total, 100) or any(len(v) != 1 for v in entries):
        raise EvaluationError("Recoll raw rows were omitted/added/truncated")
    if (total > 100) != ("printing" in lines[1]):
        raise EvaluationError("Recoll count saturation framing is contradictory")
    rows = []
    for i, (encoded,) in enumerate(entries, 1):
        try:
            url = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeError) as exc:
            raise EvaluationError("Recoll file URI is not base64 UTF8") from exc
        rows.append({"source_path": mapping.recoll_url(url), "engine_rank": i, "engine_id": url, "score": None,
                     "raw": {"url": url}, "position_status": "NOT_COMPARABLE_UNVERIFIED_ORIGINAL_COORDINATES"})
    return rows, {"processed_query": lines[0], "reported_total": total, "treatment": {"status": "ENGINE_NATIVE_PIPELINE"}}


def parse_result(text, arm, mapping, index):
    if arm.startswith("goldmine."):
        rows, meta = _goldmine(parse_json(text, "Goldmine stdout"), arm, mapping)
    elif arm.startswith("qmd."):
        rows, meta = _qmd(parse_json(text, "QMD stdout"), index)
    else:
        rows, meta = _recoll(text, mapping)
    window = file_window([r["source_path"] for r in rows])
    first = {r["source_path"]: r for r in reversed(rows)}
    cut = [{**first[p], "file_rank": i} for i, p in enumerate(window["files"], 1)]
    return {"raw_rows": rows, "file_cut": cut, "window": window, "native10_raw_rows": rows[:10],
            "position_status": "NOT_COMPARABLE_UNVERIFIED_ORIGINAL_COORDINATES", **meta}
