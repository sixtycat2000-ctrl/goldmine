"""Complete immutable asset registrations, never reduced role summaries."""
from pathlib import Path
import stat

from answer_eval.protocol import EvaluationError, fields, sha256_file
from search_comparison.engine_capture.protocol import read, reference
from search_comparison.source_views.run import write_new

SCHEMA = "comparison.complete-engine-registration/v1"
ENGINES = ("goldmine", "qmd", "recoll")


def registration(owner, plan, *, replay=False):
    root = Path(owner) / "registered-assets"
    if not replay:
        root.mkdir()
    result = {}
    for name in ENGINES:
        path = root / (name + ".json")
        expected = {"schema": 1, "suite_id": SCHEMA, "engine": name,
                    "plan_sha256": plan.sha256, "complete_descriptor": plan.value["engines"][name]}
        if not replay:
            write_new(path, expected)
            path.chmod(0o444)
        if path.is_symlink() or path.resolve() != path or stat.S_IMODE(path.stat().st_mode) & 0o222:
            raise EvaluationError("complete engine registration must be canonical, regular and read-only")
        value = read(path, limit=16 * 1024 * 1024)
        if value != expected:
            raise EvaluationError("complete registered inventory differs from the frozen plan")
        result[name] = {"schema": SCHEMA, "engine": name,
                        "artifact": {"path": str(path), "sha256": sha256_file(path)}}
    return result


def descriptor(binding, plan):
    fields(binding, {"schema", "engine", "artifact"}, "complete engine binding")
    if binding["schema"] != SCHEMA or binding["engine"] not in ENGINES:
        raise EvaluationError("unknown registered engine role/schema")
    path = reference(binding["artifact"], bound=16 * 1024 * 1024)
    if stat.S_IMODE(path.stat().st_mode) & 0o222:
        raise EvaluationError("registered descriptor became writable")
    expected = {"schema": 1, "suite_id": SCHEMA, "engine": binding["engine"],
                "plan_sha256": plan.sha256, "complete_descriptor": plan.value["engines"][binding["engine"]]}
    if read(path, limit=16 * 1024 * 1024) != expected:
        raise EvaluationError("registered descriptor omitted/changed a frozen field or entry")
    return expected["complete_descriptor"]
