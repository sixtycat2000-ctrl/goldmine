"""Frozen fixed-arm release registry and disk-only asset identity checks."""
from __future__ import annotations

import importlib.metadata
from pathlib import Path
import platform
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from acceptance_lib.evidence import external_ref, read_json, sha256
from acceptance_lib.ir_checks import evaluator_files
from answer_eval.protocol import EvaluationError, canonical_sha256, fields, timestamp
from product_eval.candidate import validate_parameters
from product_eval.runner import production_content, production_identity

KIND = "goldmine.joint-holdout-freeze/v1"
SUITE = "product.joint-holdout/v1"
BASE_KEYS = {"scorer", "z_gate", "limit", "context", "budget"}
SIMPLE_CLASSES = ("semantic", "cjk", "lookup", "pdf-page", "media-text")
ACCEPTANCE = {"simple_classes": list(SIMPLE_CLASSES), "paired_seed": 20261021, "paired_reps": 10000,
              "simple_top1": "source-group-percentile-ci95-upper-below-zero-reject/v1",
              "multi_control": "same-candidate-same-index-single-query/v1"}


def files(root):
    root = Path(root).resolve()
    paths = [root / "scripts/eval_joint_holdout.py", root / "scripts/capture_holdout.py",
             *sorted((root / "scripts/holdout_lib").glob("*.py")),
             *sorted((root / "scripts/acceptance_lib").glob("*.py"))]
    return {**evaluator_files(root, "product"), **{p.relative_to(root).as_posix(): sha256(p) for p in paths}}


def dependencies(root):
    root = Path(root)
    paths = [root / "pyproject.toml", *sorted((root / "requirements").glob("*.lock"))]
    return {path.relative_to(root).as_posix(): sha256(path) for path in paths}


def runtime():
    return {"python": platform.python_version(), "numpy": importlib.metadata.version("numpy")}


def file_ref(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256(path)}


def fixed_arms(arms, experiments):
    """At most the predeclared fast, quality-control and multi-query arms."""
    fields(experiments, {"rerank", "multi_query"}, "joint experiments")
    if any(experiments[key] not in ("enabled", "disabled") for key in experiments):
        raise EvaluationError("joint experiments must be explicitly enabled/disabled before exposure")
    rerank, multi = experiments["rerank"] == "enabled", experiments["multi_query"] == "enabled"
    expected = {"fast"} | ({"quality"} if rerank or multi else set()) | ({"multi"} if multi else set())
    if not isinstance(arms, dict) or set(arms) != expected:
        raise EvaluationError("joint registry must contain exactly the predeclared fast/quality/multi controls")
    for name, arm in arms.items():
        fields(arm, {"parameters", "parameters_sha256"}, "joint arm")
        validate_parameters(arm["parameters"], reference_run=False)
        p = arm["parameters"]
        if (p["scorer"] not in ("hybrid", "semantic", "lexical", "rrf")
                or type(p["z_gate"]) not in (int, float) or not math.isfinite(p["z_gate"])
                or type(p["limit"]) is not int or not 5 <= p["limit"] <= 50
                or type(p["context"]) is not int or not 0 <= p["context"] <= 3
                or type(p["budget"]) is not int or not 512 <= p["budget"] <= 32768):
            raise EvaluationError("joint predeclared search parameter types/ranges are invalid")
        if canonical_sha256(arm["parameters"]) != arm["parameters_sha256"]:
            raise EvaluationError("joint arm parameter SHA differs")
    fast = arms["fast"]["parameters"]
    if fast.get("mode", "fast") != "fast" or fast.get("rerank", "off") != "off" or fast.get("multi_query", False):
        raise EvaluationError("joint fast arm must use fast/off/single-query")
    if rerank or multi:
        quality = arms["quality"]["parameters"]
        expected_quality = {**fast, "mode": "quality", "rerank": "local" if rerank else "off", "multi_query": False}
        if quality != expected_quality:
            raise EvaluationError("quality control must be identical to fast except explicit quality/rerank flags")
    if multi and arms["multi"]["parameters"] != {**arms["quality"]["parameters"], "multi_query": True}:
        raise EvaluationError("multi-query treatment must differ from its same-candidate quality control only in multi_query")
    return [name for name in ("fast", "quality", "multi") if name in expected]


def checked_index(ref, base):
    path = external_ref(base, ref)
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise EvaluationError("joint audit requires explicitly checkpointed fixed SQLite bytes", status="MISSING")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise EvaluationError("joint index integrity failed")
    return path


def load_freeze(path, root, *, check_ledger=True):
    """No query evaluation; explicit freeze input is required before any read."""
    from product_eval.bundle import load_bundle
    from product_eval.ledger import _read
    from product_eval.runner import check_build

    path, root = Path(path).resolve(), Path(root).resolve()
    frozen = read_json(path)
    fields(frozen, {"schema", "kind", "frozen_at", "bundle", "bundle_identity", "candidate_root", "candidate_source",
                    "reference", "candidate", "model", "server", "reranker", "environment", "evaluator_files",
                    "dependency_files", "targets_sha256", "arms", "experiments", "acceptance", "authorization",
                    "ledger"}, "joint release freeze")
    if type(frozen["schema"]) is not int or frozen["schema"] != 1 or frozen["kind"] != KIND or frozen["acceptance"] != ACCEPTANCE:
        raise EvaluationError("unsupported joint freeze/acceptance protocol")
    if timestamp(frozen["frozen_at"], "joint frozen_at") > datetime.now(timezone.utc):
        raise EvaluationError("joint freeze is dated in the future")
    if (Path(frozen["candidate_root"]).resolve() != root
            or production_content(frozen["candidate_source"]) != production_content(production_identity(root))):
        raise EvaluationError("joint frozen production content differs from the final candidate")
    if (frozen["evaluator_files"] != files(root) or frozen["dependency_files"] != dependencies(root)
            or frozen["targets_sha256"] != sha256(root / "evals/acceptance_targets.json") or frozen["environment"] != runtime()):
        raise EvaluationError("joint evaluator/dependency/runtime/targets changed after freeze")
    order = fixed_arms(frozen["arms"], frozen["experiments"])
    bundle = load_bundle(Path(frozen["bundle"]))
    if bundle.identity != frozen["bundle_identity"] or bundle.missing or bundle.manifest["state"] != "FROZEN":
        raise EvaluationError("joint audit requires the exact complete frozen real source/label/fold bundle")
    environment_path = bundle.root / bundle.manifest["reference_environment"]["path"]
    lock = read_json(environment_path)
    if any(lock.get(key) != value for key, value in frozen["environment"].items()):
        raise EvaluationError("joint runtime differs from the original frozen environment")
    for key in ("model", "server"):
        external_ref(path.parent, frozen[key])
    if frozen["experiments"]["rerank"] == "enabled":
        external_ref(path.parent, frozen["reranker"])
    elif frozen["reranker"] is not None:
        raise EvaluationError("disabled rerank must not declare an unexecuted reranker asset")
    fields(frozen["candidate"], {"build", "build_sha256", "index"}, "joint candidate inputs")
    corpus = check_build(bundle, Path(frozen["candidate"]["build"]))
    if sha256(corpus.parent / "build.json") != frozen["candidate"]["build_sha256"]:
        raise EvaluationError("joint candidate materialized build receipt changed")
    checked_index(frozen["candidate"]["index"], path.parent)
    fields(frozen["reference"], {"production_root", "production", "build", "build_sha256", "index", "parameters",
                               "parameters_sha256", "seal_capture", "sealed_report"}, "joint original reference")
    ref = frozen["reference"]
    if production_identity(ref["production_root"], reference_run=True) != ref["production"]:
        raise EvaluationError("joint original clean production identity changed")
    reference_corpus = check_build(bundle, Path(ref["build"]))
    if sha256(reference_corpus.parent / "build.json") != ref["build_sha256"]:
        raise EvaluationError("joint original materialized build receipt changed")
    checked_index(ref["index"], path.parent)
    if Path(ref["index"]["path"]).resolve() == Path(frozen["candidate"]["index"]["path"]).resolve():
        raise EvaluationError("joint original and candidate require independent prepared SQLite indexes")
    validate_parameters(ref["parameters"], reference_run=True)
    if canonical_sha256(ref["parameters"]) != ref["parameters_sha256"]:
        raise EvaluationError("joint original parameter SHA changed")
    # Validate the seal receipt/bytes at freeze time without opening its private
    # per-query or aggregate report. Only final evidence replay opens that file.
    from .seal import check_seal

    check_seal(frozen, path.parent, root, open_report=False)
    fields(frozen["ledger"], {"path", "anchor_sha256"}, "joint ledger")
    authorization = {"schema": 1, "purpose": "final_joint_release_audit", "frozen_at": frozen["frozen_at"],
                     "bundle_identity": bundle.identity, "candidate_code_sha256": frozen["candidate_source"]["source_sha256"],
                     "parameters_sha256": canonical_sha256(frozen["arms"]), "dependency_lock_sha256": sha256(environment_path),
                     "ledger_anchor_sha256": frozen["ledger"]["anchor_sha256"]}
    if frozen["authorization"] != authorization:
        raise EvaluationError("joint authorization does not bind the entire fixed arm registry")
    if check_ledger:
        state, records = _read(Path(frozen["ledger"]["path"]))
        if state["anchor_sha256"] != frozen["ledger"]["anchor_sha256"] or records[0]["payload"].get("event") != "initialized":
            raise EvaluationError("joint exposure ledger was reset/replaced")
        if timestamp(records[0]["payload"]["created_at"], "ledger initialized") > timestamp(frozen["frozen_at"], "joint freeze"):
            raise EvaluationError("joint ledger was initialized after the pinned final freeze")
    return frozen, bundle, order
