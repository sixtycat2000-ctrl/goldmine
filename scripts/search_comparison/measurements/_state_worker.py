"""Fixed typed state worker; no arbitrary command, model or network entry."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT)]
from answer_eval.protocol import EvaluationError, sha256_file
from search_comparison.engine_capture.protocol import reference, read
from search_comparison.measurements.filesystem import execute

try:
    if len(sys.argv) != 3:
        raise EvaluationError("exact registered request path/SHA arguments required")
    path = reference({"path": sys.argv[1], "sha256": sys.argv[2]})
    request = read(path)
    value = execute(request)
    print(json.dumps({"schema": 1, "request_sha256": sha256_file(path), "operation": request["operation"], "result": value}, ensure_ascii=False, allow_nan=False))
except (EvaluationError, OSError, ValueError, TypeError, KeyError) as exc:
    print(json.dumps({"status": "MISSING", "error": str(exc), "quality_evidence": False, "release_eligible": False}))
    raise SystemExit(2)
