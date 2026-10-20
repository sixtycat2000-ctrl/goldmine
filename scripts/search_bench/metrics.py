"""Validation and nearest-rank calculations shared by benchmark and verifier."""
from __future__ import annotations

import math
from pathlib import Path

from acceptance_lib.evidence import InvalidEvidence, read_command_run, read_json, verify_artifact


def percentile(values, p):
    if not values or not 0 < p <= 1:
        raise InvalidEvidence("percentile needs samples and 0<p<=1")
    return sorted(values)[math.ceil(len(values) * p) - 1]


def fast_metrics(samples: list, *, minimum=30) -> dict:
    if len(samples) < minimum * 2 or len(samples) % 2:
        raise InvalidEvidence("warm/cold require at least30 interleaved pairs")
    values = {"warm": [], "cold": []}
    incomplete = 0
    for offset in range(0, len(samples), 2):
        pair = samples[offset:offset + 2]
        if {r["arm"] for r in pair} != {"warm", "cold"} or pair[0]["query_id"] != pair[1]["query_id"]:
            raise InvalidEvidence("interleave exactly one warm/cold sample per query")
        for sample in pair:
            if type(sample.get("wall_ms")) not in (int, float) or sample["wall_ms"] <= 0:
                raise InvalidEvidence("sample needs a positive measured wall time")
            incomplete += sample.get("exit_code") != 0 or sample.get("degraded") is not False or sample.get("json_valid") is not True
            values[sample["arm"]].append(sample["wall_ms"])
    metrics = {f"{arm}_p{int(p*100)}_ms": percentile(walls, p)
               for arm, walls in values.items() for p in (.5, .95)}
    metrics.update({"samples_per_arm": len(values["warm"]), "incomplete": incomplete})
    return metrics


def mcp_metrics(samples: list, *, minimum=30) -> dict:
    if len(samples) < minimum:
        raise InvalidEvidence("MCP needs at least30 same-request direct/stdio pairs")
    differences = []
    failures = 0
    for sample in samples:
        direct, stdio = sample["direct_ms"], sample["stdio_ms"]
        if type(direct) not in (int, float) or type(stdio) not in (int, float) or min(direct, stdio) <= 0:
            raise InvalidEvidence("pair needs positive measured times")
        failures += sample.get("equal") is not True or sample.get("via") != "serve"
        differences.append(stdio - direct)
    return {"n_pairs": len(samples), "failures": failures,
            "paired_extra_p50_ms": percentile(differences, .5),
            "paired_extra_p95_ms": percentile(differences, .95)}


def verify_fast_samples(root: Path, samples: list, identity: dict) -> None:
    seen = set()
    for sample in samples:
        manifest_path = verify_artifact(root, sample["command"])
        manifest, receipt, stdout = read_command_run(manifest_path, suite_id="latency.sample.v1")
        if manifest["run_id"] in seen or manifest["identity"] != identity:
            raise InvalidEvidence("sample command was reused or belongs to another index/code/model")
        seen.add(manifest["run_id"])
        if receipt.get("wall_ms") != sample["wall_ms"] or receipt["exit_code"] != sample["exit_code"]:
            raise InvalidEvidence("sample time/status disagrees with actual subprocess receipt")
        argv = receipt["argv"]
        if "search" not in argv or sample["query"] not in argv or ("--no-serve" in argv) != (sample["arm"] == "cold"):
            raise InvalidEvidence("sample argv does not match requested query/arm")
        try:
            rows = read_json(stdout)
            valid = isinstance(rows, list)
        except InvalidEvidence:
            valid = False
        if sample["json_valid"] != valid:
            raise InvalidEvidence("sample JSON claim disagrees with raw stdout")
        stderr = verify_artifact(manifest_path.parent, receipt["stderr"]).read_text()
        via = "serve" if sample["arm"] == "warm" else "cold"
        if sample["degraded"] != (f"via: {via}" not in stderr):
            raise InvalidEvidence("sample degradation claim disagrees with raw stderr")


def verify_mcp_samples(root: Path, samples: list, identity: dict) -> None:
    seen = set()
    for sample in samples:
        path = verify_artifact(root, sample["raw_pair"])
        if path in seen:
            raise InvalidEvidence("MCP raw pair was reused")
        seen.add(path)
        raw = read_json(path)
        if raw.get("identity") != identity or raw.get("query_id") != sample.get("query_id"):
            raise InvalidEvidence("MCP pair identity/request mismatch")
        direct, stdio = raw["direct"], raw["stdio"]
        equal = direct["payload"] == stdio["payload"]
        via = stdio["payload"].get("metadata", {}).get("via")
        if (sample["direct_ms"] != direct["wall_ms"] or sample["stdio_ms"] != stdio["wall_ms"] or
                sample["equal"] != equal or sample["via"] != via):
            raise InvalidEvidence("MCP summary disagrees with same-request raw pair")
