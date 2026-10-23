"""A complete12-case capacity gate computed from actual raw commands."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
import re

from . import cli_replay, fixture_audit, phases
from .audit import check_rss, finite, hashed, load
from .output import json_value
from .protocol import (ARMS, CACHE, CLI_RUNS, DIM, DISTRIBUTIONS, PARAMETERS, SEED, SIZES,
                       WARMUPS, digest, sha, source_identity, tool_identity)


def _sources(data: dict, project: Path, expected_reference):
    if data.get("tools") != tool_identity(project) or data.get("tools_after") != data.get("tools"):
        raise ValueError("capacity capture tool changed; recapture after the harness freeze")
    sources = data.get("sources", {})
    if set(sources) != set(ARMS) or data.get("source_after") != sources:
        raise ValueError("capacity source changed during measurements")
    for arm in ARMS:
        value = sources[arm]
        if not isinstance(value, dict) or set(value) != {"files", "source_sha256"} or value["source_sha256"] != digest(value["files"]):
            raise ValueError("source manifest content digest is invalid")
        if not isinstance(value["files"], dict) or not {"goldmine/cli.py", "goldmine/store.py"}.issubset(value["files"]):
            raise ValueError("source manifest does not bind the actual CLI/Store")
        for name, pin in value["files"].items():
            if (not name.startswith("goldmine/") or not name.endswith(".py") or ".." in Path(name).parts or
                    not re.fullmatch(r"[0-9a-f]{64}", str(pin))):
                raise ValueError("unsafe source file identity")
        source = Path(data["source_roots"][arm])
        if not source.is_absolute() or source_identity(source) != value:
            raise ValueError("declared frozen source tree is missing/stale")
    if sources["candidate"] != source_identity(project):
        raise ValueError("capacity candidate is stale relative to current production content")
    if expected_reference is not None and sources["reference"]["files"] != expected_reference:
        raise ValueError("capacity reference is not the pinned exact pre-task source")
    if data["source_roots"]["reference"] == data["source_roots"]["candidate"]:
        raise ValueError("capacity arms cannot share the same source directory")
    python = data.get("python", {})
    if set(python) != {"path", "sha256"} or sha(Path(python["path"])) != python["sha256"]:
        raise ValueError("capacity Python runtime missing/changed")


def _cache(case_dir: Path, case: dict, data: dict, reports: dict, thresholds: dict):
    before, after = (reports[arm] for arm in ARMS)
    trigger = case["rows"] >= thresholds["vector_pack_min_vectors"] and (
        before["phases_ms"]["cold_load"]["p50"] > thresholds["trigger_cold_p50_ms"] or
        before["query_process_peak_rss"]["bytes"] > thresholds["trigger_rss_bytes"])
    if not data["cache_experiment"]:
        if case["cache_build"] is not None or case["cache_receipt"] is not None:
            raise ValueError("derived-cache artifacts supplied for an unrequested experiment")
        return {"status": "not_evaluated" if trigger else "not_required", "triggered": trigger, "measured": False,
                "cold_load_reduction": None, "peak_rss_reduction": None, "meets_required_benefit": None}
    result = load(case_dir, case["cache_build"])
    receipt = load(case_dir, case["cache_receipt"])
    directory = case_dir / "candidate"
    project = Path(__file__).resolve().parents[2]
    argv = [data["python"]["path"], str(project / "scripts/bench_scale.py"), "--full-cli", "--worker", "cache",
            "--source-root", data["source_roots"]["candidate"], "--db", str(directory / "fixture.sqlite"),
            "--result", str(directory / "cache.json")]
    if (receipt.get("argv") != argv or receipt.get("exit_code") != 0 or receipt.get("timed_out") is not False or
            result.get("status") != "BUILT" or result.get("enabled_after") is not True or result.get("rows") != case["rows"]):
        raise ValueError("cache experiment lacks an actual successful explicit build/enable receipt")
    for stream in ("stdout", "stderr"):
        if hashed(directory / "cache-build", receipt[stream]).read_bytes():
            raise ValueError("cache build emitted unexpected diagnostics")
    manifest_path = hashed(directory, result["manifest_copy"])
    manifest = json_value(manifest_path.read_bytes())
    if (sha(manifest_path) != result.get("manifest_sha256") or manifest.get("rows") != case["rows"] or
            manifest.get("identity") != result.get("identity") or manifest.get("dimension") != DIM):
        raise ValueError("cache build does not bind its actual published generation")
    check_rss(result["builder_peak_rss"], scope="cache-builder-only")
    time_gain = 1 - after["phases_ms"]["cold_load"]["p50"] / before["phases_ms"]["cold_load"]["p50"]
    rss_gain = 1 - after["query_process_peak_rss"]["bytes"] / before["query_process_peak_rss"]["bytes"]
    return {"status": "measured" if trigger else "not_required", "triggered": trigger, "measured": True,
            "cold_load_reduction": time_gain, "peak_rss_reduction": rss_gain,
            "meets_required_benefit": (time_gain >= thresholds["cache_min_time_improvement"] and
                                      rss_gain >= thresholds["cache_min_rss_improvement"]) if trigger else None}


def cache_gate(results):
    required = [item["cache"] for item in results if item["cache"]["triggered"]]
    if not required:
        return {"status": "not_required", "passed": None}
    if any(not item["measured"] for item in required):
        return {"status": "MISSING", "passed": None}
    passed = all(item["meets_required_benefit"] is True for item in required)
    return {"status": "PASS" if passed else "FAIL", "passed": passed}


def validate(path: Path, *, current_project: Path, expected_reference=None, targets=None, require_all=True) -> dict:
    path, project = path.resolve(), current_project.resolve()
    data = json_value(path.read_bytes())
    if not isinstance(data, dict) or data.get("schema") != 2 or data.get("kind") != "goldmine.scale.full/v2" or data.get("suite_id") != "scale.full.v2":
        raise ValueError("complete scale.full.v2 CLI/pack evidence required; vector-only reports remain MISSING")
    fields = {"schema", "kind", "suite_id", "started_at", "status", "source_roots", "sources", "tools", "python",
              "parameters", "seed", "dim", "warmups", "phase_runs", "cli_runs", "quantile", "cache_definition",
              "cache_experiment", "scope", "quality_claim", "environment", "cases", "source_after", "tools_after", "finished_at"}
    if set(data) not in (fields, fields | {"summary"}) or data["status"] not in ("PASS", "FAIL", "MISSING"):
        raise ValueError("full capacity manifest has missing/unknown fields")
    thresholds = targets or {"sizes": list(SIZES), "segments_per_file": list(DISTRIBUTIONS), "dimension": DIM,
        "seed": SEED, "warmups": WARMUPS, "runs": 10, "vector_pack_min_vectors": 250000,
        "trigger_cold_p50_ms": 500, "trigger_rss_bytes": 1610612736,
        "cache_min_time_improvement": .3, "cache_min_rss_improvement": .3}
    if (data.get("seed") != thresholds["seed"] or data.get("dim") != thresholds["dimension"] or
            data.get("warmups") != thresholds["warmups"] or data.get("parameters") != PARAMETERS or
            data.get("cache_definition") != CACHE or data.get("quantile") != "nearest-rank" or
            data.get("quality_claim") is not False or type(data.get("cache_experiment")) is not bool):
        raise ValueError("full capacity protocol/cache/quality scope differs from preregistered controls")
    if (type(data.get("phase_runs")) is not int or data["phase_runs"] < thresholds["runs"] or
            type(data.get("cli_runs")) is not int or data["cli_runs"] < CLI_RUNS):
        raise ValueError("all phases need10 runs and full CLI cold/warm each30 runs")
    for key in ("started_at", "finished_at"):
        stamp = dt.datetime.fromisoformat(data[key])
        if stamp.utcoffset() is None:
            raise ValueError("capacity timestamps require explicit timezone")
    if data["finished_at"] < data["started_at"]:
        raise ValueError("capacity suite finishes before it starts")
    _sources(data, project, expected_reference)
    cases, seen, results = data.get("cases"), set(), []
    if not isinstance(cases, list) or not cases:
        raise ValueError("no complete capacity cases captured")
    required = {(n, k) for n in thresholds["sizes"] for k in thresholds["segments_per_file"]}
    for entry in cases:
        case_path = hashed(path.parent, entry)
        case = json_value(case_path.read_bytes())
        fields = {"schema", "kind", "rows", "segments_per_file", "fixtures", "build_receipts", "phase_reports",
                  "phase_artifacts", "phase_order", "cache_build", "cache_receipt", "logical_before", "logical_after",
                  "final_snapshots", "environment", "environment_after", "samples", "warm_workers", "fixtures_retained"}
        if set(case) != fields or case.get("schema") != 2 or case.get("kind") != "goldmine.scale.case/v2":
            raise ValueError("unknown scale case schema")
        if type(case["fixtures_retained"]) is not bool:
            raise ValueError("fixture retention must be explicit")
        for snapshot in (case["environment"], case["environment_after"]):
            for key in ("os", "release", "arch", "python", "cpu_count"):
                if key not in snapshot or snapshot[key] != data["environment"].get(key):
                    raise ValueError("capacity arms/cases do not share the recorded host/runtime")
            if not isinstance(snapshot.get("pressure"), dict):
                raise ValueError("kernel pressure probe was not recorded")
        key = (case["rows"], case["segments_per_file"])
        if any(type(v) is not int or v <= 0 for v in key) or key in seen:
            raise ValueError("duplicate/invalid scale case")
        if require_all and key not in required:
            raise ValueError("unexpected small capacity fixture cannot replace a required case")
        seen.add(key)
        fixture_audit.validate(case_path.parent, case, data, project)
        reports, phase_same, pids = phases.validate(case_path.parent, case, data, project)
        cli = cli_replay.validate(case_path.parent, case, data, project)
        cache = _cache(case_path.parent, case, data, reports, thresholds)
        results.append({"rows": key[0], "segments_per_file": key[1], "phase_consistent": phase_same,
                        "cli": cli, "cache": cache})
    complete = seen == required
    if require_all and not complete:
        raise ValueError(f"capacity requires all12 size/distribution cases; missing {sorted(required - seen)}")
    incomplete = any(item["cli"]["failures"] for item in results)
    consistent = all(item["phase_consistent"] and item["cli"]["consistent"] for item in results)
    benefit = cache_gate(results)
    status = ("MISSING" if not complete or incomplete or benefit["status"] == "MISSING" else
              "FAIL" if not consistent or benefit["status"] == "FAIL" else "PASS")
    summary = {"status": status, "complete12": complete, "consistent": consistent, "cache_benefit_pass": benefit["passed"],
               "cache_benefit_status": benefit["status"],
               "case_count": len(results), "quantile": "nearest-rank", "quality_claim": False, "cases": results}
    if "summary" in data and data["summary"] != summary:
        raise ValueError("capacity summary disagrees with replayed raw evidence")
    return summary
