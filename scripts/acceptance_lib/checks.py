"""Read-only checks and raw metric adapters. No self-reported PASS is accepted."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .evidence import (InvalidEvidence, external_ref, is_sha, process_status,
                       read_command_run, read_json, sha256)


class Inputs:
    ALLOWED = {"schema", "kind", "path_base", "models", "legacy", "full_media",
               "product", "public", "answer", "assets", "evidence", "hosted"}

    def __init__(self, data: dict | None = None):
        self.data = data or {}
        if data is not None:
            if set(data) - self.ALLOWED or data.get("schema") != 2:
                raise InvalidEvidence("inputs require schema2 with known fields")
            base = data.get("path_base")
            if not isinstance(base, str) or not Path(base).is_absolute() or not Path(base).is_dir():
                raise InvalidEvidence("inputs.path_base must be an existing explicit absolute directory")
        self.base = Path(self.data.get("path_base", ".")).resolve()

    def get(self, dotted: str):
        value = self.data
        for key in dotted.split("."):
            if not isinstance(value, dict) or key not in value or value[key] is None:
                raise InvalidEvidence(f"missing explicit input: {dotted}")
            value = value[key]
        return value

    def file(self, dotted: str, *, manifest: bool = False) -> Path:
        return external_ref(self.base, self.get(dotted), manifest=manifest)


def history(root: Path) -> dict:
    expected = read_json(root / "docs/execution-plan/baseline-20261021.json")["sha256"]
    verified = {}
    for name, pin in expected.items():
        current = root / name
        historical = root / "evals/history/20261021" / Path(name).name
        for file in (current, historical):
            if not file.is_file() or sha256(file) != pin:
                raise InvalidEvidence(f"historical bytes missing/changed: {file}")
        verified[name] = pin
    return {"verified": verified}


def plan(root: Path, targets: dict) -> dict:
    tasks = read_json(root / "docs/execution-plan/tasks.json")["tasks"]
    ids = [t["id"] for t in tasks]
    if len(ids) != 24 or len(set(ids)) != 24:
        raise InvalidEvidence("plan must register exactly 24 unique tasks")
    dependencies = {t["id"]: t["depends_on"] for t in tasks}
    seen, active = set(), set()

    def visit(item):
        if item not in dependencies or item in active:
            raise InvalidEvidence("unknown dependency or dependency cycle")
        if item not in seen:
            active.add(item)
            for dep in dependencies[item]:
                visit(dep)
            active.remove(item)
            seen.add(item)

    for item in ids:
        visit(item)
    contract = root / "docs/execution-plan/CONTRACTS.md"
    if targets.get("contract_sha256") != sha256(contract):
        raise InvalidEvidence("acceptance target contract SHA changed; reconcile specs explicitly")
    return {"tasks": len(ids), "contract_sha256": sha256(contract)}


def public_assets(root: Path, inputs: Inputs) -> dict:
    manifest_path = inputs.file("public.cache_manifest", manifest=True)
    manifest = read_json(manifest_path)
    expected = read_json(root / "evals/public_manifest.json")["datasets"]
    datasets = manifest.get("datasets")
    if not isinstance(datasets, dict) or set(datasets) != set(expected):
        raise InvalidEvidence("public asset manifest must contain all three pinned datasets")
    from .evidence import verify_artifact
    verified = {}
    for name, pin in expected.items():
        entry = datasets[name]
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "bytes"}:
            raise InvalidEvidence("public cache entry needs path/sha256/bytes")
        path = verify_artifact(manifest_path.parent, {k: entry[k] for k in ("path", "sha256")})
        if entry["sha256"] != pin["sha256"] or entry["bytes"] != pin["bytes"] or path.stat().st_size != pin["bytes"]:
            raise InvalidEvidence(f"public cache pin mismatch: {name}")
        verified[name] = entry["sha256"]
    return {"datasets": verified}


def _valid_identity(manifest: dict) -> dict:
    identity = manifest["identity"]
    required = {"code_commit", "code_diff_sha256", "corpus_sha256", "queries_sha256",
                "split_sha256", "index_sha256", "embedding_sha256"}
    if not isinstance(identity, dict) or not required.issubset(identity):
        raise InvalidEvidence("evaluation run lacks code/corpus/query/split/index/model identity")
    commit = identity["code_commit"]
    if not isinstance(commit, str) or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise InvalidEvidence("evaluation code_commit must be full SHA")
    for key in required - {"code_commit", "code_diff_sha256"}:
        if not is_sha(identity[key]):
            raise InvalidEvidence(f"evaluation identity lacks valid {key}")
    if identity["code_diff_sha256"] is not None and not is_sha(identity["code_diff_sha256"]):
        raise InvalidEvidence("evaluation diff identity invalid")
    return identity


def _rows(result: dict, *, answerable: int, noise: int) -> list[dict]:
    rows = result.get("rows")
    if not isinstance(rows, list) or len(rows) != answerable + noise:
        raise InvalidEvidence("raw per-query denominator incomplete")
    ids, clean = set(), []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in ids:
            raise InvalidEvidence("duplicate/missing per-query id")
        ids.add(row["id"])
        if type(row.get("noise")) is not bool or type(row.get("n_hits")) is not int or row["n_hits"] < 0:
            raise InvalidEvidence("invalid noise/n_hits contribution")
        for key in ("success1", "success5", "rr"):
            value = row.get(key)
            if type(value) not in (int, float) or not 0 <= value <= 1:
                raise InvalidEvidence(f"invalid per-query {key}")
        if row["success1"] not in (0, 1) or row["success5"] not in (0, 1):
            raise InvalidEvidence("success contribution must be binary")
        if row["success1"] > row["success5"]:
            raise InvalidEvidence("success@1 exceeds success@5")
        ndcg = row.get("ndcg")
        if not row["noise"] and (type(ndcg) not in (int, float) or not 0 <= ndcg <= 1):
            raise InvalidEvidence("invalid nDCG contribution")
        clean.append({"query": {"id": row["id"], "tag": row.get("tag", "noise" if row["noise"] else "semantic")},
                      "metrics": {k: row[k] for k in ("noise", "n_hits", "success1", "success5", "rr", "ndcg")}})
    if sum(r["metrics"]["noise"] for r in clean) != noise:
        raise InvalidEvidence("noise/answerable denominator mismatch")
    return clean


def paired(inputs: Inputs, name: str, targets: dict) -> tuple[str, dict]:
    import eval_real as er
    suite, n, noise = ("legacy.v3", 145, 5) if name == "legacy" else ("media-full.v3", 164, 5)
    manifests, results = [], []
    source = inputs.file(f"{name}.source_manifest")
    for arm in ("reference", "candidate"):
        path = inputs.file(f"{name}.{arm}_run", manifest=True)
        manifest, receipt, stdout = read_command_run(path, suite_id=suite)
        status, reason = process_status(receipt)
        if status != "PASS":
            return status, {"reason": reason, "arm": arm}
        identity = _valid_identity(manifest)
        index_ref = inputs.get(f"{name}.{arm}_index")
        if not isinstance(index_ref, dict) or set(index_ref) != {"path", "sha256", "identity_manifest"}:
            raise InvalidEvidence("index needs file pin and identity_manifest")
        inputs.file("models.embedding")
        index = external_ref(inputs.base, {k: index_ref[k] for k in ("path", "sha256")})
        identity_path = external_ref(inputs.base, index_ref["identity_manifest"], manifest=True)
        pinned_identity = read_json(identity_path)
        if pinned_identity != identity or identity["index_sha256"] != sha256(index):
            raise InvalidEvidence("run/index identity mismatch")
        if identity["corpus_sha256"] != sha256(source):
            raise InvalidEvidence("source corpus identity mismatch")
        if identity["embedding_sha256"] != inputs.get("models.embedding.sha256"):
            raise InvalidEvidence("embedding identity mismatch")
        result = read_json(stdout)
        if result.get("backend") != "llama":
            raise InvalidEvidence("formal retrieval requires real llama, not mock")
        manifests.append(manifest)
        results.append(_rows(result, answerable=n, noise=noise))
    if manifests[0]["run_id"] == manifests[1]["run_id"]:
        raise InvalidEvidence("same run cannot serve as independent paired arms")
    for key in ("corpus_sha256", "queries_sha256", "split_sha256", "embedding_sha256"):
        if manifests[0]["identity"][key] != manifests[1]["identity"][key]:
            raise InvalidEvidence(f"reference/candidate differ in invariant {key}")
    if {r["query"]["id"] for r in results[0]} != {r["query"]["id"] for r in results[1]}:
        raise InvalidEvidence("paired query ids differ; no automatic intersection")
    current = er.summarize(results[1])
    baseline = {"metrics": er.summarize(results[0]), "per_query": er.per_query_maps(results[0])}
    gate = er.compare_to_baseline(current, er.per_query_maps(results[1]), baseline)
    return ("FAIL" if gate["tripped"] else "PASS"), {"suite_id": suite,
        "answerable": n, "noise": noise, "reference": baseline["metrics"],
        "candidate": current, "gate": gate, "run_ids": [m["run_id"] for m in manifests]}


def percentile(samples: list[float], p: float) -> float:
    if not samples or not 0 < p <= 1:
        raise InvalidEvidence("percentile needs samples and 0<p<=1")
    return sorted(samples)[math.ceil(p * len(samples)) - 1]


def latency(inputs: Inputs, targets: dict) -> tuple[str, dict]:
    path = inputs.file("evidence.latency", manifest=True)
    manifest, receipt, stdout = read_command_run(path, suite_id="latency.fast.v1")
    status, reason = process_status(receipt)
    if status != "PASS":
        return status, {"reason": reason}
    result = read_json(stdout)
    if result.get("schema") != 1 or result.get("quantile") != "nearest-rank":
        raise InvalidEvidence("latency result needs schema1/nearest-rank")
    samples = result.get("samples")
    if not isinstance(samples, list) or len(samples) < 60 or len(samples) % 2:
        raise InvalidEvidence("warm/cold each need at least30 paired samples")
    arms = {"warm": [], "cold": []}
    failures = 0
    for i in range(0, len(samples), 2):
        pair = samples[i:i + 2]
        if {x.get("arm") for x in pair} != {"warm", "cold"} or pair[0].get("query_id") != pair[1].get("query_id"):
            raise InvalidEvidence("latency samples must interleave warm/cold for the same query")
        for sample in pair:
            duration = sample.get("wall_ms")
            if type(duration) not in (int, float) or duration <= 0:
                raise InvalidEvidence("invalid latency duration")
            if type(sample.get("exit_code")) is not int or type(sample.get("degraded")) is not bool:
                raise InvalidEvidence("latency sample lacks actual process/degradation status")
            failures += sample["exit_code"] != 0 or sample["degraded"] or sample.get("json_valid") is not True
            arms[sample["arm"]].append(duration)
    if len(arms["warm"]) < targets["latency"]["samples_per_arm"]:
        raise InvalidEvidence("too few latency samples")
    metrics = {f"{arm}_p{int(p*100)}_ms": percentile(values, p)
               for arm, values in arms.items() for p in (0.5, 0.95)}
    thresholds = targets["latency"]
    passed = failures == 0 and metrics["warm_p50_ms"] <= thresholds["fast_p50_ms"] and metrics["warm_p95_ms"] <= thresholds["fast_p95_ms"] and metrics["warm_p50_ms"] <= metrics["cold_p50_ms"] / thresholds["cold_ratio"]
    return ("PASS" if passed else "FAIL"), {**metrics, "n_per_arm": len(arms["warm"]),
                                            "failures": failures, "run_id": manifest["run_id"]}
