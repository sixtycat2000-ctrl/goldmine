"""Synthetic BEIR fixtures exercise provenance/scoring, never quality evidence."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from acceptance_lib import ir_checks as ir
from acceptance_lib.checks import Inputs
from acceptance_lib.evidence import InvalidEvidence, capture_command, read_json, write_json
from answer_eval.protocol import EvaluationError, sha256_file
import eval_public as ep
from public_eval import protocol as p, runner


def fixture_archive(path, *, changes=None):
    content = {
        "corpus.jsonl": "\n".join(json.dumps(row) for row in [
            {"_id": "q1", "title": "Self document", "text": "A query can have the same ID as a source."},
            {"_id": "d1", "title": "Blue water", "text": "Blue water is a fictional fixture fact."},
            {"_id": "d2", "title": "Red tree", "text": "Red trees are a fictional fixture fact."}]) + "\n",
        "queries.jsonl": '\n'.join(json.dumps({"_id": key, "text": text}) for key, text in (("q1", "blue water"), ("q2", "red tree"))) + "\n",
        "qrels/test.tsv": "query-id\tcorpus-id\tscore\nq1\td1\t2\nq1\tdangling\t1\nq2\td2\t2\n",
    }
    content.update(changes or {})
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for key, text in content.items():
            archive.writestr("fixture/" + key, text)
    entry = {"sha256": sha256_file(path), "bytes": path.stat().st_size, "n_docs": 3,
             "n_queries_test": 2, "grade_range": [1, 2]}
    return entry, p.load_archive(path, entry)


def fixture_arms(data):
    rows = [p.ranked_row(qid, ["q1", "d1", "d2"], data, 2) for qid in sorted(data["qrels"])]
    return {arm: {"rows": copy.deepcopy(rows), "metrics": p.summarize(rows)} for arm in p.ARMS}


def test_raw_rankings_all_arms_self_drop_and_dangling_idcg(tmp_path):
    entry, data = fixture_archive(tmp_path / "mini.zip")
    arms = fixture_arms(data)
    result = p.validate_arms(arms, data, entry)
    row = arms["hybrid"]["rows"][0]
    assert row["fetched_ranked_docids"] == ["q1", "d1", "d2"]
    assert row["post_cut_ranked_docids"] == ["d1", "d2"]
    assert 0 < row["metrics"]["ndcg"] < 1  # dangling positive remains in IDCG.
    assert row["metrics"] == ep.score_query_public(row["post_cut_ranked_docids"], data["qrels"]["q1"], 2)
    assert set(result) == set(p.ARMS) and result["hybrid"]["nDCG@10"]["n"] == 2


@pytest.mark.parametrize("mutation", ["missing_arm", "missing_query", "reordered_queries", "duplicate_query", "unknown_doc", "duplicate_doc", "self_in_cut", "query_sha", "qrels_sha", "row_metric", "summary"])
def test_reject_incomplete_or_tampered_rankings(mutation, tmp_path):
    entry, data = fixture_archive(tmp_path / "mini.zip")
    arms = fixture_arms(data)
    arm, row = arms["hybrid"], arms["hybrid"]["rows"][0]
    if mutation == "missing_arm":
        del arms["semantic"]
    elif mutation == "missing_query":
        arm["rows"].pop()
    elif mutation == "reordered_queries":
        arm["rows"].reverse()
    elif mutation == "duplicate_query":
        arm["rows"][1] = copy.deepcopy(row)
    elif mutation == "unknown_doc":
        row["fetched_ranked_docids"][1] = "absent"
    elif mutation == "duplicate_doc":
        row["fetched_ranked_docids"].append("d1")
    elif mutation == "self_in_cut":
        row["post_cut_ranked_docids"].insert(0, "q1")
    elif mutation in ("query_sha", "qrels_sha"):
        row["query_text_sha256" if mutation == "query_sha" else "qrels_sha256"] = "0" * 64
    elif mutation == "row_metric":
        row["metrics"]["ndcg"] = 1
    elif mutation == "summary":
        arm["metrics"]["nDCG@10"]["value"] = 1
    with pytest.raises(EvaluationError):
        p.validate_arms(arms, data, entry)


@pytest.mark.parametrize("changes,match", [
    ({"queries.jsonl": '{"_id":"q1","text":"a"}\n{"_id":"q1","text":"b"}\n'}, "duplicate"),
    ({"qrels/test.tsv": "q1\td1\t2\nq1\td1\t2\nq2\td2\t2\n"}, "duplicate"),
    ({"qrels/test.tsv": "q1\td1\t2\nmissing-query\td2\t2\n"}, "missing query"),
    ({"qrels/test.tsv": "q1\td1\t7\nq2\td2\t2\n"}, "invalid grade"),
    ({"qrels/test.tsv": "q1\td1\t2\n"}, "denominator"),
])
def test_archive_rejects_ambiguous_queries_qrels_or_shrunk_denominator(changes, match, tmp_path):
    with pytest.raises(EvaluationError, match=match):
        fixture_archive(tmp_path / "mini.zip", changes=changes)


def test_zip_and_qrels_member_hashes_are_rechecked(tmp_path):
    path = tmp_path / "mini.zip"
    entry, data = fixture_archive(path)
    old = fixture_arms(data)
    new_entry, changed = fixture_archive(path, changes={"qrels/test.tsv": "q1\td2\t2\nq1\tdangling\t1\nq2\td2\t2\n"})
    assert data["identity"] != changed["identity"]
    with pytest.raises(EvaluationError, match="ZIP missing or differs"):
        p.load_archive(path, entry)
    with pytest.raises(EvaluationError, match="qrels"):
        p.validate_arms(old, changed, new_entry)


def test_readonly_mock_e2e_exports_three_by_four_complete_raw_rankings(monkeypatch, tmp_path):
    manifest = {"datasets": {}}
    for name in p.DATASETS:
        manifest["datasets"][name], _ = fixture_archive(ep.zip_path(tmp_path, name))
    monkeypatch.setattr(ep, "load_manifest", lambda: manifest)
    official, source, data, _ = runner.identity(tmp_path, ROOT)
    prepared = runner.prepare(tmp_path, source, official, data, production_root=ROOT, backend_name="mock", model=None, server=None)
    assert prepared["diagnostic_only"] is True
    before = {name: sha256_file(ep.dataset_dir(tmp_path, name) / "raw-index.sqlite") for name in p.DATASETS}
    report = runner.evaluate(tmp_path, ROOT, backend_name="mock", diagnostic=True)
    assert report["backend"] == "mock" and report["diagnostic_only"] is True and report["release_eligible"] is False
    for name in p.DATASETS:
        assert p.validate_arms(report["datasets"][name]["arms"], data[name], manifest["datasets"][name])
        assert report["datasets"][name]["index"]["sha256"] == before[name]
        assert prepared["datasets"][name]["index"] == report["datasets"][name]["index"]
        assert prepared["datasets"][name]["input_identity"] == report["datasets"][name]["input_identity"]
        assert sha256_file(ep.dataset_dir(tmp_path, name) / "raw-index.sqlite") == before[name]
        # A later docs-only commit preserves exact production bytes; observed
        # HEAD remains in provenance while index compatibility is content based.
        changed_head = {**source, "git_commit": "b" * 40}
        assert p.inspect_index(ep.dataset_dir(tmp_path, name) / "raw-index.sqlite", data[name], changed_head,
                               ep.dataset_dir(tmp_path, name) / ep.CORPUS_MD_DIR, "mock")["sha256"] == before[name]
    with pytest.raises(EvaluationError, match="refuses existing"):
        runner.prepare(tmp_path, source, official, data, production_root=ROOT, backend_name="mock", model=None, server=None)


def test_public_content_domain_survives_only_commit_metadata_changes():
    from answer_eval.protocol import canonical_sha256

    files = {"goldmine/__init__.py": "1" * 64, "pyproject.toml": "2" * 64}
    original = {"git_commit": "a" * 40, "source_sha256": canonical_sha256(files), "files": files}
    changed_head = {**original, "git_commit": "b" * 40}
    assert p.preparation_identity(original) == p.preparation_identity(changed_head)
    assert p.preparation_identity(original, reference=True) != p.preparation_identity(changed_head, reference=True)
    changed_files = {**files, "goldmine/__init__.py": "3" * 64}
    changed_code = {**changed_head, "files": changed_files, "source_sha256": canonical_sha256(changed_files)}
    assert p.preparation_identity(original) != p.preparation_identity(changed_code)


def test_actual_raw_cli_missing_cache_is_nonzero_json_without_model_launch(tmp_path):
    command = [sys.executable, str(ROOT / "scripts/eval_public.py"), "--raw", "--cache-root", str(tmp_path / "absent"),
               "--production-root", str(ROOT), "--backend", "llama", "--model", str(tmp_path / "missing.gguf"),
               "--server", str(tmp_path / "missing-server"), "--json"]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 2, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "MISSING" and "ZIP missing" in report["error"] and report["release_eligible"] is False
    assert not (tmp_path / "absent").exists()  # no silent preparation or download.


def test_print_pass_wrapper_is_not_public_evidence(tmp_path):
    directory = tmp_path / "capture"
    capture_command(directory, "public.candidate", [sys.executable, "-c", 'print("PASS")'], cwd=ROOT,
                    suite_id=p.SUITE, identity={"evaluator_files": ir.evaluator_files(ROOT, "public")})
    with pytest.raises(InvalidEvidence, match="registered Python/script"):
        ir._command(directory / "manifest.json", ROOT, kind="public", suite=p.SUITE, check_id="public.candidate")


def test_regression_uses_recomputed_per_dataset_hybrid_drop(monkeypatch):
    # Adapter orchestration unit fixture, not a captured real quality result.
    from acceptance_lib import checks
    monkeypatch.setattr(checks, "public_assets", lambda *args: {"datasets": dict.fromkeys(p.DATASETS)})
    def arm(root, inputs, role):
        metric = {"n": 100, "sum": 60.0 if role == "reference" else 57.0, "value": .60 if role == "reference" else .57}
        return "PASS", {"run_id": role, "preparation_run_id": role+"-prepare", "report": {"backend_identity": {}, "model_sha256": "same", "server_sha256": "same", "environment": {}, "parameters": {}, "manifest_sha256": "same"},
                        "summaries": {name: {"hybrid": {"nDCG@10": metric}} for name in p.DATASETS}}
    monkeypatch.setattr(ir, "_public_arm", arm)
    targets = read_json(ROOT / "evals/acceptance_targets.json")
    status, details = ir.public(ROOT, Inputs(), targets)
    assert status == "FAIL" and all(row["delta"] == -.03 for row in details["datasets"].values())
    assert details["release_eligible"] is False


@pytest.fixture
def prepared_receipt(monkeypatch, tmp_path):
    """Receipt-validation fixture only: no inference or quality result."""
    from answer_eval.protocol import canonical_sha256
    from acceptance_lib.evidence import sha256

    model, server = tmp_path / "model", tmp_path / "server"
    model.write_bytes(b"mechanical model identity")
    server.write_bytes(b"mechanical server identity")
    source = ir._current_source(ROOT)
    report = runner._context(tmp_path / "cache", ROOT, source, backend_name="llama", model=model, server=server,
                             reference=False, diagnostic=False)
    report["advisory_published_baselines"] = True
    report["backend_identity"] = {"model_sha256": sha256(model), "dim": 768}
    for name in p.DATASETS:
        report["datasets"][name] = {"input_identity": {"zip_sha256": "1" * 64},
                                    "index": {"path": str(tmp_path / (name + ".sqlite")), "sha256": "2" * 64}, "arms": {}}
    prepared = {key: copy.deepcopy(value) for key, value in report.items() if key != "advisory_published_baselines"}
    prepared["status"] = "PREPARED"
    for value in prepared["datasets"].values():
        del value["arms"]
    public_inputs = {"manifest_sha256": report["manifest_sha256"],
                     "datasets": {name: report["datasets"][name]["input_identity"] for name in p.DATASETS}}
    manifest = {"run_id": "prepared-fixture", "identity": {"evaluator_files": ir.evaluator_files(ROOT, "public"),
                 "production_source": source, "model_sha256": sha256(model), "server_sha256": sha256(server), "public_inputs": public_inputs}}
    argv = [sys.executable, str(ROOT / "scripts/eval_public.py"), "--raw", "--prepare", "--cache-root", report["cache_root"],
            "--production-root", str(ROOT), "--backend", "llama", "--model", str(model), "--server", str(server), "--json"]
    receipt = {"argv": argv, "cwd": str(ROOT), "finished_at": "2026-10-21T00:00:01+00:00", "exit_code": 0, "timed_out": False}
    score_manifest, score_receipt = {"run_id": "scoring-fixture"}, {"started_at": "2026-10-21T00:00:02+00:00"}
    path = tmp_path / "prepare.json"
    write_json(path, prepared)
    monkeypatch.setattr(ir, "_command", lambda *args, **kwargs: (manifest, receipt, path))
    inputs = Inputs({"schema": 2, "path_base": str(tmp_path), "models": {"embedding": {"path": str(model), "sha256": sha256(model)}},
                     "public": {"candidate_prepare": {"manifest_path": str(path), "sha256": sha256(path)}}})
    return prepared, manifest, receipt, report, score_manifest, score_receipt, public_inputs, inputs, path


@pytest.mark.parametrize("mutation", [None, "db_sha", "zip_sha", "runtime", "model", "source_content", "source_commit_only",
                                    "missing_dataset", "metadata_only", "late_prepare", "same_run", "bad_command", "nonzero", "diagnostic"])
def test_public_preparation_binding_rejects_poisoned_or_unexecuted_inputs(prepared_receipt, mutation):
    from answer_eval.protocol import canonical_sha256
    from acceptance_lib.evidence import sha256

    prepared, manifest, receipt, report, score_manifest, score_receipt, public_inputs, inputs, path = prepared_receipt
    if mutation == "db_sha":
        prepared["datasets"]["scifact"]["index"]["sha256"] = "3" * 64
    elif mutation == "zip_sha":
        prepared["datasets"]["nfcorpus"]["input_identity"]["zip_sha256"] = "3" * 64
    elif mutation == "runtime":
        prepared["environment"]["numpy"] = "different-runtime"
    elif mutation == "model":
        prepared["model_sha256"] = "3" * 64
    elif mutation == "source_content":
        prepared["production"]["files"]["goldmine/__init__.py"] = "3" * 64
        prepared["production"]["source_sha256"] = canonical_sha256(prepared["production"]["files"])
    elif mutation == "source_commit_only":
        prepared["production"]["git_commit"] = "b" * 40
        manifest["identity"]["production_source"] = copy.deepcopy(prepared["production"])
    elif mutation == "missing_dataset":
        del prepared["datasets"]["arguana"]
    elif mutation == "metadata_only":
        prepared.clear()
        prepared.update({"status": "PREPARED", "database_metadata_looks_valid": True})
    elif mutation == "late_prepare":
        receipt["finished_at"] = "2026-10-21T00:00:03+00:00"
    elif mutation == "same_run":
        manifest["run_id"] = score_manifest["run_id"]
    elif mutation == "bad_command":
        receipt["argv"].remove("--prepare")
    elif mutation == "nonzero":
        receipt["exit_code"] = 2
    elif mutation == "diagnostic":
        prepared["diagnostic_only"] = True
    write_json(path, prepared)
    inputs.data["public"]["candidate_prepare"]["sha256"] = sha256(path)
    call = lambda: ir._public_prepare(ROOT, inputs, "candidate", report, score_manifest, score_receipt, public_inputs)
    if mutation in (None, "source_commit_only"):
        assert call() == "prepared-fixture"
    else:
        with pytest.raises((InvalidEvidence, EvaluationError)):
            call()
