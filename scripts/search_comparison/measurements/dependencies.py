"""Ordinary dependency failures are isolated from quality and latency samples."""
from pathlib import Path
import sys

from answer_eval.protocol import EvaluationError, parse_json, sha256_file
from search_comparison.source_views.run import write_new
from search_comparison.engine_capture.protocol import read
from search_comparison.engine_capture.recipes import compile_recipe
from . import audits
from .performance import ERRORS
from .states import for_cell, changed_configuration
from .update_capture import entries, _view


def execute(ledger, plan, view, arm, registration):
    scenario = {"goldmine": "goldmine-ocr-absent", "qmd": "qmd-embedding-absent", "recoll": "recoll-indexer-absent"}[ledger.kind]
    row = {"scenario": scenario, "status": "MISSING", "error": None, "probe": None, "stage": None,
           "trial_input_view": "native" if ledger.kind == "goldmine" else view["name"],
           "scope": "separate registered engine capability experience; precomputed common conversion and timed arms are unchanged",
           "optional_filter_mask": "CONDITIONAL_MISSING_NO_REVIEWED_EFFECTIVE_FILTER_MASK" if ledger.kind == "recoll" else None}
    try:
        if ledger.commands.blocked or ledger.commands.interrupted:
            raise EvaluationError("resource block/interruption; fixed dependency scenario retained without child")
        state = None
        if ledger.kind == "goldmine":
            # Source-only deterministic first screenshot, no question/quality
            # dependence. Native pixels only: common is a plain-text view.
            native = plan.views.get("native")
            if native is None:
                raise EvaluationError("native OCR dependency experience has no complete native view")
            candidates = sorted((r for r in entries(plan, native) if r["kind"] == "screenshot"), key=lambda r: r["source_path"])
            chosen = candidates[:1]
            if not chosen:
                raise EvaluationError("fixed source-only OCR trial requires an actual screenshot")
            state = for_cell(ledger, plan, _view(native, ledger.owner / "dependency/corpus", chosen), arm, "dependency/ocr")
            changed_configuration(ledger, state)
            ledger.state("source_write", {"corpus": state["view"]["corpus"], "view": "native", "entries": chosen, "changed": chosen})
            ledger.commands.env = {**state["env"], "GOLDMINE_OCR_HELPER": str(ledger.owner / "absent-dependency/not-provisioned"), "GOLDMINE_OCR_AUTOCOMPILE": "0"}
        request = {"schema": 1, "owner": str(ledger.owner), "scenario": scenario, "arm": arm, "registration": registration,
                   "installation": plan.engines[arm]["installation"], "configuration": state["values"]["goldmine_config"] if state else None}
        path = ledger.owner / "dependency-request.json"
        if ledger.replay is None:
            write_new(path, request)
        elif read(path) != request:
            raise EvaluationError("fixed missing-dependency request differs")
        ref = {"path": str(path), "sha256": sha256_file(path)}
        python = state["values"]["python"] if state else str(Path(sys.executable).absolute())
        worker = "_ocr_unavailable.py" if state else "_dependency_worker.py"
        argv = [python, "-I", "-B", str(Path(__file__).with_name(worker)), str(path), ref["sha256"]]
        if state:
            argv.append(state["values"]["installation"])
        raw = ledger.call("measurement.dependency." + scenario, argv, binding={"dependency_request": ref})
        row["probe"] = parse_json(raw.decode("utf-8"), "actual missing-role probe")
        expected_status = "NOT_AFFECTED_UNUSED_ROLE" if arm == "qmd.search" else "EXPECTED_MISSING"
        if row["probe"].get("status") != expected_status:
            raise EvaluationError("actual dependency disposition differs from registered scenario")
        if state:
            ledger.call("measurement.dependency.ordinary-ingest", compile_recipe("goldmine.prepare", state["values"]), phase="prepare", binding={"dependency_request": ref})
            row["stage"] = audits.stage(ledger, state, "dependency_ocr", chosen, chosen)
            actual = row["stage"]["raw"]
            if actual is None or len(actual["stable"]["sources"]) != 1:
                raise EvaluationError("ordinary missing-OCR ingestion lacks actual one-source metadata readback")
            source = next(iter(actual["stable"]["sources"].values()))
            info = parse_json(source["file"]["info"], "actual missing-OCR source info")
            if info.get("ocr") != "unavailable" or not info.get("ocr_reason") or any(p["kind"] == "ocr" and p["text"] for p in source["parts"]):
                raise EvaluationError("missing OCR ordinary ingest did not preserve truthful capability/reason")
        row["status"] = "CAPTURED_EXPECTED_EXPERIENCE"
    except ERRORS as exc:
        row["error"] = str(exc)
    return row
