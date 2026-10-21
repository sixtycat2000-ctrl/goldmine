"""Freeze/seal binding fixtures do not read any genuine holdout or run models."""
from datetime import datetime, timezone
from pathlib import Path
import copy
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from acceptance_lib.evidence import InvalidEvidence, sha256, write_json
from acceptance_lib.ir_checks import evaluator_files
from answer_eval.protocol import EvaluationError, canonical_sha256
from holdout_lib import protocol, seal
from product_eval.ledger import initialize_ledger


@pytest.fixture
def mechanical_freeze(monkeypatch, tmp_path):
    from test_joint_holdout import registry
    from product_eval import bundle as bundle_module, runner

    core = {"goldmine/__init__.py": "1" * 64, "pyproject.toml": "2" * 64}
    source = {"git_commit": "a" * 40, "files": core, "source_sha256": canonical_sha256(core)}
    monkeypatch.setattr(protocol, "production_identity", lambda *args, **kwargs: source)
    bundle_root = tmp_path / "fixture-bundle"
    bundle_root.mkdir()
    lock = bundle_root / "environment.json"
    write_json(lock, protocol.runtime())
    bundle = SimpleNamespace(identity={"mechanical_fixture_only": True}, missing=[], root=bundle_root,
        manifest={"state": "FROZEN", "reference_environment": {"path": "environment.json"}})
    monkeypatch.setattr(bundle_module, "load_bundle", lambda *args: bundle)
    monkeypatch.setattr(runner, "check_build", lambda bundle, path: Path(path) / "corpus")
    monkeypatch.setattr(seal, "check_seal", lambda *args, **kwargs: (None, "fixture-seal"))
    for name in ("candidate", "original"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "build.json").write_text("mechanical build identity")
        with sqlite3.connect(directory / "index.sqlite") as connection:
            connection.execute("CREATE TABLE fixture(value)")
    for name in ("model", "server"):
        (tmp_path / name).write_bytes(b"mechanical identity only")
    ledger = tmp_path / "ledger"
    anchor = initialize_ledger(ledger)["anchor_sha256"]
    arms, experiments = registry()
    frozen_at = datetime.now(timezone.utc).isoformat()
    original = {"production_root": str(tmp_path / "original"), "production": copy.deepcopy(source),
        "build": str(tmp_path / "original"), "build_sha256": sha256(tmp_path / "original/build.json"),
        "index": protocol.file_ref(tmp_path / "original/index.sqlite"), "parameters": arms["fast"]["parameters"],
        "parameters_sha256": arms["fast"]["parameters_sha256"], "seal_capture": {}, "sealed_report": {}}
    frozen = {"schema": 1, "kind": protocol.KIND, "frozen_at": frozen_at, "bundle": str(bundle_root), "bundle_identity": bundle.identity,
        "candidate_root": str(ROOT), "candidate_source": copy.deepcopy(source), "reference": original,
        "candidate": {"build": str(tmp_path / "candidate"), "build_sha256": sha256(tmp_path / "candidate/build.json"),
                      "index": protocol.file_ref(tmp_path / "candidate/index.sqlite")},
        "model": protocol.file_ref(tmp_path / "model"), "server": protocol.file_ref(tmp_path / "server"), "reranker": None,
        "environment": protocol.runtime(), "evaluator_files": protocol.files(ROOT), "dependency_files": protocol.dependencies(ROOT),
        "targets_sha256": sha256(ROOT / "evals/acceptance_targets.json"), "arms": arms, "experiments": experiments,
        "acceptance": protocol.ACCEPTANCE, "ledger": {"path": str(ledger), "anchor_sha256": anchor}}
    frozen["authorization"] = {"schema": 1, "purpose": "final_joint_release_audit", "frozen_at": frozen_at,
        "bundle_identity": bundle.identity, "candidate_code_sha256": source["source_sha256"], "parameters_sha256": canonical_sha256(arms),
        "dependency_lock_sha256": sha256(lock), "ledger_anchor_sha256": anchor}
    path = tmp_path / "freeze.json"
    write_json(path, frozen)
    return path, frozen


@pytest.mark.parametrize("mutation", [None, "source_commit", "source_bytes", "evaluator", "dependencies", "model_bytes", "server_bytes",
                                    "build_bytes", "index_bytes", "runtime", "targets", "arms", "future", "reset_ledger", "unknown", "same_index"])
def test_freeze_binds_actual_content_tools_runtime_native_build_and_fixed_registry(mechanical_freeze, mutation):
    path, frozen = mechanical_freeze
    if mutation == "source_commit": frozen["candidate_source"]["git_commit"] = "b" * 40
    elif mutation == "source_bytes":
        frozen["candidate_source"]["files"]["goldmine/__init__.py"] = "f" * 64
        frozen["candidate_source"]["source_sha256"] = canonical_sha256(frozen["candidate_source"]["files"])
    elif mutation == "evaluator": frozen["evaluator_files"] = {}
    elif mutation == "dependencies": frozen["dependency_files"] = {}
    elif mutation == "model_bytes": Path(frozen["model"]["path"]).write_bytes(b"changed")
    elif mutation == "server_bytes": Path(frozen["server"]["path"]).write_bytes(b"changed")
    elif mutation == "build_bytes": (Path(frozen["candidate"]["build"]) / "build.json").write_bytes(b"changed")
    elif mutation == "index_bytes": Path(frozen["candidate"]["index"]["path"]).write_bytes(b"changed")
    elif mutation == "runtime": frozen["environment"]["numpy"] = "different"
    elif mutation == "targets": frozen["targets_sha256"] = "f" * 64
    elif mutation == "arms": frozen["arms"]["new_winner"] = copy.deepcopy(frozen["arms"]["fast"])
    elif mutation == "future": frozen["frozen_at"] = "2999-01-01T00:00:00+00:00"
    elif mutation == "reset_ledger": frozen["ledger"]["anchor_sha256"] = "f" * 64
    elif mutation == "unknown": frozen["relax_gate_after_scores"] = True
    elif mutation == "same_index": frozen["reference"]["index"] = copy.deepcopy(frozen["candidate"]["index"])
    write_json(path, frozen)
    if mutation in (None, "source_commit"):
        assert protocol.load_freeze(path, ROOT)[2] == ["fast"]
    else:
        with pytest.raises((EvaluationError, InvalidEvidence, sqlite3.Error)):
            protocol.load_freeze(path, ROOT)


def test_reference_seal_preflight_never_opens_private_score_json(monkeypatch, tmp_path):
    captured = tmp_path / "capture"
    output = captured / "sealed"
    output.mkdir(parents=True, mode=0o700)
    report = output / "run.json"
    report.write_bytes(b"not JSON: preflight may hash bytes but cannot parse score content")
    report.chmod(0o600)
    manifest_path = captured / "manifest.json"
    manifest_path.write_text("fixture capture identity")
    for name in ("model", "server", "index"):
        (tmp_path / name).write_bytes(b"mechanical seal identity")
    params = {"scorer": "hybrid", "z_gate": 3, "limit": 10, "context": 1, "budget": 4000}
    parameter_path = tmp_path / "parameters.json"
    write_json(parameter_path, params)
    source = {"git_commit": "a" * 40}
    frozen = {"bundle": str(tmp_path / "bundle"), "bundle_identity": {"fixture": True}, "frozen_at": "2026-10-21T00:01:00+00:00",
        "model": protocol.file_ref(tmp_path / "model"), "server": protocol.file_ref(tmp_path / "server"),
        "reference": {"build": str(tmp_path / "build"), "production_root": str(tmp_path / "original"), "production": source,
            "index": protocol.file_ref(tmp_path / "index"), "parameters": params, "parameters_sha256": canonical_sha256(params),
            "seal_capture": {"manifest_path": str(manifest_path), "sha256": sha256(manifest_path)}, "sealed_report": protocol.file_ref(report)}}
    identity = {"evaluator_files": evaluator_files(ROOT, "product"), "production_source": source, "index_sha256": frozen["reference"]["index"]["sha256"],
        "server_sha256": frozen["server"]["sha256"], "model_sha256": frozen["model"]["sha256"], "parameters_sha256": canonical_sha256(params),
        "bundle_identity": frozen["bundle_identity"]}
    argv = [sys.executable, str(ROOT / "scripts/eval_product.py"), "--bundle", frozen["bundle"], "--build", frozen["reference"]["build"],
        "--db", frozen["reference"]["index"]["path"], "--production-root", frozen["reference"]["production_root"], "--backend", "llama",
        "--model", frozen["model"]["path"], "--server", frozen["server"]["path"], "--parameters", str(parameter_path), "--split", "holdout",
        "--reference-code", "--seal-reference", "--out", str(output), "--json"]
    receipt = {"argv": argv, "exit_code": 0, "timed_out": False, "finished_at": "2026-10-21T00:00:01+00:00"}
    stdout = captured / "stdout.json"
    write_json(stdout, {"status": "SEALED_REFERENCE", "sha256": sha256(report), "bundle_identity": frozen["bundle_identity"],
        "production_commit": source["git_commit"], "release_eligible": False, "metrics_exposed": False})
    monkeypatch.setattr(seal, "_command", lambda *args, **kwargs: ({"run_id": "mechanical-seal", "identity": identity}, receipt, stdout))
    assert seal.check_seal(frozen, tmp_path, ROOT, open_report=False) == (None, "mechanical-seal")
    with pytest.raises(InvalidEvidence, match="cannot read JSON"):
        seal.check_seal(frozen, tmp_path, ROOT, open_report=True)


def test_freeze_creator_pins_explicit_sources_without_reading_private_scores_or_consuming_exposure(mechanical_freeze, monkeypatch):
    from holdout_lib import freeze as creator
    from product_eval.ledger import _read

    path, frozen = mechanical_freeze
    bundle = protocol.load_freeze(path, ROOT)[1]
    monkeypatch.setattr(creator, "load_bundle", lambda *args: bundle)
    monkeypatch.setattr(creator, "check_build", lambda bundle, build: Path(build) / "corpus")
    monkeypatch.setattr(creator, "production_identity", lambda *args, **kwargs: frozen["candidate_source"])
    capture = path.parent / "seal-capture"
    (capture / "sealed").mkdir(parents=True)
    private = capture / "sealed/run.json"
    private.write_bytes(b"Private score bytes deliberately are not JSON; freezing must not parse them.")
    seal_manifest = capture / "manifest.json"
    seal_manifest.write_text("mechanical-only seal ref")
    parameter = path.parent / "parameters.json"
    write_json(parameter, frozen["arms"]["fast"]["parameters"])
    spec = {"schema": 1, "bundle": str(bundle.root), "candidate_root": str(ROOT),
        "candidate_build": frozen["candidate"]["build"], "candidate_index": frozen["candidate"]["index"]["path"],
        "reference_root": frozen["reference"]["production_root"], "reference_build": frozen["reference"]["build"],
        "reference_index": frozen["reference"]["index"]["path"], "reference_parameters": str(parameter),
        "reference_seal_capture": {"manifest_path": str(seal_manifest), "sha256": sha256(seal_manifest)},
        "model": frozen["model"]["path"], "server": frozen["server"]["path"], "reranker": None,
        "ledger": frozen["ledger"]["path"], "parameters": {"fast": str(parameter)}, "experiments": frozen["experiments"]}
    spec_path = path.parent / "spec.json"
    write_json(spec_path, spec)
    made = creator.create(spec_path, ROOT)
    assert made["reference"]["sealed_report"]["sha256"] == sha256(private)
    assert made["candidate"]["build_sha256"] == frozen["candidate"]["build_sha256"]
    assert made["authorization"]["parameters_sha256"] == canonical_sha256(made["arms"])
    assert len(_read(Path(frozen["ledger"]["path"]))[1]) == 1
