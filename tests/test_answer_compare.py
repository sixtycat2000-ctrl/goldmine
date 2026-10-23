"""Independent jobs, pack ownership, group uncertainty, and separate trap gates."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from answer_eval import EvaluationError, compare_runs, load_run, paired_group_bootstrap
from answer_eval.cli import command
from answer_eval_fixtures import edit, make_run


def test_different_packs_with_fresh_owned_jobs_are_comparable(tmp_path):
    base = make_run(tmp_path / "base", run_id="base", mode="real")
    candidate = make_run(tmp_path / "candidate", run_id="candidate", mode="real", pack_suffix=" updated")
    result, code = compare_runs(base, candidate)
    assert code == 0 and result["release_eligible"] and result["independent_runs"]
    assert result["mean_drop"] == 0 and result["paired"]["ci95"] == [0, 0]
    assert len(result["changes"][0]["question_ids"]) == 80
    assert result["reference"]["n_answerable"] == 60
    assert result["candidate"]["per_question"]["q-000"]["raw_scores"] == [2, 2, 2, 2]


def test_new_pack_cannot_use_old_answer_provenance(tmp_path):
    base = make_run(tmp_path / "base", run_id="base")
    candidate = make_run(tmp_path / "candidate", run_id="candidate", pack_suffix=" new")
    original = json.loads((base / "answers_a1/q-000.json").read_text())
    edit(candidate / "answers_a1/q-000.json", lambda a: a.update(pack_sha256=original["pack_sha256"]))
    with pytest.raises(EvaluationError, match="provenance"):
        compare_runs(base, candidate, diagnostic=True)


def test_self_comparison_requires_diagnostic_and_never_release_evidence(tmp_path):
    run = make_run(tmp_path / "run", mode="real")
    with pytest.raises(EvaluationError, match="independent"):
        compare_runs(run, run)
    result, code = compare_runs(run, run, diagnostic=True)
    assert code == 0 and result["mean_drop"] == 0
    assert result["status"] == "DIAGNOSTIC" and not result["release_eligible"]


@pytest.mark.parametrize("lost,expected", [(6, 0), (7, 1)])
def test_exact_practical_threshold_not_significance_controls_drop(tmp_path, lost, expected):
    base = make_run(tmp_path / "base", run_id="base")
    candidate = make_run(tmp_path / "candidate", run_id="candidate",
                         score_for=lambda q, a, j: 1 if int(q["id"][2:]) < lost else 2)
    result, code = compare_runs(base, candidate, diagnostic=True)
    assert code == expected
    assert result["practical_drop_threshold"] == 0.10
    if lost == 6:
        assert result["mean_drop"] == 0.10


def test_trap_failure_cannot_be_offset_by_perfect_answerable_score(tmp_path):
    base = make_run(tmp_path / "base", run_id="base")
    candidate = make_run(tmp_path / "candidate", run_id="candidate")
    for i in range(60, 63):
        edit(candidate / f"grades_a2_j2/q-{i:03d}.json", lambda g: g.update(fabricated_fact=True))
    report, code = compare_runs(base, candidate, diagnostic=True)
    assert code == 1 and report["failed"] == ["trap_refusal"]
    assert report["candidate"]["mean_score"] == 2
    assert report["candidate"]["trap"]["successes"] == 17


def test_missing_one_trap_or_duplicate_question_never_shrinks_denominator(tmp_path):
    run = make_run(tmp_path / "run")
    (run / "grades_a2_j2/q-079.json").unlink()
    with pytest.raises(EvaluationError) as error:
        load_run(run, allow_fixture=True)
    assert error.value.status == "MISSING"


def test_wrong_prompt_and_path_traversal_rejected(tmp_path):
    run = make_run(tmp_path / "run")
    (run / "requests_a1/q-000.txt").write_text("old pack or leaked facts")
    with pytest.raises(EvaluationError, match="request"):
        load_run(run, allow_fixture=True)
    edit(run / "run.json", lambda m: m["questions"].update(path="../outside.jsonl"))
    with pytest.raises(EvaluationError, match="relative"):
        load_run(run, allow_fixture=True)


def test_group_bootstrap_handles_shared_source_counterexample():
    deltas = {f"g{g}-q{q}": 0.5 if g < 7 else -1.0 for g in range(8) for q in range(8)}
    groups = {f"g{g}-q{q}": f"g{g}" for g in range(8) for q in range(8)}
    result = paired_group_bootstrap(deltas, groups, reps=20000)
    assert result["mean_delta"] == 0.3125
    assert result["ci95"][0] <= 0 <= result["ci95"][1]
    assert result["n_queries"] == 64 and result["n_source_groups"] == 8
    assert not result["significant_improvement"] and result["inference_status"] == "insufficient_groups"
    assert result == paired_group_bootstrap(dict(reversed(list(deltas.items()))), groups, reps=20000)


def test_group_bootstrap_preserves_query_weight_with_unequal_groups():
    values = {"a": 1.0, "b": 1.0, "c": -1.0}
    result = paired_group_bootstrap(values, {"a": "large", "b": "large", "c": "small"}, reps=1000)
    assert result["mean_delta"] == 1 / 3
    with pytest.raises(ValueError):
        paired_group_bootstrap(values, {"a": "large"}, reps=1000)


def test_cli_missing_records_are_machine_readable_missing(tmp_path, capsys):
    assert command("compare", base=tmp_path / "absent", candidate=tmp_path / "absent2", as_json=True) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "MISSING" and not report["release_eligible"]


def test_fresh_ids_cannot_reuse_another_runs_execution_receipt(tmp_path):
    base = make_run(tmp_path / "base", run_id="base", mode="real")
    candidate = make_run(tmp_path / "candidate", run_id="candidate", mode="real")
    edit(candidate / "answers_a1/q-000.json", lambda a: a.update(execution_id="base/q-000/a1"))
    with pytest.raises(EvaluationError, match="independent"):
        compare_runs(base, candidate)


def test_invariants_and_preregistration_cannot_be_changed_per_candidate(tmp_path):
    from answer_eval.protocol import sha256_file
    base = make_run(tmp_path / "base", run_id="base")
    candidate = make_run(tmp_path / "candidate", run_id="candidate")
    # A self-consistent different registration is still not the SAME registration.
    edit(candidate / "preregistration.json", lambda p: p.update(practical_drop_threshold=0.30))
    edit(candidate / "run.json", lambda m: m["preregistration"].update(sha256=sha256_file(candidate / "preregistration.json")))
    with pytest.raises(EvaluationError, match="preregistration"):
        compare_runs(base, candidate, diagnostic=True)


def test_known_unsupported_fact_cannot_claim_full_score(tmp_path):
    run = make_run(tmp_path / "run")
    edit(run / "grades_a1_j1/q-000.json", lambda g: g["fact_results"][0].update(supported=False))
    with pytest.raises(EvaluationError, match="score 2"):
        load_run(run, allow_fixture=True)


def test_adapter_rejects_conflicting_actions_without_models():
    import eval_answer
    with pytest.raises(SystemExit) as error:
        eval_answer.main(["--compare", "missing-base", "missing-candidate", "--update-baseline", "--yes"])
    assert error.value.code == 2
