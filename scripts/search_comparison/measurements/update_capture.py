"""Execute the fixed112+10 and deletion/replacement operations, retaining all19."""
from copy import deepcopy
from pathlib import Path

from answer_eval.protocol import EvaluationError, parse_json
from search_comparison.engine_capture.run import _prepare
from . import audits, updates
from .recipes import ordinary_update, operations
from .states import for_cell, changed_configuration
from .performance import ERRORS


def entries(plan, view):
    return [{**d, "input": str(Path(view["corpus"]) / d["view_path"]), "kind": plan.bundle.sources[d["source_path"]]["kind"]}
            for d in view["documents"]]


def _view(original, corpus, rows):
    return {**original, "corpus": str(corpus), "documents": [{k: r[k] for k in ("source_path", "view_path", "sha256")} for r in rows]}


def _source(ledger, state, rows, changed, *, remove=False):
    changed_configuration(ledger, state)
    return ledger.state("source_remove" if remove else "source_write", {"corpus": state["view"]["corpus"], "view": state["view"]["name"],
                        "entries": rows, "changed": changed})


def _prepare_or_update(ledger, state, plan, arm, *, fresh):
    changed_configuration(ledger, state)
    start = len(ledger.events)
    if fresh:
        class PrepareCalls:
            def call(self, label, argv, *, phase="prepare"):
                return ledger.call(label, argv, phase=phase)
        _prepare(ledger.kind, arm, PrepareCalls(), state["values"], state["view"], plan.value["engines"][ledger.kind])
    else:
        for n, argv in enumerate(ordinary_update(arm, state["values"]), 1):
            raw = ledger.call("measurement." + ledger.kind + ".ordinary-update." + str(n), argv, phase="prepare")
            # Preserve actual engine-native counters independently of our DB
            # comparison. No stdout-derived claim substitutes for readback.
            if ledger.kind == "goldmine":
                parse_json(raw.decode("utf-8"), "ordinary Goldmine ingestion JSON")
    return list(range(start + 1, len(ledger.events) + 1))


def _coverage(value, engine):
    expected = {r["source_path"]: r for r in value["expected"]}
    if value["logical"] is None:
        return {"status": "MISSING", "expected_sources": len(expected), "error": value["error"], "snapshot_retained": value["snapshot"]}
    observed = value["logical"]["sources"]
    if engine == "goldmine":
        missing, stale = sorted(set(expected) - set(observed)), sorted(set(observed) - set(expected))
        changed = sorted(p for p in set(observed) & set(expected) if observed[p]["metadata"]["sha256"] != expected[p]["sha256"])
    else:
        membership = value["logical"]["active_membership"]
        missing, stale, changed = membership["missing"], membership["stale_active"], []
    return {"expected_sources": len(expected), "observed_source_signatures": len(observed), "missing": missing,
            "stale_active": stale, "wrong_indexed_source_sha": changed,
            "capabilities": {p: r["capability"] for p, r in observed.items()},
            "status": "FAIL" if stale or changed or engine != "recoll" and missing else "CAPTURED",
            "extraction_coverage_is_separate_from_file_ranking": True}


def compare_stages(left, right):
    result = updates.compare(left, right)
    if left["engine"] == "goldmine":
        if "query_index" not in left or "query_index" not in right:
            raise EvaluationError("actual source-anchored FTS/CJK/lookup/excerpt signatures absent")
        shared = set(left["query_index"]) & set(right["query_index"])
        changes = sorted(p for p in shared if left["query_index"][p] != right["query_index"][p])
        result["changes"]["query_index"] = changes
        result["counts"]["query_index"] = len(changes)
        if changes:
            result["status"] = "LOGICAL_DIFFERENCES_OBSERVED"
    return result


def execute(ledger, plan, view, arm, cohorts):
    all_rows = entries(plan, view)
    by_path = {r["source_path"]: r for r in all_rows}
    base = [by_path[p] for p in cohorts["base112"]]
    adds = [by_path[p] for p in cohorts["add10"]]
    full = base + adds
    removed = [r for r in full if r["source_path"] != cohorts["remove"]]
    replacement = deepcopy(full)
    donor = by_path[cohorts["replace_donor"]]
    for row in replacement:
        if row["source_path"] == cohorts["replace_target"]:
            row.update(input=donor["input"], sha256=donor["sha256"])
    states, stages, comparisons, dispositions = {}, {}, {}, []
    source_events = {}
    for ordinal, op in enumerate(operations(cohorts), 1):
        row = {"ordinal": ordinal, "operation": op, "status": "CAPTURED", "error": None, "events": [], "result": None}
        start = len(ledger.events)
        try:
            if ledger.commands.blocked or ledger.commands.interrupted:
                raise EvaluationError("owned cell interrupted/blocked; fixed operation retained without a child")
            kind = op["kind"]
            if kind == "materialize_base":
                states["inc"] = for_cell(ledger, plan, _view(view, ledger.owner / "update/corpus", base), arm, "update/incremental")
                source_events["base112"] = _source(ledger, states["inc"], base, base)
                row["result"] = source_events["base112"]
            elif kind == "add_unchanged_sources":
                states["inc"]["view"] = _view(view, ledger.owner / "update/corpus", full)
                source_events["incremental122"] = _source(ledger, states["inc"], full, adds)
                row["result"] = source_events["incremental122"]
            elif kind == "separate_lifecycle_copy_prepare":
                states["life"] = for_cell(ledger, plan, _view(view, ledger.owner / "lifecycle/corpus", full), arm, "lifecycle/incremental")
                source_events["lifecycle122"] = _source(ledger, states["life"], full, full)
                _prepare_or_update(ledger, states["life"], plan, arm, fresh=True)
                stages["lifecycle122"] = audits.stage(ledger, states["life"], "lifecycle122", full, full)
                row["result"] = {"source": source_events["lifecycle122"], "coverage": _coverage(stages["lifecycle122"], ledger.kind)}
                row["status"] = row["result"]["coverage"]["status"]
            elif kind == "remove_owned_source":
                states["life"]["view"] = _view(view, ledger.owner / "lifecycle/corpus", removed)
                row["result"] = _source(ledger, states["life"], removed, [by_path[cohorts["remove"]]], remove=True)
            elif kind == "restore_original_and_replace_owned_source":
                states["life"]["view"] = _view(view, ledger.owner / "lifecycle/corpus", replacement)
                changed = [r for r in replacement if r["source_path"] in {cohorts["remove"], cohorts["replace_target"]}]
                row["result"] = _source(ledger, states["life"], replacement, changed)
            elif kind in {"ordinary_prepare", "ordinary_update"}:
                key = "life" if op["stage"] in {"removed121", "replaced122"} else "inc"
                row["result"] = {"engine_event_ordinals": _prepare_or_update(ledger, states[key], plan, arm, fresh=kind == "ordinary_prepare")}
            elif kind == "independent_fresh_prepare_same_physical_corpus":
                key = "fresh" if op["stage"] == "fresh122" else "life_fresh"
                original = states["inc" if key == "fresh" else "life"]
                name = "update/fresh" if key == "fresh" else "lifecycle/fresh"
                states[key] = for_cell(ledger, plan, original["view"], arm, name)
                row["result"] = {"engine_event_ordinals": _prepare_or_update(ledger, states[key], plan, arm, fresh=True)}
            elif kind == "readonly_audit_and_closed_snapshot":
                name = op["stage"]
                key = {"base112": "inc", "incremental122": "inc", "fresh122": "fresh", "removed121": "life", "replaced122": "life", "replacement_fresh122": "life_fresh"}[name]
                expected = base if name == "base112" else removed if name == "removed121" else replacement if name in {"replaced122", "replacement_fresh122"} else full
                known = replacement if name in {"replaced122", "replacement_fresh122"} else full
                stages[name] = audits.stage(ledger, states[key], name, known, expected)
                row["result"] = _coverage(stages[name], ledger.kind)
                row["status"] = row["result"]["status"]
            elif kind == "compare_logical_signatures":
                left, right = stages[op["left"]], stages[op["right"]]
                if left["logical"] is None or right["logical"] is None:
                    raise EvaluationError("logical stage readback incomplete; retained snapshots/raw cannot be relabeled equal")
                if left["source_witness"] != right["source_witness"]:
                    raise EvaluationError("incremental/fresh inputs are not the same physical corpus/bytes/metadata")
                result = compare_stages(left["logical"], right["logical"])
                comparisons[op["left"]] = result
                row["result"] = result
                row["status"] = "CAPTURED" if result["status"] == "LOGICAL_EQUAL" else "FAIL"
            else:
                raise EvaluationError("unknown fixed update operation")
        except ERRORS as exc:
            row.update(status="MISSING", error=str(exc))
        row["events"] = list(range(start + 1, len(ledger.events) + 1))
        dispositions.append(row)
    return {"operations": dispositions, "stages": stages, "comparisons": comparisons,
            "status": "FAIL" if any(r["status"] == "FAIL" for r in dispositions) else "CAPTURED" if all(r["status"] == "CAPTURED" for r in dispositions) else "MISSING",
            "denominator": {"operations": 19, "base": 112, "added": 10, "full": 122, "removed": 121},
            "counter_authority": "actual full index membership/body/vector/semantic-info readback; native stdout retained independently"}
