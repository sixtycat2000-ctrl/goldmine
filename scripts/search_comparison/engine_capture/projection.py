"""Explicit fixed dev exporter; engine workers never open original420 labels."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys

from acceptance_lib.evidence import artifact, capture_command, read_command_run, verify_artifact
from answer_eval.protocol import EvaluationError, canonical_sha256, fields, parse_json, sha256_file
from product_eval.bundle import relative_path
from product_eval.schema import validate_query
from product_eval.splits import validate_splits
from search_comparison.source_views.run import owned_output, write_new
from search_comparison.source_views.sources import load_sources, read_bounded
from .identity import implementation
from .protocol import CLASSES, SUITE, read, reference

QUERY_SHA = "44bea88ae1ffecfd9363eb235a0c8f6c55d9162c77beb6477d5b3cafde5ec947"
SPLIT_SHA = "c2685d6a27c23420d51d3567b6388dff0bfa89b9d6dfd60edd344d4dc7b48cc8"


def _input(bundle, key):
    ref = bundle.manifest[key]
    relative_path(ref["path"], "fixed label input")
    path = bundle.root / ref["path"]
    if not path.resolve().is_relative_to(bundle.root) or path.is_symlink() or sha256_file(path) != ref["sha256"]:
        raise EvaluationError("fixed exporter label input changed", status="MISSING")
    return path


def export_worker(bundle_path, pin, output, *, fixture=False):
    bundle = load_sources(bundle_path, pin, fixture=fixture)
    if not fixture and (bundle.manifest["queries"]["sha256"] != QUERY_SHA or bundle.manifest["splits"]["sha256"] != SPLIT_SHA):
        raise EvaluationError("dev export requires the exact preregistered420 input pins", status="MISSING")
    query_file, split_file = _input(bundle, "queries"), _input(bundle, "splits")
    queries = [parse_json(line, "frozen query") for line in read_bounded(query_file).decode().splitlines() if line.strip()]
    for q in queries:
        validate_query(q)
    lookup = {q["id"]: q for q in queries}
    if len(lookup) != len(queries):
        raise EvaluationError("duplicate original query IDs")
    split = read(split_file)
    if not fixture:
        validate_splits(split, queries)
    elif (split.get("queries_sha256") != canonical_sha256(queries) or set(split.get("dev", [])) & set(split.get("holdout", []))
          or set(split.get("dev", [])) | set(split.get("holdout", [])) != set(lookup)):
        raise EvaluationError("fixture split partition/identity differs")
    ids = split["dev"]
    if not isinstance(ids, list) or any(not isinstance(i, str) or i not in lookup for i in ids) or len(ids) != len(set(ids)):
        raise EvaluationError("dev split contains unknown/duplicate IDs")
    selected = [lookup[i] for i in ids]
    if not fixture and (len(queries) != 420 or len(ids) != 210 or Counter(q["primary_class"] for q in selected) != Counter({c: 30 for c in CLASSES})):
        raise EvaluationError("fixed210 dev category denominator changed")
    requests = [{k: q[k] for k in ("id", "q", "primary_class", "source_group")} for q in selected]
    qrels = []
    for q in selected:
        grades = {}
        for e in q["expect"]:
            if e["path"] not in bundle.sources:
                raise EvaluationError("qrel points outside frozen original source inventory")
            grades[e["path"]] = max(grades.get(e["path"], 0), e["grade"])
        qrels.append({"id": q["id"], "relevance": grades})
    output = Path(output).resolve()
    if output.is_relative_to(bundle.root) or bundle.root.is_relative_to(output):
        raise EvaluationError("export output cannot overlap original source/label tree")
    output.mkdir(parents=True, exist_ok=False)
    write_new(output / "requests.json", requests)
    write_new(output / "qrels.json", qrels)
    report = {"schema": 1, "suite_id": SUITE, "scope": "dev_only", "evidence_mode": "fixture" if fixture else "real",
              "source_identity": bundle.identity, "implementation": implementation(), "ids": ids,
              "requests": artifact(output, output / "requests.json"), "qrels": artifact(output, output / "qrels.json"),
              "status": "MECHANICAL_DEV_PROJECTION" if fixture else "DEV_PROJECTION_EXPORTED", "release_eligible": False}
    write_new(output / "selection.json", report)
    return report


def capture_export(bundle_path, pin, output, allowed_parent, *, fixture=False):
    bundle = load_sources(bundle_path, pin, fixture=fixture)
    owner = owned_output(output, allowed_parent, bundle)
    owner.mkdir()
    root = Path(__file__).resolve().parents[3]
    argv = [str(Path(sys.executable).resolve()), str(root / "scripts/capture_comparison_engines.py"), "export-worker",
            "--bundle", str(bundle.root), "--manifest-sha256", pin, "--out", str(owner / "raw/output")]
    if fixture:
        argv.append("--fixture")
    capture_command(owner / "raw", "comparison.dev-export", argv, cwd=root,
                    env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
                    identity={"source_identity": bundle.identity, "implementation": implementation()},
                    suite_id=SUITE, timeout=60, outputs={"selection": "output/selection.json"}, owned_process_group=True)
    return read_projection({"path": str(owner / "raw/manifest.json"), "sha256": sha256_file(owner / "raw/manifest.json")}, bundle)


def read_projection(ref, bundle):
    """Disk-only validation against the separately captured, frozen export ref."""
    manifest_path = reference(ref, bound=8 * 1024 * 1024)
    manifest, receipt, stdout = read_command_run(manifest_path, check_id="comparison.dev-export", suite_id=SUITE)
    owner = manifest_path.parent.parent
    root = Path(__file__).resolve().parents[3]
    expected = [str(Path(sys.executable).resolve()), str(root / "scripts/capture_comparison_engines.py"), "export-worker",
                "--bundle", str(bundle.root), "--manifest-sha256", bundle.identity["manifest_sha256"], "--out", str(owner / "raw/output")]
    if bundle.fixture:
        expected.append("--fixture")
    if (receipt["exit_code"] != 0 or receipt["timed_out"] or receipt["argv"] != expected
            or manifest["identity"] != {"source_identity": bundle.identity, "implementation": implementation()}):
        raise EvaluationError("dev exporter command/input/code identity differs", status="MISSING")
    # Capture artifacts are run-relative. selection lives inside raw/output so
    # the lifecycle/evidence boundary never accepts an escaping artifact ref.
    selection = verify_artifact(manifest_path.parent, receipt["artifacts"].get("selection"))
    report = read(selection)
    if report != read(stdout):
        raise EvaluationError("projection stdout differs from retained selection")
    fields(report, {"schema", "suite_id", "scope", "evidence_mode", "source_identity", "implementation", "ids",
                    "requests", "qrels", "status", "release_eligible"}, "dev projection")
    if (report["schema"] != 1 or report["suite_id"] != SUITE or report["scope"] != "dev_only" or report["source_identity"] != bundle.identity
            or report["implementation"] != implementation() or report["evidence_mode"] != ("fixture" if bundle.fixture else "real")
            or report["status"] != ("MECHANICAL_DEV_PROJECTION" if bundle.fixture else "DEV_PROJECTION_EXPORTED") or report["release_eligible"] is not False):
        raise EvaluationError("projection is not the fixed source-bound dev export")
    requests = read(verify_artifact(selection.parent, report["requests"]))
    qrels = read(verify_artifact(selection.parent, report["qrels"]))
    if not isinstance(requests, list) or not isinstance(qrels, list) or any(not isinstance(v, dict) for v in requests + qrels):
        raise EvaluationError("projection rows must all be objects")
    if (not requests or [r.get("id") for r in requests] != report["ids"] or [r.get("id") for r in qrels] != report["ids"]
            or len(report["ids"]) != len(set(report["ids"]))):
        raise EvaluationError("projection dev ID/order/denominator differs")
    for r, q in zip(requests, qrels):
        fields(r, {"id", "q", "primary_class", "source_group"}, "dev request")
        fields(q, {"id", "relevance"}, "dev qrels")
        if (r["primary_class"] not in CLASSES or any(not isinstance(r[k], str) or not r[k] for k in ("id", "q", "source_group"))
                or not isinstance(q["relevance"], dict) or set(q["relevance"]) - set(bundle.sources)
                or any(type(g) is not int or not 1 <= g <= 3 for g in q["relevance"].values())
                or (r["primary_class"] == "noise") != (not q["relevance"])):
            raise EvaluationError("dev request/qrel content shape differs")
    if not bundle.fixture and (len(requests) != 210 or Counter(r["primary_class"] for r in requests) != Counter({c: 30 for c in CLASSES})):
        raise EvaluationError("fixed210 dev category denominator differs")
    return {"reference": ref, "completed_at": receipt["finished_at"], "identity": {"selection_sha256": sha256_file(selection), "source_identity": bundle.identity,
            "requests_sha256": report["requests"]["sha256"], "qrels_sha256": report["qrels"]["sha256"], "ids_sha256": canonical_sha256(report["ids"])},
            "requests": requests, "qrels": qrels}
