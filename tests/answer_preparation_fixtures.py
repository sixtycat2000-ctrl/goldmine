"""Synthetic disk-only preparation fixtures; NO engines or quality evidence."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from acceptance_lib.evidence import artifact, write_json
from answer_eval.protocol import canonical_json, canonical_sha256, sha256_file
from answer_eval.job_material import descriptor
from answer_eval.preparation import ARMS

REGISTERED = "2026-10-21T08:00:00+00:00"
MATERIAL_START = "2026-10-21T08:01:00+00:00"
MATERIAL_END = "2026-10-21T08:02:00+00:00"


def production():
    files = {"goldmine/__init__.py": "c"*64, "pyproject.toml": "d"*64}
    return {"git_commit": "a"*40, "source_sha256": canonical_sha256(files), "files": files}


def ref(path):
    return {"path": str(path.resolve()), "sha256": sha256_file(path)}


def fixture_plan(root):
    root.mkdir()
    questions = []
    for i in range(80):
        cls = ("single", "multi", "cross", "trap")[i // 20]
        facts, evidence = [], []
        for f in range(0 if cls == "trap" else 1 if cls == "single" else 2):
            text, eid = "ORACLE_ONLY_SECRET_" + str(f), "e" + str(f)
            evidence.append({"evidence_id": eid, "path": f"docs/source-{f}.txt", "kind": "doc", "grade": 3,
                "evidence_type": "content_text", "location": {"unit": "line", "start": 1, "end": 1},
                "span": {"start_char": 0, "end_char": len(text), "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}})
            facts.append({"fact_id": "f" + str(f), "description": text, "sufficient_evidence": [[eid]]})
        questions.append({"id": f"fixture-{i:03}", "class": cls, "q": f"Unanswered synthetic question {i}?",
            "source_group": f"fixture-group-{i//5}", "expect": evidence, "required_facts": facts})
    (root / "questions.jsonl").write_text("".join(canonical_json(q) + "\n" for q in questions))
    write_json(root / "source-map.json", [{"id": q["id"]} for q in questions])
    write_json(root / "amendment.json", {"synthetic": "unit-only"})
    corpus = {"state": "DRAFT_SOURCE_REVIEW_AMENDED", "evidence_mode": "real", "note": "SYNTHETIC reader-branch fixture, never genuine evidence",
              "sources": [{"path": f"docs/source-{i}.txt"} for i in range(122)],
              "question_source_map": ref(root / "source-map.json"), "data_amendment": ref(root / "amendment.json")}
    write_json(root / "reviewed-corpus.json", corpus)
    write_json(root / "corpus.json", dict(corpus, state="FROZEN", frozen_at=REGISTERED, reviewers=["synthetic fixture"], data_freeze_scope="fixture only"))
    pins = {k: ref(root / name) for k, name in (("questions", "questions.jsonl"), ("corpus", "corpus.json"),
                                              ("source_map", "source-map.json"), ("amendment", "amendment.json"))}
    write_json(root / "review.json", {"state": "INDEPENDENT_SOURCE_SEMANTIC_CLOSURE_ACCEPTABLE_DRAFT002",
        "note": "Synthetic audit object for mechanical branch coverage only", "remaining_required_source_findings": [],
        "input_pins": {name: (ref(root / "reviewed-corpus.json") if key == "corpus" else pins[key])["sha256"] for key, name in (("questions", "questions.draft.jsonl"),
            ("corpus", "corpus.draft.json"), ("source_map", "source-map.json"), ("amendment", "data-amendment-registration.json"))}})
    pins["review"] = ref(root / "review.json")
    pins["reviewed_corpus"] = ref(root / "reviewed-corpus.json")
    write_json(root / "review-package.json", {"artifacts": [{"path": "review.json", "sha256": pins["review"]["sha256"]}]})
    pins["review_package"] = ref(root / "review-package.json")
    write_json(root / "adoption.json", {"state": "SOURCE_DATA_ADOPTED_NO_REAL_JOBS",
        "answer_corpus_source_and_annotation_content_unchanged": True, "answer_questions_sha256": pins["questions"]["sha256"],
        "answer_corpus_sha256": pins["corpus"]["sha256"], "answer_draft_corpus_sha256": pins["reviewed_corpus"]["sha256"],
        "answer_review_package_sha256": pins["review_package"]["sha256"]})
    pins["adoption"] = ref(root / "adoption.json")
    for name, body in (("engine", "#!/bin/sh\nexit 0\n"), ("model", "SYNTHETIC model marker"), ("tokenizer", "SYNTHETIC tokenizer marker")):
        (root / name).write_text(body)
    models = {role: {"model_id": "synthetic-no-engine", "model_sha256": ref(root / "model")["sha256"]} for role in ("answerer", "judge")}
    settings = {role: {"temperature": 0.1, "seed": 7} for role in models}
    token = {"model_id": "synthetic-tokenizer", "sha256": ref(root / "tokenizer")["sha256"], "mode": "estimate"}
    (root / "answerer.txt").write_text("Answer only from this material.\n<question text verbatim>\n<pack file contents verbatim>\n")
    (root / "grader.txt").write_text("Judge grounded support.\n<QUESTION>\n<FACTS_JSON>\n<PACK>\n<ANSWER>\n")
    prereg = {"schema": 3, "protocol": "answer-eval/v3", "registered_at": REGISTERED,
        "questions_sha256": pins["questions"]["sha256"], "corpus_sha256": pins["corpus"]["sha256"],
        "source_groups_sha256": canonical_sha256({q["id"]: q["source_group"] for q in questions}),
        "models": models, "settings": settings, "tokenizer": token, "score_scale": [0, 2],
        "answerer_prompt_sha256": ref(root / "answerer.txt")["sha256"], "grader_prompt_sha256": ref(root / "grader.txt")["sha256"],
        "practical_drop_threshold": 0.10, "trap_min_rate": 0.90, "bootstrap_unit": "source_group", "bootstrap_seed": 20261022, "bootstrap_reps": 1000}
    write_json(root / "preregistration.json", prereg)
    for name in ("index", "build", "embedding", "server", "index-prepare"):
        (root / name).write_text("SYNTHETIC non-executed " + name)
    variables = {key: {"fixture": key} for key in ("ranking", "chunker", "embedding", "lexical", "reranker", "pack")}
    role = {"production_root": str(root), "index": ref(root / "index"), "build_receipt": ref(root / "build"),
        "embedding_model": ref(root / "embedding"), "server": ref(root / "server"), "index_prepare": ref(root / "index-prepare"),
        "settings": {"scorer": "hybrid", "z_gate": 3.0, "limit": 10, "context": 1, "budget": 4000}, "variables": variables,
        "prepare_evaluator": {"root": str(root), "files": {}, "python": ref(root / "engine")}}
    p = {"schema": 1, "protocol": "answer-eval/v3", "registered_at": REGISTERED, "data": pins,
        "bundle_root": str(root / "source-assets"), "materials": {"reference": dict(role), "candidate": dict(role)},
        "common": {"models": models, "settings": settings, "tokenizer": token,
            "answerer_prompt": ref(root / "answerer.txt"), "grader_prompt": ref(root / "grader.txt"),
            "preregistration": ref(root / "preregistration.json"),
            "runtime": {**{role: {"engine": ref(root / "engine"), "model_file": ref(root / "model")} for role in models}, "tokenizer_file": ref(root / "tokenizer")}},
        "runs": {arm: str(root / arm) for arm in ARMS}}
    write_json(root / "plan.json", p)
    return root / "plan.json"


def command(root, *, identity, check, argv, stdout, started, finished, artifacts=None, run_id="fixture-capture"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "stdout.log").write_bytes(stdout)
    (root / "stderr.log").write_text("SYNTHETIC receipt, no process executed\n")
    receipt = {"schema": 1, "run_id": run_id, "check_id": check, "argv": argv, "cwd": str(root), "env": {},
        "started_at": started, "finished_at": finished, "exit_code": 0, "timed_out": False,
        "stdout": artifact(root, root / "stdout.log"), "stderr": artifact(root, root / "stderr.log"), "artifacts": artifacts or {}}
    write_json(root / "receipt.json", receipt)
    write_json(root / "manifest.json", {"schema": 1, "kind": "goldmine.command-run", "run_id": run_id,
        "check_id": check, "suite_id": "answer-eval/v3", "identity": identity, "receipt": artifact(root, root / "receipt.json")})
    return root / "manifest.json"


def materials(prep, role, directory, evaluator):
    out = directory / "output"
    out.mkdir(parents=True)
    arms = ARMS[:2] if role == "reference" else ARMS[2:]
    rows, files = {}, {}
    for qid, q in prep.questions.items():
        representations = {}
        for arm in arms:
            p = out / arm / (qid + ".md");p.parent.mkdir(exist_ok=True)
            p.write_text("[1] Stored fixture source material <FACTS_JSON>.\n")
            representations[arm] = artifact(out, p);files[p.relative_to(out).as_posix()] = sha256_file(p)
        rows[qid] = {"query_sha256": canonical_sha256(q["q"]), "hits": [{"segment_id": 42}],
            "hits_sha256": canonical_sha256([{"segment_id": 42}]), "pack_metadata": {}, "representations": representations}
    v = prep.plan["materials"][role]
    report = {"schema": 1, "protocol": "answer-eval/v3", "status": "MATERIAL_PREPARED", "role": role,
        "plan_sha256": sha256_file(prep.path), "production": production(),
        "module_paths": {"goldmine": str(Path(v["production_root"]) / "goldmine/__init__.py")},
        "settings": v["settings"], "variables": v["variables"], "index": {"sha256": v["index"]["sha256"]},
        "embedding_model": v["embedding_model"], "server": v["server"], "build_receipt_sha256": v["build_receipt"]["sha256"],
        "evaluator_files": {}, "files": files, "questions": rows}
    write_json(out / "material.json", report)
    argv = ["synthetic-python", str(evaluator / "scripts/prepare_answer.py"), "material-worker", "--plan", str(prep.path), "--role", role, "--out", str(out)]
    return command(directory, identity={"plan_sha256": sha256_file(prep.path), "evaluator_files": {}},
        check="answer.material."+role, argv=argv, stdout=(json.dumps(report)+"\n").encode(),
        started=MATERIAL_START, finished=MATERIAL_END, artifacts={"material": artifact(directory, out / "material.json")})


def completion(run, prepared, q, a, j=None):
    """Create synthetic consistent disk records; deliberately never invoke engine."""
    job = descriptor(run, prepared, q["id"], a, judge=j)
    folder = f"grades_{a}_{j}" if j else f"answers_{a}"
    raw = run / "raw" / folder / q["id"]
    raw.mkdir(parents=True)
    request = run / job["request"]["path"]
    (raw / "request.txt").write_bytes(request.read_bytes())
    write_json(raw / "usage.json", {"input_tokens": 12, "output_tokens": 4})
    stamp = datetime.now(timezone.utc).isoformat()
    record = {"schema": 3, "run_id": prepared["run_id"], "question_id": q["id"], "answer_round": a,
        "execution_id": job["job_id"], "completed_at": stamp, "pack_sha256": job["pack"]["sha256"]}
    if j:
        payload = {"score": 2, "refused": q["class"] == "trap", "fabricated_fact": False,
            "fact_results": [{"fact_id": f["fact_id"], "supported": True, "evidence_ids": f["sufficient_evidence"][0]} for f in q["required_facts"]]}
        record.update(payload, judge_round=j, answer_sha256=sha256_file(run / f"answers_{a}" / (q["id"]+".md")),
            grader_prompt_sha256=job["request"]["sha256"], judge_model=job["model"], judge_settings=job["settings"])
        stdout = (canonical_json(payload)+"\n").encode()
    else:
        text = "This is a synthetic response, not a genuine answer.\n"
        (run / folder / (q["id"]+".md")).write_text(text)
        record.update(answer_sha256=sha256_file(run / folder / (q["id"]+".md")),
            answerer_prompt_sha256=prepared["answerer_prompt"]["sha256"], request_sha256=job["request"]["sha256"],
            answerer_model=job["model"], answerer_settings=job["settings"])
        stdout = text.encode()
    write_json(run / folder / (q["id"]+".json"), record)
    # command() owns root creation. The raw inputs already exist for artifact refs.
    receipt = {"schema": 1, "run_id": job["job_id"], "check_id": "answer.job", "argv": [job["runtime"]["engine"]["path"]],
        "cwd": str(run), "env": {}, "started_at": stamp, "finished_at": stamp, "exit_code": 0, "timed_out": False,
        "artifacts": {"request": artifact(raw, raw / "request.txt"), "usage": artifact(raw, raw / "usage.json")}}
    (raw / "stdout.log").write_bytes(stdout);(raw / "stderr.log").write_text("SYNTHETIC NO PROCESS\n")
    receipt.update(stdout=artifact(raw, raw / "stdout.log"), stderr=artifact(raw, raw / "stderr.log"))
    write_json(raw / "receipt.json", receipt)
    write_json(raw / "manifest.json", {"schema": 1, "kind": "goldmine.command-run", "run_id": job["job_id"], "check_id": "answer.job",
        "suite_id": "answer-eval/v3", "identity": {"job": job}, "receipt": artifact(raw, raw / "receipt.json")})
    return record
