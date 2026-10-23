"""All18 private cells and all finite state configs are bounded before children."""
import json
from pathlib import Path

from answer_eval.protocol import EvaluationError
from search_comparison.engine_capture import resources
from .packet import identity
from .states import for_cell
from .update_capture import _view


def context(plan, packet_ref, baseline_ref, view, arm, registration):
    return {"implementation": identity(), "plan_sha256": plan.sha256, "source_identity": plan.bundle.identity,
            "view_identity": view["identity"], "arm": arm, "engine_resources": registration,
            "projection_identity": plan.projection["identity"], "registered_at": plan.value["registered_at"],
            "measurement_packet": packet_ref, "base_engine_capture": baseline_ref,
            "registered_resource_runtime": plan.value["resource_observer"],
            "input_refs": {"plan": {"path": str(plan.path), "sha256": plan.sha256}, "projection": plan.projection["reference"],
                           "view": view["binding"], "packet": packet_ref, "baseline": baseline_ref}}


def prepare_configs(ledger, plan, packet, view, arm):
    states = []
    for name in ("states/cold", "states/warm", "update/incremental", "update/fresh", "lifecycle/incremental", "lifecycle/fresh"):
        corpus = view if name.startswith("states/") else _view(view, ledger.owner / ("update/corpus" if name.startswith("update/") else "lifecycle/corpus"), view["documents"])
        states.append(for_cell(ledger, plan, corpus, arm, name))
    if ledger.kind == "goldmine":
        native = plan.views["native"]
        states.append(for_cell(ledger, plan, _view(native, ledger.owner / "dependency/corpus", native["documents"]), arm, "dependency/ocr"))
    sizes = []
    relevant = [s for s in packet["schedule"] if s["view"] == view["name"] and s["arm"] == arm]
    largest_slot = max(relevant, key=lambda s: len(json.dumps(s, ensure_ascii=False)))
    for state in states:
        # Actual finite state environment/configuration plus the largest fixed
        # slot and conservative1KiB request-path reserve. The real exact source
        # context is validated again by Commands before every child.
        binding = {"slot": largest_slot, "typed_request": {"path": str(ledger.owner / "requests" / "00001.json"), "sha256": "0" * 64},
                   "unexecuted_shape_reserve": "x" * 1024}
        for phase in ("prepare", "queries", "audit"):
            source = {**ledger.commands.context, "state": state["state"], "configuration_contract": state["configuration"],
                      "working_corpus": state["view"]["corpus"], "environment": state["env"], "phase": phase,
                      "label": "measurement.dependency.goldmine-ocr-absent", "measurement_operation": binding,
                      "resource_contract": "observer003-sampled-private-session-v1"}
            resources.expected_policy(ledger.owner, phase, source, fixture=ledger.commands.fixture)
            sizes.append({"state": state["state"], "phase": phase, "source_context_bytes": len(json.dumps(source, ensure_ascii=False, allow_nan=False).encode())})
    return {"status": "BOUNDED_BEFORE_ANY_CHILD", "limit_bytes": 1024 * 1024, "shapes": sizes,
            "maximum_source_context_bytes": max(r["source_context_bytes"] for r in sizes),
            "registration_scope": "full descriptor bytes verified against frozen plan via immutable artifact; no role omitted",
            "accounting_scope": "one whole cell includes every state/corpus/log/snapshot; shared read-only registrations and original assets external; no filesystem sandbox claim"}
