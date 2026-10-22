"""First preparation costs come from the authenticated base capture, never repeats."""
from pathlib import Path

from acceptance_lib.evidence import read_command_run
from answer_eval.protocol import EvaluationError
from search_comparison.engine_capture.protocol import SUITE, reference
from .filesystem import verify_snapshot, snapshot_ref

PREPARE = {"goldmine": {"goldmine.ingest"}, "qmd": {"qmd.collection-add", "qmd.embed"}, "recoll": {"recoll.index"}}


def first_preparation(system, engine):
    events = []
    for row in system.get("resource_observations", []):
        if row.get("label") not in PREPARE[engine]:
            continue
        ref = row.get("command")
        event = {"label": row["label"], "command": ref, "wall_ms": None, "exit_code": None,
                 "timed_out": None, "resource_observation": row.get("observation"), "error": row.get("error")}
        try:
            _, receipt, _ = read_command_run(reference(ref), check_id="comparison.command." + row["label"], suite_id=SUITE)
            event.update(wall_ms=receipt.get("wall_ms"), exit_code=receipt["exit_code"], timed_out=receipt["timed_out"])
        except (EvaluationError, OSError, ValueError, TypeError, KeyError) as exc:
            event["error"] = str(exc)
        events.append(event)
    snapshot = system.get("prepared_snapshot")
    disk = None
    if snapshot is not None:
        actual = snapshot_ref(Path(snapshot["path"]))
        if actual["files"] != snapshot["files"]:
            raise EvaluationError("base first-prepare index snapshot changed")
        disk = {"snapshot": actual, "logical_bytes": sum(r["bytes"] for r in actual["files"].values()),
                "allocated_bytes": sum((Path(snapshot["path"]) / p).stat().st_blocks * 512 for p in actual["files"])}
    observations = [r["resource_observation"] for r in events]
    complete = (bool(events) and disk is not None and system.get("preparation") is not None
                and all(r["exit_code"] == 0 and not r["timed_out"] and r["wall_ms"] is not None and r["error"] is None for r in events))
    return {"status": "CAPTURED" if complete else "MISSING", "events": events, "prepared_index_disk": disk,
            "phase_wall_ms_sum": sum(r["wall_ms"] for r in events) if complete else None,
            "sampled_target_tree_rss_bytes": max((r.get("sampled_target_tree_rss_bytes") or 0 for r in observations if r), default=None),
            "resource_status": "FAIL" if any(r and r["sampled_resource_status"] == "FAIL" for r in observations) else "PASS" if observations and all(r and r["sampled_resource_status"] == "PASS" for r in observations) else "MISSING",
            "excluded_phases": "source conversion/provisioning/runtime-only probes/readback/snapshot copy; raw base commands retained",
            "rss_definition": "maximum of actual simultaneous sampled target-tree RSS over sequential preparation phases; not sum of independent process maxima",
            "model_startup_breakdown": "NOT_SEPARABLE_FROM_CLI_TOTAL", "kernel_lifetime_tree_peak": None}
