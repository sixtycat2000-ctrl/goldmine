"""The complete preregistered schedule; no retries or successful-subset timing."""
from pathlib import Path

from answer_eval.protocol import EvaluationError, sha256_file
from search_comparison.adapters import DocumentMap
from search_comparison.engine_capture.native import readback as native_readback
from search_comparison.engine_capture.results import parse_result
from .filesystem import snapshot_ref, verify_index_snapshot
from .recipes import query
from .states import for_cell, index, changed_configuration

ERRORS = (EvaluationError, OSError, UnicodeError, ValueError, TypeError, KeyError)


def prepare(cell, plan, view, arm, baseline):
    """Only closed preparation can seed timing, irrespective of quality scores."""
    if baseline.get("preparation") is None or baseline.get("prepared_snapshot") is None:
        raise EvaluationError("actual first preparation/pre-query snapshot unavailable")
    expected = baseline["prepared_snapshot"]
    full = snapshot_ref(Path(expected["path"]))
    if full["files"] != expected["files"]:
        raise EvaluationError("base prepared snapshot changed")
    states = {regime: for_cell(cell, plan, view, arm, "states/" + regime)
              for regime in ("cold", "warm")}
    for state in states.values():
        changed_configuration(cell, state)
        if cell.kind == "goldmine":
            native_readback(cell, state["values"], plan.value["engines"][cell.kind])
        clone = cell.state("index_clone", {"index": index(state, cell.kind), "source": full, "reset_cold": False}, binding={"initialize": state["state"]})
        if clone.get("cache") not in (None, []):
            raise EvaluationError("pre-query prepared QMD cache is not empty; no silent cache deletion")
        state["baseline_witness"] = clone
    return {"states": states, "snapshot": full, "prepared": baseline["preparation"], "error": None, "final": []}


def missing(slot, reason):
    return {"ordinal": slot["ordinal"], "id": slot["id"], "status": "MISSING", "wall_ms": None,
            "command": None, "resource_status": "MISSING", "error": reason, "result": None,
            "pre_index": None, "post_index": None, "clock": None}


def attempt(cell, runtime, slot, request, view, observations):
    row = missing(slot, runtime.get("error"))
    if runtime.get("error"):
        return row
    state = runtime["states"]["cold" if slot["regime"] == "cold_cli" else "warm"]
    changed_configuration(cell, state)
    query_event = None
    try:
        if slot["restore_pre_query_snapshot"]:
            restored = cell.state("index_clone", {"index": index(state, cell.kind), "source": runtime["snapshot"], "reset_cold": True}, binding=slot)
            if restored.get("cache") not in (None, []):
                raise EvaluationError("cold pre-query cache was not empty")
        before = cell.state("index_witness", {"index": index(state, cell.kind)}, binding={"slot": slot, "edge": "before"})
        row["pre_index"] = before
        if before["stable_sha256"] != state["baseline_witness"]["stable_sha256"]:
            runtime["error"] = "working content/vector index changed before a registered query"
            raise EvaluationError(runtime["error"])
        if slot["prime_ordinal"] is not None:
            prime = observations[slot["prime_ordinal"] - 1]
            if prime["status"] != "OK":
                raise EvaluationError("warm sample's immediate same-query prime failed; fixed sample retained unmeasured")
            if prime["post_index"] != before:
                runtime["error"] = "warm pre-query index/cache differs from the exact same-query prime post-state"
                raise EvaluationError(runtime["error"])
        try:
            raw = cell.call("measurement.query." + slot["arm"], query(slot["arm"], state["values"], request["q"]), phase="queries", binding=slot)
        finally:
            query_event = cell.events[-1] if cell.events and cell.events[-1]["phase"] == "queries" else None
        mapping = DocumentMap(view["corpus"], view["documents"], collection="sources", index="comparison")
        result = parse_result(raw.decode("utf-8"), slot["arm"], mapping, runtime["prepared"])
        row["result"] = result
        row["status"] = "FAIL" if result["window"]["status"] == "TRUNCATED" or result["treatment"]["status"] == "DEGRADED" else "OK"
        row["error"] = None if row["status"] == "OK" else "registered service output was truncated/degraded; native result retained"
    except ERRORS as exc:
        row["error"] = str(exc)
        row["status"] = "MISSING" if query_event is None else "PARSE_ERROR" if query_event["error"] is None else "FAIL"
    if query_event is not None:
        clock = query_event["clock"]
        observation = (query_event["disposition"] or {}).get("observation", {})
        row.update(command=query_event["command"], clock=clock, wall_ms=clock["wall_ms"] if clock else None,
                   resource_status=observation.get("sampled_resource_status", "MISSING"))
        if clock and clock["timed_out"]:
            row["status"] = "TIMEOUT"
    if row["pre_index"] is not None and not cell.commands.blocked:
        try:
            after = cell.state("index_witness", {"index": index(state, cell.kind)}, binding={"slot": slot, "edge": "after"})
            row["post_index"] = after
            if after["stable_sha256"] != row["pre_index"]["stable_sha256"]:
                runtime["error"] = "actual query mutated content/vector index; later cell slots remain MISSING"
                row.update(status="FAIL", error=runtime["error"])
        except ERRORS as exc:
            runtime["error"] = str(exc)
            row.update(status="MISSING", error=str(exc))
    if cell.commands.blocked or cell.commands.interrupted:
        runtime["error"] = "resource proof/budget failed or capture interrupted; subsequent cell slots not launched"
    return row


def finish(cell, runtime, plan):
    final = []
    if runtime.get("states") and not cell.commands.blocked and not cell.commands.interrupted:
        for regime, state in runtime["states"].items():
            try:
                changed_configuration(cell, state)
                if cell.kind == "goldmine":
                    native_readback(cell, state["values"], plan.value["engines"][cell.kind], after=True)
                snap = cell.state("index_snapshot", {"index": index(state, cell.kind), "stage": "performance_" + regime + "_final"})
                verify_index_snapshot(snap["snapshot"], snap["witness"], cell.kind)
                if snap["witness"]["stable_sha256"] != state["baseline_witness"]["stable_sha256"]:
                    raise EvaluationError("final working index differs from the actual prepared content")
                final.append({"regime": regime, **snap})
            except ERRORS as exc:
                runtime["error"] = str(exc)
    return {"status": "CAPTURED" if runtime.get("states") and runtime.get("error") is None else "MISSING",
            "error": runtime.get("error"), "final_snapshots": final,
            "snapshot": runtime.get("snapshot"), "resident_latency": "CONDITIONAL_MISSING_NO_MATCHING_RESIDENT_ADAPTER",
            "model_startup_breakdown": "NOT_SEPARABLE_FROM_CLI_TOTAL", "idle_resident_rss": None,
            "definition": "fresh CLI/app-cache regimes; no OS-cache flush or loaded-model-residency claim"}
