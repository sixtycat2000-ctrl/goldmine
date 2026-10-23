#!/usr/bin/env python3
"""Capture the strict real distill evaluator with explicit inputs and raw receipts."""
import argparse
from pathlib import Path
import sys

from acceptance_lib.evidence import InvalidEvidence, capture_command, read_command_run, sha256
from distill_eval.identity import evaluator_identity
from distill_eval.preparation import validate_preparation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("db", "labels", "corpus-root", "incremental-db", "preparation", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        preparation = validate_preparation(args.preparation, args.db, args.labels, args.corpus_root, args.incremental_db)
    except (InvalidEvidence, ValueError, OSError, TypeError) as exc:
        parser.error(str(exc))
    if str(Path(sys.executable).absolute()) != preparation["python_path"]:
        parser.error("use the exact Python interpreter recorded by native preparation")
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        parser.error("output must be new or empty")
    out.mkdir(parents=True, exist_ok=True)
    script = root / "scripts/eval_distill.py"
    command = [sys.executable, str(script), "--report-real", str(args.db.resolve()), "--json"]
    for key in ("labels", "corpus_root", "incremental_db", "preparation"):
        command.extend(["--" + key.replace("_", "-"), str(getattr(args, key).resolve())])
    capture_command(out / "execution", "release.distill", command, cwd=out,
        identity={"source_files": evaluator_identity(root), "tool_sha256": sha256(script), "preparation": preparation}, suite_id="distill.real.v1")
    _, receipt, _ = read_command_run(out / "execution/manifest.json")
    print(f"real distill exit{receipt['exit_code']}; raw manifest: {out / 'execution/manifest.json'}")
    return receipt["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
