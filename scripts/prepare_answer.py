#!/usr/bin/env python3
"""Prepare v3 answer material/jobs; never call an answerer or judge."""
from __future__ import annotations

import argparse
import inspect
import json
import os
from pathlib import Path
import sys

from answer_eval.preparation import ARMS, load_preparation, owned_output, readiness
from answer_eval.protocol import EvaluationError, PROTOCOL, sha256_file

ROOT = Path(__file__).resolve().parents[1]


def capture_material(plan, role, out):
    from acceptance_lib.evidence import capture_command, read_command_run
    from answer_eval.materials import implementation
    prep = load_preparation(plan, require_ready=True, verify_runtime=True)
    out = owned_output(prep.plan, out)
    if "owned_process_group" not in inspect.signature(capture_command).parameters:
        raise EvaluationError("reviewed owned-process capture dependency not installed", status="MISSING")
    argv = [sys.executable, str(ROOT / "scripts/prepare_answer.py"), "material-worker", "--plan",
            str(Path(plan).resolve()), "--role", role, "--out", str(out / "output")]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GOLDMINE_") and k != "PYTHONPATH"}
    env.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", PIP_NO_INDEX="1")
    capture_command(out, "answer.material." + role, argv, cwd=ROOT, env=env,
        identity={"plan_sha256": sha256_file(Path(plan)), "evaluator_files": implementation(ROOT)},
        suite_id=PROTOCOL, timeout=1800, outputs={"material": "output/material.json"}, owned_process_group=True)
    _, receipt, _ = read_command_run(out / "manifest.json", check_id="answer.material." + role, suite_id=PROTOCOL)
    status = "MATERIAL_COMMAND_CAPTURED" if receipt["exit_code"] == 0 and not receipt["timed_out"] else "MISSING"
    return {"status": status, "exit_code": receipt["exit_code"], "timed_out": receipt["timed_out"],
            "manifest": {"path": str(out / "manifest.json"), "sha256": sha256_file(out / "manifest.json")},
            "release_eligible": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("inspect", "material", "material-worker", "jobs"):
        command = sub.add_parser(action)
        command.add_argument("--plan", required=True, type=Path)
        if action in ("material", "material-worker"):
            command.add_argument("--role", choices=("reference", "candidate"), required=True)
            command.add_argument("--out", type=Path, required=True)
        if action == "jobs":
            command.add_argument("--reference", required=True, type=Path)
            command.add_argument("--candidate", required=True, type=Path)
            command.add_argument("--study", required=True, type=Path)
    command = sub.add_parser("judges")
    command.add_argument("--study", required=True, type=Path)
    command.add_argument("--arm", choices=ARMS, required=True)
    command.add_argument("--question", required=True)
    command.add_argument("--answer-round", choices=("a1", "a2"), required=True)
    command = sub.add_parser("check-completions")
    command.add_argument("--study", required=True, type=Path)
    command = sub.add_parser("comparison-jobs")
    command.add_argument("--study", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "inspect":
            result = readiness(args.plan)
        elif args.action == "material":
            result = capture_material(args.plan, args.role, args.out)
        elif args.action == "material-worker":
            from answer_eval.materials import material_worker
            result = material_worker(args.plan, args.role, args.out, root=ROOT)
        elif args.action == "jobs":
            from answer_eval.job_material import prepare_jobs
            result = prepare_jobs(args.plan, args.reference, args.candidate, args.study, root=ROOT)
        elif args.action == "judges":
            from answer_eval.completion_material import prepare_judges
            result = prepare_judges(args.study, args.arm, args.question, args.answer_round)
        elif args.action == "check-completions":
            from answer_eval.completion_material import check_complete_records
            result = check_complete_records(args.study)
        else:
            from answer_eval.job_material import comparison_jobs
            result = comparison_jobs(args.study, root=ROOT)
        code = 2 if result.get("status") in ("NOT_READY", "MISSING", "NOT_COMPARABLE") else 1 if result.get("status") == "FAIL" else 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result, code = {"status": getattr(exc, "status", "NOT_COMPARABLE"), "error": str(exc), "release_eligible": False}, 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
