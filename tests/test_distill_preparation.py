"""Mechanical input-binding tests only: no real corpus/model/indexer/clustering run."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from acceptance_lib.evidence import InvalidEvidence, artifact, capture_command, read_command_run, sha256, write_json
from distill_eval.identity import assert_extension, assert_prepared_copies, canonical_sha, evaluator_identity, index_identity
from distill_eval import materialization as mat
from distill_eval import preparation as prep
from distill_eval.scoring import excerpt_audit

ROOT = Path(__file__).resolve().parents[1]


def sql_fixture(path, *, start=1, count=2):
    """Tiny native-shaped SQL rows, not outputs of an indexer or a model."""
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT,kind TEXT,sha256 TEXT,state TEXT,capability TEXT,info TEXT);
      CREATE TABLE segments(id INTEGER PRIMARY KEY,file INTEGER,kind TEXT,modality TEXT,loc REAL,loc_end REAL,excerpt TEXT,vec BLOB);
      CREATE TABLE fts(name TEXT,body TEXT,original TEXT); PRAGMA user_version=6;""")
    fp = json.dumps({"backend": "llama", "model_sha256": "model", "dim": 768})
    db.executemany("INSERT INTO meta VALUES(?,?)", [("index_uuid", "12345678-1234-4234-8234-123456789abc"),
        ("fingerprint_id", "fp"), ("fingerprint_json", fp), ("embed_id", "embed"), ("schema_version", "6")])
    for number in range(start, start + count):
        db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?)", (number, f"/fixture/{number}.txt", "doc", "sha", "done", "content_indexed", "{}"))
        sid = number * 10
        db.execute("INSERT INTO segments VALUES(?,?,?,?,?,?,?,?)", (sid, number, "chunk", "text", 1, 1, "source", b"\x00\x3c" * 768))
        db.execute("INSERT INTO fts(rowid,name,body,original) VALUES(?,?,?,?)", (sid, "name", "source", "Original source"))
        db.execute("INSERT INTO segments VALUES(?,?,?,?,?,?,?,?)", (sid+1, number, "ocr", "text", 2, 2, "proof", None))
        db.execute("INSERT INTO fts(rowid,name,body,original) VALUES(?,?,?,?)", (sid+1, "name", "proof", "Nonvector original proof"))
    db.commit()
    return db


@pytest.mark.parametrize("relative", ["scripts/distill_eval/scoring.py", "scripts/distill_eval/runner.py",
    "scripts/distill_eval/labels.py", "scripts/distill_eval/preparation.py", "scripts/prepare_distill.py",
    "scripts/acceptance_lib/owned_process.py", "scripts/product_eval/materialization.py"])
def test_identity_binds_actual_helpers(relative):
    assert evaluator_identity(ROOT)[relative] == sha256(ROOT / relative)


@pytest.mark.parametrize("mutation", [
    "UPDATE meta SET value='87654321-4321-4321-8321-cba987654321' WHERE key='index_uuid'",
    "UPDATE fts SET original='Changed nonvector proof' WHERE rowid=11",
    "UPDATE fts SET body='Changed normalized body' WHERE rowid=11",
    "UPDATE files SET info='{\"taken\":\"false\"}' WHERE id=1",
    "UPDATE segments SET loc=99 WHERE id=10",
    "UPDATE segments SET kind='name' WHERE id=11",
    "UPDATE segments SET vec=X'0000' WHERE id=10",
    "DELETE FROM fts WHERE rowid=11",
    "DELETE FROM segments WHERE id=11",
])
def test_all_original_rows_must_survive(tmp_path, mutation):
    path = tmp_path / "base.sqlite"
    db = sql_fixture(path)
    original = index_identity(path)
    db.execute(mutation)
    db.commit()
    with pytest.raises(InvalidEvidence):
        assert_extension(original, index_identity(path))
    db.close()


def test_file_id_renumbering_is_not_same_index(tmp_path):
    path = tmp_path / "base.sqlite"
    db = sql_fixture(path)
    original = index_identity(path)
    db.execute("UPDATE files SET id=99 WHERE id=1")
    db.execute("UPDATE segments SET file=99 WHERE file=1")
    db.commit()
    with pytest.raises(InvalidEvidence, match="files IDs"):
        assert_extension(original, index_identity(path))
    db.close()


def test_add_only_new_file_rows_and_keep_fts_provenance(tmp_path):
    path = tmp_path / "base.sqlite"
    db = sql_fixture(path)
    original = index_identity(path)
    db.execute("INSERT INTO files SELECT 3,'/fixture/3.txt',kind,sha256,state,capability,info FROM files WHERE id=1")
    db.execute("INSERT INTO segments SELECT 30,3,kind,modality,loc,loc_end,excerpt,vec FROM segments WHERE id=10")
    db.execute("INSERT INTO fts(rowid,name,body,original) SELECT 30,name,body,original FROM fts WHERE rowid=10")
    db.commit()
    assert_extension(original, index_identity(path))
    db.execute("INSERT INTO segments SELECT 31,1,kind,modality,loc,loc_end,excerpt,vec FROM segments WHERE id=10")
    db.commit()
    with pytest.raises(InvalidEvidence, match="original file"):
        assert_extension(original, index_identity(path))
    db.close()


def test_uncheckpointed_input_is_not_hashed_as_complete(tmp_path):
    path = tmp_path / "base.sqlite"
    db = sql_fixture(path)
    db.close()
    Path(str(path) + "-wal").write_bytes(b"uncheckpointed proof")
    with pytest.raises(InvalidEvidence, match="WAL"):
        index_identity(path)


def test_corrupt_sqlite_is_missing_evidence(tmp_path):
    path = tmp_path / "not-sqlite"
    path.write_bytes(b"not a SQLite database")
    with pytest.raises(InvalidEvidence, match="SQLite"):
        index_identity(path)


def test_both_copies_cannot_drift_together_after_preparation(tmp_path):
    path = tmp_path / "base.sqlite"
    db = sql_fixture(path)
    original = index_identity(path)
    proof = {"base_identity_sha256": canonical_sha(original), "incremental_identity_sha256": canonical_sha(original)}
    assert_prepared_copies(original, original, proof)
    db.execute("UPDATE fts SET original='changed in both later copies' WHERE rowid=11")
    db.commit()
    changed = index_identity(path)
    assert_extension(changed, changed)  # Prefix-only comparison cannot detect common drift.
    with pytest.raises(InvalidEvidence, match="actual evaluation copies"):
        assert_prepared_copies(changed, changed, proof)
    db.close()


@pytest.mark.parametrize("node", ["label-file", "empty-directory", "symlink", "fifo"])
def test_only_registered_source_nodes_enter_corpus(tmp_path, node):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "source.txt").write_text("Original bytes")
    sources = {"source.txt": {"kind": "doc", "asset": {"sha256": sha256(corpus / "source.txt")}}}
    receipt = mat.build_receipt(sources)
    mat.check_corpus(corpus, sources, receipt)
    extra = corpus / "extra"
    if node == "label-file": extra.write_text('{"topic":"answer"}')
    elif node == "empty-directory": extra.mkdir()
    elif node == "symlink": extra.symlink_to(corpus / "source.txt")
    else: os.mkfifo(extra)
    with pytest.raises(InvalidEvidence):
        mat.check_corpus(corpus, sources, receipt)


def test_screenshot_capture_flag_is_required_and_bytes_stay_literal(tmp_path, monkeypatch):
    from product_eval import materialization
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    path = corpus / "real-capture.png"
    path.write_bytes(b"mechanical byte transport only; no image decoder/model")
    sources = {path.name: {"kind": "screenshot", "asset": {"sha256": sha256(path)}}}
    receipt = mat.build_receipt(sources)
    monkeypatch.setattr(materialization.sys, "platform", "darwin")
    monkeypatch.setattr(materialization, "read_flag", lambda path: materialization.CAPTURE_VALUE.hex())
    mat.check_corpus(corpus, sources, receipt)
    monkeypatch.setattr(materialization, "read_flag", lambda path: None)
    with pytest.raises(ValueError, match="lost/changed"):
        mat.check_corpus(corpus, sources, receipt)
    assert sha256(path) == sources[path.name]["asset"]["sha256"]


def test_recognized_screenshot_requires_ocr_not_filename_evidence(tmp_path):
    path = tmp_path / "base.sqlite"
    db = sql_fixture(path, count=1)
    db.close()
    identity = index_identity(path)
    sources = {"1.txt": {"kind": "screenshot", "asset": {"sha256": "sha"}}}
    with pytest.raises(InvalidEvidence, match="screenshot OCR"):
        prep.validate_index(identity, sources, Path("/fixture"), "model")
    identity["segments"]["10"]["kind"] = "ocr"
    prep.validate_index(identity, sources, Path("/fixture"), "model")
    identity["meta"]["fingerprint_json"] = json.dumps({"backend": "mock", "model_sha256": "model"})
    with pytest.raises(InvalidEvidence, match="backend/model"):
        prep.validate_index(identity, sources, Path("/fixture"), "model")


def test_emitted_header_excerpts_are_audited(tmp_path, monkeypatch):
    from goldmine import citations
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE fts(original,body)")
    db.execute("INSERT INTO fts(rowid,original,body) VALUES(1,'Literal source sentence.','literal source sentence')")
    citation = SimpleNamespace(citation_id="mechanical-cid", segment_id=1, file_id=1,
                               path="proof.txt", kind="doc", loc=1, loc_end=1)
    monkeypatch.setattr(citations, "read_evidence", lambda store, sid: SimpleNamespace(citation=citation, text="Literal source sentence."))
    monkeypatch.setattr(citations, "resolve_citation", lambda store, cid: SimpleNamespace(text="Literal source sentence.", citation=citation))
    store = SimpleNamespace(db=db, segment_text=lambda sid: "Literal source sentence.")
    member = SimpleNamespace(sid=1, fid=1, text="Literal source sentence.", path="proof.txt", kind="doc", loc=1, loc_end=1)
    excerpt = {"path": "proof.txt", "kind": "doc", "loc": 1, "loc_end": 1, "text": "Literal source sentence."}
    header = SimpleNamespace(status="header", members=[member], excerpts=[excerpt], title_kind="terms", terms=["source"], title="source", topic_id=20)
    report = excerpt_audit(store, [header])
    assert report["checked"] == report["passed"] == 1
    excerpt["text"] = "Fabricated source sentence."
    report = excerpt_audit(store, [header])
    assert report["checked"] == 1 and report["passed"] == 0
    db.close()


@pytest.mark.parametrize("bad", ["wrong-python", "wrong-script", "extra-flag", "missing-preparation", "different-helper-code", "wrong-exit"])
def test_acceptance_requires_fixed_command_and_preparation(tmp_path, monkeypatch, bad):
    from acceptance_lib import distill_evidence as adapter
    from distill_eval import runner
    model = tmp_path / "model"
    model.write_bytes(b"MECHANICAL ONLY fake model asset; never executed")
    preparation = {"preparation_sha256": "prep", "python_path": sys.executable}
    flags = {name: tmp_path / name[2:] for name in ("--report-real", "--labels", "--corpus-root", "--incremental-db", "--preparation")}
    argv = [sys.executable, str(ROOT / "scripts/eval_distill.py"), "--report-real", str(flags["--report-real"]), "--json"]
    for name in ("--labels", "--corpus-root", "--incremental-db", "--preparation"):
        argv += [name, str(flags[name])]
    report = {"schema": 2, "kind": "goldmine.distill-real", "evidence_mode": "real", "status": "PASS", "fingerprint": {"model_sha256": sha256(model)},
              "metrics": {}, "gates": {}, "labels_sha256": "labels", "source_index_sha256": "index"}
    stdout = tmp_path / "stdout"
    write_json(stdout, report)
    manifest = {"identity": {"source_files": evaluator_identity(ROOT), "tool_sha256": sha256(ROOT / "scripts/eval_distill.py"), "preparation": preparation}, "run_id": "mechanical"}
    receipt = {"argv": argv, "exit_code": 0, "timed_out": False}
    monkeypatch.setattr(adapter, "read_command_run", lambda *a, **kw: (manifest, receipt, stdout))
    monkeypatch.setattr(prep, "validate_preparation", lambda *a: preparation)
    monkeypatch.setattr(runner, "evaluate", lambda *a: report)
    if bad == "wrong-python": argv[0] = "/bin/echo"
    elif bad == "wrong-script": argv[1] = "/tmp/print-green.py"
    elif bad == "extra-flag": argv += ["--variant", "mock"]
    elif bad == "missing-preparation": del argv[-2:]
    elif bad == "different-helper-code": manifest["identity"]["source_files"]["scripts/distill_eval/scoring.py"] = "0" * 64
    else: receipt["exit_code"] = 1
    inputs = SimpleNamespace(file=lambda key, **kw: model if key == "models.embedding" else tmp_path / "manifest.json")
    with pytest.raises(InvalidEvidence):
        adapter.check(inputs, {})


@pytest.fixture
def preparation_fixture(tmp_path):
    """Locally synthesized receipt/SQL mechanics; never a real-quality submission."""
    bundle, out = tmp_path / "fixture-bundle", tmp_path / "fixture-preparation"
    bundle.mkdir()
    out.mkdir()
    corpus = out / "corpus"
    corpus.mkdir()
    license_path = bundle / "LICENSE"
    license_path.write_text("MECHANICAL FIXTURE ONLY, not real licensed source evidence.\n")
    topics = [f"fixture-topic-{number}" for number in range(8)]
    sources, documents = [], []
    for number in range(120):
        name = f"fixture-{number}.txt"
        asset = bundle / name
        asset.write_text(f"MECHANICAL FIXTURE ONLY {number}\n")
        (corpus / name).write_bytes(asset.read_bytes())
        sources.append({"path": name, "kind": "doc", "source_group": f"fixture:{number}", "asset": artifact(bundle, asset),
            "provenance": {"type": "real", "license": "MECHANICAL ONLY", "license_file": artifact(bundle, license_path)}})
        role = "cross_topic" if number == 108 else "noise" if number == 109 else "topic"
        documents.append({"path": name, "split": "base" if number < 110 else "incremental", "role": role,
            "topics": topics[:2] if role == "cross_topic" else [] if role == "noise" else [topics[number % 8]],
            "case_tags": ["duplicate", "paraphrase", "language:en" if number % 2 else "language:zh"]})
    source_path = bundle / "sources.json"
    write_json(source_path, {"state": "FROZEN", "evidence_mode": "real", "sources": sources})
    labels_path = bundle / "labels.json"
    write_json(labels_path, {"schema": 1, "suite_id": "distill.real.v1", "state": "FROZEN", "evidence_mode": "real",
        "frozen_at": "2026-10-21T00:00:00+00:00", "labeling_protocol": "independent-before-clustering", "reviewers": ["MECHANICAL FIXTURE ONLY"],
        "product_manifest": {"manifest_path": "sources.json", "sha256": sha256(source_path)}, "topics": topics, "documents": documents})
    assets = {"python": {"path": sys.executable, "sha256": sha256(Path(sys.executable))}}
    for name in ("model", "server", "ocr"):
        asset = out / name
        asset.write_bytes(f"MECHANICAL ONLY {name}; never execute this asset".encode())
        assets[name] = {"path": str(asset), "sha256": sha256(asset)}
    helper = out / "state/bin/goldmine-ocr"
    helper.parent.mkdir(parents=True)
    helper.write_bytes((out / "ocr").read_bytes())
    config = out / "config.toml"
    config.write_bytes(b"")
    base, extended = out / "base.sqlite", out / "incremental.sqlite"
    db = sql_fixture(base, count=110)
    fingerprint = json.dumps({"backend": "llama", "model_sha256": assets["model"]["sha256"], "dim": 768})
    db.execute("UPDATE meta SET value=? WHERE key='fingerprint_json'", (fingerprint,))
    for number in range(110):
        db.execute("UPDATE files SET path=?,sha256=? WHERE id=?", (str(corpus / sources[number]["path"]), sources[number]["asset"]["sha256"], number + 1))
    db.commit()
    ext = sqlite3.connect(extended)
    db.backup(ext)
    db.close()
    for number in range(110, 120):
        fid, sid = number + 1, (number + 1) * 10
        ext.execute("INSERT INTO files SELECT ?,?,kind,?,state,capability,info FROM files WHERE id=1",
                    (fid, str(corpus / sources[number]["path"]), sources[number]["asset"]["sha256"]))
        ext.execute("INSERT INTO segments SELECT ?,?,kind,modality,loc,loc_end,excerpt,vec FROM segments WHERE id=10", (sid, fid))
        ext.execute("INSERT INTO fts(rowid,name,body,original) SELECT ?,name,body,original FROM fts WHERE rowid=10", (sid,))
    ext.commit()
    ext.close()
    document = {"schema": 1, "kind": "goldmine.distill-preparation", "status": "PREPARED", "source_files": evaluator_identity(ROOT),
        "assets": assets, "labels": {"path": str(labels_path), "sha256": sha256(labels_path)},
        "source_manifest": {"path": str(source_path), "sha256": sha256(source_path)}, "corpus_root": str(corpus),
        "working_index": str(out / "state/index.sqlite"), "config": artifact(out, config), "builds": {}, "commands": {}, "indexes": {}, "identities": {}}
    for phase, names, index, count, unchanged in (("base", sources[:110], base, 110, 0), ("incremental", sources, extended, 10, 110)):
        build_path = out / f"build-{phase}.json"
        write_json(build_path, mat.build_receipt({row["path"]: row for row in names}))
        document["builds"][phase] = artifact(out, build_path)
        identity = index_identity(index)
        identity_path = out / f"identity-{phase}.json"
        write_json(identity_path, identity)
        document["indexes"][phase] = artifact(out, index)
        document["identities"][phase] = artifact(out, identity_path)
        command = out / "commands" / phase
        command.mkdir(parents=True)
        stdout, stderr = command / "stdout", command / "stderr"
        write_json(stdout, {"dry_run": False, "cancelled": False, "failures": [], "files": {"scanned": 110+count if unchanged else count,
            "content_indexed": count, "unchanged": unchanged, "failed": 0, "unsupported": 0, "metadata_only": 0, "partial": 0, "deleted": 0},
            "metrics": {"model_spawns": 1, "embedding_http_requests": 2, "embed_calls": 1, "embed_inputs": 1}, "fingerprint": {"id": "fp"}})
        stderr.write_text("MECHANICAL FIXTURE ONLY: no commands or models executed.\n")
        stamp = "2026-10-21T01:00:00+00:00" if phase == "base" else "2026-10-21T02:00:00+00:00"
        receipt = {"schema": 1, "run_id": phase, "check_id": f"distill.prepare.{phase}",
            "argv": prep.command_argv(sys.executable, out / "state/index.sqlite", corpus), "cwd": str(out), "env": prep.command_env(ROOT, out, assets),
            "started_at": stamp, "finished_at": stamp, "exit_code": 0, "wall_ms": .1, "timed_out": False,
            "stdout": artifact(command, stdout), "stderr": artifact(command, stderr), "artifacts": {}}
        receipt_path = command / "receipt.json"
        write_json(receipt_path, receipt)
        manifest = {"schema": 1, "kind": "goldmine.command-run", "run_id": phase, "check_id": receipt["check_id"], "suite_id": prep.SUITE,
            "identity": {"source_files": document["source_files"], "assets": assets, "labels_sha256": document["labels"]["sha256"],
                "source_manifest_sha256": document["source_manifest"]["sha256"], "build_sha256": document["builds"][phase]["sha256"]},
            "receipt": artifact(command, receipt_path)}
        manifest_path = command / "manifest.json"
        write_json(manifest_path, manifest)
        document["commands"][phase] = {"manifest_path": str(manifest_path.relative_to(out)), "sha256": sha256(manifest_path)}
    manifest_path = out / "preparation.json"
    write_json(manifest_path, document)
    return manifest_path, document, labels_path


def test_complete_mechanical_preparation_replays_without_quality_claim(preparation_fixture):
    manifest, _, labels = preparation_fixture
    out = manifest.parent
    proof = prep.validate_preparation(manifest, out / "base.sqlite", labels, out / "corpus", out / "incremental.sqlite")
    assert proof["real_quality_claim"] is False and proof["preparation_sha256"] == sha256(manifest)


def test_mechanical_preparation_recipe_keeps_one_working_index(preparation_fixture, monkeypatch):
    """Replace only native execution with explicit SQL/receipt fixtures; no model."""
    import shutil
    import prepare_distill
    existing, document, labels = preparation_fixture
    original = existing.parent
    output = original.parent / "mechanical-recipe"
    for name in ("server", "ocr"):
        Path(document["assets"][name]["path"]).chmod(0o755)
    calls = []
    def fake_native(command_dir, check_id, argv, *, cwd, env, identity, suite_id, timeout, owned_process_group):
        phase = check_id.rsplit(".", 1)[1]
        assert suite_id == prep.SUITE and timeout == 3600 and owned_process_group is True
        calls.append(argv)
        work_index = Path(argv[argv.index("--db") + 1])
        if phase == "base":
            shutil.copyfile(original / "base.sqlite", work_index)
            with sqlite3.connect(work_index) as db:
                db.execute("UPDATE files SET path=replace(path,?,?)", (str(original / "corpus"), str(output / "corpus")))
        else:
            with sqlite3.connect(original / "incremental.sqlite") as source, sqlite3.connect(work_index) as target:
                for row in source.execute("SELECT * FROM files WHERE id>110"):
                    row = list(row); row[1] = row[1].replace(str(original / "corpus"), str(output / "corpus"))
                    target.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?)", row)
                target.executemany("INSERT INTO segments VALUES(?,?,?,?,?,?,?,?)", source.execute("SELECT * FROM segments WHERE file>110"))
                target.executemany("INSERT INTO fts(rowid,name,body,original) VALUES(?,?,?,?)", source.execute("SELECT rowid,name,body,original FROM fts WHERE rowid>=1110"))
        template = original / "commands" / phase
        command_dir.mkdir(parents=True)
        for name in ("stdout", "stderr"):
            shutil.copyfile(template / name, command_dir / name)
        receipt = json.loads((template / "receipt.json").read_text())
        receipt.update(argv=argv, cwd=str(cwd), env=prep.command_env(ROOT, output, identity["assets"]))
        for name in ("stdout", "stderr"): receipt[name] = artifact(command_dir, command_dir / name)
        write_json(command_dir / "receipt.json", receipt)
        manifest = json.loads((template / "manifest.json").read_text())
        manifest.update(identity=identity, receipt=artifact(command_dir, command_dir / "receipt.json"))
        write_json(command_dir / "manifest.json", manifest)
    monkeypatch.setattr(prepare_distill, "capture_command", fake_native)
    path = prepare_distill.prepare(labels, output, original / "model", original / "server", original / "ocr")
    assert path == output / "preparation.json" and len(calls) == 2 and calls[0] == calls[1]
    proof = prep.validate_preparation(path, output / "base.sqlite", labels, output / "corpus", output / "incremental.sqlite")
    assert proof["real_quality_claim"] is False
    assert len(index_identity(output / "base.sqlite")["files"]) == 110
    assert len(index_identity(output / "incremental.sqlite")["files"]) == 120


@pytest.mark.parametrize("bad", ["source-code", "base-build", "phase-missing", "extra-answer-file", "asset-changed", "label-path", "wrong-root", "old-nonvector", "old-file-id", "rebuilt-uuid", "zero-native-calls", "boolean-counter", "array-report", "wrong-command"])
def test_preparation_binding_counterexamples(preparation_fixture, bad):
    manifest_path, document, labels = preparation_fixture
    out = manifest_path.parent
    def rebind_command(phase, change):
        command = out / "commands" / phase
        receipt_path = command / "receipt.json"
        receipt = json.loads(receipt_path.read_text())
        change(command, receipt)
        write_json(receipt_path, receipt)
        command_manifest = json.loads((command / "manifest.json").read_text())
        command_manifest["receipt"] = artifact(command, receipt_path)
        write_json(command / "manifest.json", command_manifest)
        document["commands"][phase]["sha256"] = sha256(command / "manifest.json")
    if bad == "source-code": document["source_files"]["scripts/distill_eval/scoring.py"] = "0"*64
    elif bad == "base-build":
        build = out / "build-base.json"
        data = json.loads(build.read_text()); data["files"].pop(next(iter(data["files"])))
        write_json(build, data); document["builds"]["base"] = artifact(out, build)
    elif bad == "phase-missing": document["commands"].pop("base")
    elif bad == "extra-answer-file": (out / "corpus/labels.json").write_text("leaked labels")
    elif bad == "asset-changed": (out / "model").write_text("changed after preparation")
    elif bad == "label-path": labels = out / "other-labels.json"
    elif bad == "wrong-root": document["corpus_root"] = str(out / "different-root")
    elif bad in ("old-nonvector", "old-file-id", "rebuilt-uuid"):
        path = out / "incremental.sqlite"
        db = sqlite3.connect(path)
        if bad == "old-nonvector": db.execute("UPDATE fts SET original='changed original nonvector evidence' WHERE rowid=11")
        elif bad == "old-file-id":
            db.execute("UPDATE files SET id=9999 WHERE id=1"); db.execute("UPDATE segments SET file=9999 WHERE file=1")
        else: db.execute("UPDATE meta SET value='87654321-4321-4321-8321-cba987654321' WHERE key='index_uuid'")
        db.commit(); db.close()
        write_json(out / "identity-incremental.json", index_identity(path))
        document["indexes"]["incremental"] = artifact(out, path)
        document["identities"]["incremental"] = artifact(out, out / "identity-incremental.json")
    elif bad == "zero-native-calls":
        def change(command, receipt):
            stdout = command / "stdout"; report = json.loads(stdout.read_text()); report["metrics"]["model_spawns"] = 0
            write_json(stdout, report); receipt["stdout"] = artifact(command, stdout)
        rebind_command("base", change)
    elif bad in ("boolean-counter", "array-report"):
        def change(command, receipt):
            stdout = command / "stdout"
            report = json.loads(stdout.read_text())
            if bad == "boolean-counter": report["files"]["failed"] = False
            else: report = []
            write_json(stdout, report); receipt["stdout"] = artifact(command, stdout)
        rebind_command("base", change)
    else:
        rebind_command("base", lambda command, receipt: receipt["argv"].__setitem__(0, "/bin/echo"))
    write_json(manifest_path, document)
    with pytest.raises(InvalidEvidence):
        prep.validate_preparation(manifest_path, out / "base.sqlite", labels, out / "corpus", out / "incremental.sqlite")


def test_draft_is_refused_before_native_or_model_work(tmp_path, monkeypatch):
    import prepare_distill
    data = {"schema": 1, "suite_id": "distill.real.v1", "state": "DRAFT", "evidence_mode": "real", "frozen_at": None,
        "labeling_protocol": "independent-before-clustering", "reviewers": [], "product_manifest": {}, "topics": [], "documents": []}
    labels = tmp_path / "draft.json"
    write_json(labels, data)
    monkeypatch.setattr(prepare_distill, "capture_command", lambda *a, **kw: pytest.fail("must not start a command"))
    with pytest.raises(InvalidEvidence, match="frozen real labels"):
        prepare_distill.prepare(labels, tmp_path / "must-not-exist", tmp_path / "missing-model", tmp_path / "missing-server", tmp_path / "missing-ocr")
    assert not (tmp_path / "must-not-exist").exists()


def test_owned_timeout_kills_nested_child_and_preserves_failure_receipt(tmp_path):
    child_path = tmp_path / "child.pid"
    program = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);open(sys.argv[1],'w').write(str(p.pid));print('partial proof',flush=True);time.sleep(30)"
    run = tmp_path / "timeout"
    capture_command(run, "mechanical.timeout", [sys.executable, "-c", program, str(child_path)], cwd=tmp_path,
                    timeout=.15, owned_process_group=True)
    _, receipt, stdout = read_command_run(run / "manifest.json")
    assert receipt["exit_code"] == 124 and receipt["timed_out"]
    assert stdout.read_text() == "partial proof\n"
    pid = int(child_path.read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    # A terminated orphan can briefly await the host init's reap; it must not run.
    import subprocess
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    assert not state or state.startswith("Z")


def test_owned_sigterm_cleans_child_and_retains_interrupted_status(tmp_path):
    program = "import os,signal,time;print('before term',flush=True);os.kill(os.getppid(),signal.SIGTERM);time.sleep(30)"
    run = tmp_path / "signal"
    capture_command(run, "mechanical.signal", [sys.executable, "-c", program], cwd=tmp_path,
                    timeout=2, owned_process_group=True)
    _, receipt, stdout = read_command_run(run / "manifest.json")
    assert receipt["exit_code"] == 128 + signal.SIGTERM and not receipt["timed_out"]
    assert stdout.read_text() == "before term\n"


def test_owned_spawn_failure_preserves_receipt_and_signal_handlers(tmp_path):
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    run = tmp_path / "missing"
    capture_command(run, "mechanical.missing", [sys.executable, "-c", "pass"], cwd=tmp_path / "missing-cwd",
                    timeout=1, owned_process_group=True)
    _, receipt, _ = read_command_run(run / "manifest.json")
    assert receipt["exit_code"] == 127 and not receipt["timed_out"]
    assert {sig: signal.getsignal(sig) for sig in handlers} == handlers


def test_owned_fast_exit_does_not_reap_leader_before_cleanup(tmp_path, monkeypatch):
    from acceptance_lib import owned_process
    stop = owned_process._stop
    observations = []
    def observe(proc, **kwargs):
        observations.append(proc.returncode)
        assert proc.returncode is None  # The PID/PGID is still reserved for our leader.
        return stop(proc, **kwargs)
    monkeypatch.setattr(owned_process, "_stop", observe)
    run = tmp_path / "fast-exit"
    capture_command(run, "mechanical.fast", [sys.executable, "-c", "raise SystemExit(7)"], cwd=tmp_path,
                    timeout=1, owned_process_group=True)
    _, receipt, _ = read_command_run(run / "manifest.json")
    assert observations == [None] and receipt["exit_code"] == 7


@pytest.mark.parametrize("output", [b"42 42 Z\n43 42 S\n", b"42 42 S\n", b"43 42 Z\n", b"42 99 Z\n", b"garbled\n", b"", b"42 42 Z\n" * 600])
def test_owned_dead_leader_does_not_prove_no_live_children(monkeypatch, output):
    from acceptance_lib import owned_process
    import subprocess
    proc = SimpleNamespace(pid=42, args=["mechanical only"], returncode=None)
    monkeypatch.setattr(owned_process.sys, "platform", "darwin")
    monkeypatch.setattr(owned_process, "_wait_unreaped", lambda *args: None)
    def refused(*args): raise PermissionError(1, "mechanical refused group signal")
    monkeypatch.setattr(owned_process.os, "killpg", refused)
    monkeypatch.setattr(owned_process.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, output, b""))
    with pytest.raises(PermissionError):
        owned_process._signal_group(proc, signal.SIGKILL, lambda message: None)


def test_owned_already_reaped_leader_never_receives_a_group_signal(monkeypatch):
    from acceptance_lib import owned_process
    monkeypatch.setattr(owned_process.os, "killpg", lambda *args: pytest.fail("must not signal a reusable PGID"))
    with pytest.raises(OSError, match="already reaped"):
        owned_process._signal_group(SimpleNamespace(pid=42, returncode=0), signal.SIGKILL, lambda message: None)


@pytest.mark.parametrize("condition", ["nondefault-sigchld", "no-completion-api"])
def test_owned_unsupported_capture_is_missing_before_spawn(tmp_path, monkeypatch, condition):
    from acceptance_lib import owned_process
    if condition == "nondefault-sigchld":
        original = owned_process.signal.getsignal
        monkeypatch.setattr(owned_process.signal, "getsignal", lambda sig: signal.SIG_IGN if sig == signal.SIGCHLD else original(sig))
    else:
        monkeypatch.delattr(owned_process.os, "waitid", raising=False)
        monkeypatch.delattr(owned_process.select, "kqueue", raising=False)
    monkeypatch.setattr(owned_process.subprocess, "Popen", lambda *a, **kw: pytest.fail("unsupported capture must not spawn"))
    run = tmp_path / "unsupported"
    capture_command(run, "mechanical.unsupported", [sys.executable, "-c", "pass"], cwd=tmp_path, owned_process_group=True)
    _, receipt, _ = read_command_run(run / "manifest.json")
    assert receipt["exit_code"] == 127 and not receipt["timed_out"]


def test_owned_observation_error_cleans_direct_child_and_retains_failure(tmp_path, monkeypatch):
    from acceptance_lib import owned_process
    def broken(*args): raise OSError(5, "mechanical observation error")
    monkeypatch.setattr(owned_process, "_wait_unreaped", broken)
    run = tmp_path / "observation-error"
    capture_command(run, "mechanical.error", [sys.executable, "-c", "import time;time.sleep(30)"], cwd=tmp_path,
                    timeout=1, owned_process_group=True)
    _, receipt, _ = read_command_run(run / "manifest.json")
    assert receipt["exit_code"] == 127 and "observation error" in (run / "stderr.log").read_text()


def test_owned_timeout_is_not_hidden_by_cleanup_error(tmp_path, monkeypatch):
    from acceptance_lib import owned_process
    def refused(*args): raise PermissionError(1, "mechanical group cleanup refused")
    monkeypatch.setattr(owned_process.os, "killpg", refused)
    run = tmp_path / "cleanup-error"
    capture_command(run, "mechanical.timeout-cleanup", [sys.executable, "-c", "import time;time.sleep(30)"], cwd=tmp_path,
                    timeout=.05, owned_process_group=True)
    _, receipt, _ = read_command_run(run / "manifest.json")
    assert receipt["exit_code"] == 124 and receipt["timed_out"]
    assert "owned cleanup failed: PermissionError" in (run / "stderr.log").read_text()


def test_owned_cleanup_never_targets_preexisting_process(tmp_path):
    import subprocess
    other = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    try:
        run = tmp_path / "owned"
        capture_command(run, "mechanical.owned", [sys.executable, "-c", "import time;time.sleep(30)"], cwd=tmp_path,
                        timeout=.05, owned_process_group=True)
        assert other.poll() is None
    finally:
        other.terminate()
        other.wait(timeout=1)


def test_capture_default_preserves_original_nonzero_exit(tmp_path):
    run = tmp_path / "default"
    capture_command(run, "mechanical.default", [sys.executable, "-c", "print('literal');raise SystemExit(1)"], cwd=tmp_path)
    _, receipt, stdout = read_command_run(run / "manifest.json")
    assert receipt["exit_code"] == 1 and not receipt["timed_out"] and stdout.read_text() == "literal\n"
