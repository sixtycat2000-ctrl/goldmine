"""Synthetic ledger/registry mechanics only; NO real holdout/model/quality run."""
from datetime import datetime, timezone
from pathlib import Path
import copy
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from acceptance_lib.checks import Inputs
from acceptance_lib.evidence import read_json, write_json
from acceptance_lib.holdout import check
from answer_eval.protocol import EvaluationError, canonical_json, canonical_sha256
from holdout_lib.ledger import JointAuthorization, verify_chain
from holdout_lib.protocol import ACCEPTANCE, fixed_arms
from holdout_lib.runtime import record_candidate_calls
from holdout_lib.validation import controls, validate_usage
from product_eval.ledger import _read, initialize_ledger


def registry(*, rerank=False, multi=False):
    fast = {"scorer": "hybrid", "z_gate": 3., "limit": 10, "context": 1, "budget": 4000}
    parameters = {"fast": fast}
    if rerank or multi:
        parameters["quality"] = {**fast, "mode": "quality", "rerank": "local" if rerank else "off", "multi_query": False}
    if multi:
        parameters["multi"] = {**parameters["quality"], "multi_query": True}
    arms = {name: {"parameters": p, "parameters_sha256": canonical_sha256(p)} for name, p in parameters.items()}
    experiments = {"rerank": "enabled" if rerank else "disabled", "multi_query": "enabled" if multi else "disabled"}
    return arms, experiments


@pytest.mark.parametrize("rerank,multi,n", [(False, False, 1), (True, False, 2), (False, True, 3), (True, True, 3)])
def test_fixed_registry_declares_every_same_candidate_control_before_exposure(rerank, multi, n):
    arms, experiments = registry(rerank=rerank, multi=multi)
    assert len(fixed_arms(arms, experiments)) == n


@pytest.mark.parametrize("mutation", ["extra", "missing_control", "different_budget", "different_admission", "wrong_mode", "wrong_multi", "wrong_sha", "bad_budget", "unknown_rule"])
def test_registry_rejects_posthoc_or_nonidentical_controls(mutation):
    arms, experiments = registry(rerank=True, multi=True)
    if mutation == "extra": arms["winning_holdout_variant"] = copy.deepcopy(arms["fast"])
    elif mutation == "missing_control": del arms["quality"]
    elif mutation == "unknown_rule": experiments["rerank"] = "pick_best_after_scores"
    else:
        name = "multi" if mutation in ("different_budget", "different_admission", "wrong_multi") else "fast"
        params = arms[name]["parameters"]
        if mutation == "different_budget": params["budget"] = 8000
        elif mutation == "different_admission": params["admission"] = {"coverage_quorum": .1}
        elif mutation == "wrong_mode": params["mode"] = "quality"
        elif mutation == "wrong_multi": params["multi_query"] = False
        elif mutation == "bad_budget": params["budget"] = 0
        arms[name]["parameters_sha256"] = "0" * 64 if mutation == "wrong_sha" else canonical_sha256(params)
    with pytest.raises(EvaluationError): fixed_arms(arms, experiments)


def frozen_fixture(tmp_path, *, multi=True):
    ledger = tmp_path / "ledger"
    anchor = initialize_ledger(ledger)["anchor_sha256"]
    arms, experiments = registry(multi=multi)
    frozen_at = datetime.now(timezone.utc).isoformat()
    authorization = {"schema": 1, "purpose": "final_joint_release_audit", "frozen_at": frozen_at,
        "bundle_identity": {"mechanical_fixture_only": True}, "candidate_code_sha256": "a" * 64,
        "parameters_sha256": canonical_sha256(arms), "dependency_lock_sha256": "b" * 64, "ledger_anchor_sha256": anchor}
    return {"authorization": authorization, "ledger": {"path": str(ledger), "anchor_sha256": anchor},
            "bundle_identity": authorization["bundle_identity"], "frozen_at": frozen_at, "arms": arms,
            "experiments": experiments, "acceptance": ACCEPTANCE}


def start(auth, frozen, arm):
    auth.select(arm)
    auth(frozen["ledger"]["path"], frozen["authorization"], identity=frozen["bundle_identity"],
         candidate_code_sha256="a" * 64, parameters_sha256=frozen["arms"][arm]["parameters_sha256"], dependency_lock_sha256="b" * 64)


def completed_fixture(tmp_path):
    frozen = frozen_fixture(tmp_path)
    order = fixed_arms(frozen["arms"], frozen["experiments"])
    auth = JointAuthorization(frozen, order)
    audit = {"exposure": auth.exposure, "arms": {}}
    for index, arm in enumerate(order):
        start(auth, frozen, arm)
        refs = {"report": {"sha256": str(index + 1) * 64}, "usage": {"sha256": str(index + 4) * 64}}
        auth.complete(refs["report"]["sha256"], refs["usage"]["sha256"])
        audit["arms"][arm] = refs
    auth.finish()
    return frozen, order, audit


def test_joint_arms_share_one_prior0_exposure_consumed_before_any_mock_query(tmp_path):
    frozen = frozen_fixture(tmp_path)
    order = fixed_arms(frozen["arms"], frozen["experiments"])
    auth = JointAuthorization(frozen, order)
    state, records = _read(Path(frozen["ledger"]["path"]))
    assert state["count"] == 2 and records[1]["payload"]["prior_exposures"] == 0
    start(auth, frozen, "fast")
    _, records = _read(Path(frozen["ledger"]["path"]))
    assert records[-1]["payload"]["event"] == "joint_arm_started"
    # Simulate a process crash before it writes a report. Even zero completed
    # query rows cannot create a new unseen audit or rerun this started arm.
    with pytest.raises(EvaluationError, match="already been exposed"):
        JointAuthorization(frozen, order)
    with pytest.raises(EvaluationError, match="restart, repeat"):
        start(auth, frozen, "fast")


def test_complete_joint_chain_has_one_exposure_and_fixed_full_registry(tmp_path):
    frozen, order, audit = completed_fixture(tmp_path)
    result = verify_chain(frozen, order, audit)
    assert result["prior_exposures"] == 0 and result["arms"] == ["fast", "quality", "multi"]
    assert "release_eligible" not in result  # Protocol mechanics are not quality.


@pytest.mark.parametrize("mutation", ["reset_anchor", "prior1", "duplicate_arm", "changed_report", "missing_arm", "time_reversal", "extra_recheck", "changed_freeze"])
def test_replay_rejects_edited_reordered_reset_or_repeated_joint_exposure(tmp_path, mutation):
    frozen, order, audit = completed_fixture(tmp_path)
    root = Path(frozen["ledger"]["path"])
    state, records = _read(root)
    if mutation == "reset_anchor": frozen["ledger"]["anchor_sha256"] = "f" * 64
    elif mutation == "changed_report": audit["arms"]["fast"]["report"]["sha256"] = "f" * 64
    elif mutation == "missing_arm": records.pop(4)
    elif mutation == "changed_freeze": frozen["arms"]["fast"]["parameters"]["budget"] = 8000
    elif mutation == "prior1":
        records[1]["payload"]["prior_exposures"] = 1
        audit["exposure"] = records[1]
    elif mutation == "duplicate_arm": records[4]["payload"]["arm"] = "fast"
    elif mutation == "time_reversal": records[3]["payload"]["at"] = "2020-01-01T00:00:00+00:00"
    elif mutation == "extra_recheck": records.append(copy.deepcopy(records[-1])); records[-1]["payload"]["event"] = "deterministic_recheck"
    # Rehashing edited records does not bypass semantic/anchor/registry checks.
    previous = None
    for sequence, row in enumerate(records):
        row["sequence"], row["previous_sha256"] = sequence, previous
        row["sha256"] = canonical_sha256({key: value for key, value in row.items() if key != "sha256"})
        previous = row["sha256"]
    if mutation == "prior1": audit["exposure"] = records[1]
    (root / "holdout_exposure.jsonl").write_text("".join(canonical_json(row) + "\n" for row in records))
    write_json(root / "state.json", {"schema": 1, "anchor_sha256": records[0]["sha256"], "head_sha256": previous, "count": len(records)})
    with pytest.raises(EvaluationError): verify_chain(frozen, order, audit)


def usage_fixture(*, rerank=False):
    from goldmine.model_manifest import DEFAULT_RERANKER_KEY

    query = {"id": "mechanical-only", "q": "A synthetic observation, not a corpus quality item"}
    settings = {"quality_validation": "unvalidated_experiment"}
    if rerank: settings["reranker"] = {"executed": True, "candidates": 1, "model": DEFAULT_RERANKER_KEY}
    usage = [{"id": query["id"], "query_sha256": canonical_sha256(query["q"]), "quality_settings": settings, "fallback_reason": None}]
    report = {"rows": [{"id": query["id"], "hits": ["a mechanic-only hit"]}]}
    return usage, report, SimpleNamespace(queries=[query])


@pytest.mark.parametrize("mutation", [None, "missing", "wrong_query", "self_accepted", "silent_skip", "never_executed", "unexpected_expansion",
                                     "trailing_null", "trailing_dict", "null_row", "duplicate_row"])
def test_actual_usage_is_not_inferred_from_requested_quality_parameters(mutation):
    usage, report, bundle = usage_fixture(rerank=True)
    params = {"mode": "quality", "rerank": "local", "multi_query": False}
    if mutation == "missing": usage.clear()
    elif mutation == "wrong_query": usage[0]["query_sha256"] = "0" * 64
    elif mutation == "self_accepted": usage[0]["quality_settings"]["quality_validation"] = "PASS"
    elif mutation == "silent_skip": usage[0]["quality_settings"]["reranker"] = {"executed": False}
    elif mutation == "never_executed":
        usage[0]["quality_settings"]["reranker"] = {"executed": False}
        usage[0]["fallback_reason"] = "reranker_failed:MissingModel"
    elif mutation == "unexpected_expansion": usage[0]["quality_settings"]["query_plan"] = {"queries": 2}
    elif mutation == "trailing_null": usage.append(None)
    elif mutation == "trailing_dict": usage.append({})
    elif mutation == "null_row": usage[0] = None
    elif mutation == "duplicate_row": usage.append(copy.deepcopy(usage[0]))
    if mutation is None:
        assert validate_usage(usage, report, bundle, params)["reranker_executed"] == 1
    else:
        with pytest.raises(EvaluationError): validate_usage(usage, report, bundle, params)


@pytest.mark.parametrize("count,expanded,expected_status", [(2, True, None), (3, True, None),
                                                          (1, False, "MISSING"), (1, True, "NOT_COMPARABLE"), (2, False, "NOT_COMPARABLE")])
def test_multi_query_requires_consistent_counts_and_an_actual_expanded_treatment(count, expanded, expected_status):
    usage, report, bundle = usage_fixture()
    usage[0]["quality_settings"]["query_plan"] = {"queries": count, "expanded": expanded, "subquery_failures": []}
    parameters = {"mode": "quality", "rerank": "off", "multi_query": True}
    if expected_status is None:
        assert validate_usage(usage, report, bundle, parameters) == {
            "n": 1, "degraded": 0, "reranker_executed": 0, "queries_expanded": 1}
    else:
        with pytest.raises(EvaluationError) as error:
            validate_usage(usage, report, bundle, parameters)
        assert error.value.status == expected_status


def test_observation_preserves_actual_return_and_restores_after_failure(monkeypatch):
    from product_eval import candidate

    expected = object(), object(), {"quality_settings": {}, "fallback_reason": None}
    original = lambda *args: expected
    monkeypatch.setattr(candidate, "retrieve", original)
    bundle = SimpleNamespace(queries=[{"id": "fixture", "q": "synthetic exact query"}], splits={"holdout": ["fixture"]})
    from goldmine.search import parse_filters
    query, filters = parse_filters(bundle.queries[0]["q"])
    usage = []
    with record_candidate_calls(bundle, "holdout", usage):
        assert candidate.retrieve(None, None, None, query, filters, {}) is expected
    assert candidate.retrieve is original and usage[0]["id"] == "fixture"
    with pytest.raises(EvaluationError):
        with record_candidate_calls(bundle, "holdout", []): candidate.retrieve(None, None, None, "wrong order", filters, {})
    assert candidate.retrieve is original


def test_missing_joint_inputs_never_open_any_real_holdout_or_claim_release(tmp_path):
    status, details = check(ROOT, Inputs(), {})
    assert status == "MISSING" and details["release_eligible"] is False
    result = subprocess.run([sys.executable, str(ROOT / "scripts/eval_joint_holdout.py"), "--freeze", str(tmp_path / "missing.json"),
        "--ledger", str(tmp_path / "missing-ledger"), "--out", str(tmp_path / "never-created"), "--json"], capture_output=True, text=True, timeout=15)
    assert result.returncode == 2 and json.loads(result.stdout)["release_eligible"] is False
    assert not (tmp_path / "never-created").exists() and not (tmp_path / "missing-ledger").exists()


@pytest.mark.parametrize("drop,degraded,multi_gain,expected", [(False, 0, .1, False), (True, 0, .1, True),
                                                            (False, 1, .1, True), (False, 0, .05, True)])
def test_controls_use_fixed_simple_groups_noise_degradation_and_exact_compound_gain(drop, degraded, multi_gain, expected):
    def report(correct):
        rows = [{"id": str(i), "source_group": "independent-fixture-" + str(i), "primary_class": "semantic",
                 "metrics": {"success@1": correct, "success@5": correct, "MRR": correct, "nDCG@10": float(correct),
                             "all_required_facts@pack": None, "noise_correct_empty": None}} for i in range(12)]
        return {"rows": rows, "summary": {"classes": {"noise": {"noise_correct_empty": {"n": 30, "sum": 30, "value": 1}},
                "compound": {"all_required_facts@pack": {"n": 100, "sum": 70, "value": .70}}}}}
    reports = {"fast": report(1), "quality": report(0 if drop else 1), "multi": report(1)}
    reports["multi"]["summary"]["classes"]["compound"]["all_required_facts@pack"] = {"n": 100, "sum": 70 + 100 * multi_gain, "value": .70 + multi_gain}
    usage = {name: {"n": 30, "degraded": degraded if name == "quality" else 0, "reranker_executed": 0} for name in reports}
    frozen = {"acceptance": ACCEPTANCE, "experiments": {"rerank": "disabled"}}
    targets = {"product": {"noise_empty_rate": .90, "compound_all_facts": .75, "compound_gain": .10},
               "latency": {"quality_max_degraded_ratio": .01}}
    failed, details = controls(reports, usage, frozen, targets)
    assert bool(failed) is expected
    assert details["quality"]["simple_top1"]["n_source_groups"] == 12
    assert details["compound"]["gain"] == pytest.approx(multi_gain)


@pytest.mark.parametrize("gain,failed", [(0, True), (.019, True), (.02, False), (.021, False)])
def test_rerank_requires_predeclared_ndcg_gain_against_same_candidate_off_control(gain, failed):
    rows = [{"id": str(i), "source_group": "mechanical-group-" + str(i), "primary_class": "semantic",
             "metrics": {"success@1": 1, "success@5": 1, "MRR": 1, "nDCG@10": .5,
                         "all_required_facts@pack": None, "noise_correct_empty": None}} for i in range(25)]
    report = {"rows": rows, "summary": {"overall": {"nDCG@10": {"n": 25, "sum": 12.5}},
              "classes": {"noise": {"noise_correct_empty": {"n": 30, "sum": 30}}}}}
    reports = {"fast": copy.deepcopy(report), "quality": copy.deepcopy(report)}
    reports["quality"]["summary"]["overall"]["nDCG@10"]["sum"] = 12.5 + 25 * gain
    usage = {name: {"n": 30, "degraded": 0, "reranker_executed": 30 if name == "quality" else 0} for name in reports}
    frozen = {"acceptance": ACCEPTANCE, "experiments": {"rerank": "enabled"}}
    targets = read_json(ROOT / "evals/acceptance_targets.json")
    failures, details = controls(reports, usage, frozen, targets)
    assert bool(failures) is failed
    assert [row["check"] for row in failures] == (["quality.rerank_ndcg_gain"] if failed else [])
    assert details["rerank"]["gain"] == pytest.approx(gain)
    assert targets["product"]["rerank_ndcg_gain"] == .02
    reports["quality"]["summary"]["overall"]["nDCG@10"]["n"] -= 1
    with pytest.raises(EvaluationError, match="denominators differ"):
        controls(reports, usage, frozen, targets)


def test_wrapper_printing_pass_cannot_become_joint_holdout_evidence(tmp_path):
    from acceptance_lib.evidence import capture_command, sha256
    from holdout_lib.protocol import SUITE

    directory = tmp_path / "wrapper"
    capture_command(directory, "release.holdout", [sys.executable, "-c", 'print("PASS")'], cwd=ROOT, suite_id=SUITE)
    inputs = Inputs({"schema": 2, "path_base": str(tmp_path), "product": {"sealed_holdout_audit": {
        "manifest_path": str(directory / "manifest.json"), "sha256": sha256(directory / "manifest.json")}}})
    status, details = check(ROOT, inputs, {})
    assert status == "MISSING" and "fixed evaluator directly" in details["reason"] and details["release_eligible"] is False
