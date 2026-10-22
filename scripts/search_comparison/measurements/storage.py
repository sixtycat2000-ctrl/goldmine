"""Fixed bounded shards; raw material never disappears to fit an aggregate."""
import json
from pathlib import Path

from answer_eval.protocol import EvaluationError, canonical_sha256, parse_json, sha256_file
from search_comparison.engine_capture.protocol import local_path, reference, read, inventory
from search_comparison.source_views.run import write_new

RECORD_BYTES = 64 * 1024 * 1024
LEDGER_FIELDS = ("commands", "resource_observations", "events", "requests")
ROOT_MANIFEST_BYTES = 8 * 1024 * 1024
ROOT_ADMIN_BYTES = 16 * 1024 * 1024


class RecordLimit(EvaluationError):
    def __init__(self, size):
        self.size = size
        super().__init__("typed derived object exceeds64MiB accepted record cap; full raw commands/snapshots retained", status="MISSING")


def object_file(path, value, *, replay=False):
    path = local_path(str(path))
    if replay:
        if canonical_sha256(read(path, limit=RECORD_BYTES)) != canonical_sha256(value):
            raise EvaluationError("retained typed object differs from raw replay: " + str(path))
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if len(data) > RECORD_BYTES:
            raise RecordLimit(len(data))
        with path.open("xb") as stream:
            stream.write(data)
    return {"path": str(path), "sha256": sha256_file(path)}


def bounded_object(path, value, *, replay=False):
    size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()) + 1
    if size > RECORD_BYTES:
        if Path(path).exists():
            raise EvaluationError("over-limit derived object unexpectedly has a substituted artifact")
        return {"status": "MISSING_DERIVED_RECORD_LIMIT", "path": str(path), "serialized_bytes": size,
                "accepted_record_bytes": RECORD_BYTES, "retained_authority": "complete original raw command streams and closed index snapshots"}
    return object_file(path, value, replay=replay)


def lines(path, values, *, replay=False):
    path = local_path(str(path))
    if not isinstance(values, list) or len(values) > 50000:
        raise EvaluationError("typed stream exceeds the preregistered finite record count")
    if replay:
        actual = read_lines({"path": str(path), "sha256": sha256_file(path), "rows": len(values)}, len(values))
        if canonical_sha256(actual) != canonical_sha256(values):
            raise EvaluationError("fixed stream differs from attempted raw replay")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            for value in values:
                data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode() + b"\n"
                if len(data) > RECORD_BYTES:
                    raise EvaluationError("one typed stream record exceeds64MiB; partial stream retained")
                stream.write(data)
    return {"path": str(path), "sha256": sha256_file(path), "rows": len(values)}


def read_lines(ref, count):
    if (not isinstance(ref, dict) or set(ref) != {"path", "sha256", "rows"} or type(count) is not int
            or not 0 <= count <= 50000 or ref["rows"] != count):
        raise EvaluationError("typed stream must retain its exact fixed denominator")
    path = reference({k: ref[k] for k in ("path", "sha256")})
    result = []
    with path.open("rb") as stream:
        for n in range(count):
            line = stream.readline(RECORD_BYTES + 1)
            if not line.endswith(b"\n") or len(line) > RECORD_BYTES or line == b"\n":
                raise EvaluationError("truncated/oversized/blank typed stream record")
            result.append(parse_json(line.decode("utf-8"), "typed stream record"))
        if stream.read(1):
            raise EvaluationError("extra typed stream rows beyond the fixed registry")
    return result


def result(cell, slot, value):
    if value["result"] is not None:
        path = cell.owner / "results" / f"{slot['ordinal']:05d}.json"
        ref = bounded_object(path, value["result"], replay=cell.replay is not None)
        value = {**value, "result": ref}
        if ref.get("status") == "MISSING_DERIVED_RECORD_LIMIT":
            value.update(status="MISSING", error="derived result record cap exceeded; complete raw output retained",
                         storage_error="accepted derived record limit exceeded")
    return value


def seal_cell(cell, value):
    # The caller keeps these same dispositions for final aggregation; a
    # storage-limit MISSING cannot be hidden behind the pre-storage success.
    value["updates"] = dict(value["updates"])
    value["updates"]["stages"] = {name: bounded_object(cell.owner / "stage-data" / (name + ".json"), stage, replay=cell.replay is not None)
                                  for name, stage in value["updates"].get("stages", {}).items()}
    if any(v.get("status") == "MISSING_DERIVED_RECORD_LIMIT" for v in value["updates"]["stages"].values()):
        value["updates"]["status"] = "MISSING"
    dependency = dict(value["dependency"])
    if dependency.get("stage") is not None:
        dependency["stage"] = bounded_object(cell.owner / "stage-data/dependency_ocr.json", dependency["stage"], replay=cell.replay is not None)
        if dependency["stage"].get("status") == "MISSING_DERIVED_RECORD_LIMIT":
            dependency["status"] = "MISSING"
    value["dependency"] = dependency
    for key in LEDGER_FIELDS:
        value[key] = lines(cell.owner / "ledger" / (key + ".jsonl"), value[key], replay=cell.replay is not None)
    files = inventory(cell.owner)
    files.pop("cell.json", None)
    files.pop("inventory.jsonl", None)
    value["artifacts"] = lines(cell.owner / "inventory.jsonl", [{"path": p, "file": row} for p, row in files.items()], replay=cell.replay is not None)
    return object_file(cell.owner / "cell.json", value, replay=cell.replay is not None)


def load_cell(ref, owner):
    path = reference(ref, bound=RECORD_BYTES)
    if path != owner / "cell.json":
        raise EvaluationError("cell registry points outside the fixed owned path")
    value = read(path, limit=RECORD_BYTES)
    artifacts = value["artifacts"]
    if artifacts["path"] != str(owner / "inventory.jsonl"):
        raise EvaluationError("cell inventory shard escaped fixed path")
    actual_files = inventory(owner)
    actual_files.pop("cell.json", None); actual_files.pop("inventory.jsonl", None)
    if read_lines(artifacts, artifacts["rows"]) != [{"path": p, "file": row} for p, row in actual_files.items()]:
        raise EvaluationError("owned cell has omitted/changed/extra artifacts")
    for key in LEDGER_FIELDS:
        stream = value[key]
        if stream["path"] != str(owner / "ledger" / (key + ".jsonl")):
            raise EvaluationError("ledger stream path differs from fixed schema")
        value[key] = read_lines(stream, stream["rows"])
    for name, stage in value["updates"]["stages"].items():
        if stage.get("status") == "MISSING_DERIVED_RECORD_LIMIT":
            if stage["path"] != str(owner / "stage-data" / (name + ".json")) or Path(stage["path"]).exists() or value["updates"]["status"] != "MISSING":
                raise EvaluationError("derived overflow marker path/status was substituted")
            continue
        if reference(stage, bound=RECORD_BYTES) != owner / "stage-data" / (name + ".json"):
            raise EvaluationError("stage shard escaped its registered name")
        value["updates"]["stages"][name] = read(stage["path"], limit=RECORD_BYTES)
    dependency = value["dependency"]
    if dependency.get("stage") is not None:
        if dependency["stage"].get("status") == "MISSING_DERIVED_RECORD_LIMIT":
            if dependency["stage"]["path"] != str(owner / "stage-data/dependency_ocr.json") or Path(dependency["stage"]["path"]).exists() or dependency["status"] != "MISSING":
                raise EvaluationError("dependency overflow marker path/status was substituted")
            return value
        if reference(dependency["stage"], bound=RECORD_BYTES) != owner / "stage-data/dependency_ocr.json":
            raise EvaluationError("dependency stage shard escaped fixed path")
        dependency["stage"] = read(dependency["stage"]["path"], limit=RECORD_BYTES)
    return value


def disk(owner):
    files = inventory(owner)
    return {"logical_bytes": sum(r["bytes"] for r in files.values()),
            "allocated_bytes": sum((Path(owner) / p).stat().st_blocks * 512 for p in files), "files": len(files)}


def root_accounting(report):
    files = report["artifacts"]
    size = sum(r["bytes"] for r in files.values())
    allocated = sum(r["allocated_bytes"] for r in files.values())
    report["root_administration"] = {"root_manifest_limit_bytes": ROOT_MANIFEST_BYTES, "total_limit_bytes": ROOT_ADMIN_BYTES,
                                     "non_manifest_logical_bytes": size, "non_manifest_allocated_bytes": allocated,
                                     "manifest_allocation_reserve_bytes": ROOT_MANIFEST_BYTES, "status": "PASS",
                                     "method": "complete admin files plus actual serialized manifest/logical cap and conservative full manifest allocation reserve; separate from each entire cell4GiB"}
    encoded = len(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode()) + 1
    if encoded > ROOT_MANIFEST_BYTES or size + encoded > ROOT_ADMIN_BYTES or allocated + ROOT_MANIFEST_BYTES > ROOT_ADMIN_BYTES:
        report["root_administration"]["status"] = "FAIL"
        report.update(status="MISSING", full_comparison_status="MISSING", resource_status="FAIL")
    return report
