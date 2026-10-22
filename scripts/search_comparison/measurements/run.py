"""Actual typed execution, followed by the identical read-only raw replay."""
from datetime import datetime, timezone
from pathlib import Path
import uuid

from answer_eval.protocol import EvaluationError, canonical_sha256, sha256_file
from search_comparison.source_views.run import owned_output, write_new
from search_comparison.engine_capture.protocol import inventory, read, reference, LIMITS
from search_comparison.engine_capture.views import verify_view
from search_comparison.engine_capture.plan import engine_resources
from . import configuration, dependencies, performance, registered, slots, storage, timing, update_capture
from .inputs import load
from .ledger import Ledger
from .packet import identity
from .selection import ARMS, VIEWS
from .preparation import first_preparation

SUITE = "comparison.measurements/v1"
ERRORS = (EvaluationError, OSError, UnicodeError, ValueError, TypeError, KeyError)


def _header(owner, packet_ref, baseline_ref, plan, *, replay=False):
    expected = {"schema": 1, "suite_id": SUITE, "owner": str(owner), "packet": packet_ref, "baseline": baseline_ref}
    path = owner / "ownership.json"
    if not replay:
        write_new(path, {**expected, "nonce": str(uuid.uuid4())})
    actual = read(path)
    if {k: v for k, v in actual.items() if k != "nonce"} != expected or str(uuid.UUID(actual["nonce"])) != actual["nonce"]:
        raise EvaluationError("measurement owner/packet/baseline registration changed")
    return registered.registration(owner, plan, replay=replay)


def _cell(plan, packet, packet_ref, baseline_ref, view, arm, owner, binding, *, saved=None):
    marker = {"schema": 1, "suite_id": SUITE, "owner": str(owner), "view": view, "arm": arm, "packet_sha256": packet_ref["sha256"]}
    if saved is None:
        owner.mkdir(parents=True)
        (owner / "cwd").mkdir()
        write_new(owner / "cell-ownership.json", marker)
    elif read(owner / "cell-ownership.json") != marker:
        raise EvaluationError("owned cell marker changed")
    original = plan.views.get(view)
    env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
    context = configuration.context(plan, packet_ref, baseline_ref, original, arm, binding) if original else {
        "implementation": identity(), "registered_at": plan.value["registered_at"], "arm": arm, "engine_resources": binding}
    cell = Ledger(owner, env, context, arm.split(".")[0], replay=saved)
    return cell


def _admin_artifacts(owner):
    # Cell contents have their own complete bounded inventory shards. The root
    # manifest binds every such shard, rather than repeating tens of thousands
    # of raw command artifact entries in one JSON document.
    files = {p: row for p, row in inventory(owner).items() if not p.startswith("cells/") and p != "report.json"}
    expected = {"ownership.json", "slots.jsonl", *["registered-assets/" + n + ".json" for n in registered.ENGINES]}
    if set(files) != expected:
        raise EvaluationError("root administrative registry has omitted/extra files")
    return {p: {**row, "allocated_bytes": (Path(owner) / p).stat().st_blocks * 512} for p, row in files.items()}


def _execute(owner, packet_ref, baseline_ref, plan, packet, baseline, *, saved=None):
    bindings = _header(owner, packet_ref, baseline_ref, plan, replay=saved is not None)
    matrix = [(v, a) for v in VIEWS for a in ARMS]
    saved_cells = {} if saved is None else {pair: storage.load_cell(ref, owner / "cells" / pair[0] / pair[1]) for pair, ref in zip(matrix, saved["systems"])}
    bases = {(s["view"], s["arm"]): s for s in baseline["systems"]}
    cells, runtimes, setup, outcomes = {}, {}, {}, {}
    # Finish every finite context/asset shape preflight before first child.
    for view, arm in matrix:
        pair, name = (view, arm), arm.split(".")[0]
        cell = _cell(plan, packet, packet_ref, baseline_ref, view, arm, owner / "cells" / view / arm, bindings[name], saved=saved_cells.get(pair))
        cells[pair] = cell
        reason = plan.missing.get(view) or plan.missing.get(arm)
        if not reason:
            try:
                registered.descriptor(bindings[name], plan)
                setup[pair] = configuration.prepare_configs(cell, plan, packet, plan.views[view], arm)
            except ERRORS as exc:
                reason = str(exc)
        runtimes[pair] = {"error": reason, "states": None}
        outcomes[pair] = {"view": view, "arm": arm, "setup_error": reason, "preflight": setup.get(pair),
                          "first_preparation": first_preparation(bases[pair], name)}
    interrupted = False
    for view, arm in matrix:
        pair, cell = (view, arm), cells[(view, arm)]
        if interrupted:
            runtimes[pair]["error"] = "capture interrupted; no subsequent cell child launched"
        if runtimes[pair]["error"] is None:
            try:
                runtimes[pair] = performance.prepare(cell, plan, plan.views[view], arm, bases[pair])
            except ERRORS as exc:
                runtimes[pair]["error"] = str(exc)
        interrupted = interrupted or cell.commands.interrupted
    requests = {r["id"]: r for r in packet["selection"]["requests"]}
    observations, slot_refs = [], []
    for slot in packet["schedule"]:
        pair, cell = (slot["view"], slot["arm"]), cells[(slot["view"], slot["arm"])]
        if interrupted:
            runtimes[pair]["error"] = "capture interrupted; remaining fixed slots retained without children"
        row = performance.attempt(cell, runtimes[pair], slot, requests[slot["id"]], plan.views.get(slot["view"]), observations)
        row = storage.result(cell, slot, row)
        row, slot_ref = slots.retain(cell, slot, row)
        observations.append(row)
        slot_refs.append(slot_ref)
        if row.get("storage_error"):
            runtimes[pair]["error"] = row["storage_error"]
        interrupted = interrupted or cell.commands.interrupted
    systems, accounting = [], []
    for view, arm in matrix:
        pair, cell = (view, arm), cells[(view, arm)]
        performance_result = (performance.finish(cell, runtimes[pair], plan) if not interrupted else
                              {"status": "MISSING", "error": "global capture interrupted; no finalizer child launched", "final_snapshots": []})
        reason = outcomes[pair]["setup_error"]
        if reason or interrupted:
            reason = reason or "capture interrupted; update/dependency phases retained unexecuted"
            update = {"status": "MISSING", "error": reason, "stages": {}, "operations": [{"ordinal": n, "operation": op, "status": "MISSING", "error": reason, "events": [], "result": None} for n, op in enumerate(packet["update_operations"], 1)]}
            dependency = {"status": "MISSING", "error": reason, "stage": None}
        else:
            update = update_capture.execute(cell, plan, plan.views[view], arm, packet["source_cohorts"])
            dependency = dependencies.execute(cell, plan, plan.views[view], arm, bindings[cell.kind])
        value = {**outcomes[pair], "performance": performance_result, "updates": update, "dependency": dependency, **cell.finish()}
        ref = storage.seal_cell(cell, value)
        systems.append(ref)
        census = storage.disk(cell.owner)
        accounting.append({"view": view, "arm": arm, **census, "status": "FAIL" if max(census["logical_bytes"], census["allocated_bytes"]) > LIMITS["owned_bytes"] else "PASS"})
        outcomes[pair] = value
        interrupted = interrupted or cell.commands.interrupted
    # Failure remains a complete MISSING report, including all fixed slots,
    # instead of aborting publication and retaining only a successful prefix.
    integrity_errors = []
    checks = [("source bundle", lambda: plan.bundle.verify())]
    checks.extend(("source view " + name, lambda view=view: verify_view(view)) for name, view in plan.views.items())
    checks.extend(("asset registration " + name, lambda binding=binding: registered.descriptor(binding, plan)) for name, binding in bindings.items())
    for arm, paths in plan.engines.items():
        def check_engine(arm=arm, paths=paths):
            if engine_resources(arm.split(".")[0], plan.value["engines"][arm.split(".")[0]], fixture=False, arm=arm) != paths:
                raise EvaluationError("frozen resolved engine paths changed")
        checks.append(("engine " + arm, check_engine))
    for label, check in checks:
        try:
            check()
        except ERRORS as exc:
            integrity_errors.append(label + ": " + str(exc))
    try:
        if identity() != packet["implementation"]:
            integrity_errors.append("current implementation content changed after preregistration")
    except ERRORS as exc:
        integrity_errors.append("current implementation unavailable: " + str(exc))
    rows_ref = slots.index(owner, packet["schedule"], slot_refs, replay=saved is not None)
    metrics = timing.summary(packet["selection"], packet["schedule"], observations)
    for summary in metrics["cells"]:
        guard = outcomes[(summary["view"], summary["arm"])]["performance"]
        summary["source_integrity_status"] = guard["status"]
        if guard["status"] != "CAPTURED" or integrity_errors:
            summary["complete_service_latency"] = None
            summary["authentication_error"] = guard["error"] or "; ".join(integrity_errors)
    resource = ("FAIL" if baseline.get("resource_status") == "FAIL" or any(v["resource_status"] == "FAIL" for v in outcomes.values()) or any(r["status"] == "FAIL" for r in accounting)
                else "PASS" if baseline.get("resource_status") == "PASS" and all(v["resource_status"] == "PASS" for v in outcomes.values()) else "MISSING")
    complete = (not integrity_errors and all(r["status"] == "OK" for r in observations) and all(v["performance"]["status"] == "CAPTURED" and v["updates"]["status"] == "CAPTURED"
                and v["dependency"]["status"] == "CAPTURED_EXPECTED_EXPERIENCE" and v["first_preparation"]["status"] == "CAPTURED" for v in outcomes.values()))
    return storage.root_accounting({"schema": 2, "suite_id": SUITE, "owner": str(owner), "packet": packet_ref, "baseline": baseline_ref,
            "implementation": packet["implementation"], "systems": systems, "fixed_matrix": [[v, a] for v, a in matrix], "slots": rows_ref,
            "denominators": packet["denominators"], "timing": metrics, "cell_post_publication_accounting": accounting,
            "resource_status": resource, "status": "CAPTURED_REQUIRED_MEASUREMENTS" if complete and resource == "PASS" else "MISSING",
            "full_comparison_status": "CAPTURED_REQUIRED_DEV_COMPARISON" if complete and resource == "PASS" and baseline["status"] == "CAPTURED_DEV_RESULTS" else "MISSING",
            "sampled_resource_contract": "observer003 sampled_resource_status; all raw gaps/lifetime caveats retained",
            "final_integrity": {"status": "MISSING" if integrity_errors else "VERIFIED", "errors": integrity_errors},
            "resident_track": packet["resident_track"], "quality_evidence": False, "release_eligible": False,
            "artifacts": _admin_artifacts(owner)})


def capture(packet_ref, baseline_ref, output, parent):
    plan, packet, baseline = load(packet_ref, baseline_ref)
    owner = owned_output(output, parent, plan.bundle)
    protected = [plan.path, reference(packet_ref), reference(baseline_ref).parent, *[Path(v["corpus"]) for v in plan.views.values()]]
    for paths in plan.engines.values():
        protected.extend(Path(p) for p in paths.values() if isinstance(p, str) and Path(p).is_absolute())
    if any(owner.is_relative_to(p) or p.is_relative_to(owner) for p in protected):
        raise EvaluationError("measurement output overlaps protected source/packet/baseline/engine inputs")
    owner.mkdir()
    report = _execute(owner, packet_ref, baseline_ref, plan, packet, baseline)
    write_new(owner / "report.json", report)
    return report


def verify(report_path):
    path = reference({"path": str(Path(report_path).absolute()), "sha256": sha256_file(Path(report_path))}, bound=8 * 1024 * 1024)
    saved = read(path)
    owner = path.parent
    if (saved.get("owner") != str(owner) or saved.get("suite_id") != SUITE or type(saved.get("schema")) is not int or saved.get("schema") != 2
            or saved.get("implementation") != identity() or saved.get("quality_evidence") is not False or saved.get("release_eligible") is not False
            or saved.get("fixed_matrix") != [[v, a] for v in VIEWS for a in ARMS] or len(saved.get("systems", [])) != 18
            or saved.get("artifacts") != _admin_artifacts(owner)):
        raise EvaluationError("measurement schema/code/complete artifact/cell registry changed")
    plan, packet, baseline = load(saved["packet"], saved["baseline"])
    slots.entries(saved["slots"], packet["schedule"], owner)
    actual = _execute(owner, saved["packet"], saved["baseline"], plan, packet, baseline, saved=saved)
    if canonical_sha256(actual) != canonical_sha256(saved):
        raise EvaluationError("measurement result differs from complete raw/disk replay")
    return saved
