"""Recompute fixed full-fold reports and controls from already exposed bytes."""
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import json
import sqlite3

from acceptance_lib.evidence import external_ref, sha256, verify_artifact
from acceptance_lib.ir_checks import _readback
from answer_eval.protocol import EvaluationError, canonical_sha256, fields
from product_eval.gates import check_targets, compare_reference
from product_eval.results import validate_report
from product_eval.runner import production_content
from product_eval.scoring import paired_changes
from .protocol import SIMPLE_CLASSES


def _index_identity(path, reference):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        meta = dict(connection.execute("SELECT key,value FROM meta"))
        expected = {"schema": connection.execute("PRAGMA user_version").fetchone()[0],
            "build_receipt_sha256": meta.get("product.build_receipt_sha256"), "fingerprint_id": meta.get("fingerprint_id"),
            "fingerprint_json": meta.get("fingerprint_json"), "lexical_version": meta.get("lexical_version"),
            "chunker_versions": [row[0] for row in connection.execute("SELECT DISTINCT chunker_version FROM files ORDER BY 1")]}
        if reference:
            expected["original_support"] = json.loads(meta.get("product.original_support_json", "null"))
    return expected


def report_bindings(report, frozen, bundle, *, arm, root, exposure):
    report = validate_report(bundle, report)
    original = arm == "reference"
    if (report["split"] != "holdout" or report["backend"] != "llama" or report["diagnostic_only"] is not False
            or report["quality_evidence"] is not True or report["missing"] or report["sealed_reference"] is not original
            or report["reference_verified"] is not original or report["role"] != ("reference" if original else "candidate")
            or report["fixed_suite_targets"] != {"queries": 420, "answerable": 360, "noise": 60}
            or report["environment"] != frozen["environment"] or report["exposure"] != (None if original else exposure)):
        raise EvaluationError("joint report must be a complete real heldout arm with its exact seal/exposure role")
    expected_source = frozen["reference"]["production"] if original else frozen["candidate_source"]
    if (report["production"] != expected_source if original else production_content(report["production"]) != production_content(expected_source)):
        raise EvaluationError("joint report production content differs from the final frozen source")
    parameters = frozen["reference"]["parameters"] if original else frozen["arms"][arm]["parameters"]
    if report["parameters"] != parameters:
        raise EvaluationError("joint report parameters differ from their predeclared fixed arm")
    fingerprint = report["backend_identity"]
    if (not isinstance(fingerprint, dict) or fingerprint.get("model_sha256") != frozen["model"]["sha256"] or fingerprint.get("dim") != 768):
        raise EvaluationError("joint arm lacks the exact real embedding model fingerprint")
    data = frozen["reference"] if original else frozen["candidate"]
    index = external_ref(Path(root), data["index"])
    expected_index = _index_identity(index, original)
    if report["index_identity"] != expected_index:
        raise EvaluationError("joint report index identity differs from actual pinned SQLite metadata")
    actual_fingerprint = json.loads(expected_index["fingerprint_json"])
    if actual_fingerprint.get("backend") != "llama" or any(actual_fingerprint.get(key) != value for key, value in fingerprint.items()):
        raise EvaluationError("joint actual index embedding fingerprint differs from its report/native model")
    corpus = Path(data["build"]).resolve() / "corpus"
    context = {"production_root": data["production_root"], "production": report["production"]} if original else None
    checked = _readback(report, index, corpus, bundle, original_reference=context)
    if sha256(index) != data["index"]["sha256"]:
        raise EvaluationError("joint index bytes changed during read-only evidence replay")
    return checked


def validate_usage(usage, report, bundle, parameters):
    """Observe actual returned quality metadata; parameters alone prove no use."""
    rows = report["rows"]
    if (not isinstance(usage, list) or len(usage) != len(rows) or not all(isinstance(row, dict) for row in usage)
            or [row.get("id") for row in usage] != [row["id"] for row in rows]):
        raise EvaluationError("joint usage sidecar lacks complete same-order heldout query observations")
    queries = {query["id"]: query for query in bundle.queries}
    degraded = executed = expanded = 0
    for observed, row in zip(usage, rows):
        fields(observed, {"id", "query_sha256", "quality_settings", "fallback_reason"}, "joint actual usage")
        if observed["query_sha256"] != canonical_sha256(queries[row["id"]]["q"]):
            raise EvaluationError("joint usage observation belongs to a different frozen query")
        settings, fallback = observed["quality_settings"], observed["fallback_reason"]
        if fallback is not None and (not isinstance(fallback, str) or not fallback):
            raise EvaluationError("invalid actual quality fallback observation")
        degraded += fallback is not None
        if parameters.get("mode", "fast") == "fast":
            if settings != {} or fallback is not None:
                raise EvaluationError("fast arm unexpectedly changed to quality/fallback execution")
            continue
        fields(settings, {"quality_validation"}, "quality execution metadata", {"reranker", "query_plan"})
        if settings["quality_validation"] != "unvalidated_experiment":
            raise EvaluationError("quality execution metadata cannot self-declare acceptance")
        if parameters.get("rerank", "off") == "local":
            rerank = settings.get("reranker")
            if not isinstance(rerank, dict) or type(rerank.get("executed")) is not bool:
                raise EvaluationError("joint rerank arm lacks actual executed/no-candidate/fallback metadata")
            if rerank["executed"]:
                from goldmine.model_manifest import DEFAULT_RERANKER_KEY

                if rerank.get("model") != DEFAULT_RERANKER_KEY or not 1 <= rerank.get("candidates", 0) <= 20:
                    raise EvaluationError("joint rerank usage lacks the real pinned model/candidate count")
                executed += 1
            elif fallback is None and (row["hits"] or rerank.get("reason") != "no_admitted_candidates"):
                raise EvaluationError("joint rerank silently skipped admitted candidates")
        elif "reranker" in settings:
            raise EvaluationError("disabled rerank unexpectedly executed")
        if parameters.get("multi_query", False):
            plan = settings.get("query_plan")
            if (not isinstance(plan, dict) or type(plan.get("queries")) is not int or not 1 <= plan["queries"] <= 3
                    or type(plan.get("expanded")) is not bool or plan["expanded"] != (plan["queries"] > 1)
                    or not isinstance(plan.get("subquery_failures"), list)):
                raise EvaluationError("joint multi-query arm lacks actual bounded query-plan metadata")
            expanded += plan["expanded"]
        elif "query_plan" in settings:
            raise EvaluationError("single-query control unexpectedly expanded queries")
    if parameters.get("rerank", "off") == "local" and not executed:
        raise EvaluationError("joint rerank model never actually executed", status="MISSING")
    if parameters.get("multi_query", False) and not expanded:
        raise EvaluationError("joint multi-query treatment never actually expanded a query", status="MISSING")
    return {"n": len(rows), "degraded": degraded, "reranker_executed": executed, "queries_expanded": expanded}


def controls(reports, usage, frozen, targets):
    """Fixed predeclared engineering rules; no selecting winning holdout arms."""
    failed, details = [], {}
    for arm in (name for name in reports if name != "fast"):
        noise = reports[arm]["summary"]["classes"]["noise"]["noise_correct_empty"]
        if not noise["n"] or Decimal(str(noise["sum"])) / noise["n"] < Decimal(str(targets["product"]["noise_empty_rate"])):
            failed.append({"check": arm + ".noise", "observed": noise})
        measured = usage[arm]
        if not measured["n"] or Decimal(measured["degraded"]) / measured["n"] > Decimal(str(targets["latency"]["quality_max_degraded_ratio"])):
            failed.append({"check": arm + ".degraded", **measured})
        baseline = [row for row in reports["fast"]["rows"] if row["primary_class"] in SIMPLE_CLASSES]
        candidate = [row for row in reports[arm]["rows"] if row["primary_class"] in SIMPLE_CLASSES]
        comparison = paired_changes(baseline, candidate, seed=frozen["acceptance"]["paired_seed"], reps=frozen["acceptance"]["paired_reps"])["success@1"]
        if comparison is None or comparison["inference_status"] != "eligible":
            raise EvaluationError("joint simple-query control has fewer than12 independent source groups", status="MISSING")
        details[arm] = {"simple_top1": comparison, "usage": measured, "noise": noise}
        if comparison["ci95"][1] < 0:
            failed.append({"check": arm + ".simple_top1_significant_drop", "comparison": comparison})
    if frozen["experiments"]["rerank"] == "enabled":
        control = reports["fast"]["summary"]["overall"]["nDCG@10"]
        treatment = reports["quality"]["summary"]["overall"]["nDCG@10"]
        if not control["n"] or control["n"] != treatment["n"]:
            raise EvaluationError("joint rerank nDCG control denominators differ")
        a, b = Decimal(str(control["sum"])) / control["n"], Decimal(str(treatment["sum"])) / treatment["n"]
        details["rerank"] = {"n": treatment["n"], "control": float(a), "value": float(b), "gain": float(b - a)}
        if b - a < Decimal(str(targets["product"]["rerank_ndcg_gain"])):
            failed.append({"check": "quality.rerank_ndcg_gain", **details["rerank"]})
    if "multi" in reports:
        control = reports["quality"]["summary"]["classes"]["compound"]["all_required_facts@pack"]
        treatment = reports["multi"]["summary"]["classes"]["compound"]["all_required_facts@pack"]
        if not control["n"] or control["n"] != treatment["n"]:
            raise EvaluationError("joint multi-query complete-fact control denominators differ")
        a, b = Decimal(str(control["sum"])) / control["n"], Decimal(str(treatment["sum"])) / treatment["n"]
        details["compound"] = {"n": treatment["n"], "value": float(b), "gain": float(b - a)}
        if b < Decimal(str(targets["product"]["compound_all_facts"])) or b - a < Decimal(str(targets["product"]["compound_gain"])):
            failed.append({"check": "multi.experimental_acceptance", **details["compound"]})
    return failed, details
