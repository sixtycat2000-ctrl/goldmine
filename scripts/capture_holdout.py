#!/usr/bin/env python3
"""Explicit original seal, final freeze, and one-shot joint capture; no downloads."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

from acceptance_lib.evidence import capture_command, read_command_run, sha256
from acceptance_lib.ir_checks import evaluator_files
from answer_eval.protocol import EvaluationError, canonical_sha256
from holdout_lib.protocol import SUITE, files, load_freeze

ROOT = Path(__file__).resolve().parents[1]


def _environment(output):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GOLDMINE_") and key != "PYTHONPATH"}
    env.update({"GOLDMINE_HOME": str(output / "isolated-home"), "GOLDMINE_CONFIG": str(output / "isolated-config.toml"),
                "GOLDMINE_OCR_AUTOCOMPILE": "0"})
    return env


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    seal = actions.add_parser("seal-reference", help="Actual original holdout command; stdout contains only its seal, never metrics.")
    for name in ("bundle", "build", "db", "production-root", "model", "server", "parameters", "out"):
        seal.add_argument("--" + name, type=Path, required=True)
    seal.add_argument("--python", type=Path, default=Path(sys.executable))
    freeze = actions.add_parser("freeze", help="Pin final assets and fixed arms; no model query or private-reference metric read.")
    freeze.add_argument("--spec", type=Path, required=True)
    freeze.add_argument("--out", type=Path, required=True)
    audit = actions.add_parser("audit", help="Consume the ONE final exposure; a failed/crashed audit remains spent.")
    audit.add_argument("--freeze", type=Path, required=True)
    audit.add_argument("--out", type=Path, required=True)
    audit.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args(argv)
    try:
        output = args.out.resolve()
        if args.action == "freeze":
            from holdout_lib.freeze import create

            frozen = create(args.spec, ROOT)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(frozen, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            load_freeze(output, ROOT)
            print(json.dumps({"status": "FROZEN", "freeze": str(output), "sha256": sha256(output),
                              "metrics_exposed": False, "release_eligible": False}))
            return 0
        if args.action == "seal-reference":
            from product_eval.bundle import load_bundle
            from product_eval.runner import production_identity

            source = production_identity(args.production_root, reference_run=True)
            bundle = load_bundle(args.bundle)
            parameters = json.loads(args.parameters.read_text())
            identity = {"evaluator_files": evaluator_files(ROOT, "product"), "production_source": source,
                        "index_sha256": sha256(args.db), "server_sha256": sha256(args.server), "model_sha256": sha256(args.model),
                        "parameters_sha256": canonical_sha256(parameters), "bundle_identity": bundle.identity}
            command = [str(args.python.absolute()), str(ROOT / "scripts/eval_product.py"),
                "--bundle", str(args.bundle.resolve()), "--build", str(args.build.resolve()), "--db", str(args.db.resolve()),
                "--production-root", str(args.production_root.resolve()), "--backend", "llama", "--model", str(args.model.resolve()),
                "--server", str(args.server.resolve()), "--parameters", str(args.parameters.resolve()), "--split", "holdout",
                "--reference-code", "--seal-reference", "--out", str(output / "sealed"), "--json"]
            check_id, suite, timeout, outputs = "product.holdout_reference", "product.v4", 7200, {}
        else:
            frozen, _, _ = load_freeze(args.freeze, ROOT)
            identity = {"evaluator_files": files(ROOT), "freeze_sha256": sha256(args.freeze),
                        "candidate_source": frozen["candidate_source"], "model_sha256": frozen["model"]["sha256"],
                        "server_sha256": frozen["server"]["sha256"], "index_sha256": frozen["candidate"]["index"]["sha256"]}
            command = [str(args.python.absolute()), str(ROOT / "scripts/eval_joint_holdout.py"), "--freeze", str(args.freeze.resolve()),
                       "--ledger", frozen["ledger"]["path"], "--out", str(output / "results"), "--json"]
            check_id, suite, timeout, outputs = "release.holdout", SUITE, 21600, {"joint": "results/joint.json"}
        manifest = capture_command(output, check_id, command, cwd=ROOT, env=_environment(output), identity=identity,
                                   suite_id=suite, timeout=timeout, outputs=outputs)
        _, receipt, _ = read_command_run(output / "manifest.json")
        print(json.dumps({"manifest": str(output / "manifest.json"), "run_id": manifest["run_id"], "exit_code": receipt["exit_code"],
                          "release_eligible": False}))
        return receipt["exit_code"]
    except (EvaluationError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        print(json.dumps({"status": "MISSING", "error": str(error), "release_eligible": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
