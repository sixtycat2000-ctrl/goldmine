#!/usr/bin/env python3
"""Run a source-pinned, model-free diagnostic comparison of all three systems."""

import argparse
import json
from pathlib import Path

from answer_eval.protocol import EvaluationError, read_json
from search_comparison.manifest import load_comparison
from search_comparison.adapters import DocumentMap
from search_comparison.runner import recompute_report, run_comparison

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "evals/comparison/manifest.json")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--validate", action="store_true")
    action.add_argument("--recompute", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--diagnostic", action="store_true", help="required acknowledgement: partial seed and lexical modes only")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.recompute:
            report = read_json(args.recompute)
            view, qmd = report["configuration"]["view"], report["configuration"]["systems"]["qmd"]
            mapping = DocumentMap(ROOT / view["corpus_root"], view["documents"], collection=qmd["collection"], index=qmd["index"])
            result = {"status": report["status"], "quality_evidence": False,
                      "systems": recompute_report(report, raw_root=args.recompute.parent, mapping=mapping)}
        elif args.validate:
            manifest, bundle, mapping = load_comparison(args.manifest, ROOT)
            result = {"status": "DIAGNOSTIC_INPUTS_VALID", "quality_evidence": False, "n_queries": len(bundle.queries),
                      "n_sources": len(mapping.local), "missing": bundle.missing + manifest["missing"]}
        else:
            if args.out is None or not args.diagnostic:
                raise EvaluationError("explicit --diagnostic and a new --out directory are required")
            report = run_comparison(args.manifest, root=ROOT, output=args.out)
            result = {"status": report["status"], "quality_evidence": False, "n_queries": report["n_queries"],
                      "systems": {name: system["summary"] for name, system in report["systems"].items()}, "report": str(args.out / "report.json")}
        code = 2 if result["status"] == "MISSING" else 0
    except (EvaluationError, OSError, ValueError, KeyError) as error:
        result, code = {"status": getattr(error, "status", "MISSING"), "error": str(error), "quality_evidence": False}, 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) if args.json else json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
