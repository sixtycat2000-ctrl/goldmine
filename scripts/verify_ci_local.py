#!/usr/bin/env python3
"""Combine same-script scoped CI runs with actual failure-injection and lint runs."""
import argparse
from pathlib import Path
import sys

from acceptance_lib.ci_evidence import verify
from acceptance_lib.evidence import InvalidEvidence, capture_command, sha256, write_json
from acceptance_lib.pytest_checks import run_pytest
from acceptance_lib.runner import code_identity, exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("base", "native", "mcp", "actionlint", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        parser.error("output must be new or empty")
    out.mkdir(parents=True, exist_ok=True)
    pin = lambda path: {"manifest_path": str(path.resolve()), "sha256": sha256(path)}
    try:
        counter = out / "counterexamples"
        status, _, _ = run_pytest(root, counter, "P2-05", code_identity(root, out))
        if status != "PASS":
            print(f"counterexamples: {status}")
            return exit_code(status)
        capture_command(out / "actionlint", "ci.actionlint", [str(args.actionlint.resolve()), "-shellcheck=",
            *(str(root / ".github/workflows" / name) for name in ("ci.yml", "live-validation.yml", "build-release.yml"))],
            cwd=root, suite_id="ci.actionlint.v1")
        report = {"schema": 1, "kind": "goldmine.ci-local",
                  "evidence": {name: pin(getattr(args, name)) for name in ("base", "native", "mcp")},
                  "counterexamples": {"command": pin(counter / "execution/manifest.json"),
                                      "collection": pin(counter / "collection/manifest.json")},
                  "actionlint": pin(out / "actionlint/manifest.json")}
        write_json(out / "manifest.json", report)
        status, _ = verify(out / "manifest.json")
        print(f"local CI: {status}; hosted verification: MISSING")
        return exit_code(status)
    except (InvalidEvidence, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"local CI: MISSING — {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
