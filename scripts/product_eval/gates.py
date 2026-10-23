"""Read the plan's sole numeric targets; dev gates are not sealed release proof."""

from decimal import Decimal
from pathlib import Path

from answer_eval.protocol import EvaluationError, read_json, sha256_file
from .scoring import paired_changes


def check_targets(report, targets_path):
    if not report.get("quality_evidence") or report.get("diagnostic_only") or report.get("missing"):
        raise EvaluationError("mock/draft/diagnostic results cannot satisfy product quality gates", status="MISSING")
    targets = read_json(Path(targets_path))
    contract = Path(targets_path).resolve().parents[1] / "docs/execution-plan/CONTRACTS.md"
    if targets.get("schema") != 1 or targets.get("contract_sha256") != sha256_file(contract):
        raise EvaluationError("acceptance targets do not match the finalized contract")
    t, summary = targets["product"], report["summary"]
    failures = []
    def check(label, observed, minimum):
        if not observed["n"] or observed["value"] is None:
            raise EvaluationError(label + " has no denominator", status="MISSING")
        actual = Decimal(str(observed["sum"])) / observed["n"]
        if actual < Decimal(str(minimum)):
            failures.append({"check": label, "value": observed["value"], "n": observed["n"], "target": minimum})
    for name, values in summary["classes"].items():
        if values["n"] < t["min_class_n"]:
            raise EvaluationError(f"{name} fold has insufficient questions", status="MISSING")
    check("all.success@5", summary["overall"]["success@5"], t["all_success5"])
    for category, metric, target in (
        ("semantic", "success@5", "semantic_success5"), ("semantic", "success@1", "semantic_success1"),
        ("cjk", "success@5", "cjk_success5"), ("cjk", "success@1", "cjk_success1"),
        ("lookup", "success@1", "lookup_success1"), ("pdf-page", "success@5", "pdf_success5"),
        ("pdf-page", "success@1", "pdf_success1"), ("media-text", "success@5", "media_success5"),
        ("noise", "noise_correct_empty", "noise_empty_rate"),
    ):
        check(category + "." + metric, summary["classes"][category][metric], t[target])
    for name, values in summary["media"].items():
        if values["n"] < t["min_media_subtype_n"]:
            raise EvaluationError("media subtype fold has insufficient questions", status="MISSING")
        check("media." + name, values["success@5"], t["media_subtype_success5"])
    return {"status": "FAIL" if failures else "PASS", "failed": failures,
            "scope": "sealed_release_audit" if report.get("exposure") else "dev_task_check",
            "release_eligible": not failures and bool(report.get("exposure"))}


def compare_reference(reference, candidate):
    if not reference.get("reference_verified") or reference.get("role") != "reference":
        raise EvaluationError("reference report must come from the original isolated production checkout")
    for name in ("suite_id", "split", "bundle_identity"):
        if reference[name] != candidate[name]:
            raise EvaluationError("reference/candidate source/query/split identity differs")
    if reference.get("diagnostic_only") or candidate.get("diagnostic_only"):
        raise EvaluationError("mechanical fixture runs are not real reference quality evidence", status="MISSING")
    return paired_changes(reference["rows"], candidate["rows"])
