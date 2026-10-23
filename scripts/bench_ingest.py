#!/usr/bin/env python3
"""Measure repeated ingest on disposable licensed copies with a real backend."""
import argparse
import json
import os
from pathlib import Path
import sys

from acceptance_lib.evidence import InvalidEvidence, capture_command, read_command_run, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-root", "manifest", "model", "server", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    for name in ("ocr-helper", "whisper", "models-dir"):
        parser.add_argument(f"--{name}", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        from ingest_bench.runner import run

        try:
            report = run(args.out, args.source_root, args.manifest)
        except (InvalidEvidence, ValueError, OSError) as exc:
            report = {"schema": 1, "kind": "goldmine.repeated-ingest", "status": "MISSING", "reason": str(exc)}
        write_json(args.out / "samples.json", report)
        print(json.dumps(report, ensure_ascii=False, allow_nan=False))
        return {"PASS": 0, "FAIL": 1, "MISSING": 2}[report["status"]]
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        parser.error("output must be new or empty")
    out.mkdir(parents=True, exist_ok=True)
    home = out / "state"
    home.mkdir()
    config = home / "config.toml"
    config.write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GOLDMINE_") and k not in ("PYTHONPATH", "PYTHONHOME")}
    env.update({"PYTHONPATH": str(args.source_root.resolve()), "GOLDMINE_HOME": str(home), "GOLDMINE_CONFIG": str(config),
                "GOLDMINE_MODEL": str(args.model.resolve()), "GOLDMINE_LLAMA_SERVER": str(args.server.resolve()), "GOLDMINE_OCR_AUTOCOMPILE": "0"})
    for key, variable in (("ocr_helper", "GOLDMINE_OCR_HELPER"), ("whisper", "GOLDMINE_WHISPER"), ("models_dir", "GOLDMINE_MODELS")):
        if getattr(args, key) is not None:
            env[variable] = str(getattr(args, key).resolve())
    command = [str(args.python.absolute()), str(Path(__file__).resolve()), "--worker"]
    for key in ("source_root", "manifest", "model", "server", "out"):
        command += ["--" + key.replace("_", "-"), str(getattr(args, key).resolve())]
    capture_command(out / "execution", "resources.ingest", command, cwd=out, env=env, suite_id="resources.ingest.v1", timeout=1800)
    _, receipt, _ = read_command_run(out / "execution/manifest.json")
    print(f"repeated-ingest exit{receipt['exit_code']}; raw evidence: {out / 'execution/manifest.json'}")
    return receipt["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
