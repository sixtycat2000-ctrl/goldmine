"""Small adapters for eval_answer.py and python -m answer_eval."""

from __future__ import annotations

import argparse
import json
import sys

from .protocol import EvaluationError
from .workflow import compare_runs, gate_baseline, validate_run, write_baseline


def command(action: str, *, base=None, candidate=None, run=None, output=None,
            yes=False, diagnostic=False, as_json=False) -> int:
    try:
        if action == "compare":
            report, code = compare_runs(base, candidate, diagnostic=diagnostic)
        elif action == "validate":
            report, code = validate_run(run, diagnostic=diagnostic), 0
        elif action == "baseline":
            report, code = write_baseline(run, output, yes=yes), 0
        elif action == "gate":
            report, code = gate_baseline(base, candidate, diagnostic=diagnostic)
        else:
            raise EvaluationError("unknown answer evaluation action")
    except EvaluationError as exc:
        report, code = {"schema": 3, "status": exc.status, "error": str(exc),
                        "release_eligible": False}, 2
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(f"answer-eval/v3: {report.get('status', 'REFERENCE_RECORDED')}")
        if "error" in report:
            print(report["error"], file=sys.stderr)
        elif "mean_drop" in report:
            print(f"mean drop {report['mean_drop']:.6f}; preregistered threshold {report['practical_drop_threshold']:.2f}")
            print(f"trap refusal {report['candidate']['trap']['successes']}/{report['candidate']['trap']['n']}; calibration uncalibrated")
        else:
            print("Provenance checked; calibration uncalibrated. No population/false-positive guarantee.")
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description="Strict disk-only answer-eval/v3")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("validate", "compare", "baseline"):
        p = sub.add_parser(name)
        p.add_argument("--json", action="store_true")
        if name == "compare":
            p.add_argument("base")
            p.add_argument("candidate")
        else:
            p.add_argument("run")
        if name == "baseline":
            p.add_argument("--out", required=True)
            p.add_argument("--yes", action="store_true")
        else:
            p.add_argument("--diagnostic", action="store_true")
    args = parser.parse_args(argv)
    return command(args.action, base=getattr(args, "base", None), candidate=getattr(args, "candidate", None),
                   run=getattr(args, "run", None), output=getattr(args, "out", None),
                   yes=getattr(args, "yes", False), diagnostic=getattr(args, "diagnostic", False), as_json=args.json)
