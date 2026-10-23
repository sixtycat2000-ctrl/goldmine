#!/usr/bin/env python3
"""Capture one explicitly prepared original/candidate public raw run; no downloads."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from acceptance_lib.evidence import capture_command, sha256
from acceptance_lib.ir_checks import evaluator_files
from product_eval.runner import production_identity
from public_eval.protocol import DATASETS, SUITE
from public_eval.protocol import load_archive

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("reference", "candidate"), required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--production-root", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--prepare", action="store_true", help="Capture the actual explicit preparation before scoring, without downloads.")
    parser.add_argument("--query-deadline-seconds", type=float, default=None,
                        help="Registered per-query deadline policy forwarded to the evaluator; identical for "
                             "reference and candidate roles; timeouts fail loudly, never empty hits.")
    args = parser.parse_args(argv)
    try:
        cache, source, model, server = (path.resolve() for path in (args.cache_root, args.production_root, args.model, args.server))
        if args.role == "candidate" and source != ROOT:
            raise ValueError("candidate capture must use the current checkout")
        production = production_identity(source, reference_run=args.role == "reference")
        from acceptance_lib.evidence import read_json

        official = read_json(ROOT / "evals/public_manifest.json")
        public_inputs = {"manifest_sha256": sha256(ROOT / "evals/public_manifest.json"),
                         "datasets": {name: load_archive(cache / "beir" / (name + ".zip"), official["datasets"][name])["identity"] for name in DATASETS}}
        identity = {"evaluator_files": evaluator_files(ROOT, "public"), "production_source": production,
                    "model_sha256": sha256(model), "server_sha256": sha256(server), "public_inputs": public_inputs}
        if not args.prepare:
            identity["indexes_sha256"] = {name: sha256(cache / "beir" / name / "raw-index.sqlite") for name in DATASETS}
        command = [str(args.python.absolute()), str(ROOT / "scripts/eval_public.py"), "--raw"]
        if args.prepare:
            command.append("--prepare")
        command += ["--cache-root", str(cache),
                   "--production-root", str(source), "--backend", "llama", "--model", str(model), "--server", str(server)]
        if args.role == "reference":
            command.append("--reference-code")
        if args.query_deadline_seconds is not None:
            command += ["--query-deadline-seconds", str(args.query_deadline_seconds)]
        command.append("--json")
        # No user daemon is consulted; the selected backend owns its process.
        env = {key: value for key, value in os.environ.items() if not key.startswith("GOLDMINE_") and key != "PYTHONPATH"}
        env.update({"GOLDMINE_HOME": str(args.out.resolve() / "isolated-home"),
                    "GOLDMINE_CONFIG": str(args.out.resolve() / "isolated-config.toml"), "GOLDMINE_OCR_AUTOCOMPILE": "0"})
        check_id = "public." + args.role + ("_prepare" if args.prepare else "")
        manifest = capture_command(args.out, check_id, command, cwd=ROOT, env=env,
                                   identity=identity, suite_id=SUITE, timeout=7200, owned_process_group=True)
        from acceptance_lib.evidence import read_command_run

        _, receipt, _ = read_command_run(args.out / "manifest.json")
        print(json.dumps({"manifest": str(args.out / "manifest.json"), "run_id": manifest["run_id"], "exit_code": receipt["exit_code"]}))
        return receipt["exit_code"]
    except (ValueError, OSError) as error:
        print(json.dumps({"status": "MISSING", "error": str(error), "release_eligible": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
