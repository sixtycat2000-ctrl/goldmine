"""Product-specific labels; shared typed evidence validation is authoritative."""

from __future__ import annotations

import re

from answer_eval.protocol import EvaluationError, fields, nonempty, validate_question

CLASSES = ("semantic", "cjk", "lookup", "pdf-page", "media-text", "compound", "noise")
MEDIA = ("ocr", "asr", "metadata")
NOISE = ("near_topic", "random_terms", "conflict_id")
TAGS = frozenset(("code", "docs", "zh_only", "mixed", "ja", "ko", "filename", "path",
                  "mixed_identifier", "punctuated_identifier", "text_pdf", "scanned_pdf", "mixed_pdf",
                  "cross_file", "canary", "real") + MEDIA + NOISE)
NEEDS = frozenset(("ocr", "asr", "exif"))


def validate_query(q):
    fields(q, {"id", "primary_class", "tags", "q", "expect", "required_facts", "needs", "source_group"}, "product question")
    if q["primary_class"] not in CLASSES:
        raise EvaluationError("unknown product primary_class")
    for key, allowed in (("tags", TAGS), ("needs", NEEDS)):
        values = q[key]
        if (not isinstance(values, list) or any(not isinstance(v, str) for v in values)
                or len(set(values)) != len(values) or not set(values) <= allowed):
            raise EvaluationError(f"{q.get('id')}: unknown/duplicate {key}")
    if not isinstance(q["expect"], list) or not isinstance(q["required_facts"], list):
        raise EvaluationError("expect/required_facts must be lists")
    converted = []
    for expected in q["expect"]:
        if not isinstance(expected, dict):
            raise EvaluationError("expect must contain objects")
        nonempty(expected.get("view_id"), "expect.view_id")
        converted.append({k: v for k, v in expected.items() if k != "view_id"})
    cls = "trap" if q["primary_class"] == "noise" else ("single" if len(q["required_facts"]) == 1 else "multi")
    validate_question({"id": q["id"], "class": cls, "q": q["q"], "source_group": q["source_group"],
                       "expect": converted, "required_facts": q["required_facts"]})
    tags = set(q["tags"])
    for expected in q["expect"]:
        need = {"ocr_text": "ocr", "asr_text": "asr"}.get(expected["evidence_type"])
        if need is not None and need not in q["needs"]:
            raise EvaluationError("content evidence requires its declared OCR/ASR capability; never shrink by omission")
    primary = q["primary_class"]
    if primary == "noise":
        if len(tags & set(NOISE)) != 1 or q["needs"]:
            raise EvaluationError("noise requires exactly one registered subtype and no capability exclusion")
    if primary == "media-text":
        if len(tags & set(MEDIA)) != 1:
            raise EvaluationError("media-text requires exactly one ocr/asr/metadata subtype")
        subtype = next(iter(tags & set(MEDIA)))
        required = {"ocr": "ocr", "asr": "asr", "metadata": None}[subtype]
        if required is not None and required not in q["needs"]:
            raise EvaluationError("media subtype must declare its real capability need")
        evidence_type = {"ocr": "ocr_text", "asr": "asr_text", "metadata": "name_metadata"}[subtype]
        if not any(e["evidence_type"] == evidence_type for e in q["expect"]):
            raise EvaluationError("media subtype cannot be satisfied by filenames instead of actual content")
    if primary == "pdf-page" and (not q["expect"] or any(e["kind"] != "pdf" for e in q["expect"])):
        raise EvaluationError("pdf-page requires explicit PDF page evidence")
    if primary == "pdf-page":
        if len(tags & {"text_pdf", "scanned_pdf", "mixed_pdf"}) != 1:
            raise EvaluationError("PDF questions require one independently audited document subtype")
        if tags & {"scanned_pdf", "mixed_pdf"} and not any(e["evidence_type"] == "ocr_text" for e in q["expect"]):
            raise EvaluationError("scanned/mixed PDF coverage must exercise actual OCR page evidence")
    if primary == "compound":
        if len(q["required_facts"]) < 2:
            raise EvaluationError("compound requires at least two frozen facts")
        if ("cross_file" in tags) != (len({e["path"] for e in q["expect"]}) >= 2):
            raise EvaluationError("cross_file tag disagrees with compound evidence")
    if primary == "cjk":
        if len(tags & {"zh_only", "mixed", "ja", "ko"}) != 1:
            raise EvaluationError("cjk requires an explicit language subtype")
        text = q["q"]
        han = bool(re.search(r"[\u3400-\u9fff]", text))
        latin = bool(re.search(r"[A-Za-z]", text))
        if "zh_only" in tags and (not han or any(c.isalpha() and not "\u3400" <= c <= "\u9fff" for c in text)):
            raise EvaluationError("zh_only query must contain Chinese and no Latin letters")
        if "mixed" in tags and (not han or not latin or re.search(r"[\u3040-\u30ff\uac00-\ud7af]", text)):
            raise EvaluationError("mixed query requires Chinese and Latin letters")
        if "ja" in tags and not re.search(r"[\u3040-\u30ff]", text):
            raise EvaluationError("Japanese query must contain kana")
        if "ko" in tags and not re.search(r"[\uac00-\ud7af]", text):
            raise EvaluationError("Korean query must contain Hangul")
