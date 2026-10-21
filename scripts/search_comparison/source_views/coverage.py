"""Coverage is complete source traversal, not a claim that OCR/ASR is accurate."""
from __future__ import annotations

import hashlib
import json
import math

from answer_eval.protocol import EvaluationError, fields
from product_eval.bundle import normalized


def unit(axis, start, end, text, method, *, error=None, details=None):
    text = normalized(text)
    return {"axis": axis, "start": start, "end": end, "text": text,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "method": method,
            "status": "MISSING" if error else "OK" if text.strip() else "EMPTY",
            "error": error, "details": details or {}}


def validate_coverage(kind, expected, units):
    if not isinstance(units, list) or not units:
        raise EvaluationError("source has no independently established full-coverage units", status="MISSING")
    for value in units:
        fields(value, {"axis", "start", "end", "text", "text_sha256", "method", "status", "error", "details"}, "converted unit")
        if not isinstance(value["text"], str) or value["text_sha256"] != hashlib.sha256(value["text"].encode()).hexdigest():
            raise EvaluationError("converted unit text identity changed")
        if value["status"] not in ("OK", "EMPTY", "MISSING"):
            raise EvaluationError("converted unit has an unknown status")
        if not isinstance(value["details"], dict) or not isinstance(value["method"], str) or not value["method"]:
            raise EvaluationError("converted unit method/details are malformed")
        if value["error"] is not None and (not isinstance(value["error"], str) or not value["error"]):
            raise EvaluationError("converted unit failure must have an explicit error")
        if (value["status"] == "OK") != bool(value["text"].strip()) or (value["status"] == "MISSING") != bool(value["error"]):
            raise EvaluationError("converted unit status/text/error contradict each other")
        if value["status"] == "MISSING" and value["text"]:
            raise EvaluationError("failed converter cannot supply fallback body text")
    if kind == "pdf":
        if type(expected) is not int or expected < 1 or [u["start"] for u in units] != list(range(1, expected + 1)):
            raise EvaluationError("PDF pages are omitted, duplicated or reordered", status="MISSING")
        if any(u["axis"] != "page" or u["end"] != u["start"] or type(u["start"]) is not int for u in units):
            raise EvaluationError("PDF page coverage is not exact")
    elif kind == "audio":
        if type(expected) is not int or expected <= 0 or len(units) != 1:
            raise EvaluationError("audio duration or full-input unit is absent", status="MISSING")
        if units[0]["axis"] != "millisecond" or units[0]["start"] != 0 or units[0]["end"] != expected:
            raise EvaluationError("audio attempted interval does not cover the entire clip", status="MISSING")
        details = units[0]["details"]
        if units[0]["status"] != "MISSING" and (details.get("full_input") is not True or type(details.get("decoded_duration_ms")) not in (int, float)
                or not math.isfinite(details["decoded_duration_ms"]) or abs(details["decoded_duration_ms"] - expected) > 2):
            raise EvaluationError("full audio decode/transcription coverage lacks independent duration proof", status="MISSING")
    else:
        axis = "byte" if kind in ("doc", "code") else "image" if kind == "screenshot" else "metadata"
        if len(units) != 1 or units[0]["axis"] != axis or units[0]["start"] != 0 or units[0]["end"] != expected:
            raise EvaluationError("full source body/image/metadata coverage changed", status="MISSING")
    return not any(u["status"] == "MISSING" for u in units)


def render(source, units):
    sections, mapping, cursor = [], [], 0
    for number, value in enumerate(units, 1):
        header = "\n=== source unit " + json.dumps({"source": source["path"], "kind": source["kind"],
            "unit": number, "axis": value["axis"], "start": value["start"], "end": value["end"],
            "method": value["method"], "status": value["status"],
            "publisher_page": value["details"].get("publisher_page")}, sort_keys=True, ensure_ascii=False) + " ===\n"
        start = cursor + len(header)
        section = header + value["text"] + "\n=== end source unit ===\n"
        mapping.append({"unit": number, "source_sha256": source["asset"]["sha256"],
                        "axis": value["axis"], "start": value["start"], "end": value["end"],
                        "text_sha256": value["text_sha256"], "output_start_char": start,
                        "output_end_char": start + len(value["text"]), "status": value["status"]})
        sections.append(section)
        cursor += len(section)
    return "".join(sections), mapping


def file_window(paths, *, raw_limit=100, file_limit=10):
    if type(raw_limit) is not int or type(file_limit) is not int or raw_limit < 1 or file_limit < 1 or len(paths) > raw_limit or any(not isinstance(p, str) or not p for p in paths):
        raise EvaluationError("invalid bounded mapped file results")
    unique = list(dict.fromkeys(paths))
    return {"files": unique[:file_limit], "raw_rows": len(paths), "unique_files": len(unique),
            "status": "TRUNCATED" if len(paths) == raw_limit and len(unique) < file_limit else "COMPLETE_RETURNED_WINDOW"}
