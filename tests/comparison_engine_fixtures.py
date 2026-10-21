"""Tiny declared synthetic dev fixtures; no actual420 labels or engines."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from answer_eval.protocol import canonical_sha256, sha256_file
from acceptance_lib.resource_policy import runtime_identity as observer_runtime
from product_eval.bundle import build_corpus
from product_eval.schema import CLASSES
from search_comparison.engine_capture.identity import implementation
from search_comparison.engine_capture.projection import capture_export
from search_comparison.engine_capture.protocol import CONTROLS, LIMITS, MANDATORY, SUITE
from search_comparison.source_views.recipes import runtime_identity
from comparison_source_fixtures import dump, make_fixture, ref


def question(category, qid):
    path, kind, tags, needs, evidence = "docs/a.md", "doc", [], [], "content_text"
    location = {"unit": "line", "start": 1, "end": 1}
    if category == "pdf-page":
        path, kind, tags = "pdf/a.pdf", "pdf", ["text_pdf"]
        location = {"unit": "page", "start": 2, "end": 2}
    if category == "media-text":
        path, kind, tags, needs, evidence = "audio/a.flac", "audio", ["asr"], ["asr"], "asr_text"
        location = {"unit": "second", "start": 0, "end": 1}
    if category == "cjk":
        tags = ["zh_only"]
    text = "DECLARED SYNTHETIC FACT"
    span = {"start_char": 0, "end_char": len(text), "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    expect = [{"evidence_id": "e1", "path": path, "kind": kind, "grade": 3, "evidence_type": evidence,
               "location": location, "span": span, "view_id": "independent-fixture-view"}]
    facts = [{"fact_id": "f1", "description": text, "sufficient_evidence": [["e1"]]}]
    if category == "compound":
        expect.append({**expect[0], "evidence_id": "e2"})
        facts.append({"fact_id": "f2", "description": "SECOND DECLARED FACT", "sufficient_evidence": [["e2"]]})
    if category == "noise":
        expect, facts, tags = [], [], ["near_topic"]
    return {"id": qid, "primary_class": category, "tags": tags, "q": "合成查询" if category == "cjk" else "synthetic bounded query " + qid,
            "expect": expect, "required_facts": facts, "needs": needs, "source_group": "fixture:" + category}


def data(root):
    fixture = make_fixture(root)
    queries = [question(c, prefix + str(i)) for prefix in ("dev", "unselected") for i, c in enumerate(CLASSES)]
    bundle = fixture["bundle"]
    query_path, split_path = bundle / "fixture-queries.jsonl", bundle / "fixture-splits.json"
    query_path.write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in queries))
    dump(split_path, {"queries_sha256": canonical_sha256(queries), "dev": [q["id"] for q in queries[:7]], "holdout": [q["id"] for q in queries[7:]]})
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["queries"] = {"path": query_path.name, "sha256": sha256_file(query_path)}
    manifest["splits"] = {"path": split_path.name, "sha256": sha256_file(split_path)}
    dump(bundle / "manifest.json", manifest)
    fixture["pin"] = sha256_file(bundle / "manifest.json")
    return fixture


def missing_plan(root, fixture):
    native = root / "native"
    # The source-only loader intentionally exposes only original sources and
    # opaque label refs; this test build exercises the reviewed native policy.
    from search_comparison.source_views.sources import load_sources
    bundle = load_sources(fixture["bundle"], fixture["pin"], fixture=True)
    build_corpus(bundle, native)
    projection = capture_export(bundle.root, fixture["pin"], root / "dev-export", root, fixture=True)
    plan = {"schema": 1, "suite_id": SUITE, "state": "FROZEN", "evidence_mode": "fixture",
            "registered_at": datetime.now(timezone.utc).isoformat(), "bundle": {"path": str(bundle.root), "manifest_sha256": fixture["pin"]},
            "projection": projection["reference"], "views": {"native": {"build": ref(native / "build.json")}, "common": None},
            "arms": list(MANDATORY + CONTROLS), "engines": {"goldmine": None, "qmd": None, "recoll": None},
            "limits": LIMITS, "offline": True, "fallback": "forbidden", "runtime": runtime_identity(), "implementation": implementation(), "resource_observer": observer_runtime()}
    path = root / "plan.json"
    dump(path, plan)
    return path, sha256_file(path), projection
