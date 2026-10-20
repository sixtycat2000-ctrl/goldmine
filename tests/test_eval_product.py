"""Evidence coverage, sealed audit discipline and an actual mock-index smoke."""

import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from answer_eval.protocol import EvaluationError
from product_eval.bundle import build_corpus, load_bundle
from product_eval.ledger import authorize_holdout, initialize_ledger
from product_eval.runner import evaluate, prepare_index, production_identity
from product_eval.scoring import score_query
from product_eval.results import validate_report

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "evals/product_v4"


def proof(expected):
    """Synthetic proof is solely a scorer unit fixture, not model evidence."""
    return {"path": expected["path"], "view_id": expected["view_id"], "evidence_type": expected["evidence_type"],
            "location": expected["location"], "start_char": expected["span"]["start_char"],
            "end_char": expected["span"]["end_char"], "text": expected["span"]["text"],
            "citation_id": "unit-fixture:1", "readback_verified": True}


def test_compound_requires_all_actual_emitted_facts_not_one_file_hit():
    bundle = load_bundle(SEED, require_complete=False)
    q = next(q for q in bundle.queries if q["primary_class"] == "compound")
    a, b = map(proof, q["expect"])
    hits = [{"path": a["path"], "spans": [a]}, {"path": b["path"], "spans": [b]}]
    partial = score_query(q, hits, [a])
    assert partial["success@5"] == 1 and partial["all_required_facts@pack"] == 0
    assert score_query(q, hits, [a, b])["all_required_facts@pack"] == 1
    b["readback_verified"] = False
    assert score_query(q, hits, [a, b])["all_required_facts@pack"] == 0


def test_and_or_evidence_and_document_ndcg_never_duplicate_credit():
    bundle = load_bundle(SEED, require_complete=False)
    q = copy.deepcopy(next(q for q in bundle.queries if q["primary_class"] == "compound"))
    # Two evidence locations in the same document cannot make ideal DCG
    # require two copies of that document.
    q["expect"][1]["path"] = q["expect"][0]["path"]
    a, b = map(proof, q["expect"])
    hits = [{"path": a["path"], "spans": [a, b]}]
    result = score_query(q, hits, [a, b])
    assert result["nDCG@10"] == 1.0
    with pytest.raises(EvaluationError, match="duplicate"):
        score_query(q, hits + hits, [a, b])
    q["required_facts"] = [{"fact_id": "and", "description": "both evidence atoms", "sufficient_evidence": [["e0", "e1"]]}]
    assert score_query(q, hits, [a])["all_required_facts@pack"] == 0
    assert score_query(q, hits, [a, b])["all_required_facts@pack"] == 1
    q["required_facts"][0]["sufficient_evidence"] = [["e0"], ["e1"]]
    assert score_query(q, hits, [a])["all_required_facts@pack"] == 1


def test_wrong_page_time_or_unemitted_header_text_is_not_evidence():
    bundle = load_bundle(SEED, require_complete=False)
    q = copy.deepcopy(bundle.queries[0])
    e = q["expect"][0]
    e["location"] = {"unit": "second", "start": 100, "end": 110}
    wrong = proof(e)
    wrong["location"] = {"unit": "second", "start": 0, "end": 2}
    result = score_query(q, [{"path": e["path"], "spans": [wrong]}], [wrong])
    assert result["success@5"] == result["all_required_facts@pack"] == 0
    correct = proof(e)
    result = score_query(q, [{"path": e["path"], "spans": [correct]}], [])
    assert result["success@5"] == 1 and result["all_required_facts@pack"] == 0


def freeze_for(anchor):
    return {"schema": 1, "purpose": "final_joint_release_audit", "frozen_at": "2026-01-01T00:00:00+00:00",
            "bundle_identity": {"suite": "unit-ledger-only"}, "candidate_code_sha256": "a" * 64,
            "parameters_sha256": "b" * 64, "dependency_lock_sha256": "c" * 64,
            "ledger_anchor_sha256": anchor}


def expose(path, freeze, **extra):
    return authorize_holdout(path, freeze, identity=freeze["bundle_identity"], candidate_code_sha256="a" * 64,
                             parameters_sha256="b" * 64, dependency_lock_sha256="c" * 64, **extra)


def test_single_final_audit_consumes_exposure_before_results(tmp_path):
    path = tmp_path / "ledger"
    anchor = initialize_ledger(path)["anchor_sha256"]
    freeze = freeze_for(anchor)
    event = expose(path, freeze)
    assert event["payload"]["prior_exposures"] == 0
    with pytest.raises(EvaluationError, match="already"):
        expose(path, freeze)
    repeat = expose(path, freeze, recheck=True)
    assert repeat["payload"]["event"] == "deterministic_recheck"
    changed = copy.deepcopy(freeze)
    changed["candidate_code_sha256"] = "d" * 64
    with pytest.raises(EvaluationError, match="candidate"):
        expose(path, changed, recheck=True)


@pytest.mark.parametrize("mutation", ["clear", "edit", "delete_state"])
def test_exposure_cannot_be_cleared_or_edited_to_get_another_attempt(tmp_path, mutation):
    path = tmp_path / "ledger"
    anchor = initialize_ledger(path)["anchor_sha256"]
    freeze = freeze_for(anchor)
    expose(path, freeze)
    if mutation == "clear":
        (path / "holdout_exposure.jsonl").write_text("")
    elif mutation == "edit":
        text = (path / "holdout_exposure.jsonl").read_text().replace("holdout_exposed", "not_exposed")
        (path / "holdout_exposure.jsonl").write_text(text)
    else:
        (path / "state.json").unlink()
    with pytest.raises(EvaluationError):
        expose(path, freeze)
    with pytest.raises(EvaluationError, match="initialized once"):
        initialize_ledger(path)


def test_absent_or_wrong_freeze_never_spends_exposure(tmp_path):
    path = tmp_path / "ledger"
    anchor = initialize_ledger(path)["anchor_sha256"]
    before = (path / "holdout_exposure.jsonl").read_bytes()
    invalid = freeze_for(anchor)
    invalid["purpose"] = "P1-02-task-check"
    with pytest.raises(EvaluationError, match="final"):
        expose(path, invalid)
    assert (path / "holdout_exposure.jsonl").read_bytes() == before


def test_mock_index_smoke_is_portable_and_never_quality_evidence(tmp_path):
    bundle = load_bundle(SEED, require_complete=False)
    build = tmp_path / "build"
    build_corpus(bundle, build, allow_draft=True)
    db = tmp_path / "mock.sqlite"
    prepare_index(bundle, build, db, backend_name="mock", production_root=ROOT, diagnostic=True)
    report = evaluate(bundle, build, db, backend_name="mock", production_root=ROOT, diagnostic=True)
    assert report["split"] == "dev" and report["diagnostic_only"] and not report["quality_evidence"]
    assert len(report["rows"]) == len(bundle.queries)
    assert any(row["metrics"]["covered_evidence_ids"] for row in report["rows"])
    assert report["fixed_suite_targets"] == {"queries": 420, "answerable": 360, "noise": 60}
    assert report["missing"]
    validate_report(bundle, report, diagnostic=True)
    edited = copy.deepcopy(report)
    edited["summary"]["overall"]["n"] += 1
    with pytest.raises(EvaluationError, match="summary"):
        validate_report(bundle, edited, diagnostic=True)
    with pytest.raises(EvaluationError, match="sealed"):
        evaluate(bundle, build, db, backend_name="mock", production_root=ROOT, split="holdout", diagnostic=True)


def test_reference_never_uses_changed_candidate_checkout():
    with pytest.raises(EvaluationError, match="original"):
        production_identity(ROOT, reference_run=True)


def test_cli_defaults_dev_and_reports_missing_before_models(capsys):
    import eval_product
    assert eval_product.main(["--validate", "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "MISSING" and not report["release_eligible"]


def test_query_header_echo_cannot_impersonate_a_material_record():
    from product_eval.results import record_body_present
    record = {"n": 1, "path": "/tmp/source.txt", "kind": "doc", "text": "needle only in the query header"}
    assert not record_body_present(record, "# query: needle only in the query header\n# sources\n")


def test_mock_cannot_pass_numeric_targets_even_if_all_scores_are_one():
    from product_eval.gates import check_targets
    with pytest.raises(EvaluationError) as error:
        check_targets({"quality_evidence": False, "diagnostic_only": True, "summary": "invented perfect scores"}, ROOT / "evals/acceptance_targets.json")
    assert error.value.status == "MISSING"


def test_actual_original_checkout_runs_readonly_with_legacy_pack_sidecars(tmp_path):
    """A fresh interpreter imports actual 769 source, never candidate modules."""
    import subprocess
    import textwrap
    from product_eval.bundle import REFERENCE_COMMIT

    checkout = tmp_path / "original"
    subprocess.run(["git", "clone", "--shared", "--no-checkout", "--quiet", str(ROOT), str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", "--quiet", REFERENCE_COMMIT], check=True)
    program = textwrap.dedent('''
        import sys, json, hashlib
        from pathlib import Path
        sys.path.insert(0, sys.argv[1])
        from product_eval.bundle import build_corpus, load_bundle
        from product_eval.runner import prepare_index, evaluate
        from product_eval.reference import open_evaluation_store
        from product_eval.results import validate_report
        root, checkout, work = map(Path, sys.argv[2:])
        bundle = load_bundle(root / "evals/product_v4", require_complete=False)
        build = work / "build"
        db = work / "original index # %.sqlite"
        build_corpus(bundle, build, allow_draft=True)
        prepare_index(bundle, build, db, backend_name="mock", production_root=checkout,
                      diagnostic=True, reference_run=True)
        original = db.read_bytes()
        report = evaluate(bundle, build, db, backend_name="mock", production_root=checkout,
                          diagnostic=True, reference_run=True)
        validate_report(bundle, report, diagnostic=True)
        assert report["reference_verified"] and report["role"] == "reference"
        assert not report["quality_evidence"]
        assert any(row["pack_records"] for row in report["rows"])
        assert any(row["emitted_spans"] for row in report["rows"])
        assert db.read_bytes() == original
        store = open_evaluation_store(db, original=True)
        try:
            assert store.db.execute("PRAGMA query_only").fetchone()[0] == 1
            try:
                store.db.execute("DELETE FROM segments")
            except Exception as error:
                assert "readonly" in str(error)
            else:
                raise AssertionError("original adapter permitted writes")
        finally:
            store.close()
        print(json.dumps({"queries": len(report["rows"]), "emitted": sum(bool(row["emitted_spans"]) for row in report["rows"])}))
    ''')
    result = subprocess.run([sys.executable, "-c", program, str(ROOT / "scripts"), str(ROOT), str(checkout), str(tmp_path)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["emitted"] > 0
