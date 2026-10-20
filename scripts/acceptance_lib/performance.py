"""Recompute latency gates from each bound subprocess/request pair."""
from __future__ import annotations

from pathlib import Path
import hashlib
import io
import subprocess
import tarfile

from .evidence import InvalidEvidence, read_command_run, read_json, sha256


def _run(inputs, key, *, profile, verify_cli=True, reference=False):
    from search_bench.metrics import fast_metrics, verify_fast_samples, verify_mcp_samples
    from search_bench.session import package_identity

    path = inputs.file(key, manifest=True)
    manifest, receipt, stdout = read_command_run(path, suite_id="latency.fast.v2")
    if receipt["exit_code"] not in (0, 1) or receipt["timed_out"]:
        raise InvalidEvidence("latency command was incomplete; raw measurements are required")
    result = read_json(stdout)
    if (result.get("schema") != 2 or result.get("quantile") != "nearest-rank" or
            result.get("profile") != profile or result.get("kind") != "goldmine.search-latency"):
        raise InvalidEvidence("latency requires schema2/nearest-rank and the declared pressure profile")
    identity = result.get("identity", {})
    if identity.get("profile") != profile or manifest["identity"].get("source_files") != identity.get("source_files"):
        raise InvalidEvidence("latency command/source identity mismatch")
    if result.get("source_after") != identity.get("source_files"):
        raise InvalidEvidence("latency source changed during measurement")
    project = Path(__file__).resolve().parents[2]
    expected = _reference_source(project) if reference else package_identity(project)
    if identity.get("source_files") != expected:
        raise InvalidEvidence("latency source is not the current candidate or pinned pre-task reference")
    if identity.get("model_sha256") != sha256(inputs.file("models.embedding")):
        raise InvalidEvidence("latency model differs from the explicitly pinned model")
    root = path.parent.parent
    if verify_cli:
        verify_fast_samples(root, result.get("samples", []), identity)
        for sample in result["samples"]:
            from .evidence import verify_artifact

            command, raw, _ = read_command_run(verify_artifact(root, sample["command"]))
            override = raw["env"].get("GOLDMINE_SERVE_PRESSURE_LEVEL")
            if override != ("1" if profile == "controlled" else None):
                raise InvalidEvidence("real pressure must not be overridden; controlled profile must declare level1")
        measured = fast_metrics(result["samples"])
        if measured != result.get("fast_metrics"):
            raise InvalidEvidence("latency summary disagrees with raw subprocess times")
    else:
        verify_mcp_samples(root, result.get("mcp_pairs", []), identity)
    before, after = result.get("guard_before", {}), result.get("guard_after", {})
    if (not before.get("resident") or not after.get("resident") or
            before.get("guard", {}).get("unloads") != after.get("guard", {}).get("unloads")):
        raise InvalidEvidence("guard changed residency during required performance measurement")
    for environment in (result.get("environment", {}), result.get("environment_after", {})):
        if environment.get("pressure", {}).get("level") is None:
            raise InvalidEvidence("real kernel pressure was not measured")
    return result, manifest["run_id"]


def fast(inputs, targets):
    from search_bench.metrics import fast_metrics

    controlled, controlled_id = _run(inputs, "evidence.latency", profile="controlled")
    real, real_id = _run(inputs, "evidence.latency_real", profile="real")
    baseline, baseline_id = _run(inputs, "evidence.latency_reference", profile="controlled", reference=True)
    keys = ("model_sha256", "server_sha256", "source_index_sha256")
    if any(controlled["identity"].get(k) != other["identity"].get(k) for k in keys for other in (real, baseline)):
        raise InvalidEvidence("controlled/real/reference require the same source index/model/server")
    if controlled["queries_sha256"] != real["queries_sha256"] or controlled["queries_sha256"] != baseline["queries_sha256"]:
        raise InvalidEvidence("latency reference uses different queries")
    if controlled["identity"]["source_files"] != real["identity"]["source_files"]:
        raise InvalidEvidence("controlled and real pressure runs use different candidate code")
    if controlled["identity"]["source_files"] == baseline["identity"]["source_files"]:
        raise InvalidEvidence("latency baseline must be the declared pre-task source version")
    cm, rm, bm = (fast_metrics(result["samples"]) for result in (controlled, real, baseline))
    if cm["incomplete"] or rm["incomplete"] or bm["incomplete"]:
        return "MISSING", {"reason": "a required CLI sample failed or degraded"}
    thresholds = targets["latency"]
    regression = cm["warm_p50_ms"] / bm["warm_p50_ms"]
    passed = all(m["warm_p50_ms"] <= thresholds["fast_p50_ms"] and
                 m["warm_p95_ms"] <= thresholds["fast_p95_ms"] and
                 m["warm_p50_ms"] <= m["cold_p50_ms"] / thresholds["cold_ratio"] for m in (cm, rm))
    passed = passed and regression <= thresholds["task_max_regression_ratio"]
    return ("PASS" if passed else "FAIL"), {"controlled": cm, "real": rm, "reference": bm,
        "candidate_reference_p50_ratio": regression, "run_ids": [controlled_id, real_id, baseline_id]}


def mcp(inputs, targets):
    from search_bench.metrics import mcp_metrics

    result, run_id = _run(inputs, "evidence.mcp_latency", profile="controlled", verify_cli=False)
    metrics = mcp_metrics(result.get("mcp_pairs", []))
    if result.get("mcp_metrics") != metrics:
        raise InvalidEvidence("MCP summary differs from paired raw requests")
    passed = metrics["failures"] == 0 and metrics["paired_extra_p95_ms"] <= targets["latency"]["mcp_extra_p95_ms"]
    return ("PASS" if passed else "FAIL"), {**metrics, "run_id": run_id}


def _reference_source(project):
    revision = read_json(project / "docs/execution-plan/baseline-20261021.json")["source_commit"]
    proc = subprocess.run(["git", "archive", "--format=tar", revision, "goldmine"],
                          cwd=project, capture_output=True, check=False)
    if proc.returncode:
        raise InvalidEvidence("pinned pre-task source revision unavailable")
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as archive:
        return {member.name: hashlib.sha256(archive.extractfile(member).read()).hexdigest()
                for member in archive.getmembers() if member.isfile() and member.name.endswith(".py")}
