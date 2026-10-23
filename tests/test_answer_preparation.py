"""Synthetic mechanics only: no native backend, model, endpoint or quality run."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from acceptance_lib.evidence import write_json
import answer_eval.preparation as preparation
import answer_eval.job_material as jobs
import answer_eval.materials as material
from answer_eval.completion_material import checked_answer, check_complete_records, prepare_judges
from answer_eval.protocol import EvaluationError, read_json, sha256_file
from answer_eval.protocol import grader_request
from answer_preparation_fixtures import completion, fixture_plan, materials, production, ref
from answer_preparation_fixtures import command as synthetic_command


def change(path, edit):
    data = read_json(path);edit(data);write_json(path, data)


@pytest.fixture
def plan(tmp_path):
    return fixture_plan(tmp_path / "inputs")


@pytest.fixture
def ready(plan, tmp_path, monkeypatch):
    # No material worker/inference executes: canned captured bytes are fixture-only.
    monkeypatch.setattr(jobs, "implementation", lambda root: {})
    import product_eval.runner
    monkeypatch.setattr(product_eval.runner, "production_identity", lambda root, reference_run=False: production())
    prep = preparation.load_preparation(plan, require_ready=True)
    root = tmp_path / "evaluator"
    reference = materials(prep, "reference", tmp_path / "reference", root)
    candidate = materials(prep, "candidate", tmp_path / "candidate", root)
    study = tmp_path / "study"
    return plan, prep, reference, candidate, root, study


def make_study(ready):
    plan, prep, reference, candidate, root, study = ready
    result = jobs.prepare_jobs(plan, reference, candidate, study, root=root)
    return study, result


def test_proposal_modules_are_loaded_from_the_isolated_tree():
    tree = Path(__file__).resolve().parents[1]
    import answer_eval.protocol, answer_eval.completion_material, goldmine.pack
    for module in (preparation, jobs, material, answer_eval.protocol, answer_eval.completion_material, goldmine.pack):
        assert Path(module.__file__).resolve().is_relative_to(tree)
    if os.environ.get("ANSWER_PREPARATION_MODULE_RECEIPT"):
        modules = (preparation, jobs, material, answer_eval.protocol, answer_eval.completion_material, goldmine.pack)
        write_json(Path(os.environ["ANSWER_PREPARATION_MODULE_RECEIPT"]), {
            module.__name__: {"path": str(Path(module.__file__).resolve()), "sha256": sha256_file(Path(module.__file__))} for module in modules})


@pytest.mark.parametrize("key", ["models", "settings", "tokenizer", "answerer_prompt", "grader_prompt", "preregistration", "runtime"])
def test_missing_engine_settings_and_registration_are_not_ready(plan, key):
    change(plan, lambda p: p["common"].update({key: None}))
    result = preparation.readiness(plan)
    assert result["status"] == "NOT_READY" and result["actual_answer_completions"] == 0
    assert result["planned_completions"] == {"answer_completions": 480, "judge_completions": 960}
    with pytest.raises(EvaluationError, match="NOT_READY"):
        preparation.load_preparation(plan, require_ready=True)
    assert not any(Path(path).exists() for path in read_json(plan)["runs"].values())


@pytest.mark.parametrize("key", ["models", "settings", "tokenizer"])
def test_preregistration_invariants_cannot_be_silently_rebound(plan, key):
    def edit(p):
        if key == "settings": p["common"][key]["answerer"]["seed"] = 8
        elif key == "models": p["common"][key]["answerer"]["model_id"] = "different"
        else: p["common"][key]["mode"] = "tokenize"
    change(plan, edit)
    with pytest.raises(EvaluationError, match="preregistration"):
        preparation.load_preparation(plan)


def test_forged_source_pin_and_legacy_twenty_are_refused(plan):
    change(plan, lambda p: p["data"]["questions"].update(sha256="f"*64))
    with pytest.raises(EvaluationError, match="bytes changed"):
        preparation.load_preparation(plan)


def test_legacy20_and_duplicate_ids_are_not_an80question_denominator(plan):
    p = read_json(plan)
    qfile = Path(p["data"]["questions"]["path"])
    lines = qfile.read_text().splitlines()
    qfile.write_text("\n".join(lines[:20])+"\n")
    p["data"]["questions"] = ref(qfile);write_json(plan, p)
    with pytest.raises(EvaluationError, match="exactly80"):
        preparation.load_preparation(plan)
    qfile.write_text("\n".join(lines[:-1]+[lines[0]])+"\n")
    p["data"]["questions"] = ref(qfile);write_json(plan, p)
    with pytest.raises(EvaluationError, match="duplicate question"):
        preparation.load_preparation(plan)


def test_data_freeze_cannot_change_reviewed_source_annotations_even_with_rebound_adoption(plan):
    p = read_json(plan)
    corpus, adoption = [Path(p["data"][key]["path"]) for key in ("corpus", "adoption")]
    change(corpus, lambda c: c["sources"][0].update(path="fake-replacement.txt"))
    p["data"]["corpus"] = ref(corpus)
    change(adoption, lambda a: a.update(answer_corpus_sha256=p["data"]["corpus"]["sha256"]))
    p["data"]["adoption"] = ref(adoption);write_json(plan, p)
    with pytest.raises(EvaluationError, match="preserve the exact"):
        preparation.load_preparation(plan)


@pytest.mark.parametrize("kind", ["same", "nested"])
def test_run_directories_cannot_share_or_nest(plan, kind):
    def edit(p):
        p["runs"]["candidate_PACK"] = p["runs"]["reference_PACK"] + ("/nested" if kind == "nested" else "")
    change(plan, edit)
    with pytest.raises(EvaluationError, match="disjoint"):
        preparation.load_preparation(plan)


def test_run_outputs_cannot_enter_indexed_corpus(plan):
    change(plan, lambda p: p["runs"].update(candidate_PACK=p["bundle_root"]+"/leaked-answers"))
    with pytest.raises(EvaluationError, match="outside source"):
        preparation.load_preparation(plan)


def test_byte_identical_build_receipts_at_different_paths_are_not_one_shared_corpus(plan):
    change(plan, lambda p: p["materials"]["candidate"]["build_receipt"].update(path=str(plan.parent / "different-build.json")))
    with pytest.raises(EvaluationError, match="same physical built corpus"):
        preparation.load_preparation(plan)


@pytest.mark.parametrize("key,value", [("budget", True), ("budget", 0), ("context", 4), ("z_gate", float("nan")), ("limit", False)])
def test_invalid_material_parameters_stop_before_backend_start(key, value):
    settings = {"scorer": "hybrid", "z_gate": 3.0, "limit": 10, "context": 1, "budget": 4000}
    settings[key] = value
    with pytest.raises(EvaluationError):
        material.validate_settings(settings, "reference")


def test_reference_list_and_pack_share_exactly_one_hit_set_and_no_oracle(monkeypatch):
    calls, hits = [], [SimpleNamespace(segment_id=7), SimpleNamespace(segment_id=8)]
    def search(store, backend, question, settings):
        assert question == {"q": "Visible question?"}
        calls.append("search");return "Visible question?", {}, hits
    fake = SimpleNamespace(search_one=search, PackSettings=lambda **kw: kw,
        build_pack_from_hits=lambda s,b,p,h: (calls.append(("pack", id(h))) or "pack", {}),
        render_list_arm=lambda h: calls.append(("list", id(h))) or "list")
    monkeypatch.setitem(sys.modules, "eval_answer", fake)
    assert material.build_reference(None, None, "Visible question?", {}) == (hits, "pack", {}, "list")
    assert calls == ["search", ("pack", id(hits)), ("list", id(hits))]


def test_fixed_material_capture_preserves_failed_receipt_and_does_not_return_ready(plan, tmp_path, monkeypatch):
    import prepare_answer
    import acceptance_lib.evidence
    monkeypatch.setattr(prepare_answer, "load_preparation", lambda *a, **kw: SimpleNamespace(plan=read_json(plan)))
    monkeypatch.setattr(material, "implementation", lambda root: {})
    def failed(out, check, argv, *, owned_process_group, **kw):
        assert owned_process_group is True and argv[2] == "material-worker"
        manifest = synthetic_command(out, identity=kw["identity"], check=check, argv=argv,
            stdout=b"", started="2026-10-21T08:00:00+00:00", finished="2026-10-21T08:01:00+00:00")
        change(out / "receipt.json", lambda r: r.update(exit_code=124, timed_out=True))
        change(manifest, lambda m: m.update(receipt={"path": "receipt.json", "sha256": sha256_file(out / "receipt.json")}))
        return read_json(manifest)
    monkeypatch.setattr(acceptance_lib.evidence, "capture_command", failed)
    out = tmp_path / "failed-capture"
    result = prepare_answer.capture_material(plan, "reference", out)
    assert result["status"] == "MISSING" and result["exit_code"] == 124 and result["timed_out"]
    assert (out / "stderr.log").is_file()


@pytest.mark.parametrize("entry", ["capture", "worker"])
@pytest.mark.parametrize("target", ["bundle-child", "corpus-child", "equal", "ancestor", "output-alias", "protected-alias"])
def test_material_output_ownership_guard_precedes_capture_and_backend(plan, tmp_path, monkeypatch, entry, target):
    import prepare_answer
    import acceptance_lib.evidence
    import product_eval.runner
    p = read_json(plan)
    bundle = Path(p["bundle_root"])
    corpus = Path(p["materials"]["reference"]["build_receipt"]["path"]).parent / "corpus"
    for tree in (bundle, corpus):
        tree.mkdir(parents=True, exist_ok=True)
        (tree / "original-asset.bin").write_bytes(b"untouched source bytes")
    if target == "bundle-child": output = bundle / "new-capture"
    elif target == "corpus-child": output = corpus / "new-capture"
    elif target == "equal": output = corpus
    elif target == "ancestor": output = corpus.parent
    elif target == "output-alias":
        alias = tmp_path / "capture-alias";alias.symlink_to(corpus, target_is_directory=True)
        output = alias / "new-capture"
    else:
        # The registered build/corpus itself can be a path alias; reject before
        # later strict check_build reports that invalid transport configuration.
        actual = tmp_path / "actual-protected-assets";actual.mkdir()
        (actual / "original-asset.bin").write_bytes(b"untouched source bytes")
        for node in corpus.iterdir(): node.unlink()
        corpus.rmdir();corpus.symlink_to(actual, target_is_directory=True)
        output = actual / "new-capture"
    loaded = SimpleNamespace(plan=p)
    monkeypatch.setattr(prepare_answer, "load_preparation", lambda *a, **kw: loaded)
    monkeypatch.setattr(material, "load_preparation", lambda *a, **kw: loaded)
    calls = []
    def forbidden_capture(*a, owned_process_group=False, **kw):
        calls.append("capture");raise AssertionError("must not create capture/logs")
    def forbidden_role(*a, **kw):
        calls.append("role/backend");raise AssertionError("must guard before source/index/backend work")
    monkeypatch.setattr(acceptance_lib.evidence, "capture_command", forbidden_capture)
    monkeypatch.setattr(material, "role_inputs", forbidden_role)
    monkeypatch.setattr(product_eval.runner, "make_backend", forbidden_role)
    before = {path.relative_to(tmp_path).as_posix(): path.read_bytes()
              for path in tmp_path.rglob("*") if path.is_file()}
    before_nodes = {path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")}
    with pytest.raises(EvaluationError, match="protected source/indexed corpus"):
        if entry == "capture": prepare_answer.capture_material(plan, "reference", output)
        else: material.material_worker(plan, "reference", output, root=tmp_path)
    after = {path.relative_to(tmp_path).as_posix(): path.read_bytes()
             for path in tmp_path.rglob("*") if path.is_file()}
    after_nodes = {path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")}
    assert calls == [] and after == before and after_nodes == before_nodes
    if target not in ("equal", "ancestor"):
        assert not output.exists()


def test_material_output_guard_allows_a_disjoint_owned_sibling(plan, tmp_path):
    p = read_json(plan)
    # An owned reports directory inside the candidate checkout remains valid.
    output = Path(p["materials"]["candidate"]["production_root"]) / "reports" / "owned-material-capture"
    assert preparation.owned_output(p, output) == output.resolve()
    assert not output.exists()


def test_index_identity_includes_nonvector_rows_metadata_and_fts(tmp_path):
    db = tmp_path / "tiny.sqlite"
    with sqlite3.connect(db) as con:
        con.executescript("CREATE TABLE meta(key,value);CREATE TABLE files(id,path,info);CREATE TABLE segments(id,file,vec);CREATE TABLE fts(name,body,original);")
        con.execute("INSERT INTO files VALUES(1,'source','original')")
        con.execute("INSERT INTO segments VALUES(1,1,NULL)")
        con.execute("INSERT INTO fts VALUES('name','body',NULL)")
    before = material._database(db)
    with sqlite3.connect(db) as con: con.execute("UPDATE fts SET body='changed'")
    after = material._database(db)
    assert before["rows_sha256"]["fts"] != after["rows_sha256"]["fts"]
    assert before["counts"]["segments"] == 1


def test_three_fresh_runs_prepare480_requests_but_no_completions(ready):
    study, result = make_study(ready)
    assert result["answer_descriptors"] == 480 and result["judge_descriptors_deferred"] == 960
    assert result["actual_answer_completions"] == result["actual_judge_completions"] == 0
    ids, common = set(), []
    for arm in preparation.ARMS:
        prep, run, record = jobs.study_run(study, arm)
        ids.add(record["run_id"])
        common.append((record["questions"]["sha256"], record["preregistration"]["sha256"], record["models"], record["settings"], record["tokenizer"]))
        assert not (run / "run.json").exists()
        assert not list((run / "answers_a1").iterdir()) and not list((run / "grades_a1_j1").iterdir())
        for request in (run / "requests_a1").iterdir():
            assert "ORACLE_ONLY_SECRET" not in request.read_text()
            assert "required_facts" not in request.read_text()
    assert len(ids) == 3 and common[0] == common[1] == common[2]
    with pytest.raises(EvaluationError, match="fresh"):
        make_study(ready)


def test_changed_material_and_copied_pack_cannot_be_rebound(ready):
    study, _ = make_study(ready)
    _, run, _ = jobs.study_run(study, "reference_PACK")
    (run / "packs/fixture-000.md").write_text("Different material")
    with pytest.raises(EvaluationError, match="bytes changed"):
        jobs.study_run(study, "reference_PACK")


@pytest.mark.parametrize("corruption", ["query", "hitset", "role", "index", "source", "imports"])
def test_resigned_material_receipt_still_has_to_match_the_fixed_inputs(ready, corruption):
    _, prep, reference, _, root, _ = ready
    path = reference.parent
    report_path = path / "output/material.json"
    report = read_json(report_path)
    if corruption == "query": report["questions"]["fixture-000"]["query_sha256"] = "a"*64
    elif corruption == "hitset": report["questions"]["fixture-000"]["hits"].append({"segment_id": 99})
    elif corruption == "role": report["role"] = "candidate"
    elif corruption == "index": report["index"]["sha256"] = "a"*64
    elif corruption == "source": report["production"]["source_sha256"] = "a"*64
    else: report["module_paths"]["goldmine"] = "/unregistered/goldmine/__init__.py"
    write_json(report_path, report)
    write_json(path / "stdout.log", report)
    change(path / "receipt.json", lambda r: r.update(stdout={"path": "stdout.log", "sha256": sha256_file(path / "stdout.log")},
        artifacts={"material": {"path": "output/material.json", "sha256": sha256_file(report_path)}}))
    change(reference, lambda m: m.update(receipt={"path": "receipt.json", "sha256": sha256_file(path / "receipt.json")}))
    with pytest.raises(ValueError):
        jobs.material_capture(reference, prep, "reference", root)


def test_judges_require_genuine_input_shapes_and_raw_receipt_before_any_emit(ready):
    study, _ = make_study(ready)
    _, run, record = jobs.study_run(study, "reference_PACK")
    with pytest.raises(ValueError):
        prepare_judges(study, "reference_PACK", "fixture-000", "a1")
    assert not list((run / "judge_prompts_a1").iterdir())


def test_judges_use_actual_pack_and_each_answer_gets_two_distinct_jobs(ready):
    study, _ = make_study(ready)
    prep, run, record = jobs.study_run(study, "reference_PACK")
    completion(run, record, prep.questions["fixture-000"], "a1")
    result = prepare_judges(study, "reference_PACK", "fixture-000", "a1")
    assert result["judge_descriptors"] == 2 and result["actual_judge_completions"] == 0
    prompt = (run / "judge_prompts_a1/fixture-000.txt").read_text()
    assert "ORACLE_ONLY_SECRET_0" in prompt and "Stored fixture source material <FACTS_JSON>." in prompt
    first, second = [read_json(run / "jobs" / f"fixture-000-a1-{j}.json") for j in ("j1", "j2")]
    assert first["job_id"] != second["job_id"] and first["request"] == second["request"]
    with pytest.raises(EvaluationError, match="overwrites"):
        prepare_judges(study, "reference_PACK", "fixture-000", "a1")


@pytest.mark.parametrize("corruption", ["pack", "raw", "model", "usage_bool", "oversize_answer", "oversize_raw"])
def test_answer_input_corruptions_do_not_emit_judges(ready, corruption):
    study, _ = make_study(ready)
    prep, run, record = jobs.study_run(study, "reference_PACK")
    completion(run, record, prep.questions["fixture-000"], "a1")
    if corruption == "pack":
        change(run / "answers_a1/fixture-000.json", lambda r: r.update(pack_sha256="f"*64))
    elif corruption == "raw":
        (run / "raw/answers_a1/fixture-000/stdout.log").write_text("copied other answer")
    elif corruption == "model":
        Path(record["runtime"]["answerer"]["model_file"]["path"]).write_text("changed model")
    elif corruption == "oversize_answer":
        (run / "answers_a1/fixture-000.md").write_bytes(b"x" * (1024*1024+1))
    elif corruption == "oversize_raw":
        (run / "raw/answers_a1/fixture-000/stdout.log").write_bytes(b"x" * (1024*1024+1))
    else:
        path = run / "raw/answers_a1/fixture-000"
        write_json(path / "usage.json", {"input_tokens": True, "output_tokens": 4})
        change(path / "receipt.json", lambda r: r["artifacts"].update(usage={"path": "usage.json", "sha256": sha256_file(path / "usage.json")}))
        change(path / "manifest.json", lambda m: m.update(receipt={"path": "receipt.json", "sha256": sha256_file(path / "receipt.json")}))
    with pytest.raises(ValueError):
        prepare_judges(study, "reference_PACK", "fixture-000", "a1")
    assert not (run / "judge_prompts_a1/fixture-000.txt").exists()


def test_incomplete_three_arm_family_cannot_write_run_manifest(ready):
    study, _ = make_study(ready)
    with pytest.raises(EvaluationError, match="exactly160"):
        check_complete_records(study)
    assert all(not (Path(row["path"]) / "run.json").exists() for row in read_json(study / "study.json")["runs"].values())


def test_full1440_synthetic_record_guard_rejects_cross_arm_reuse_without_quality_claim(ready):
    # Deliberately fabricate ONLY unit fixtures. No command/engine executes, and
    # final status must keep actual execution review/quality acceptance MISSING.
    study, _ = make_study(ready)
    owned = {}
    for arm in preparation.ARMS:
        prep, run, record = jobs.study_run(study, arm)
        owned[arm] = run
        for qid, q in prep.questions.items():
            for a in ("a1", "a2"):
                completion(run, record, q, a)
                prompt = grader_request((run / "grader.txt").read_text(), q,
                    (run / f"answers_{a}" / (qid+".md")).read_text(), (run / "packs" / (qid+".md")).read_text())
                (run / f"judge_prompts_{a}" / (qid+".txt")).write_text(prompt)
                for j in ("j1", "j2"):
                    write_json(run / "jobs" / f"{qid}-{a}-{j}.json", jobs.descriptor(run, record, qid, a, judge=j))
                    completion(run, record, q, a, j)
        # Only this synthetic test fixture constructs a completed v3 record.
        # The production preparation adapter never does so for an unknown engine.
        manifest = {key: record[key] for key in ("protocol", "run_id", "started_at", "questions", "corpus",
            "preregistration", "source_groups_sha256", "models", "settings", "answerer_prompt", "grader_prompt", "tokenizer", "variables")}
        latest = max(read_json(p)["completed_at"] for p in (run / "grades_a2_j2").iterdir())
        manifest.update(schema=3, evidence_mode="real", completed_at=latest)
        write_json(run / "run.json", manifest)
    unchanged_runs = {run: (run / "run.json").read_bytes() for run in owned.values()}
    first = read_json(owned["reference_LIST"] / "answers_a1/fixture-000.json")["execution_id"]
    other = owned["reference_PACK"]
    paths = [other / "answers_a1/fixture-000.json", other / "raw/answers_a1/fixture-000/receipt.json", other / "raw/answers_a1/fixture-000/manifest.json"]
    originals = {p: p.read_bytes() for p in paths}
    change(paths[0], lambda r: r.update(execution_id=first))
    change(paths[1], lambda r: r.update(run_id=first))
    change(paths[2], lambda m: m.update(run_id=first, receipt={"path": "receipt.json", "sha256": sha256_file(paths[1])}))
    with pytest.raises(EvaluationError, match="distinct actual completion"):
        check_complete_records(study)
    assert all((run / "run.json").read_bytes() == original for run, original in unchanged_runs.items())
    for p, data in originals.items(): p.write_bytes(data)
    result = check_complete_records(study)
    assert (result["answer_records"], result["judge_records"], result["distinct_execution_ids"]) == (480, 960, 1440)
    assert result["status"] == "MISSING" and result["record_consistency"] == "COMPLETE_DISK_RECORDS" and result["release_eligible"] is False
    assert result["engine_specific_execution_review"].startswith("MISSING")
    assert result["quality_acceptance"].startswith("MISSING")
    assert all((run / "run.json").read_bytes() == original for run, original in unchanged_runs.items())
