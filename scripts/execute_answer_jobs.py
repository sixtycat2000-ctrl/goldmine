#!/usr/bin/env python3
"""Execute prepared answer-eval/v3 jobs against the reviewed remote engine bridge.

This is the engine-specific coordinator of
reports/execution/P0-03/answer-engine-extension-001/SPEC.md: it consumes the
immutable descriptors produced by scripts/prepare_answer.py, performs one
sequential remote call per job (no retries), and writes the raw HTTP receipt
family plus the existing v3 answer/grade sidecars, re-verified with the frozen
readers. The API key comes from GLM_KEY and never reaches disk. It never runs
the full three-arm study by itself and never sees labels the descriptors do
not already carry.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

from acceptance_lib.evidence import artifact, now, write_json
from answer_eval.completion_material import (ANSWER_FIELDS, checked_answer,
                                             checked_judge, prepare_judges)
from answer_eval.job_material import descriptor, study_run
from answer_eval.preparation import ARMS
from answer_eval.protocol import (PROTOCOL, EvaluationError, answer_request, canonical_json,
                                  canonical_sha256, grader_request, read_json, read_text,
                                  sha256_file, timestamp, validate_question)
from answer_eval.remote_engine import (chat_url, engine_binding, execute_prompt,
                                       judge_output, load_engine, require_api_key,
                                       validate_engine, write_failure_record,
                                       write_raw_completion)

SMOKE_FLAG = "GLM_SMOKE"
# Exactly the field sets protocol.load_run enforces on the two sidecar families.
GRADE_RECORD_FIELDS = {"schema", "run_id", "question_id", "answer_round", "judge_round",
                       "execution_id", "completed_at", "score", "refused", "fabricated_fact",
                       "fact_results", "answer_sha256", "pack_sha256", "grader_prompt_sha256",
                       "judge_model", "judge_settings"}


def execute_job(prep, run, prepared, job, engine, key):
    """Execute one immutable descriptor; write raw family + v3 sidecar, then verify."""
    validate_engine(engine)
    qid, a, j = job["question_id"], job["answer_round"], job["judge_round"]
    if (job["run_id"] != prepared["run_id"] or job["model"] != engine_binding(engine)
            or job["settings"] != engine["settings"] or job["runtime"] != {"engine": engine}):
        raise EvaluationError("job descriptor was not prepared for this registered remote engine identity")
    question = prep.questions[qid]
    name = f"grades_{a}_{j}" if j else f"answers_{a}"
    prompt_path = run / job["request"]["path"]
    prompt_bytes = prompt_path.read_bytes()
    if hashlib.sha256(prompt_bytes).hexdigest() != job["request"]["sha256"]:
        raise EvaluationError("prepared request bytes changed before execution")
    output_path = run / name / (qid + (".json" if j else ".md"))
    raw = run / "raw" / name / qid
    if output_path.exists() or raw.exists():  # refuse before spending a remote call
        raise EvaluationError("execution never overwrites an existing completion")
    completion, failure = execute_prompt(engine, key, prompt_bytes.decode("utf-8"))
    if completion is None:
        summary = {"status": "FAILED", "job_id": job["job_id"], "kind": job["kind"],
                   "failure": failure.get("failure"), "error": failure.get("error"),
                   "http_status": failure.get("status"), "retries": 0,
                   "wall_ms": round(failure["duration_ms"], 3) if "duration_ms" in failure else None,
                   "release_eligible": False}
        record = write_failure_record(run / "raw" / "failures" / job["job_id"].replace("/", "_"),
                                       job, engine, failure)
        summary["failure_record"] = {"path": str(record), "sha256": sha256_file(record)}
        return summary
    manifest = write_raw_completion(raw, job, engine, prompt_bytes, completion)
    execution_id, completed_at = manifest["run_id"], completion.finished_at
    if j:
        answer_record = read_json(run / f"answers_{a}" / (qid + ".json"))
        payload = judge_output(completion.content, question)
        grade_path = output_path
        sidecar = {"schema": 3, "run_id": prepared["run_id"], "question_id": qid, "answer_round": a,
                   "judge_round": j, "execution_id": execution_id, "completed_at": completed_at,
                   **payload, "answer_sha256": sha256_file(run / f"answers_{a}" / (qid + ".md")),
                   "pack_sha256": job["pack"]["sha256"], "grader_prompt_sha256": job["request"]["sha256"],
                   "judge_model": job["model"], "judge_settings": job["settings"]}
        write_json(grade_path, sidecar)
        output = {"path": str(grade_path), "sha256": sha256_file(grade_path)}
        checked_judge(run, prepared, qid, a, j, answer_record)
    else:
        answer_path = output_path
        answer_path.write_bytes(completion.content.encode("utf-8"))
        sidecar = {"schema": 3, "run_id": prepared["run_id"], "question_id": qid, "answer_round": a,
                   "execution_id": execution_id, "completed_at": completed_at,
                   "answer_sha256": sha256_file(answer_path), "pack_sha256": job["pack"]["sha256"],
                   "answerer_prompt_sha256": prepared["answerer_prompt"]["sha256"],
                   "request_sha256": job["request"]["sha256"],
                   "answerer_model": job["model"], "answerer_settings": job["settings"]}
        write_json(run / name / (qid + ".json"), sidecar)
        output = {"path": str(answer_path), "sha256": sha256_file(answer_path)}
        checked_answer(prep, run, prepared, qid, a)
    if set(sidecar) != (GRADE_RECORD_FIELDS if j else ANSWER_FIELDS):
        raise EvaluationError("written sidecar fields do not match the frozen v3 record")
    return {"status": "JOB_EXECUTED", "job_id": job["job_id"], "kind": job["kind"],
            "execution_id": execution_id, "completed_at": completed_at,
            "http": {"method": "POST", "url": chat_url(engine), "status": completion.status,
                     "finish_reason": completion.finish_reason, "response_model": completion.response_model},
            "wall_ms": round(completion.duration_ms, 3), "usage": completion.usage,
            "raw_manifest": {"path": str(raw / "manifest.json"), "sha256": sha256_file(raw / "manifest.json")},
            "output": output, "retries": 0, "release_eligible": False}


def execute_question(study, arm, qid, a, engine):
    """Answer job, then judge preparation, then both judge jobs — strictly in order."""
    key = require_api_key(engine)
    prep, run, prepared = study_run(study, arm)
    if qid not in prep.questions or a not in ("a1", "a2"):
        raise EvaluationError("unknown question/answer round")
    job = read_json(run / "jobs" / f"{qid}-{a}.json")
    if job != descriptor(run, prepared, qid, a):
        raise EvaluationError("prepared answer job changed")
    answer = execute_job(prep, run, prepared, job, engine, key)
    if answer["status"] == "FAILED":
        return {"status": "FAILED", "arm": arm, "question_id": qid, "answer_round": a,
                "jobs": [answer], "judge_descriptors_prepared": 0, "release_eligible": False}
    prepare_judges(study, arm, qid, a)
    judges = []
    for j in ("j1", "j2"):
        job = read_json(run / "jobs" / f"{qid}-{a}-{j}.json")
        if job != descriptor(run, prepared, qid, a, judge=j):
            raise EvaluationError("prepared judge job changed")
        result = execute_job(prep, run, prepared, job, engine, key)
        judges.append(result)
        if result["status"] == "FAILED":
            return {"status": "FAILED", "arm": arm, "question_id": qid, "answer_round": a,
                    "jobs": [answer] + judges, "judge_descriptors_prepared": 2, "release_eligible": False}
    return {"status": "QUESTION_EXECUTED", "arm": arm, "question_id": qid, "answer_round": a,
            "jobs": [answer] + judges, "judge_descriptors_prepared": 2,
            "genuine_completions": 3, "release_eligible": False}


def finalize_run(study, arm):
    """Write the final existing-schema run.json only after every record verifies."""
    prep, run, prepared = study_run(study, arm)
    completions = []
    for qid in prep.questions:
        for a in ("a1", "a2"):
            _, answer, _ = checked_answer(prep, run, prepared, qid, a)
            completions.append(answer["completed_at"])
            for j in ("j1", "j2"):
                grade, _ = checked_judge(run, prepared, qid, a, j, answer)
                completions.append(grade["completed_at"])
    if (run / "run.json").exists():
        raise EvaluationError("run finalization never overwrites an existing run.json")
    manifest = {key: prepared[key] for key in ("protocol", "run_id", "started_at", "questions", "corpus",
        "preregistration", "source_groups_sha256", "models", "settings", "answerer_prompt",
        "grader_prompt", "tokenizer", "variables")}
    manifest.update(schema=3, evidence_mode="real",
                    completed_at=max(completions, key=lambda t: timestamp(t, "completion")))
    write_json(run / "run.json", manifest)
    return {"status": "RUN_FINALIZED", "arm": arm, "run": {"path": str(run / "run.json"),
            "sha256": sha256_file(run / "run.json")}, "records": len(completions),
            "release_eligible": False}


SMOKE_QID = "smoke-001"
SMOKE_GROUP = "smoke-group-001"
SMOKE_SOURCE = "source-assets/smoke-source.txt"
SMOKE_LINE = "Internal note: the material transport codename for this cycle is GOLDRUSH-SMOKE-7."
SMOKE_QUESTION = "According to the supplied material, which material transport codename is assigned to this cycle?"
SMOKE_FACT = "The supplied material states the material transport codename for this cycle is GOLDRUSH-SMOKE-7."
SMOKE_ANSWERER = """Answer the question using only the supplied material. If the material does not
establish the answer, say so explicitly instead of guessing. Keep the answer
under 60 words.

QUESTION
<question text verbatim>

SUPPLIED MATERIAL
<pack file contents verbatim>
"""
SMOKE_GRADER = """Judge the answer below against the frozen reference definitions and the actual
supplied material only; do not use outside knowledge. Output only one JSON
object with exactly the keys score (integer 0, 1 or 2), refused (boolean),
fabricated_fact (boolean) and fact_results (an array containing exactly one
object per required fact, each with exactly fact_id, supported (boolean) and
evidence_ids (an array of strings)). Use no markdown fences and no explanations.

QUESTION
<QUESTION>

FROZEN REFERENCE DEFINITIONS
<FACTS_JSON>

ACTUAL SUPPLIED MATERIAL
<PACK>

ANSWER TO JUDGE
<ANSWER>
"""


def smoke_question():
    question = {"id": SMOKE_QID, "class": "single", "q": SMOKE_QUESTION, "source_group": SMOKE_GROUP,
                "expect": [{"evidence_id": "smoke-e0", "path": SMOKE_SOURCE, "kind": "doc", "grade": 3,
                            "evidence_type": "content_text",
                            "location": {"unit": "line", "start": 1, "end": 1},
                            "span": {"start_char": 0, "end_char": len(SMOKE_LINE), "text": SMOKE_LINE,
                                     "sha256": hashlib.sha256(SMOKE_LINE.encode()).hexdigest()}}],
                "required_facts": [{"fact_id": "smoke-f0", "description": SMOKE_FACT,
                                    "sufficient_evidence": [["smoke-e0"]]}]}
    validate_question(question)
    return question


def build_smoke_layout(out: Path, engine: dict):
    """One synthetic single/trap-free question family; never the 80 frozen questions."""
    out = Path(out).resolve()
    if out.exists():
        raise EvaluationError("smoke output must be a fresh directory")
    run = out / "run"
    for folder in ("packs", "raw", "jobs", "requests_a1", "answers_a1", "judge_prompts_a1", "grades_a1_j1"):
        (run / folder).mkdir(parents=True)
    question = smoke_question()
    (run / "questions.jsonl").write_text(canonical_json(question) + "\n", encoding="utf-8")
    (run / "corpus.json").write_text(json.dumps({"smoke": True, "sources": [{"path": SMOKE_SOURCE}],
                                                 "note": "synthetic smoke material, never the frozen corpus"},
                                                indent=2) + "\n", encoding="utf-8")
    source = run / SMOKE_SOURCE
    source.parent.mkdir(parents=True)
    source.write_text(SMOKE_LINE + "\n", encoding="utf-8")
    (run / "answerer.txt").write_text(SMOKE_ANSWERER, encoding="utf-8")
    (run / "grader.txt").write_text(SMOKE_GRADER, encoding="utf-8")
    (run / "tokenizer.txt").write_text("SYNTHETIC smoke tokenizer marker; estimate mode\n", encoding="utf-8")
    pack = "# Smoke material\n\n[1] " + SMOKE_SOURCE + "\n\n" + SMOKE_LINE + "\n"
    (run / "packs" / (SMOKE_QID + ".md")).write_text(pack, encoding="utf-8")
    binding = engine_binding(engine)
    models = {role: dict(binding) for role in ("answerer", "judge")}
    settings = {role: dict(engine["settings"]) for role in ("answerer", "judge")}
    tokenizer = {"model_id": engine["model_id"], "sha256": sha256_file(run / "tokenizer.txt"), "mode": "estimate"}
    preregistration = {"schema": 3, "protocol": PROTOCOL, "registered_at": now(),
                       "questions_sha256": sha256_file(run / "questions.jsonl"),
                       "corpus_sha256": sha256_file(run / "corpus.json"),
                       "source_groups_sha256": canonical_sha256({SMOKE_QID: SMOKE_GROUP}),
                       "models": models, "settings": settings, "tokenizer": tokenizer,
                       "score_scale": [0, 2], "practical_drop_threshold": 0.10, "trap_min_rate": 0.90,
                       "bootstrap_unit": "source_group", "bootstrap_seed": 20261023, "bootstrap_reps": 1000}
    write_json(run / "preregistration.json", preregistration)
    prepared = {"schema": 1, "protocol": PROTOCOL, "run_id": str(uuid.uuid4()), "arm": "smoke",
                "started_at": now(), "questions": artifact(run, run / "questions.jsonl"),
                "corpus": artifact(run, run / "corpus.json"),
                "preregistration": {"path": "preregistration.json", "sha256": sha256_file(run / "preregistration.json")},
                "source_groups_sha256": preregistration["source_groups_sha256"],
                "models": models, "settings": settings, "tokenizer": tokenizer,
                "runtime": {role: {"engine": engine} for role in ("answerer", "judge")},
                "answerer_prompt": artifact(run, run / "answerer.txt"),
                "grader_prompt": artifact(run, run / "grader.txt"),
                "variables": {"smoke": True, "engine_kind": engine["kind"]}}
    (run / "requests_a1" / (SMOKE_QID + ".txt")).write_text(
        answer_request(read_text(run / "answerer.txt"), question, pack), encoding="utf-8", newline="\n")
    write_json(run / "jobs" / f"{SMOKE_QID}-a1.json", descriptor(run, prepared, SMOKE_QID, "a1"))
    return run, prepared, question, SimpleNamespace(questions={SMOKE_QID: question})


def run_smoke(out: Path, engine: dict):
    """Real single answer+judge pair; gated by GLM_SMOKE=1, never the frozen questions."""
    if os.environ.get(SMOKE_FLAG) != "1":
        return {"status": "SMOKE_DISABLED", "error": f"set {SMOKE_FLAG}=1 to allow real remote calls",
                "network_calls": 0, "release_eligible": False}
    validate_engine(engine)
    key = require_api_key(engine)
    run, prepared, question, prep = build_smoke_layout(out, engine)
    job = read_json(run / "jobs" / f"{SMOKE_QID}-a1.json")
    if job != descriptor(run, prepared, SMOKE_QID, "a1"):
        raise EvaluationError("smoke answer job changed")
    answer = execute_job(prep, run, prepared, job, engine, key)
    checks = {"answer_sidecar_verified": answer["status"] == "JOB_EXECUTED"}
    judges = []
    if answer["status"] == "JOB_EXECUTED":
        text = read_text(run / "answers_a1" / (SMOKE_QID + ".md"))
        prompt = grader_request(read_text(run / "grader.txt"), question, text,
                                read_text(run / "packs" / (SMOKE_QID + ".md")))
        (run / "judge_prompts_a1" / (SMOKE_QID + ".txt")).write_text(prompt, encoding="utf-8", newline="\n")
        write_json(run / "jobs" / f"{SMOKE_QID}-a1-j1.json",
                   descriptor(run, prepared, SMOKE_QID, "a1", judge="j1"))
        judge_job = read_json(run / "jobs" / f"{SMOKE_QID}-a1-j1.json")
        result = execute_job(prep, run, prepared, judge_job, engine, key)
        judges.append(result)
        checks["judge_sidecar_verified"] = result["status"] == "JOB_EXECUTED"
        if result["status"] == "JOB_EXECUTED":
            answer_record = read_json(run / "answers_a1" / (SMOKE_QID + ".json"))
            grade, _ = checked_judge(run, prepared, SMOKE_QID, "a1", "j1", answer_record)
            checks["judge_record_reverified"] = set(grade) == GRADE_RECORD_FIELDS
    status = "SMOKE_EXECUTED" if answer["status"] == "JOB_EXECUTED" and judges and judges[0]["status"] == "JOB_EXECUTED" else "FAILED"
    report = {"schema": 1, "protocol": PROTOCOL, "status": status, "engine": engine,
              "smoke_gate": SMOKE_FLAG + "=1", "network_calls": 2,
              "answer": answer, "judges": judges, "checks": checks,
              "genuine_completions": sum(r["status"] == "JOB_EXECUTED" for r in [answer, *judges]),
              "release_eligible": False}
    write_json(Path(out) / "result.json", report)
    write_json(Path(out) / "validation.json", {"checks": checks, "layout": str(run),
                                               "sidecar_field_sets": {"answer": sorted(ANSWER_FIELDS)}})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    command = sub.add_parser("identity")
    command.add_argument("--engine", required=True, type=Path)
    for action in ("execute", "execute-question"):
        command = sub.add_parser(action)
        command.add_argument("--engine", required=True, type=Path)
        command.add_argument("--study", required=True, type=Path)
        command.add_argument("--arm", choices=ARMS, required=True)
        command.add_argument("--question", required=True)
        command.add_argument("--answer-round", choices=("a1", "a2"), required=True)
        if action == "execute":
            command.add_argument("--judge-round", choices=("j1", "j2"))
    command = sub.add_parser("finalize")
    command.add_argument("--study", required=True, type=Path)
    command.add_argument("--arm", choices=ARMS, required=True)
    command = sub.add_parser("smoke")
    command.add_argument("--engine", required=True, type=Path)
    command.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "identity":
            engine = load_engine(args.engine)
            require_api_key(engine)
            result = {"status": "ENGINE_REGISTERED", "identity": engine,
                      "binding": {role: engine_binding(engine) for role in ("answerer", "judge")},
                      "key_fingerprint_verified": True, "network_calls": 0, "release_eligible": False}
        elif args.action == "execute":
            engine = load_engine(args.engine)
            key = require_api_key(engine)
            prep, run, prepared = study_run(args.study, args.arm)
            j = args.judge_round
            job = read_json(run / "jobs" / (f"{args.question}-{args.answer_round}" + (f"-{j}" if j else "") + ".json"))
            if job != descriptor(run, prepared, args.question, args.answer_round, judge=j):
                raise EvaluationError("prepared job changed")
            result = execute_job(prep, run, prepared, job, engine, key)
        elif args.action == "execute-question":
            result = execute_question(args.study, args.arm, args.question, args.answer_round, load_engine(args.engine))
        elif args.action == "finalize":
            result = finalize_run(args.study, args.arm)
        else:
            result = run_smoke(args.out, load_engine(args.engine))
        code = (2 if result.get("status") in ("NOT_READY", "MISSING", "NOT_COMPARABLE", "SMOKE_DISABLED")
                else 1 if result.get("status") == "FAILED" else 0)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result, code = {"status": getattr(exc, "status", "NOT_COMPARABLE"), "error": str(exc),
                        "release_eligible": False}, 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
