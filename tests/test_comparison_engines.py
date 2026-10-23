"""Mechanical engine protocol checks; no real queries/models/index preparation."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import sqlite3
import sys

import pytest

from answer_eval.protocol import EvaluationError, sha256_file
from search_comparison.adapters import DocumentMap
from search_comparison.engine_capture import recipes
from search_comparison.engine_capture.commands import Commands
from search_comparison.engine_capture.indexes import validate_qmd
from search_comparison.engine_capture.plan import engine_resources
from search_comparison.engine_capture.protocol import CONTROLS, MANDATORY, expected_arms, reference
from search_comparison.engine_capture.recoll import body_argv, parse_metadata, sandbox_profile, stored_body
from search_comparison.engine_capture.results import parse_result
from search_comparison.engine_capture.run import capture, verify_capture
from search_comparison.engine_capture.sqlite_digest import physical
from search_comparison.engine_capture.views import snapshot, verify_view
from comparison_engine_fixtures import data, missing_plan
from comparison_source_fixtures import dump, synthetic_native_policy


@pytest.fixture
def owner(tmp_path):
    return tmp_path.resolve()


def mapping(root, n=12):
    rows = [{"source_path": f"original/{i}.md", "view_path": f"a ?#%中文{i}.md"} for i in range(n)]
    return DocumentMap(root, rows, collection="sources", index="comparison"), rows


def values():
    keys = {token[1:-1] for argv in recipes.RECIPES.values() for token in argv if token.startswith("{") and token.endswith("}")}
    return {k: "EXPLICIT_" + k for k in keys}


@pytest.mark.parametrize("arm", MANDATORY + CONTROLS)
def test_query_is_one_literal_argument_after_terminator(arm):
    query = "--option 中文 `quoted` $(literal) {not_a_template} ' \""
    argv = recipes.compile_recipe(arm, {**values(), "query": query})
    assert argv[-2:] == ["--", query]
    assert argv.count(query) == 1


@pytest.mark.parametrize("token", ["{query", "x{query}", "{unknown}", "{query}/suffix", "{query}}"])
def test_no_unresolved_or_partial_template(token, monkeypatch):
    monkeypatch.setitem(recipes.RECIPES, "qmd.search", [token])
    with pytest.raises(EvaluationError):
        recipes.compile_recipe("qmd.search", values())


def test_native_glob_is_the_only_intentional_brace_literal():
    assert recipes.compile_recipe("qmd.prepare.native", values())[-1] == "**/*.{md,rst,py,c}"


@pytest.mark.parametrize("arms", [list(MANDATORY), list(reversed(MANDATORY + CONTROLS)), list(MANDATORY + CONTROLS) + ["qmd.guessed"], []])
def test_real_fixed_arm_registry_cannot_shrink_or_reorder(arms):
    with pytest.raises(EvaluationError):
        expected_arms(arms, fixture=False)


@pytest.mark.parametrize("engine", ["goldmine", "qmd", "recoll"])
def test_absent_engine_assets_fail_missing(engine):
    with pytest.raises(EvaluationError, match="unresolved") as exc:
        engine_resources(engine, None, fixture=False)
    assert exc.value.status == "MISSING"


def test_pinned_resource_rejects_alias_and_mutation(owner):
    path = owner / "real.txt"
    path.write_text("original")
    pin = sha256_file(path)
    alias = owner / "alias.txt"
    alias.symlink_to(path)
    with pytest.raises(EvaluationError, match="canonical"):
        reference({"path": str(alias), "sha256": pin})
    path.write_text("changed")
    with pytest.raises(EvaluationError, match="changed"):
        reference({"path": str(path), "sha256": pin})


@pytest.mark.parametrize("n,unique,status", [(0, 0, "COMPLETE_RETURNED_WINDOW"), (9, 1, "COMPLETE_RETURNED_WINDOW"), (100, 1, "TRUNCATED"), (100, 10, "COMPLETE_RETURNED_WINDOW")])
def test_file_window_saturation_is_not_short_result_failure(owner, n, unique, status):
    m, docs = mapping(owner)
    hits = [{"rank": i + 1, "path": str(owner / docs[i % max(unique, 1)]["view_path"]), "score": .5} for i in range(n)]
    result = parse_result(json.dumps(hits), "goldmine.fast", m, {})
    assert result["window"]["status"] == status
    assert len(result["raw_rows"]) == n
    assert len(result["native10_raw_rows"]) == min(n, 10)
    assert len(result["file_cut"]) == unique


def qmd_index():
    return {"documents": [{"uri": "qmd://sources/a ?#%中文.md", "source_path": "original/0.md", "hash": "a" * 64}]}


def test_qmd_uses_exact_literal_uri_and_prepared_hash(owner):
    m, _ = mapping(owner)
    row = {"docid": "#aaaaaa", "score": .7, "file": qmd_index()["documents"][0]["uri"], "title": "Actual filename", "snippet": "actual emitted snippet"}
    result = parse_result(json.dumps([row]), "qmd.search", m, qmd_index())
    assert result["file_cut"][0]["source_path"] == "original/0.md"
    assert result["position_status"].startswith("NOT_COMPARABLE")
    for altered in ({**row, "file": row["file"].replace("?", "%3F")}, {**row, "docid": "#bbbbbb"}):
        with pytest.raises(EvaluationError, match="URI/hash"):
            parse_result(json.dumps([altered]), "qmd.search", m, qmd_index())


def test_recoll_saturated_header_and_complete_short_output(owner):
    m, docs = mapping(owner)
    url = (owner / docs[0]["view_path"]).as_uri()
    encoded = base64.b64encode(url.encode()).decode()
    raw = "Recoll query: synthetic\n105 results (printing  100 max):\n" + (encoded + "\n") * 100
    assert parse_result(raw, "recoll.default", m, {})["window"]["status"] == "TRUNCATED"
    for altered in (raw.replace("printing  100 max", "printing  99 max"), raw.rsplit("\n", 2)[0] + "\n"):
        with pytest.raises(EvaluationError):
            parse_result(altered, "recoll.default", m, {})


def test_recoll_stored_text_exact_framing_empty_ipath_and_denial(owner):
    m, docs = mapping(owner)
    url = (owner / docs[0]["view_path"]).as_uri()
    body = "Exact full body\n中文\n".encode()
    fields = {"url": url, "mtype": "text/plain", "dbytes": str(len(body)), "xdocid": "2"}
    line = " ".join(k + " " + base64.b64encode(v.encode()).decode() for k, v in fields.items())
    metadata = parse_metadata("Recoll query: fixed\n1 results\n" + line + "\n", m)
    assert metadata[0]["ipath"] == ""
    assert stored_body(url.encode() + b"\n" + body + b"\n", metadata[0]) == body
    for raw in (url.encode() + b"\n" + body, url.encode() + b"\nCant turn to text:" + url.encode() + b" | \n"):
        with pytest.raises(EvaluationError):
            stored_body(raw, metadata[0])
    assert "deny file-read-data" in sandbox_profile(owner)
    assert body_argv("/bin/sandbox", owner / "p", "/bin/recollq", owner / "cfg", owner, 0)[-3] == "0-1"
    with pytest.raises(EvaluationError):
        parse_metadata("Recoll query: fixed\n2 results\n" + line + "\n", m)


def quality(owner):
    m, docs = mapping(owner)
    hit = {"rank": 1, "path": str(owner / docs[0]["view_path"]), "score": 1.0}
    settings = {"scorer": "hybrid", "mode": "quality", "rerank": "local", "multi_query": False, "limit": 100,
                "reranker": {"executed": True, "candidates": 1, "model": "pinned-real-model"}}
    return m, {"hits": [hit], "settings": settings, "fallback_reason": None}


@pytest.mark.parametrize("key,value", [("mode", "fast"), ("rerank", "off"), ("multi_query", True), ("limit", 20)])
def test_quality_treatment_must_match_actual_settings(owner, key, value):
    m, payload = quality(owner)
    payload["settings"][key] = value
    with pytest.raises(EvaluationError):
        parse_result(json.dumps(payload), "goldmine.quality-rerank", m, {})


def test_degraded_quality_preserves_valid_filename_file_hit(owner):
    m, payload = quality(owner)
    payload["fallback_reason"] = "timeout retained native ranking"
    payload["settings"]["reranker"] = {"executed": False, "candidates": 0, "model": "pinned-real-model"}
    result = parse_result(json.dumps(payload), "goldmine.quality-rerank", m, {})
    assert result["treatment"]["status"] == "DEGRADED"
    assert result["window"]["files"] == ["original/0.md"]


def test_sqlite_content_identity_excludes_only_mutable_llm_cache(owner):
    path = owner / "db.sqlite"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE documents(id INTEGER, body TEXT);CREATE TABLE llm_cache(k TEXT,v TEXT);CREATE TABLE vectors(id INTEGER,embedding BLOB);")
    db.execute("INSERT INTO documents VALUES(?,?)", (1, "original"))
    db.execute("INSERT INTO vectors VALUES(?,?)", (1, b"\x00\x01"))
    db.commit()
    first = physical(path)
    db.execute("INSERT INTO llm_cache VALUES('a','b')")
    db.commit()
    assert physical(path) == first
    db.execute("UPDATE vectors SET embedding=?", (b"\x00\x02",))
    db.commit()
    assert physical(path) != first
    db.close()


def semantic_audit(owner):
    text = "Complete synthetic text.\n"
    (owner / "full.md").write_text(text)
    digest = hashlib.sha256(text.encode()).hexdigest()
    view = {"name": "common", "corpus": str(owner), "documents": [{"source_path": "original.pdf", "view_path": "full.md"}]}
    values = {"semantic": True, "embed": "/owned/embedding.gguf", "installation": "/owned/runtime"}
    cfg = {"models": {"embed": values["embed"]}, "collections": {"sources": {"path": str(owner), "pattern": "**/*.md"}}}
    value = {"schema": 1, "documents": [{"id": 1, "path": "full.md", "active": 1, "collection": "sources", "hash": digest, "body": text}],
             "configuration": cfg, "collections": [{"name": "sources", "path": str(owner), "pattern": "**/*.md", "update_command": None}],
             "native_paths": ["/owned/runtime/addon.node"], "fts": [{"rowid": 1}],
             "fts_tables": {"documents_fts_" + s: {"rows": 1} for s in ("data", "idx", "content", "docsize", "config")},
             "has_vector_table": True, "fingerprint": "declared-synthetic-fingerprint",
             "vectors": [{"hash": digest, "seq": 0, "total_chunks": 1, "model": values["embed"], "embed_fingerprint": "declared-synthetic-fingerprint"}],
             "vector_rows": [{"hash_seq": digest + "_0", "bytes": 4, "sha256": "a" * 64}], "physical": {"synthetic": True}}
    return values, view, value


@pytest.mark.parametrize("change", ["empty_vectors", "empty_actual_vectors", "chunk_gap", "model", "body", "extra_active", "no_fts", "native_escape"])
def test_semantic_empty_output_needs_real_index_coverage(owner, change):
    values, view, value = semantic_audit(owner)
    assert validate_qmd(value, values, view)["documents"][0]["source_path"] == "original.pdf"
    if change == "empty_vectors": value["vectors"] = []
    if change == "empty_actual_vectors": value["vector_rows"] = []
    if change == "chunk_gap": value["vectors"][0]["seq"] = 1
    if change == "model": value["vectors"][0]["model"] = "/different.gguf"
    if change == "body": value["documents"][0]["body"] = "excerpt only"
    if change == "extra_active": value["documents"].append({**value["documents"][0], "path": "extra.md"})
    if change == "no_fts": value["fts"] = []
    if change == "native_escape": value["native_paths"] = ["/global/addon.node"]
    with pytest.raises(EvaluationError):
        validate_qmd(value, values, view)


def test_source_view_dates_bytes_and_inventory_cannot_drift(owner):
    source = owner / "full.md"
    source.write_text("original")
    view = {"corpus": str(owner), "documents": [{"view_path": "full.md", "source_path": "original.pdf", "sha256": sha256_file(source)}]}
    view["identity"] = snapshot(view)
    verify_view(view)
    source.write_text("modified")
    with pytest.raises(EvaluationError, match="bytes changed"):
        verify_view(view)


@pytest.mark.native
def test_failure_and_interrupt_raw_commands_remain_in_registry(owner):
    (owner / "cwd").mkdir()
    ctx = {"registered_at": datetime.now(timezone.utc).isoformat(), "fixture": "mechanical only"}
    env = {"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"}
    commands = Commands(owner, env, ctx)
    argv = [str(Path(sys.executable).resolve()), "-I", "-S", "-c", "import sys;print('retained failure');sys.exit(143)"]
    with pytest.raises(EvaluationError, match="failed"):
        commands.call("synthetic-failure", argv)
    refs = commands.finish()
    assert commands.interrupted and len(refs) == 1
    replay = Commands(owner, env, ctx, replay=refs)
    with pytest.raises(EvaluationError, match="failed"):
        replay.call("synthetic-failure", argv)
    assert replay.finish() == refs
    corrupted = Commands(owner, {**env, "UNREGISTERED": "changed"}, ctx, replay=refs)
    with pytest.raises(EvaluationError, match="context differs"):
        corrupted.call("synthetic-failure", argv)


@pytest.mark.skipif(sys.platform != "darwin",
                    reason="fixture export binds the Darwin ps/lsof resource-observer identity")
def test_actual_fixture_export_fixed_missing_matrix_and_disk_replay(owner):
    fixture = data(owner / "fixture")
    with synthetic_native_policy():
        path, pin, projection = missing_plan(owner, fixture)
        assert len(projection["requests"]) == 7
        assert all(q["id"].startswith("dev") for q in projection["requests"])
        exported = Path(projection["reference"]["path"]).parent / "output/requests.json"
        assert "unselected" not in exported.read_text()
        report = capture(path, pin, owner / "capture", owner, fixture=True)
        assert report["status"] == "MISSING"
        assert len(report["systems"]) == 18
        assert all(len(s["rows"]) == 7 and not s["commands"] for s in report["systems"])
        assert report["full_comparison_status"] == "MISSING_PERFORMANCE_UPDATE_RESOURCE_PHASES"
        assert verify_capture(owner / "capture/report.json") == report
        report["systems"][0]["rows"].pop()
        dump(owner / "capture/report.json", report)
        with pytest.raises(EvaluationError, match="replay"):
            verify_capture(owner / "capture/report.json")
