"""Real tiny commands cannot be promoted by missing/forged/stale evidence."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from acceptance_lib.checks import Inputs
from acceptance_lib.scale import check
from acceptance_lib.registry import expand
from scale_bench.protocol import digest, sha
from scale_bench.validation import validate
from scale_bench_fixtures import mutate, scale_capture


def targets():
    return json.loads((ROOT / "evals/acceptance_targets.json").read_text())


def inputs(path):
    return Inputs({"schema": 2, "path_base": str(path.parent),
                   "evidence": {"scale": {"manifest_path": str(path), "sha256": sha(path)}}})


def test_registered_as_required_task_and_release_check():
    assert "capacity.scale" in {c.id for c in expand("task-check", "P2-02")}
    assert "capacity.scale" in {c.id for c in expand("local_rc")}


def test_missing_and_old_vector_only_pass_stay_missing(tmp_path):
    assert check(ROOT, Inputs(), targets())[0] == "MISSING"
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"schema": 1, "suite_id": "scale.v1", "status": "PASS", "results": [{}] * 12}))
    status, details = check(ROOT, inputs(path), targets())
    assert status == "MISSING" and "vector-only" in details["reason"]


def test_diagnostic_full_cli_cannot_stand_in_for_all12(scale_capture):
    status, details = check(ROOT, inputs(scale_capture / "manifest.json"), targets())
    assert status == "MISSING" and "required case" in details["reason"]
    # The very same raw records are complete mechanically at their explicitly
    # tiny test target, not at any production capacity denominator.
    with mutate(scale_capture) as cap:
        assert cap.replay()["status"] == "PASS"


def test_all_samples_and_all_forms_required(scale_capture):
    with mutate(scale_capture) as cap:
        cap.case["samples"].pop()
        with pytest.raises(ValueError, match="30 actual calls"):
            cap.replay()
    with mutate(scale_capture) as cap:
        cap.case["samples"][36] = copy.deepcopy(cap.case["samples"][0])
        with pytest.raises(ValueError, match="missing/duplicated/reordered"):
            cap.replay()


def test_rehashed_forged_body_is_checked_against_source(scale_capture):
    with mutate(scale_capture) as cap:
        selected = cap.selected()
        _, audit_path, _, _, receipt = selected
        stdout = audit_path.parent / receipt["stdout"]["path"]
        rows = cap.get(stdout)
        rows[0]["excerpt"] = "fabricated body; wrapper claims PASS"
        cap.put(stdout, rows)
        receipt["stdout"]["sha256"] = sha(stdout)
        cap.bind_selected(selected)
        with pytest.raises(ValueError, match="excerpt/scorer"):
            cap.replay()


def test_nonzero_exit_is_counted_without_dropping_attempt(scale_capture):
    with mutate(scale_capture) as cap:
        selected = cap.selected()
        selected[-1]["exit_code"] = 1
        cap.bind_selected(selected)
        result = cap.replay()
        cli = result["cases"][0]["cli"]
        assert result["status"] == "MISSING" and len(cli["failures"]) == 1
        assert cli["metrics"]["candidate.cold.search"]["attempts"] == 30


def test_arbitrary_pass_program_and_bad_rss_cannot_impersonate_cli(scale_capture):
    with mutate(scale_capture) as cap:
        selected = cap.selected()
        selected[-1]["argv"] = [sys.executable, "-c", "print('PASS')"]
        cap.bind_selected(selected)
        with pytest.raises(ValueError, match="actual CLI argv"):
            cap.replay()
    with mutate(scale_capture) as cap:
        selected = cap.selected()
        selected[-1]["rss"]["bytes"] += 1
        cap.bind_selected(selected)
        with pytest.raises(ValueError, match="RSS"):
            cap.replay()


def test_stale_source_and_capture_tool_rejected_even_when_self_hashes_match(scale_capture):
    with mutate(scale_capture) as cap:
        value = cap.manifest["sources"]["candidate"]
        value["files"]["goldmine/store.py"] = "0" * 64
        value["source_sha256"] = digest(value["files"])
        cap.manifest["source_after"]["candidate"] = copy.deepcopy(value)
        with pytest.raises(ValueError, match="missing/stale"):
            cap.replay()
    with mutate(scale_capture) as cap:
        cap.manifest["tools"]["scripts/bench_scale.py"] = "0" * 64
        cap.manifest["tools_after"] = copy.deepcopy(cap.manifest["tools"])
        with pytest.raises(ValueError, match="capture tool changed"):
            cap.replay()


def test_rehashed_score_regression_and_phase_summary_are_not_hidden(scale_capture):
    with mutate(scale_capture) as cap:
        selected = cap.selected()
        _, audit_path, _, _, receipt = selected
        stdout = audit_path.parent / receipt["stdout"]["path"]
        rows = cap.get(stdout)
        rows[0]["score"] += .01
        cap.put(stdout, rows)
        receipt["stdout"]["sha256"] = sha(stdout)
        cap.bind_selected(selected)
        assert cap.replay()["status"] == "FAIL"
    with mutate(scale_capture) as cap:
        entry = cap.case["phase_reports"]["candidate"]
        path = cap.case_path.parent / entry["path"]
        data = cap.get(path)
        data["phases_ms"]["cold_load"]["p50"] = 0
        cap.put(path, data)
        entry["sha256"] = sha(path)
        artifact = cap.case["phase_artifacts"]["candidate"]["receipt"]
        receipt_path = cap.case_path.parent / artifact["path"]
        receipt = cap.get(receipt_path)
        receipt["result_sha256"] = sha(path)
        cap.put(receipt_path, receipt)
        artifact["sha256"] = sha(receipt_path)
        with pytest.raises(ValueError, match="summary differs"):
            cap.replay()


def test_human_pack_budget_is_verified_from_actual_emitted_bytes(scale_capture):
    with mutate(scale_capture) as cap:
        sample = next(s for s in cap.case["samples"] if s["index"] == 3 and s["arm"] == "candidate" and
                      s["temperature"] == "cold" and s["form"] == "pack_text")
        audit_path = cap.case_path.parent / sample["audit"]["path"]
        audit = cap.get(audit_path)
        receipt_path = audit_path.parent / audit["command"]["path"]
        receipt = cap.get(receipt_path)
        stdout = audit_path.parent / receipt["stdout"]["path"]
        original = stdout.read_text()
        cap.originals[stdout] = stdout.read_bytes()
        stdout.write_text(original + " extra unbudgeted content" * 1000)
        receipt["stdout"]["sha256"] = sha(stdout)
        cap.bind_selected((sample, audit_path, audit, receipt_path, receipt))
        with pytest.raises(ValueError, match="actual emitted pack token count"):
            cap.replay()
