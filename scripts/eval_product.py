#!/usr/bin/env python3
"""Evaluate frozen product.v4 inputs; default dev, explicit final sealed audit."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

from answer_eval.protocol import EvaluationError, read_json, sha256_file
from product_eval.bundle import load_bundle
from product_eval.gates import check_targets, compare_reference
from product_eval.runner import evaluate, prepare_index
from product_eval.results import validate_report

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--validate", action="store_true")
    action.add_argument("--prepare", action="store_true", help="explicitly build a NEW index")
    parser.add_argument("--bundle", type=Path, default=ROOT / "evals/product_v4")
    parser.add_argument("--build", type=Path, help="directory made by build_product_corpus.py build")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--production-root", type=Path, default=ROOT)
    parser.add_argument("--reference-code", action="store_true", help="requires clean original isolated production checkout")
    parser.add_argument("--seal-reference", action="store_true", help="save original holdout privately; stdout contains only a seal receipt")
    parser.add_argument("--backend", choices=("mock", "llama"), default="llama")
    parser.add_argument("--model", type=Path)
    parser.add_argument("--server", type=Path)
    parser.add_argument("--parameters", type=Path)
    parser.add_argument("--split", choices=("dev", "train", "holdout", "all"), default="dev")
    parser.add_argument("--diagnostic", action="store_true", help="allow partial/mock mechanical checks; quality gate still refuses")
    parser.add_argument("--freeze", type=Path, help="final candidate freeze required for holdout/all")
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--deterministic-recheck", action="store_true")
    parser.add_argument("--reference-run", type=Path, help="original-code report for paired diagnostics")
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--targets", type=Path, default=ROOT / "evals/acceptance_targets.json")
    parser.add_argument("--out", type=Path, help="new report directory; never overwrite a reference")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.out is not None and args.out.exists():
            raise EvaluationError("report output already exists; choose a new run directory")
        if args.seal_reference and (not args.reference_code or args.split != "holdout" or args.out is None or args.gate or args.reference_run):
            raise EvaluationError("--seal-reference needs original code, holdout, and new --out; it cannot compare or gate")
        bundle = load_bundle(args.bundle, require_complete=not (args.diagnostic or args.validate), diagnostic=args.diagnostic)
        if args.validate:
            report = {"status": "MISSING" if bundle.missing else "VALID", "identity": bundle.identity,
                      "missing": bundle.missing, "n_queries": len(bundle.queries), "release_eligible": False}
            code = 2 if bundle.missing else 0
        else:
            if args.build is None or args.db is None:
                raise EvaluationError("--build and --db must select explicit corpus/index assets", status="MISSING")
            options = {"backend_name": args.backend, "production_root": args.production_root, "model": args.model,
                       "server": args.server, "diagnostic": args.diagnostic, "reference_run": args.reference_code}
            if args.prepare:
                report, code = prepare_index(bundle, args.build, args.db, **options), 0
            else:
                report = evaluate(bundle, args.build, args.db, split=args.split,
                                  parameters=read_json(args.parameters) if args.parameters else None,
                                  freeze=read_json(args.freeze) if args.freeze else None,
                                  ledger=args.ledger, recheck=args.deterministic_recheck,
                                  seal_reference=args.seal_reference, **options)
                code = 0
                validate_report(bundle, report, diagnostic=args.diagnostic)
                if args.reference_run:
                    saved = validate_report(bundle, read_json(args.reference_run))
                    report["paired_reference"] = compare_reference(saved, report)
                if args.gate:
                    report["gate"] = check_targets(report, args.targets)
                    code = 1 if report["gate"]["failed"] else 0
        if args.out:
            args.out.mkdir(parents=True, exist_ok=False, mode=0o700 if args.seal_reference else 0o777)
            output = args.out / "run.json"
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if args.seal_reference else 0o666)
            with os.fdopen(fd, "w", encoding="utf-8") as saved:
                saved.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            if args.seal_reference:
                report = {"status": "SEALED_REFERENCE", "sha256": sha256_file(output),
                          "bundle_identity": bundle.identity, "production_commit": bundle.manifest["reference_source_commit"],
                          "release_eligible": False, "metrics_exposed": False}
    except (EvaluationError, OSError, sqlite3.Error, ValueError) as exc:
        report, code = {"status": getattr(exc, "status", "NOT_COMPARABLE"), "error": str(exc), "release_eligible": False}, 2
    except RuntimeError as exc:
        missing = type(exc).__name__ in ("BackendUnavailable", "BackendStartupError", "EmbeddingError")
        report, code = {"status": "MISSING" if missing else "FAIL", "error": str(exc), "release_eligible": False}, 2 if missing else 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print("product.v4:", report.get("status", "DIAGNOSTIC" if report.get("diagnostic_only") else "MEASURED"))
        print(report.get("error", "; ".join(report.get("missing", []))))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
