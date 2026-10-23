#!/usr/bin/env python3
"""Run fixed lifecycle tests with strict collection, JUnit and no successful skips."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from acceptance_lib.evidence import InvalidEvidence, artifact, sha256, write_json
from acceptance_lib.pytest_checks import run_pytest
from acceptance_lib.runner import code_identity, exit_code
from search_bench.session import hardware


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("watch", "serve"), required=True)
    parser.add_argument("--profile", choices=("controlled", "real"), required=True)
    for name in ("model", "server", "db", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        parser.error("output must be new or empty")
    out.mkdir(parents=True, exist_ok=True)
    profile = f"live-{args.suite}-{args.profile}"
    identity = code_identity(root, out)
    environment = hardware()
    try:
        identity.update({"model_sha256": sha256(args.model), "server_sha256": sha256(args.server),
                         "source_index_sha256": sha256(args.db), "pressure_profile": args.profile})
        status, details, evidence = run_pytest(root, out, profile, identity,
            python=str(args.python.absolute()), capability_env={"GOLDMINE_LIVE_MODEL": str(args.model.resolve()),
            "GOLDMINE_LIVE_SERVER": str(args.server.resolve()), "GOLDMINE_LIVE_INDEX": str(args.db.resolve())})
        after = code_identity(root, out)
        if identity["content_sha256"] != after["content_sha256"]:
            status, details = "MISSING", {**details, "reason": "source changed during authoritative live run"}
    except (InvalidEvidence, OSError, ValueError) as exc:
        status, details, evidence = "MISSING", {"reason": str(exc)}, {}
    environment_after = hardware()
    if environment["pressure"]["level"] is None or environment_after["pressure"]["level"] is None:
        status, details = "MISSING", {**details, "reason": "actual kernel pressure not measurable"}
    write_json(out / "manifest.json", {"schema": 1, "kind": "goldmine.live-lifecycle", "profile": profile,
        "identity": identity, "status": status, "details": details, "evidence": evidence,
        "environment": environment, "environment_after": environment_after})
    print(f"{profile}: {status}; raw report: {out / 'manifest.json'}")
    return exit_code(status)


if __name__ == "__main__":
    raise SystemExit(main())
