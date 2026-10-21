"""Prepare three independent existing-schema runs, without inventing completions."""
from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

from acceptance_lib.evidence import (artifact, now, read_command_run, verify_artifact,
                                     write_json)
from .materials import implementation
from .preparation import ARMS, file_ref, load_preparation, protected_roots
from .protocol import (PROTOCOL, EvaluationError, answer_request, canonical_sha256,
                       fields, inside, read_json, read_text, sha256_file, timestamp)


def material_capture(path, prep, role, root):
    from product_eval.runner import production_content, production_identity
    manifest, receipt, stdout = read_command_run(Path(path), check_id="answer.material." + role, suite_id=PROTOCOL)
    if receipt["exit_code"] != 0 or receipt["timed_out"]:
        raise EvaluationError("material command did not complete", status="MISSING")
    material_path = verify_artifact(Path(path).parent, receipt["artifacts"].get("material"))
    report = read_json(material_path)
    value = prep.plan["materials"][role]
    expected_argv = [receipt["argv"][0], str(Path(root).resolve() / "scripts/prepare_answer.py"),
        "material-worker", "--plan", str(prep.path), "--role", role, "--out", str(material_path.parent)]
    if (receipt["argv"] != expected_argv or report != read_json(stdout)
            or manifest["identity"] != {"plan_sha256": sha256_file(prep.path), "evaluator_files": implementation(root)}
            or report.get("evaluator_files") != implementation(root)
            or report.get("status") != "MATERIAL_PREPARED" or report.get("role") != role
            or report.get("plan_sha256") != sha256_file(prep.path)
            or set(report.get("questions", {})) != set(prep.questions)
            or report.get("settings") != value["settings"] or report.get("variables") != value["variables"]
            or report.get("index", {}).get("sha256") != value["index"]["sha256"]
            or report.get("embedding_model") != value["embedding_model"] or report.get("server") != value["server"]
            or report.get("build_receipt_sha256") != value["build_receipt"]["sha256"]
            or timestamp(receipt["started_at"], "material start") < timestamp(prep.plan["registered_at"], "registration")):
        raise EvaluationError("material command/source/plan/role provenance mismatch")
    file_ref(value["index"], "material index")
    actual_source = production_identity(value["production_root"], reference_run=role == "reference")
    observed_source = report.get("production")
    source_equal = observed_source == actual_source if role == "reference" else production_content(observed_source) == production_content(actual_source)
    modules = report.get("module_paths")
    if (not source_equal or not isinstance(modules, dict) or "goldmine" not in modules
            or any(not Path(name).resolve().is_relative_to(Path(value["production_root"]).resolve()) for name in modules.values())):
        raise EvaluationError("material source/module import identity differs from the registered checkout")
    arms = ARMS[:2] if role == "reference" else ARMS[2:]
    expected_files = {}
    for qid, q in prep.questions.items():
        row = report["questions"][qid]
        if (row.get("query_sha256") != canonical_sha256(q["q"])
                or row.get("hits_sha256") != canonical_sha256(row.get("hits"))
                or set(row.get("representations", {})) != set(arms)):
            raise EvaluationError("material query/hit-set/representation binding mismatch")
        for arm in arms:
            ref = row["representations"][arm]
            if ref.get("path") != f"{arm}/{qid}.md":
                raise EvaluationError("unexpected material path")
            verify_artifact(material_path.parent, ref)
            expected_files[ref["path"]] = ref["sha256"]
    if report.get("files") != expected_files:
        raise EvaluationError("material file registry incomplete")
    return material_path, report


def descriptor(run, prepared, qid, a, *, judge=None):
    role = "judge" if judge else "answerer"
    request = run / (f"judge_prompts_{a}" if judge else f"requests_{a}") / (qid + ".txt")
    key = "/".join(x for x in (prepared["run_id"], qid, a, judge) if x)
    return {"schema": 1, "protocol": PROTOCOL, "kind": "judge" if judge else "answer",
            "job_id": key, "run_id": prepared["run_id"], "question_id": qid,
            "answer_round": a, "judge_round": judge, "request": artifact(run, request),
            "pack": artifact(run, run / "packs" / (qid + ".md")),
            "model": prepared["models"][role], "settings": prepared["settings"][role],
            "runtime": prepared["runtime"][role],
            "preregistration_sha256": prepared["preregistration"]["sha256"],
            "status": "PENDING_EXTERNAL_ENGINE_COMPLETION", "release_eligible": False}


def prepare_jobs(plan, reference, candidate, study, *, root):
    prep = load_preparation(plan, require_ready=True, verify_runtime=True)
    study = Path(study).resolve()
    if study.exists() or any(Path(path).exists() for path in prep.plan["runs"].values()):
        raise EvaluationError("study and all three run destinations must be fresh")
    if any(study.is_relative_to(protected) for protected in protected_roots(prep.plan)):
        raise EvaluationError("study cannot write labels/jobs into the source/indexed corpus")
    if any(Path(path) == study or Path(path) in study.parents or study in Path(path).parents for path in prep.plan["runs"].values()):
        raise EvaluationError("study/run trees must be disjoint")
    material = {role: material_capture(path, prep, role, root)
                for role, path in (("reference", reference), ("candidate", candidate))}
    if material["reference"][1]["build_receipt_sha256"] != material["candidate"][1]["build_receipt_sha256"]:
        raise EvaluationError("three arms must use the same exact built corpus/metadata transport")
    study.mkdir(parents=True, exist_ok=False)
    # Keep original bytes. The run.json completion record is deliberately absent.
    shutil.copyfile(prep.path, study / "plan.json")
    prepared_at, runs = now(), {}
    for arm in ARMS:
        run = Path(prep.plan["runs"][arm]).resolve()
        run.mkdir(parents=True, exist_ok=False)
        for folder in ("packs", "raw", "jobs", "requests_a1", "requests_a2", "answers_a1", "answers_a2",
                       "judge_prompts_a1", "judge_prompts_a2", "grades_a1_j1", "grades_a1_j2", "grades_a2_j1", "grades_a2_j2"):
            (run / folder).mkdir()
        copies = {"questions": "questions.jsonl", "corpus": "corpus.json", "source_map": "source-map.json",
                  "amendment": "data-amendment-registration.json", "review": "source-review.json",
                  "reviewed_corpus": "reviewed-corpus.json", "review_package": "review-package.json", "adoption": "data-adoption.json"}
        references = {}
        for key, name in copies.items():
            shutil.copyfile(file_ref(prep.plan["data"][key], key), run / name)
            references[key] = artifact(run, run / name)
        for key, name in (("answerer_prompt", "answerer.txt"), ("grader_prompt", "grader.txt"), ("preregistration", "preregistration.json")):
            shutil.copyfile(file_ref(prep.common[key], key), run / name)
            references[key] = artifact(run, run / name)
        material_path, report = material["candidate" if arm == "candidate_PACK" else "reference"]
        variables = dict(report["variables"], code=report["production"])
        variables["pack"] = {"registered": variables["pack"], "representation": arm.split("_")[1], "settings": report["settings"]}
        prepared = {"schema": 1, "protocol": PROTOCOL, "run_id": str(uuid4()), "arm": arm,
            "started_at": prepared_at, "plan_sha256": sha256_file(prep.path), **references,
            "source_groups_sha256": canonical_sha256({i: q["source_group"] for i, q in prep.questions.items()}),
            "models": prep.common["models"], "settings": prep.common["settings"], "tokenizer": prep.common["tokenizer"],
            "runtime": prep.common["runtime"], "variables": variables,
            "material_capture": {"path": str(Path(candidate if arm == "candidate_PACK" else reference).resolve()),
                                 "sha256": sha256_file(Path(candidate if arm == "candidate_PACK" else reference))},
            "material_sha256": sha256_file(material_path), "evaluator_files": implementation(root)}
        for qid, q in prep.questions.items():
            ref = report["questions"][qid]["representations"][arm]
            shutil.copyfile(verify_artifact(material_path.parent, ref), run / "packs" / (qid + ".md"))
            for a in ("a1", "a2"):
                request = answer_request(read_text(run / "answerer.txt"), {"q": q["q"]}, read_text(run / "packs" / (qid + ".md")))
                (run / f"requests_{a}" / (qid + ".txt")).write_text(request, encoding="utf-8", newline="\n")
                write_json(run / "jobs" / f"{qid}-{a}.json", descriptor(run, prepared, qid, a))
        prepared["owned_files"] = {p.relative_to(run).as_posix(): sha256_file(p)
                                   for folder in ("packs", "requests_a1", "requests_a2", "jobs")
                                   for p in sorted((run / folder).iterdir())}
        write_json(run / "preparation.json", prepared)
        runs[arm] = {"path": str(run), "preparation_sha256": sha256_file(run / "preparation.json")}
    report = {"schema": 1, "protocol": PROTOCOL, "status": "ANSWER_REQUESTS_PREPARED",
              "plan_sha256": sha256_file(prep.path), "prepared_at": prepared_at, "runs": runs,
              "answer_descriptors": 480, "judge_descriptors": 0, "judge_descriptors_deferred": 960,
              "actual_answer_completions": 0, "actual_judge_completions": 0,
              "required_two_run_comparisons": [["reference_LIST", "reference_PACK"], ["reference_PACK", "candidate_PACK"]],
              "execution_bridge": "MISSING: external engine-specific implementation/receipts required",
              "release_eligible": False}
    write_json(study / "study.json", report)
    return report


def study_run(study, arm):
    if arm not in ARMS:
        raise EvaluationError("unknown mandatory arm")
    path = Path(study).resolve()
    saved = read_json(path / "study.json")
    fields(saved["runs"], set(ARMS), "study runs")
    prep = load_preparation(path / "plan.json", require_ready=True)
    if saved["plan_sha256"] != sha256_file(path / "plan.json"):
        raise EvaluationError("prepared plan changed")
    run = Path(saved["runs"][arm]["path"]).resolve()
    if run != Path(prep.plan["runs"][arm]).resolve() or sha256_file(run / "preparation.json") != saved["runs"][arm]["preparation_sha256"]:
        raise EvaluationError("prepared run identity changed")
    record = read_json(run / "preparation.json")
    for key in ("questions", "corpus", "source_map", "amendment", "review", "reviewed_corpus", "review_package", "adoption", "preregistration", "answerer_prompt", "grader_prompt"):
        verify_artifact(run, record[key])
    for relative, digest in record["owned_files"].items():
        if sha256_file(inside(run, relative)) != digest:
            raise EvaluationError("prepared material/request/job bytes changed")
    return prep, run, record


def comparison_jobs(study, *, root):
    """Emit only the two fixed existing compare capture requests, never execute."""
    import sys
    from acceptance_lib.ir_checks import evaluator_files
    loaded = {arm: study_run(study, arm) for arm in ARMS}
    jobs = []
    for reference, candidate in (("reference_LIST", "reference_PACK"), ("reference_PACK", "candidate_PACK")):
        _, a, am = loaded[reference]
        _, b, bm = loaded[candidate]
        jobs.append({"schema": 1, "kind": "goldmine.command-request", "check_id": "answer.compare", "suite_id": PROTOCOL,
            "argv": [sys.executable, str(Path(root).resolve() / "scripts/eval_answer.py"), "--compare", str(a), str(b), "--json"],
            "cwd": str(Path(root).resolve()), "identity": {"evaluator_files": evaluator_files(root, "answer"),
                "reference_source": am["variables"]["code"], "candidate_source": bm["variables"]["code"]},
            "status": "PENDING_GENUINE_COMPLETIONS_AND_EXECUTION_REVIEW", "release_eligible": False})
    return {"status": "COMPARE_REQUESTS_PREPARED", "requests": jobs, "commands_executed": 0, "release_eligible": False}
