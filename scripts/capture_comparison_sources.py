#!/usr/bin/env python3
"""Capture full comparison source views, with no queries/indexes/models fetched."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# Select this explicit checkout, never an installed Goldmine shadow package.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from answer_eval.protocol import EvaluationError
from search_comparison.source_views.sources import REAL_MANIFEST, load_sources


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate-source", "implementation-identity", "capture", "verify"))
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--manifest-sha256", default=REAL_MANIFEST)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--owned-parent", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--mechanical", action="store_true", help="synthetic fixtures only; never real comparison evidence")
    args = parser.parse_args(argv)
    try:
        if args.action == "implementation-identity":
            from search_comparison.source_views.run import implementation_identity
            from search_comparison.source_views.recipes import runtime_identity
            result = {"status": "CODE_IDENTITY_ONLY", "implementation": implementation_identity(),
                      "runtime": runtime_identity(), "quality_evidence": False, "release_eligible": False}
        elif args.bundle is None:
            raise EvaluationError("explicit source bundle is required", status="MISSING")
        elif args.action == "validate-source":
            bundle = load_sources(args.bundle, args.manifest_sha256, fixture=args.mechanical)
            result = {"status": "MECHANICAL_SOURCE_ONLY" if args.mechanical else "VALID_SOURCE_ONLY", "source_identity": bundle.identity,
                      "source_count": len(bundle.sources), "query_files_opened": False, "quality_evidence": False}
        else:
            if args.config is None:
                raise EvaluationError("explicit frozen local converter config is required", status="MISSING")
            from search_comparison.source_views.run import capture_sources, verify_capture
            if args.action == "capture":
                if args.out is None or args.owned_parent is None:
                    raise EvaluationError("capture requires new --out and explicit --owned-parent", status="MISSING")
                result = capture_sources(args.bundle, args.manifest_sha256, args.config, args.out, args.owned_parent, fixture=args.mechanical)
            else:
                if args.report is None:
                    raise EvaluationError("verify requires --report", status="MISSING")
                result = verify_capture(args.report, args.bundle, args.manifest_sha256, args.config, fixture=args.mechanical)
            result = {"status": result["status"], "evidence_mode": result["evidence_mode"], "source_count": result["source_count"],
                      "quality_evidence": False, "release_eligible": False}
        code = 2 if result["status"] == "MISSING" else 0
    except (EvaluationError, OSError, ValueError, KeyError, TypeError, ImportError) as error:
        result, code = {"status": "MISSING", "error": str(error), "quality_evidence": False, "release_eligible": False}, 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
