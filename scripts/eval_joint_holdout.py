#!/usr/bin/env python3
"""Execute ONE explicitly frozen joint audit; never an ordinary task check."""
import argparse
import json
from pathlib import Path
import sqlite3

from answer_eval.protocol import EvaluationError
from holdout_lib.runtime import execute

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--json", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        result, code = execute(args.freeze, args.ledger, args.out, ROOT), 0
    except (EvaluationError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        result, code = {"status": "MISSING", "error": str(error), "release_eligible": False,
                        "ledger_policy": "Any already-consumed exposure stays spent; do not reset or retry with tuned inputs."}, 2
    except RuntimeError as error:
        missing = type(error).__name__ in {"BackendUnavailable", "BackendStartupError", "EmbeddingError"}
        result, code = {"status": "MISSING" if missing else "FAIL", "error": str(error), "release_eligible": False,
                       "ledger_policy": "Any already-consumed exposure stays spent; no tuned retry."}, 2 if missing else 1
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
