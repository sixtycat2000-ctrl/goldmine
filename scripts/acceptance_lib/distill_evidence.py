"""Recompute real-topic/excerpt/incremental results from the pinned original inputs."""
from pathlib import Path
import json

from .evidence import InvalidEvidence, read_command_run, read_json, sha256


def check(inputs, targets):
    from distill_eval.runner import evaluate
    from search_bench.session import package_identity

    root = Path(__file__).resolve().parents[2]
    manifest, receipt, stdout = read_command_run(inputs.file("evidence.distill", manifest=True), suite_id="distill.real.v1")
    if receipt["exit_code"] not in (0, 1) or receipt["timed_out"]:
        raise InvalidEvidence("real distill command incomplete")
    actual = manifest["identity"]
    if actual.get("source_files") != package_identity(root) or actual.get("tool_sha256") != sha256(root / "scripts/eval_distill.py"):
        raise InvalidEvidence("distill evidence uses a different candidate or evaluator")
    argv = receipt["argv"]
    arguments = {}
    for name in ("--report-real", "--labels", "--corpus-root", "--incremental-db"):
        if argv.count(name) != 1 or argv.index(name) + 1 >= len(argv):
            raise InvalidEvidence("real distill command lacks frozen raw input arguments")
        arguments[name] = Path(argv[argv.index(name) + 1])
        if not arguments[name].is_absolute():
            raise InvalidEvidence("real distill raw inputs must be explicit absolute paths")
    raw = read_json(stdout)
    if raw.get("schema") != 2 or raw.get("kind") != "goldmine.distill-real" or raw.get("evidence_mode") != "real":
        raise InvalidEvidence("distill evidence is synthetic or diagnostic-only")
    if raw.get("fingerprint", {}).get("model_sha256") != sha256(inputs.file("models.embedding")):
        raise InvalidEvidence("distill index model differs from pinned embedding asset")
    expected = json.loads(json.dumps(evaluate(arguments["--report-real"], arguments["--labels"],
        arguments["--corpus-root"], arguments["--incremental-db"]), allow_nan=False))
    # Timers are observed values. Quality/provenance/memberships are reproduced
    # from the original inputs, never accepted from a PASS string.
    for key in set(raw) | set(expected):
        if key not in ("wall_ms", "incremental_wall_ms") and raw.get(key) != expected.get(key):
            raise InvalidEvidence(f"distill raw report differs from input replay: {key}")
    return expected["status"], {"metrics": expected["metrics"], "gates": expected["gates"],
        "labels_sha256": expected["labels_sha256"], "source_index_sha256": expected["source_index_sha256"],
        "run_id": manifest["run_id"]}
