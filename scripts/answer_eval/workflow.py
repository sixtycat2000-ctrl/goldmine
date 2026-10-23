"""Validated comparisons and immutable v3 baseline receipts."""

from __future__ import annotations

import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path

from .protocol import (PROTOCOL, AnswerRun, EvaluationError, canonical_sha256,
                       fields, load_run, nonempty, read_json, sha, sha256_file)
from .statistics import compute_score_variance, paired_group_bootstrap

HISTORICAL_BASELINE = Path(__file__).resolve().parents[2] / "evals/answer_baseline.json"


def _summary(run: AnswerRun) -> dict:
    answerable = {qid: s for qid, s in run.scores.items() if run.questions[qid]["class"] != "trap"}
    traps = [qid for qid in run.questions if run.questions[qid]["class"] == "trap"]
    per_question = {}
    for qid, raw in run.scores.items():
        item = {"raw_scores": raw, "mean_score": sum(raw) / 4,
                "class": run.questions[qid]["class"],
                "source_group": run.questions[qid]["source_group"],
                "pack_sha256": run.packs[qid],
                "answer_sha256": [a["answer_sha256"] for a in run.answers[qid]]}
        if qid in traps:
            item["trap_success"] = (
                sum(raw[:2]) >= 3 and sum(raw[2:]) >= 3
                and all(g["refused"] and not g["fabricated_fact"] for g in run.grades[qid])
            )
        per_question[qid] = item
    passed_traps = sum(per_question[q]["trap_success"] for q in traps)
    variation = compute_score_variance(answerable)
    variation["answer_text_changed_count"] = sum(
        a[0]["answer_sha256"] != a[1]["answer_sha256"]
        for qid, a in run.answers.items() if qid in answerable
    )
    variation["answer_text_changed_rate"] = variation["answer_text_changed_count"] / len(answerable)
    return {
        "run_id": run.manifest["run_id"],
        "run_manifest_sha256": run.manifest_sha256,
        "evidence_mode": run.manifest["evidence_mode"],
        "n_answerable": len(answerable),
        "n_source_groups": len({run.questions[q]["source_group"] for q in answerable}),
        "mean_score": sum(sum(s) for s in answerable.values()) / (4 * len(answerable)),
        "score_scale": [0, 2],
        "trap": {"n": len(traps), "successes": passed_traps,
                 "rate": passed_traps / len(traps),
                 "required_rate": run.preregistration["trap_min_rate"],
                 "passed": Decimal(passed_traps) / len(traps) >= Decimal("0.90")},
        "stability": variation,
        "per_question": per_question,
    }


def validate_run(path: str | Path, *, diagnostic: bool = False) -> dict:
    """Completeness/provenance validation, not a release quality claim."""
    run = load_run(path, allow_fixture=diagnostic)
    result = _summary(run)
    result.update({"schema": 3, "protocol": PROTOCOL, "status": "VALID",
                   "diagnostic_only": diagnostic, "release_eligible": False,
                   "preregistration_sha256": run.manifest["preregistration"]["sha256"],
                   "validation_scope": "answer-job provenance; upstream corpus/label audit required"})
    return result


def compare_runs(base_path: str | Path, candidate_path: str | Path, *,
                 diagnostic: bool = False) -> tuple[dict, int]:
    base = load_run(base_path, allow_fixture=diagnostic)
    candidate = load_run(candidate_path, allow_fixture=diagnostic)
    bm, cm = base.manifest, candidate.manifest
    same_run = base.root == candidate.root or bm["run_id"] == cm["run_id"]
    if not diagnostic and (same_run or base.manifest_sha256 == candidate.manifest_sha256
                           or base.execution_ids & candidate.execution_ids):
        raise EvaluationError("independent runs and answer/judge execution IDs are required; self-comparison is diagnostic only")
    for name in ("models", "settings", "tokenizer", "source_groups_sha256"):
        if bm[name] != cm[name]:
            raise EvaluationError(f"invariant {name} differs")
    for name in ("questions", "corpus", "preregistration", "answerer_prompt", "grader_prompt"):
        if bm[name]["sha256"] != cm[name]["sha256"]:
            raise EvaluationError(f"invariant {name}.sha256 differs")
    if set(base.questions) != set(candidate.questions):
        raise EvaluationError("question sets differ; an intersection is never a valid denominator")
    a, b = _summary(base), _summary(candidate)
    answerable = [q for q in base.questions if base.questions[q]["class"] != "trap"]
    deltas = {q: b["per_question"][q]["mean_score"] - a["per_question"][q]["mean_score"] for q in answerable}
    p = base.preregistration
    ci = paired_group_bootstrap(deltas, {q: base.questions[q]["source_group"] for q in answerable},
                                seed=p["bootstrap_seed"], reps=p["bootstrap_reps"])
    # Integer raw scores and Decimal comparison preserve an exactly-equal wire.
    drop = Decimal(sum(sum(base.scores[q]) - sum(candidate.scores[q]) for q in answerable)) / (4 * len(answerable))
    threshold = Decimal(str(p["practical_drop_threshold"]))
    failures = []
    if drop > threshold:
        failures.append("answerable_mean_drop")
    if not b["trap"]["passed"]:
        failures.append("trap_refusal")
    changes = [{"variable": name, "reference": bm["variables"][name],
                "candidate": cm["variables"][name]}
               for name in sorted(bm["variables"]) if bm["variables"][name] != cm["variables"][name]]
    changed_packs = sorted(q for q in base.packs if base.packs[q] != candidate.packs[q])
    if changed_packs:
        changes.append({"variable": "pack_sha256", "question_ids": changed_packs})
    result = {
        "schema": 3,
        "protocol": PROTOCOL,
        "status": "DIAGNOSTIC" if diagnostic else ("FAIL" if failures else "PASS"),
        "diagnostic_only": diagnostic,
        "independent_runs": not same_run and not bool(base.execution_ids & candidate.execution_ids),
        "release_eligible": not diagnostic and not failures,
        "engineering_pass": not failures,
        "failed": failures,
        "reference": a,
        "candidate": b,
        "mean_drop": float(drop),
        "practical_drop_threshold": float(threshold),
        "paired": ci,
        "regressed_questions": sorted((q for q, d in deltas.items() if d < 0), key=lambda q: (deltas[q], q)),
        "per_question_deltas": deltas,
        "changes": changes,
        "preregistration_sha256": bm["preregistration"]["sha256"],
        "calibration_status": "uncalibrated",
        "false_positive_bound": None,
        "note": "Engineering drop/refusal gate; CI and stability do not guarantee a false positive rate.",
    }
    return result, 1 if failures else 0


def _artifact_receipt(root: Path) -> dict[str, str]:
    receipt = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            if not path.resolve().is_relative_to(root):
                raise EvaluationError(f"run artifact escapes root: {path}")
            receipt[path.relative_to(root).as_posix()] = sha256_file(path)
    return receipt


def write_baseline(run_path: str | Path, output: str | Path, *, yes: bool = False) -> dict:
    """Create a new reference receipt after validating every answer and grade.

    Existing baselines, including a previous v3 reference, are never replaced.
    The receipt binds all job evidence; moving/deleting it makes later gates
    MISSING rather than silently reverting to historical scores.
    """
    target = Path(output).resolve()
    if not yes:
        raise EvaluationError("--update-baseline requires --yes")
    if target == HISTORICAL_BASELINE.resolve() or target.exists():
        raise EvaluationError("refuse to replace a historical or existing baseline; choose a new v3 path")
    run = load_run(run_path)
    if target.is_relative_to(run.root):
        raise EvaluationError("baseline receipt must be outside its immutable source run")
    result = {"schema": 3, "protocol": PROTOCOL, "kind": "answer-reference-receipt",
              "run_path": str(run.root), "run_manifest_sha256": run.manifest_sha256,
              "artifacts": _artifact_receipt(run.root),
              "summary": _summary(run),
              "preregistration_sha256": run.manifest["preregistration"]["sha256"],
              "calibration_status": "uncalibrated", "false_positive_bound": None}
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=target.parent, encoding="utf-8", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)  # Atomic create-if-absent; never clobber another writer.
    except OSError as exc:
        raise EvaluationError(f"cannot create baseline receipt: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return result


def gate_baseline(baseline: str | Path, candidate_path: str | Path, *,
                  diagnostic: bool = False) -> tuple[dict, int]:
    receipt = read_json(Path(baseline))
    fields(receipt, {"schema", "protocol", "kind", "run_path", "run_manifest_sha256", "artifacts",
                     "summary", "preregistration_sha256", "calibration_status", "false_positive_bound"}, "baseline")
    if receipt["schema"] != 3 or receipt["protocol"] != PROTOCOL or receipt["kind"] != "answer-reference-receipt":
        raise EvaluationError("historical baseline is not a valid v3 reference receipt")
    nonempty(receipt["run_path"], "baseline.run_path")
    sha(receipt["run_manifest_sha256"], "baseline.run_manifest_sha256")
    sha(receipt["preregistration_sha256"], "baseline.preregistration_sha256")
    run = load_run(receipt["run_path"])
    if (run.manifest_sha256 != receipt["run_manifest_sha256"]
            or _artifact_receipt(run.root) != receipt["artifacts"]
            or canonical_sha256(_summary(run)) != canonical_sha256(receipt["summary"])
            or run.manifest["preregistration"]["sha256"] != receipt["preregistration_sha256"]
            or receipt["calibration_status"] != "uncalibrated" or receipt["false_positive_bound"] is not None):
        raise EvaluationError("baseline receipt or its recorded source run was modified")
    is_original = Path(candidate_path).resolve() == run.root
    if is_original and not diagnostic:
        raise EvaluationError("same-run receipt verification requires explicit --diagnostic")
    return compare_runs(run.root, candidate_path, diagnostic=diagnostic)
