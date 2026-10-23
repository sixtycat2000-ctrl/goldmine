#!/usr/bin/env python3
"""Typed fixed performance/update capture, or read-only complete raw replay."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT)]
from answer_eval.protocol import EvaluationError
from search_comparison.measurements.run import capture, verify


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    start = modes.add_parser("capture")
    for name in ("packet", "baseline", "out", "parent"):
        start.add_argument("--" + name, required=True, type=Path)
    start.add_argument("--packet-sha256", required=True)
    start.add_argument("--baseline-sha256", required=True)
    replay = modes.add_parser("verify")
    replay.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.mode == "capture":
            report = capture({"path": str(args.packet.absolute()), "sha256": args.packet_sha256},
                             {"path": str(args.baseline.absolute()), "sha256": args.baseline_sha256}, args.out, args.parent)
        else:
            report = verify(args.report)
        print(json.dumps({k: report[k] for k in ("status", "full_comparison_status", "resource_status", "denominators", "quality_evidence", "release_eligible")}))
        return 0 if report["full_comparison_status"] == "CAPTURED_REQUIRED_DEV_COMPARISON" else 2
    except (EvaluationError, OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "MISSING", "error": str(exc), "quality_evidence": False, "release_eligible": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
