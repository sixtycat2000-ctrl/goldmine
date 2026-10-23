"""Bounded caller composition tests; no engine, model, original420 or quality run."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import pytest

from acceptance_lib.evidence import InvalidEvidence
from acceptance_lib.resource_policy import policy
from answer_eval.protocol import EvaluationError
from search_comparison.engine_capture import resources
from search_comparison.engine_capture.commands import Commands
from search_comparison.engine_capture.identity import implementation


def context():
    return {"registered_at": datetime.now(timezone.utc).isoformat(), "arm": "qmd.search",
            "engine_resources": {}, "scope": "DECLARED_SYNTHETIC_COMPOSITION_TEST_ONLY", "quality_evidence": False}


def test_exact_source_context_is_preflighted_without_any_child(tmp_path):
    owner = tmp_path.resolve()
    value = context()
    env = {"PATH": "/usr/bin:/bin"}
    result = resources.preflight(owner, value, env, fixture=True)
    assert result["maximum_source_context_bytes"] == max(result["source_context_bytes"].values())
    assert result["maximum_source_context_bytes"] < result["limit_bytes"]
    original = list(owner.iterdir())
    value["entire_registered_inventory"] = "x" * (1024 * 1024)
    with pytest.raises(InvalidEvidence, match="bounded finite JSON"):
        resources.preflight(owner, value, env)
    assert list(owner.iterdir()) == original


def test_external_inventory_refs_are_complete_and_conflicts_reject():
    value = {"engine_resources": {"installation": {"path": "/owned/runtime", "files": {"all": "retained"}, "sha256": "a" * 64},
                                 "tool": {"path": "/owned/tool", "sha256": "b" * 64, "license": {"path": "/owned/license", "sha256": "c" * 64}}},
             "view_identity": {"corpus": "/owned/input", "sha256": "d" * 64},
             "input_refs": {"plan": {"path": "/owned/plan.json", "sha256": "e" * 64}}}
    refs = resources.external_inputs(value)
    assert len(refs) == 5
    assert next(r for r in refs if r["path"] == "/owned/runtime")["kind"] == "tree"
    value["input_refs"]["contradiction"] = {"path": "/owned/tool", "sha256": "f" * 64}
    with pytest.raises(EvaluationError, match="contradictory"):
        resources.external_inputs(value)


@pytest.mark.parametrize("verdict,expected", [("PASS", "PASS"), ("MISSING", "MISSING"), ("FAIL", "FAIL"), (None, "MISSING"), ("UNREVIEWED", "MISSING")])
def test_only_explicit_sampled_verdict_is_consumed(verdict, expected):
    ref = {"path": "/owned/queries/0001/manifest.json", "sha256": "a" * 64}
    row = {"command": ref, "expected_manifest": ref["path"], "phase": "queries", "label": "synthetic", "disposition": "validated",
           "observation": {"sampled_resource_status": verdict, "status": "MISSING", "coverage_gaps": ["fork-descendant-lifetimes-not-atomically-tracked"],
                            "lifetime_complete": False, "full_tree_ceiling_status": "MISSING"}}
    assert resources.status([row], [ref], attempted=1) == expected
    assert row["observation"]["status"] == "MISSING"
    assert resources.status([], [], attempted=0) == "MISSING"
    assert resources.status([row, {"observation": {"sampled_resource_status": "FAIL"}}], [ref], attempted=2) == "FAIL"


def test_policy_keeps_exact_caps_and_full_source(tmp_path):
    value = context()
    expected = resources.expected_policy(tmp_path.resolve(), "queries", value)
    assert expected["source"] == value
    assert expected["mode"] == "comparison" and expected["wall_seconds"] == 600
    assert expected["limits"] == {"owned_bytes": 4 * 1024 ** 3, "stdout_bytes": 16 * 1024 ** 2, "stderr_bytes": 16 * 1024 ** 2, "nofile": 512}
    assert resources.expected_policy(tmp_path.resolve(), "audit", value)["wall_seconds"] == 1800
    assert resources.expected_policy(tmp_path.resolve(), "prepare", value, fixture=True)["mode"] == "fixture"


def test_local_identity_includes_all_nine_observer_modules():
    from acceptance_lib.resource_policy import MODULES
    files = implementation()["files"]
    assert all("scripts/acceptance_lib/" + name in files for name in MODULES)


def test_normalized_qmd_config_is_bound_without_reading_mutable_yaml(tmp_path):
    owner = tmp_path.resolve()
    values = {"qmd_db": str(owner / "xdg/cache/qmd/comparison.sqlite"), "embed": "/owned/embedding.gguf"}
    view = {"name": "common", "corpus": "/owned/common"}
    value = resources.configuration("qmd", values, view)
    assert value["value"]["models"] == {"embed": "/owned/embedding.gguf"}
    assert value["value"]["collections"]["sources"] == {"path": "/owned/common", "pattern": "**/*.md"}
    assert not (owner / "qmd-config").exists()


@pytest.mark.native
def test_owned_observed_child_and_disk_only_replay(tmp_path, monkeypatch):
    owner = tmp_path.resolve()
    (owner / "cwd").mkdir()
    env = {"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"}
    source = context()
    command = [str(Path(sys.executable).resolve()), "-I", "-S", "-B", "-c", "import time; a=bytes(1024*1024); print('synthetic actual child',flush=True); time.sleep(.5)"]
    calls = Commands(owner, env, source, fixture=True)
    assert calls.call("synthetic-actual-child", command, phase="queries") == b"synthetic actual child\n"
    refs = calls.finish()
    measured = calls.resources[0]["observation"]
    assert measured["sampled_resource_status"] == "MISSING"
    assert "fixture-only-policy" in measured["sampled_coverage_gaps"]
    assert measured["sampled_target_tree_rss_bytes"] > 0 and measured["full_tree_ceiling_status"] == "MISSING"
    assert measured["lifetime_complete"] is False
    monkeypatch.setattr("search_comparison.engine_capture.commands.capture_command", lambda *a, **k: pytest.fail("replay must never launch a process"))
    replay = Commands(owner, env, source, replay=refs, fixture=True)
    assert replay.call("synthetic-actual-child", command, phase="queries") == b"synthetic actual child\n"
    assert replay.finish() == refs and replay.resources == calls.resources


@pytest.mark.native
def test_measured_output_overflow_stops_subsequent_owned_children(tmp_path, monkeypatch):
    owner = tmp_path.resolve()
    (owner / "cwd").mkdir()
    source = context()
    def small(owner, phase, identity, *, fixture):
        return policy("query" if phase == "queries" else "prepare", owner, source=identity, fixture_limits={"owned_bytes": 128 * 1024})
    monkeypatch.setattr(resources, "expected_policy", small)
    calls = Commands(owner, {"PATH": "/usr/bin:/bin"}, source, fixture=True)
    command = [str(Path(sys.executable).resolve()), "-I", "-S", "-B", "-c", "import pathlib,time;pathlib.Path('overflow.bin').write_bytes(bytes(160*1024));time.sleep(.5)"]
    with pytest.raises(EvaluationError):
        calls.call("synthetic-overflow", command)
    assert len(calls.finish()) == 1
    assert calls.resources[0]["observation"]["sampled_resource_status"] == "FAIL"
    assert calls.blocked and (owner / "cwd/overflow.bin").stat().st_size == 160 * 1024
    with pytest.raises(EvaluationError, match="no subsequent child"):
        calls.call("must-not-execute", command)
    assert len(calls.finish()) == 1
