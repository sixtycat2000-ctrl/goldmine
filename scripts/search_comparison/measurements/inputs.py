"""Require the same frozen packet, actual base report, plan, source and runtime."""
from answer_eval.protocol import EvaluationError, timestamp
from search_comparison.engine_capture.plan import load_plan
from search_comparison.engine_capture.protocol import reference, read
from search_comparison.engine_capture.run import verify_capture
from .packet import load_packet
from .selection import ARMS, VIEWS


def load(packet_ref, baseline_ref):
    packet_path = reference(packet_ref, bound=8 * 1024 * 1024)
    raw = read(packet_path)
    plan = load_plan(raw["plan"]["path"], raw["plan"]["sha256"])
    packet = load_packet(packet_ref, plan)
    baseline = verify_capture(reference(baseline_ref))
    if (baseline["evidence_mode"] != "real" or baseline["plan"] != packet["plan"]
            or baseline["source_identity"] != packet["source_identity"]
            or baseline["projection_identity"] != packet["projection_identity"]):
        raise EvaluationError("measurements must reuse the identical real engine plan/source/dev projection")
    if [(s["view"], s["arm"]) for s in baseline["systems"]] != [(v, a) for v in VIEWS for a in ARMS]:
        raise EvaluationError("base report omitted a fixed native/common arm")
    # Selection is deterministic and query-only; enforce its registration
    # before any observed quality-phase command as a second ordering check.
    for system in baseline["systems"]:
        for row in system.get("resource_observations", []):
            if row.get("disposition") == "validated":
                from acceptance_lib.evidence import read_command_run
                _, receipt, _ = read_command_run(reference(row["command"]))
                if timestamp(receipt["started_at"], "base phase start") < timestamp(packet["registered_at"], "measurement preregistration"):
                    raise EvaluationError("measurement selection was not preregistered before base engine execution")
    return plan, packet, baseline
