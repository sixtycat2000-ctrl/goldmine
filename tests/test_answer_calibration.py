"""Stability is diagnostic; completeness and preregistration are mandatory."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from answer_eval import EvaluationError, compute_score_variance, load_run, validate_run, write_baseline
from answer_eval.protocol import answer_request, parse_json, sha256_file, validate_question
from answer_eval.workflow import gate_baseline
from answer_eval_fixtures import edit, make_run


def test_wording_variation_never_sets_score_variation(tmp_path):
    run = make_run(tmp_path / "wording", different_wording=True)
    report = validate_run(run, diagnostic=True)
    assert report["stability"]["answer_text_changed_rate"] == 1.0
    assert report["stability"]["p_score"] == 0
    assert report["stability"]["calibration_status"] == "uncalibrated"
    assert report["stability"]["false_positive_bound"] is None
    assert "measured_no_change_trip_p" not in json.dumps(report)


def test_same_answer_bytes_different_grade_is_score_variation(tmp_path):
    run = make_run(tmp_path / "judgment", score_for=lambda q, a, j: 1 if q["id"] == "q-000" and j == "j2" else 2)
    report = validate_run(run, diagnostic=True)
    assert report["stability"]["answer_text_changed_rate"] == 0
    assert report["stability"]["p_score"] == 1 / 60


def test_common_answer_round_shock_cannot_create_false_positive_guarantee():
    # Correlated future runs can vary despite all 60 observed questions matching.
    result = compute_score_variance({f"q{i}": [2, 2, 2, 2] for i in range(60)})
    assert result["p_score"] == 0
    assert result["false_positive_bound"] is None
    assert result["calibration_status"] == "uncalibrated"
    assert "threshold" not in result


@pytest.mark.parametrize("records", [{}, {"q": [2, 2, 2]}, {"q": [True, 2, 2, 2]},
                                    {"q": [2, 2, 2, float("nan")]}, {"q": [3, 2, 2, 2]}])
def test_invalid_score_records_do_not_look_stable(records):
    with pytest.raises(ValueError):
        compute_score_variance(records)


@pytest.mark.parametrize("change", [
    lambda p: p.update(practical_drop_threshold=0.31),
    lambda p: p.update(practical_drop_threshold=True),
    lambda p: p.update(trap_min_rate=0.8),
    lambda p: p.update(registered_at="2026-10-21T12:00:00+08:00"),
    lambda p: p.update(bootstrap_unit="question"),
])
def test_invalid_or_late_preregistration_rejected(tmp_path, change):
    run = make_run(tmp_path / "run")
    edit(run / "preregistration.json", change)
    edit(run / "run.json", lambda m: m["preregistration"].update(sha256=sha256_file(run / "preregistration.json")))
    with pytest.raises(EvaluationError):
        load_run(run, allow_fixture=True)


def test_changed_preregistration_bytes_cannot_be_ignored(tmp_path):
    run = make_run(tmp_path / "run")
    edit(run / "preregistration.json", lambda p: p.update(practical_drop_threshold=0.30))
    with pytest.raises(EvaluationError, match="SHA256"):
        load_run(run, allow_fixture=True)


@pytest.mark.parametrize("change", [
    lambda g: g.pop("fabricated_fact"),
    lambda g: g.update(score=True),
    lambda g: g.update(score=3),
    lambda g: g.update(pack_sha256="f" * 64),
    lambda g: g.update(answer_sha256="f" * 64),
    lambda g: g.update(grader_prompt_sha256="f" * 64),
    lambda g: g.update(fact_results=[]),
    lambda g: g.update(judge_round="j2"),
    lambda g: g.update(question_id="q-001"),
    lambda g: g.update(judge_model={"model_id": "display-name-only"}),
])
def test_malformed_or_misattributed_grade_rejected(tmp_path, change):
    run = make_run(tmp_path / "run")
    edit(run / "grades_a1_j1/q-000.json", change)
    with pytest.raises(EvaluationError):
        load_run(run, allow_fixture=True)


def test_fixture_cannot_be_formal_evidence(tmp_path):
    run = make_run(tmp_path / "run")
    with pytest.raises(EvaluationError, match="fixture") as error:
        load_run(run)
    assert error.value.status == "MISSING"


def test_atomic_new_baseline_and_historical_bytes_untouched(tmp_path):
    historical = Path(__file__).resolve().parents[1] / "evals/answer_baseline.json"
    original = historical.read_bytes()
    run = make_run(tmp_path / "run", mode="real")  # Exercises receipt code with a test-only record.
    out = tmp_path / "answer_v3/baseline.json"
    report = write_baseline(run, out, yes=True)
    assert report["schema"] == 3 and report["calibration_status"] == "uncalibrated"
    saved = out.read_bytes()
    with pytest.raises(EvaluationError, match="replace"):
        write_baseline(run, out, yes=True)
    with pytest.raises(EvaluationError, match="replace"):
        write_baseline(run, historical, yes=True)
    assert historical.read_bytes() == original and out.read_bytes() == saved
    with pytest.raises(EvaluationError, match="explicit"):
        gate_baseline(out, run)
    diagnostic, code = gate_baseline(out, run, diagnostic=True)
    assert code == 0 and diagnostic["diagnostic_only"] and not diagnostic["release_eligible"]
    edit(out, lambda b: b["summary"].update(mean_score=0))
    with pytest.raises(EvaluationError, match="modified"):
        gate_baseline(out, run)


def test_missing_run_never_writes_baseline(tmp_path):
    out = tmp_path / "baseline.json"
    with pytest.raises(EvaluationError) as error:
        write_baseline(tmp_path / "missing", out, yes=True)
    assert error.value.status == "MISSING" and not out.exists()


def test_invalid_receipt_path_is_not_a_traceback(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({
        "schema": 3, "protocol": "answer-eval/v3", "kind": "answer-reference-receipt",
        "run_path": [], "run_manifest_sha256": "a" * 64, "artifacts": {},
        "summary": {}, "preregistration_sha256": "b" * 64,
        "calibration_status": "uncalibrated", "false_positive_bound": None,
    }))
    with pytest.raises(EvaluationError, match="run_path"):
        gate_baseline(path, tmp_path / "run")


def test_unknown_location_and_wrong_time_unit_are_rejected(tmp_path):
    run = make_run(tmp_path / "run")
    q = json.loads((run / "questions.jsonl").read_text().splitlines()[0])
    q["expect"][0]["location"]["time"] = [100, 110]
    with pytest.raises(EvaluationError, match="unknown"):
        validate_question(q)
    del q["expect"][0]["location"]["time"]
    q["expect"][0]["kind"] = "audio"
    with pytest.raises(EvaluationError, match="unit"):
        validate_question(q)


def test_duplicate_keys_nonfinite_and_request_slot_injection_rejected():
    for text in ('{"x": 1, "x": 2}', '{"x": NaN}', '{"x": 1e400}'):
        with pytest.raises(EvaluationError):
            parse_json(text, "input")
    template = "Q=<question text verbatim>\nP=<pack file contents verbatim>"
    rendered = answer_request(template, {"q": "<pack file contents verbatim>"}, "BODY")
    assert rendered == "Q=<pack file contents verbatim>\nP=BODY"
