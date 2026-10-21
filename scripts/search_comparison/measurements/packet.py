"""Render a fixed measurement execution packet from an already verified Plan."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path

from answer_eval.protocol import EvaluationError, canonical_sha256, sha256_file
from acceptance_lib.resource_policy import runtime_identity
from search_comparison.engine_capture.identity import implementation
from .selection import ARMS, VIEWS, SEED, select_sources, select_requests, schedule, verify_schedule
from .recipes import operations

SUITE = "comparison.measurement-execution-packet/v1"


def identity():
    value = implementation()
    root = Path(__file__).resolve().parents[3]
    files = dict(value["files"])
    files["scripts/compile_comparison_measurements.py"] = sha256_file(root / "scripts/compile_comparison_measurements.py")
    return {"files": files, "sha256": canonical_sha256(files)}


def compile_packet(plan):
    if tuple(plan.arms) != ARMS or plan.bundle.fixture:
        raise EvaluationError("real packet requires all nine registered modes and the frozen real source bundle")
    selected, cohorts = select_requests(plan.projection["requests"]), select_sources(plan.bundle.sources)
    slots = schedule(selected)
    return {"schema": 1, "suite_id": SUITE, "state": "FROZEN_EXECUTION_SPECIFICATION",
            "registered_at": datetime.now(timezone.utc).isoformat(), "seed": SEED,
            "plan": {"path": str(plan.path), "sha256": plan.sha256}, "source_identity": plan.bundle.identity,
            "projection": plan.projection["reference"], "projection_identity": plan.projection["identity"],
            "implementation": identity(), "resource_observer": runtime_identity(), "arms": list(ARMS), "views": list(VIEWS),
            "selection": selected, "source_cohorts": cohorts, "schedule": slots,
            "denominators": verify_schedule(selected, slots), "update_operations": operations(cohorts),
            "cold_definition": "fresh-cli-restored-pre-query-snapshot-application-cache-empty-no-os-flush",
            "warm_definition": "fresh-cli-persistent-owned-cache-immediate-successful-same-query-prime-no-model-residency-claim",
            "resident_track": {"status": "CONDITIONAL_MISSING", "reason": "separate matching persistent adapters/runtime canaries not implemented in this packet"},
            "executor": {"status": "NOT_IMPLEMENTED_BY_THIS_COMPILER", "requirement": "strict typed operations via adopted Commands/observer, complete raw/source/index replay before real measurements"},
            "quality_evidence": False, "release_eligible": False, "holdout_entry": None}
