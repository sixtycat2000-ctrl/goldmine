"""Recompute saved reports from frozen source spans and actual pack bodies."""

import hashlib
from pathlib import Path
import re

from answer_eval.protocol import EvaluationError, fields, nonempty, validate_location
from .alignment import validate_word_alignment
from .bundle import normalized
from .scoring import score_query, summarize


def record_body_present(record, context):
    if (not isinstance(record, dict) or type(record.get("n")) is not int or not isinstance(record.get("path"), str)
            or not isinstance(record.get("kind"), str) or not isinstance(record.get("text"), str)):
        raise EvaluationError("malformed pack record sidecar")
    header = re.search(r"(?m)^\[" + str(record["n"]) + r"\] " + re.escape(record["path"])
                       + r" \(" + re.escape(record["kind"]) + r"\) [^\n]*\n", context)
    start = header.end() if header else -1
    if start >= 0 and context.startswith("# why: ", start):
        start = context.find("\n", start) + 1
    return start >= 0 and context.startswith(normalized(record["text"]), start)


def _proof(bundle, span):
    fields(span, {"path", "view_id", "evidence_type", "location", "start_char", "end_char", "text", "citation_id", "readback_verified"}, "returned span", {"word_alignment"})
    nonempty(span["path"], "span.path")
    nonempty(span["view_id"], "span.view_id")
    key = (span["path"], span["view_id"])
    if key not in bundle.views:
        raise EvaluationError("returned span source/view is unregistered")
    view, source = bundle.views[key], bundle.sources[span["path"]]
    if (span["readback_verified"] is not True or not isinstance(span["citation_id"], str)
            or re.fullmatch(r"eval-segment:[1-9][0-9]*", span["citation_id"]) is None
            or span["evidence_type"] != view["evidence_type"]):
        raise EvaluationError("invalid citation/read-back evidence type")
    if (type(span["start_char"]) is not int or type(span["end_char"]) is not int or span["start_char"] < 0
            or not isinstance(span["text"], str) or not span["text"]
            or span["end_char"] - span["start_char"] != len(span["text"])
            or view["text"][span["start_char"]:span["end_char"]] != span["text"]):
        raise EvaluationError("returned span does not match the frozen source character range")
    validate_location(span["location"], source["kind"], span["evidence_type"])
    validate_word_alignment(span)
    location, oracle = span["location"], view["location"]
    if (location is None) != (oracle is None):
        raise EvaluationError("returned span location is missing or invented")
    if location is not None:
        if location["unit"] == "page" and not location["start"] == location["end"] == oracle["start"]:
            raise EvaluationError("returned PDF span is on a different page")
        if location["end"] < oracle["start"] or location["start"] > oracle["end"]:
            raise EvaluationError("returned span location does not overlap its oracle view")
        if location["unit"] == "line":
            a = oracle["start"] + view["text"][:span["start_char"]].count("\n")
            b = oracle["start"] + view["text"][:span["end_char"] - 1].count("\n")
            if not location["start"] <= a <= b <= location["end"]:
                raise EvaluationError("returned span offsets are outside its claimed lines")


def validate_report(bundle, report, *, diagnostic=False):
    required = {"schema", "suite_id", "split", "bundle_identity", "production", "environment", "backend",
                "backend_identity", "parameters", "summary", "rows", "diagnostic_only", "quality_evidence", "exposure",
                "fixed_suite_targets", "missing", "index_identity", "role", "reference_verified", "sealed_reference"}
    fields(report, required, "product report", {"gate", "paired_reference"})
    if report["schema"] != 1 or report["suite_id"] != "product.v4" or report["bundle_identity"] != bundle.identity:
        raise EvaluationError("saved product report suite/input identity mismatch")
    if any(type(report[k]) is not bool for k in ("diagnostic_only", "quality_evidence", "reference_verified", "sealed_reference")):
        raise EvaluationError("saved report evidence flags must be explicit booleans")
    if report["diagnostic_only"] or not report["quality_evidence"]:
        if not diagnostic:
            raise EvaluationError("saved diagnostic/mock result cannot be quality evidence", status="MISSING")
    if report["backend"] == "mock" and (not report["diagnostic_only"] or report["quality_evidence"]):
        raise EvaluationError("mock output was relabeled as real quality evidence")
    if report["split"] not in ("dev", "holdout", "all") or not isinstance(report["rows"], list):
        raise EvaluationError("invalid report split/rows")
    expected = set(bundle.splits[report["split"]]) if bundle.splits and report["split"] != "all" else {q["id"] for q in bundle.queries}
    queries = {q["id"]: q for q in bundle.queries}
    seen = set()
    for row in report["rows"]:
        fields(row, {"id", "primary_class", "tags", "source_group", "metrics", "hits", "emitted_spans", "pack_records",
                     "context_text", "pack_sha256", "used_tokens", "token_mode"}, "product result row")
        nonempty(row["id"], "result question_id")
        if not isinstance(row["context_text"], str) or any(not isinstance(row[k], list) for k in ("hits", "emitted_spans", "pack_records")):
            raise EvaluationError("saved row context/hits/emitted records have invalid types")
        if row["id"] in seen or row["id"] not in expected:
            raise EvaluationError("saved report has duplicate/unknown question IDs")
        seen.add(row["id"])
        q = queries[row["id"]]
        if any(row[k] != q[k] for k in ("primary_class", "tags", "source_group")):
            raise EvaluationError("saved row class/tag/source group differs from frozen labels")
        if hashlib.sha256(row["context_text"].encode("utf-8")).hexdigest() != row["pack_sha256"]:
            raise EvaluationError("saved actual context_text SHA mismatch")
        for hit in row["hits"]:
            fields(hit, {"path", "spans", "kind", "loc", "loc_end"}, "product hit")
            nonempty(hit["path"], "hit.path")
            if not isinstance(hit["spans"], list):
                raise EvaluationError("saved hit spans must be a list")
            if hit["path"] not in bundle.sources:
                raise EvaluationError("saved hit source is outside corpus")
            for span in hit["spans"]:
                _proof(bundle, span)
                if span["path"] != hit["path"]:
                    raise EvaluationError("saved hit span belongs to another source")
        for record in row["pack_records"]:
            if not record_body_present(record, normalized(row["context_text"])):
                raise EvaluationError("saved sidecar body was not emitted")
        for span in row["emitted_spans"]:
            _proof(bundle, span)
            observed = validate_word_alignment(span) or span["text"]
            sid = int(span["citation_id"].split(":")[-1])
            included = False
            for record in row["pack_records"]:
                ids = {p["segment_id"] for p in record["segments"]} | {record["segment_id"]}
                path = Path(record["path"]).as_posix()
                if sid in ids and path.endswith("/" + span["path"]) and observed in normalized(record["text"]):
                    included = True
            if not included:
                raise EvaluationError("claimed emitted citation is absent from actual record bodies")
        if row["metrics"] != score_query(q, row["hits"], row["emitted_spans"]):
            raise EvaluationError("saved per-query metrics were edited")
    if seen != expected or summarize(report["rows"]) != report["summary"]:
        raise EvaluationError("saved report denominator/summary differs from raw evidence")
    return report
