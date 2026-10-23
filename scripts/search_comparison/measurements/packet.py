"""Render a fixed measurement execution packet from an already verified Plan."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path

from answer_eval.protocol import EvaluationError, canonical_sha256, sha256_file
from acceptance_lib.resource_policy import runtime_identity
from search_comparison.engine_capture.identity import implementation
from search_comparison.engine_capture.protocol import read, reference
from .selection import ARMS, VIEWS, SEED, select_sources, select_requests, schedule, verify_schedule
from .recipes import operations

SUITE = "comparison.measurement-execution-packet/v1"


def identity():
    value = implementation()
    root = Path(__file__).resolve().parents[3]
    files = dict(value["files"])
    for name in ("scripts/compile_comparison_measurements.py", "scripts/capture_comparison_measurements.py"):
        path = root / name
        if path.is_symlink():
            raise EvaluationError("measurement entrypoint aliases another source")
        files[name] = sha256_file(path)
    return {"files": files, "sha256": canonical_sha256(files)}


def compile_packet(plan, *, registered_at=None):
    if tuple(plan.arms) != ARMS or plan.bundle.fixture:
        raise EvaluationError("real packet requires all nine registered modes and the frozen real source bundle")
    selected, cohorts = select_requests(plan.projection["requests"]), select_sources(plan.bundle.sources)
    slots = schedule(selected)
    return {"schema": 1, "suite_id": SUITE, "state": "FROZEN_EXECUTION_SPECIFICATION",
            "registered_at": registered_at or datetime.now(timezone.utc).isoformat(), "seed": SEED,
            "plan": {"path": str(plan.path), "sha256": plan.sha256}, "source_identity": plan.bundle.identity,
            "projection": plan.projection["reference"], "projection_identity": plan.projection["identity"],
            "implementation": identity(), "resource_observer": runtime_identity(), "arms": list(ARMS), "views": list(VIEWS),
            "selection": selected, "source_cohorts": cohorts, "schedule": slots,
            "denominators": verify_schedule(selected, slots), "update_operations": operations(cohorts),
            "cold_definition": "fresh-cli-restored-pre-query-snapshot-application-cache-empty-no-os-flush",
            "warm_definition": "fresh-cli-persistent-owned-cache-immediate-successful-same-query-prime-no-model-residency-claim",
            "resident_track": {"status": "CONDITIONAL_MISSING", "reason": "separate matching persistent adapters/runtime canaries not implemented in this packet"},
            "executor": {"status": "IMPLEMENTED_TYPED_CAPTURE_AND_DISK_REPLAY", "real_execution": "REQUIRES_SEPARATE_ACTUAL_CAPTURE_AND_STRICT_VERIFY",
                         "slot_storage": "comparison.measurement-slot-shards/v2; complete cell-owned slot payloads and fixed ordered root refs",
                         "asset_binding": "comparison.complete-engine-registration/v1", "owned_accounting": "all command logs/index/corpus/snapshots below each cell owner; shared read-only complete registrations are external declarations, not a filesystem sandbox"},
            "framing_limits": {"root_manifest_bytes": 8 * 1024 * 1024, "root_admin_bytes": 16 * 1024 * 1024,
                               "typed_record_bytes": 64 * 1024 * 1024, "maximum_records_per_stream": 50000,
                               "overflow": "MISSING; no raw command material discarded or live hard byte-ceiling claim"},
            "quality_evidence": False, "release_eligible": False, "holdout_entry": None}


def load_packet(ref, plan):
    from answer_eval.protocol import timestamp
    path = reference(ref, bound=8 * 1024 * 1024)
    packet = read(path)
    registered = timestamp(packet.get("registered_at"), "measurement registration")
    if registered > datetime.now(timezone.utc) or registered < timestamp(plan.value["registered_at"], "engine registration"):
        raise EvaluationError("measurement registration must follow the frozen plan and precede execution")
    if canonical_sha256(packet) != canonical_sha256(compile_packet(plan, registered_at=packet.get("registered_at"))):
        raise EvaluationError("packet differs from current strict compiler/source/schedule/cohorts; explicit recompile required")
    return packet
