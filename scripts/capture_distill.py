#!/usr/bin/env python3
"""Capture the strict real distill evaluator with explicit inputs and raw receipts."""
import argparse
from pathlib import Path
import sys

from acceptance_lib.evidence import capture_command, read_command_run, sha256
from search_bench.session import package_identity


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("db", "labels", "corpus-root", "incremental-db", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        parser.error("output must be new or empty")
    out.mkdir(parents=True, exist_ok=True)
    script = root / "scripts/eval_distill.py"
    command = [sys.executable, str(script), "--report-real", str(args.db.resolve()), "--json"]
    for key in ("labels", "corpus_root", "incremental_db"):
        command.extend(["--" + key.replace("_", "-"), str(getattr(args, key).resolve())])
    capture_command(out / "execution", "release.distill", command, cwd=out,
        identity={"source_files": package_identity(root), "tool_sha256": sha256(script)}, suite_id="distill.real.v1")
    _, receipt, _ = read_command_run(out / "execution/manifest.json")
    print(f"real distill exit{receipt['exit_code']}; raw manifest: {out / 'execution/manifest.json'}")
    return receipt["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
