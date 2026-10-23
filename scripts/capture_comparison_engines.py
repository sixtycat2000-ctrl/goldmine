#!/usr/bin/env python3
"""Capture registered dev engines. No holdout selector, downloads or free argv."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from answer_eval.protocol import EvaluationError
from search_comparison.engine_capture.protocol import read, reference


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    for name in ("export-dev", "export-worker"):
        s = sub.add_parser(name)
        s.add_argument("--bundle", required=True, type=Path)
        s.add_argument("--manifest-sha256", required=True)
        s.add_argument("--out", required=True, type=Path)
        if name == "export-dev":
            s.add_argument("--parent", required=True, type=Path)
        s.add_argument("--fixture", action="store_true")
    for name in ("capture", "preflight"):
        s = sub.add_parser(name)
        s.add_argument("--plan", required=True, type=Path)
        s.add_argument("--plan-sha256", required=True)
        if name == "capture":
            s.add_argument("--out", required=True, type=Path)
            s.add_argument("--parent", required=True, type=Path)
        s.add_argument("--fixture", action="store_true")
    s = sub.add_parser("verify")
    s.add_argument("--report", required=True, type=Path)
    for name in ("qmd-canary", "recoll-canary"):
        s = sub.add_parser(name)
        s.add_argument("--engine", required=True, type=Path)
        s.add_argument("--engine-sha256", required=True)
        s.add_argument("--out", required=True, type=Path)
        s.add_argument("--parent", required=True, type=Path)
        s.add_argument("--fixture", action="store_true")
        if name == "qmd-canary":
            s.add_argument("--mode", choices=("query", "vsearch"), default="query")
    a = p.parse_args(argv)
    try:
        if a.action.startswith("export"):
            from search_comparison.engine_capture.projection import capture_export, export_worker
            if a.action == "export-dev":
                value = capture_export(a.bundle, a.manifest_sha256, a.out, a.parent, fixture=a.fixture)
            else:
                value = export_worker(a.bundle, a.manifest_sha256, a.out, fixture=a.fixture)
        elif a.action == "preflight":
            from search_comparison.engine_capture.plan import load_plan
            plan = load_plan(a.plan, a.plan_sha256, fixture=a.fixture)
            value = {"status": "MISSING" if plan.missing else "INPUTS_READY_FOR_INTERMEDIATE_CAPTURE", "missing": plan.missing,
                     "n_fixed_queries": len(plan.projection["requests"]), "source_count": len(plan.bundle.sources), "index_queries_executed": 0,
                     "full_comparison_status": "MISSING_PERFORMANCE_UPDATE_RESOURCE_PHASES", "release_eligible": False}
        elif a.action in ("capture", "verify"):
            from search_comparison.engine_capture.run import capture, verify_capture
            value = capture(a.plan, a.plan_sha256, a.out, a.parent, fixture=a.fixture) if a.action == "capture" else verify_capture(a.report)
            # The full logical dictionary remains the Python verifier API.
            # CLI stdout binds only its bounded complete wire manifest.
            value = read((a.out if a.action == "capture" else a.report.parent) / "report.json")
        else:
            from search_comparison.engine_capture.canaries import capture_qmd, capture_recoll
            engine = read(reference({"path": str(a.engine.absolute()), "sha256": a.engine_sha256}))
            value = capture_qmd(engine, a.out, a.parent, fixture=a.fixture, mode=a.mode) if a.action == "qmd-canary" else capture_recoll(engine, a.out, a.parent, fixture=a.fixture)
        code = 2 if value.get("status") == "MISSING" else 0
    except (EvaluationError, OSError, ValueError, KeyError, TypeError) as exc:
        value, code = {"status": "MISSING", "error": str(exc), "release_eligible": False}, 2
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) if a.action in ("capture", "verify")
          else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
