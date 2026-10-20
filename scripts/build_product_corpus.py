#!/usr/bin/env python3
"""Validate/copy the portable product.v4 source bundle; no model or downloads."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from answer_eval.protocol import EvaluationError, canonical_json
from product_eval.bundle import build_corpus, load_bundle
from product_eval.coverage import audit_sample_ids
from product_eval.ledger import initialize_ledger
from product_eval.splits import make_splits

DEFAULT_BUNDLE = Path(__file__).resolve().parents[1] / "evals/product_v4"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "build", "split", "init-ledger"))
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--allow-draft", action="store_true", help="explicit asset-only draft copy; never quality evidence")
    parser.add_argument("--diagnostic", action="store_true", help="allow unit-test fixture bundles")
    args = parser.parse_args(argv)
    try:
        if args.action == "init-ledger":
            if args.out is None:
                parser.error("init-ledger needs --out NEW_DIRECTORY")
            report, code = initialize_ledger(args.out), 0
        else:
            bundle = load_bundle(args.bundle, require_complete=False, diagnostic=args.diagnostic)
            if args.action == "validate":
                report = {"status": "MISSING" if bundle.missing else "VALID", "identity": bundle.identity,
                          "missing": bundle.missing, "n_queries": len(bundle.queries), "n_sources": len(bundle.sources),
                          "audit_sample_ids": audit_sample_ids(bundle.queries), "release_eligible": False}
                code = 2 if bundle.missing else 0
            else:
                if args.out is None:
                    parser.error("build/split needs --out NEW_PATH")
                if args.action == "build":
                    report = build_corpus(bundle, args.out, allow_draft=args.allow_draft)
                else:
                    report = make_splits(bundle.queries)
                    with args.out.open("x", encoding="utf-8") as stream:
                        stream.write(canonical_json(report) + "\n")
                code = 0
    except (EvaluationError, OSError) as exc:
        report, code = {"status": getattr(exc, "status", "NOT_COMPARABLE"), "error": str(exc), "release_eligible": False}, 2
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
