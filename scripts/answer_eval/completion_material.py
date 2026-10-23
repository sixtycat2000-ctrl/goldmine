"""Render judge jobs only after actual disk records, without executing engines.

Raw command receipts establish local consistency, not an independent attestation
that the stated model consumed a request. An engine-specific reviewed bridge is
still required before these records can be promoted as genuine quality evidence.
"""
from __future__ import annotations

from pathlib import Path

from acceptance_lib.evidence import read_command_run, verify_artifact, write_json
from .job_material import descriptor, study_run
from .preparation import ARMS, file_ref
from .protocol import (PROTOCOL, EvaluationError, answer_request, fields, grader_request,
                       inside, load_run, read_json, read_text, sha256_file, timestamp)
from .remote_engine import ENGINE_KIND, verify_http_manifest

ANSWER_FIELDS = {"schema", "run_id", "question_id", "answer_round", "execution_id", "completed_at",
                 "answer_sha256", "pack_sha256", "answerer_prompt_sha256", "request_sha256",
                 "answerer_model", "answerer_settings"}
GRADE_OUTPUT = {"score", "refused", "fabricated_fact", "fact_results"}
MAX_COMPLETION_BYTES = 1024 * 1024


def _bounded_completion(path, label):
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise EvaluationError(label + " is missing/unreadable", status="MISSING") from exc
    if size > MAX_COMPLETION_BYTES:
        raise EvaluationError(label + " exceeds1MiB")


def _remote_raw_completion(run, job, record, qid, a, j, name):
    """Extension branch (answer-engine-extension-001/SPEC.md §3): verify the
    remote http-run receipt family with the same strictness as the local one."""
    path = run / "raw" / name / qid
    for artifact in ("manifest.json", "receipt.json"):
        _bounded_completion(path / artifact, "raw " + artifact)
    manifest, receipt, stdout = verify_http_manifest(path, job)
    if (record["execution_id"] != manifest["run_id"]
            or record["completed_at"] != receipt["finished_at"]):
        raise EvaluationError("raw completion job/execution/engine identity mismatch")
    response = read_json(path / "response.json")
    choice = response["choices"][0]
    if receipt["finish_reason"] != str(choice.get("finish_reason")) or receipt["response_model"] != str(response.get("model")):
        raise EvaluationError("raw receipt finish_reason/model differs from the native response")
    if j:
        result = read_json(stdout)
        fields(result, GRADE_OUTPUT, "raw judge response")
        if result != {key: record[key] for key in GRADE_OUTPUT}:
            raise EvaluationError("raw judge output differs from supplied v3 grade")
    elif stdout.read_bytes() != (run / name / (qid + ".md")).read_bytes():
        raise EvaluationError("raw answer output differs from supplied answer bytes")
    return receipt


def raw_completion(run, job, record):
    """Closed receipt layout; accepts no executable command or endpoint argument."""
    qid, a, j = job["question_id"], job["answer_round"], job["judge_round"]
    name = f"grades_{a}_{j}" if j else f"answers_{a}"
    engine = job["runtime"].get("engine") if isinstance(job["runtime"], dict) else None
    if isinstance(engine, dict) and engine.get("kind") == ENGINE_KIND:
        return _remote_raw_completion(run, job, record, qid, a, j, name)
    for key, ref in job["runtime"].items():
        file_ref(ref, "actual completion runtime." + key)
    path = run / "raw" / name / qid / "manifest.json"
    _bounded_completion(path, "raw manifest")
    header = read_json(path)
    if header.get("receipt", {}).get("path") != "receipt.json":
        raise EvaluationError("raw completion uses the fixed owned capture layout")
    small = path.parent / "receipt.json"
    _bounded_completion(small, "raw receipt")
    raw_header = read_json(small)
    if raw_header.get("stdout", {}).get("path") != "stdout.log":
        raise EvaluationError("raw completion uses the fixed owned stdout layout")
    _bounded_completion(path.parent / "stdout.log", "raw completion output")
    manifest, command, stdout = read_command_run(path, check_id="answer.job", suite_id=PROTOCOL)
    if (manifest["identity"] != {"job": job} or command["exit_code"] != 0 or command["timed_out"]
            or record["execution_id"] != manifest["run_id"] or record["completed_at"] != command["finished_at"]
            or Path(command["argv"][0]).resolve() != Path(job["runtime"]["engine"]["path"]).resolve()):
        raise EvaluationError("raw completion job/execution/engine identity mismatch")
    request = verify_artifact(path.parent, command["artifacts"].get("request"))
    if sha256_file(request) != job["request"]["sha256"]:
        raise EvaluationError("raw engine receipt lacks the exact request bytes")
    usage = read_json(verify_artifact(path.parent, command["artifacts"].get("usage")))
    fields(usage, {"input_tokens", "output_tokens"}, "actual engine usage")
    if any(type(v) is not int or v < 0 for v in usage.values()):
        raise EvaluationError("actual usage requires nonnegative integer counters, no invented unknown=0")
    if j:
        result = read_json(stdout)
        fields(result, GRADE_OUTPUT, "raw judge response")
        if result != {key: record[key] for key in GRADE_OUTPUT}:
            raise EvaluationError("raw judge output differs from supplied v3 grade")
    elif stdout.read_bytes() != (run / name / (qid + ".md")).read_bytes():
        raise EvaluationError("raw answer output differs from supplied answer bytes")
    return command


def checked_answer(prep, run, prepared, qid, a):
    if qid not in prep.questions or a not in ("a1", "a2"):
        raise EvaluationError("unknown question/answer round")
    q = prep.questions[qid]
    answer_path = run / f"answers_{a}" / (qid + ".md")
    _bounded_completion(answer_path, "answer completion")
    text = read_text(answer_path)
    if not text.strip():
        raise EvaluationError("missing actual answer text", status="MISSING")
    sidecar = run / f"answers_{a}" / (qid + ".json")
    _bounded_completion(sidecar, "answer sidecar")
    record = read_json(sidecar)
    fields(record, ANSWER_FIELDS, "answer")
    pack = run / "packs" / (qid + ".md")
    request = run / f"requests_{a}" / (qid + ".txt")
    if read_text(request) != answer_request(read_text(run / "answerer.txt"), q, read_text(pack)):
        raise EvaluationError("answer request changed from its frozen template/question/pack")
    expected = {"schema": 3, "run_id": prepared["run_id"], "question_id": qid, "answer_round": a,
        "answer_sha256": sha256_file(run / f"answers_{a}" / (qid + ".md")), "pack_sha256": sha256_file(pack),
        "answerer_prompt_sha256": prepared["answerer_prompt"]["sha256"], "request_sha256": sha256_file(request),
        "answerer_model": prepared["models"]["answerer"], "answerer_settings": prepared["settings"]["answerer"]}
    if type(record["schema"]) is not int or any(record[k] != v for k, v in expected.items()):
        raise EvaluationError("answer sidecar provenance mismatch")
    job = descriptor(run, prepared, qid, a)
    if read_json(run / "jobs" / f"{qid}-{a}.json") != job:
        raise EvaluationError("prepared answer job changed")
    raw = raw_completion(run, job, record)
    if timestamp(raw["started_at"], "answer start") < timestamp(prepared["started_at"], "prepared start"):
        raise EvaluationError("answer predates its fresh owned run")
    return text, record, raw


def prepare_judges(study, arm, qid, a):
    prep, run, prepared = study_run(study, arm)
    text, _, _ = checked_answer(prep, run, prepared, qid, a)
    output = run / f"judge_prompts_{a}" / (qid + ".txt")
    jobs = [run / "jobs" / f"{qid}-{a}-{j}.json" for j in ("j1", "j2")]
    if output.exists() or any(path.exists() for path in jobs):
        raise EvaluationError("judge preparation never overwrites an existing job")
    prompt = grader_request(read_text(run / "grader.txt"), prep.questions[qid], text,
                            read_text(run / "packs" / (qid + ".md")))
    output.write_text(prompt, encoding="utf-8", newline="\n")
    for j, path in zip(("j1", "j2"), jobs):
        write_json(path, descriptor(run, prepared, qid, a, judge=j))
    return {"status": "JUDGE_REQUESTS_PREPARED", "judge_descriptors": 2,
            "actual_judge_completions": 0, "release_eligible": False}


def checked_judge(run, prepared, qid, a, j, answer):
    """Verify one judge record family against the frozen disk protocol."""
    grade_path = run / f"grades_{a}_{j}" / (qid + ".json")
    _bounded_completion(grade_path, "grade sidecar")
    grade = read_json(grade_path)
    expected_job = descriptor(run, prepared, qid, a, judge=j)
    if read_json(run / "jobs" / f"{qid}-{a}-{j}.json") != expected_job:
        raise EvaluationError("judge job changed")
    command = raw_completion(run, expected_job, grade)
    if timestamp(command["started_at"], "judge start") < timestamp(answer["completed_at"], "answer completion"):
        raise EvaluationError("judge started before the actual answer completed")
    return grade, command


def check_complete_records(study):
    """Read all1440 distinct external completions; never complete a run for an engine."""
    run_manifests, all_ids, all_capture_ids, n_answers, n_judges = [], set(), set(), 0, 0
    for arm in ARMS:
        prep, run, prepared = study_run(study, arm)
        expected_jobs = {f"{qid}-{a}{'-'+j if j else ''}.json" for qid in prep.questions
                         for a in ("a1", "a2") for j in (None, "j1", "j2")}
        expected_raw = {f"{folder}/{qid}/manifest.json" for qid in prep.questions
                        for folder in ("answers_a1", "answers_a2", "grades_a1_j1", "grades_a1_j2", "grades_a2_j1", "grades_a2_j2")}
        if ({p.name for p in (run / "jobs").iterdir()} != expected_jobs or
                {p.relative_to(run / "raw").as_posix() for p in (run / "raw").rglob("manifest.json")} != expected_raw):
            raise EvaluationError("require exactly160 answer/320 judge jobs and raw completions per arm", status="MISSING")
        completed = []
        for qid in prep.questions:
            for a in ("a1", "a2"):
                _, answer, raw = checked_answer(prep, run, prepared, qid, a)
                records = [(answer, raw)]
                n_answers += 1
                for j in ("j1", "j2"):
                    grade, command = checked_judge(run, prepared, qid, a, j, answer)
                    records.append((grade, command))
                    n_judges += 1
                for record, command in records:
                    if record["execution_id"] in all_ids or command["run_id"] in all_capture_ids:
                        raise EvaluationError("all480 answers/960 judgments require distinct actual completion IDs across three arms")
                    all_ids.add(record["execution_id"])
                    all_capture_ids.add(command["run_id"])
                    completed.append(record["completed_at"])
        manifest = {key: prepared[key] for key in ("protocol", "run_id", "started_at", "questions", "corpus",
            "preregistration", "source_groups_sha256", "models", "settings", "answerer_prompt", "grader_prompt", "tokenizer", "variables")}
        manifest.update(schema=3, evidence_mode="real", completed_at=max(completed, key=lambda t: timestamp(t, "completion")))
        # The genuine external executor must supply this existing-schema record.
        # Preparation never relabels an incomplete/unknown engine as evidence_mode=real.
        if read_json(run / "run.json") != manifest:
            raise EvaluationError("external completed run differs from its owned material/actual completion family")
        load_run(run)
        run_manifests.append({"path": str(run / "run.json"), "sha256": sha256_file(run / "run.json")})
    if (n_answers, n_judges, len(all_ids)) != (480, 960, 1440):
        raise EvaluationError("incomplete mandatory three-arm completion family", status="MISSING")
    result = {"status": "MISSING", "record_consistency": "COMPLETE_DISK_RECORDS", "answer_records": n_answers, "judge_records": n_judges,
              "distinct_execution_ids": len(all_ids), "runs": run_manifests,
              "authenticity_scope": "local raw-command/request/output/model metadata consistency only",
              "engine_specific_execution_review": "MISSING until an actual engine bridge is reviewed",
              "quality_acceptance": "MISSING until both real captured comparisons and execution review",
              "release_eligible": False}
    return result
