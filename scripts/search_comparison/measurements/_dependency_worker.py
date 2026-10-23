"""Fixed offline missing-role probes, never an install/download/engine hook."""
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT)]
from answer_eval.protocol import EvaluationError, fields
from search_comparison.engine_capture.plan import engine_resources
from search_comparison.engine_capture.protocol import read, reference, local_path


def probe(value):
    fields(value, {"schema", "owner", "scenario", "arm", "registration", "installation", "configuration"}, "fixed missing-role request")
    owner = local_path(value["owner"])
    missing = owner / "absent-dependency" / "not-provisioned"
    if value["schema"] != 1 or not (owner / "cell-ownership.json").is_file() or missing.exists():
        raise EvaluationError("missing-role probe lacks owned registration or role is present")
    scenario, arm = value["scenario"], value["arm"]
    registered = read(reference(value["registration"]["artifact"]), limit=16 * 1024 * 1024)
    fields(registered, {"schema", "suite_id", "engine", "plan_sha256", "complete_descriptor"}, "complete engine registration")
    descriptor = deepcopy(registered["complete_descriptor"])
    if (registered["schema"] != 1 or registered["suite_id"] != "comparison.complete-engine-registration/v1"
            or registered["engine"] != arm.split(".")[0]):
        raise EvaluationError("missing-role registration engine differs")
    # Validate the entire unchanged registered runtime first. An unrelated
    # already-broken dependency cannot masquerade as the deliberate absence.
    engine_resources(arm.split(".")[0], descriptor, fixture=False, require_canary=False, arm=arm)
    if scenario == "qmd-embedding-absent" and arm in {"qmd.search", "qmd.vsearch", "qmd.query"}:
        descriptor["model_roles"]["embed"]["resource"]["path"] = str(missing)
        role = "embedding"
    elif scenario == "recoll-indexer-absent" and arm in {"recoll.default", "recoll.anyterm"}:
        descriptor["tools"]["recollindex"]["path"] = str(missing)
        role = "recollindex"
    else:
        raise EvaluationError("unregistered missing-role scenario/arm")
    try:
        engine_resources(arm.split(".")[0], descriptor, fixture=False, require_canary=False, arm=arm)
    except (EvaluationError, OSError, ValueError, TypeError, KeyError) as exc:
        if arm == "qmd.search":
            raise EvaluationError("lexical arm unexpectedly depends on the absent unused embedding role") from exc
        return {"status": "EXPECTED_MISSING", "role": role, "path": str(missing), "reason": str(exc),
                "download_fallback": "forbidden; resource preflight failed before engine invocation"}
    if arm == "qmd.search":
        return {"status": "NOT_AFFECTED_UNUSED_ROLE", "role": role, "path": str(missing), "reason": "registered lexical search uses no embedding role"}
    raise EvaluationError("required absent dependency was incorrectly accepted")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3:
            raise EvaluationError("fixed request path and SHA required")
        request = read(reference({"path": sys.argv[1], "sha256": sys.argv[2]}))
        print(json.dumps(probe(request), ensure_ascii=False, allow_nan=False))
    except (EvaluationError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "MISSING", "error": str(exc)}))
        raise SystemExit(2)
