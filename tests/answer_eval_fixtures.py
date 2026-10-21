"""Synthetic disk records for protocol unit tests; never formal eval evidence."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from answer_eval.protocol import (PROTOCOL, answer_request, canonical_json,
                                  canonical_sha256, grader_request, sha256_file)


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def edit(path, change):
    value = json.loads(path.read_text())
    change(value)
    dump(path, value)


def make_run(root: Path, *, run_id="reference", mode="fixture", pack_suffix="", score_for=None,
             different_wording=False, threshold=0.10) -> Path:
    """mode=real only exercises reader branches in tests; no model is executed."""
    root.mkdir()
    for folder in ("packs", "answers_a1", "answers_a2", "requests_a1", "requests_a2",
                   "judge_prompts_a1", "judge_prompts_a2", "grades_a1_j1", "grades_a1_j2",
                   "grades_a2_j1", "grades_a2_j2"):
        (root / folder).mkdir()
    questions = []
    for i in range(80):
        cls = ("single", "multi", "cross", "trap")[i // 20]
        group = f"source-{i//5:02d}"
        evidence, facts = [], []
        if cls != "trap":
            for f in range(1 if cls == "single" else 2):
                text = f"Frozen fact {group} {f}."
                eid = f"e-{f}"
                evidence.append({"evidence_id": eid, "path": f"docs/{group}-{f}.txt", "kind": "doc",
                                 "grade": 3, "evidence_type": "content_text",
                                 "location": {"unit": "line", "start": 1, "end": 1},
                                 "span": {"start_char": 0, "end_char": len(text), "text": text,
                                          "sha256": hashlib.sha256(text.encode()).hexdigest()}})
                facts.append({"fact_id": f"fact-{f}", "description": text, "sufficient_evidence": [[eid]]})
        questions.append({"id": f"q-{i:03d}", "class": cls, "q": f"Question {i}?",
                          "source_group": group, "expect": evidence, "required_facts": facts})
    (root / "questions.jsonl").write_text("".join(canonical_json(q) + "\n" for q in questions), encoding="utf-8")
    dump(root / "corpus.json", {"schema": 1, "test_only": True, "source_text": "synthetic unit fixture"})
    a_template = "QUESTION: <question text verbatim>\nMATERIAL:\n<pack file contents verbatim>\n"
    g_template = "QUESTION: <QUESTION>\nFACTS: <FACTS_JSON>\nMATERIAL: <PACK>\nANSWER: <ANSWER>\n"
    (root / "answerer.txt").write_text(a_template)
    (root / "grader.txt").write_text(g_template)
    ref = lambda name: {"path": name, "sha256": sha256_file(root / name)}
    models = {role: {"model_id": "test-only-model", "model_sha256": "a" * 64} for role in ("answerer", "judge")}
    settings = {role: {"temperature": 0.1} for role in models}
    tokenizer = {"model_id": "test-tokenizer", "sha256": "b" * 64, "mode": "tokenize"}
    group_sha = canonical_sha256({q["id"]: q["source_group"] for q in questions})
    prereg = {"schema": 3, "protocol": PROTOCOL, "registered_at": "2026-10-21T08:00:00+08:00",
              "questions_sha256": ref("questions.jsonl")["sha256"], "corpus_sha256": ref("corpus.json")["sha256"],
              "source_groups_sha256": group_sha, "models": models, "settings": settings,
              "answerer_prompt_sha256": ref("answerer.txt")["sha256"], "grader_prompt_sha256": ref("grader.txt")["sha256"],
              "tokenizer": tokenizer, "score_scale": [0, 2], "practical_drop_threshold": threshold,
              "trap_min_rate": 0.90, "bootstrap_unit": "source_group", "bootstrap_seed": 20261021, "bootstrap_reps": 1000}
    dump(root / "preregistration.json", prereg)
    manifest = {"schema": 3, "protocol": PROTOCOL, "run_id": run_id, "evidence_mode": mode,
                "started_at": "2026-10-21T09:00:00+08:00", "completed_at": "2026-10-21T11:00:00+08:00",
                "questions": ref("questions.jsonl"), "corpus": ref("corpus.json"), "preregistration": ref("preregistration.json"),
                "source_groups_sha256": group_sha, "models": models, "settings": settings,
                "answerer_prompt": ref("answerer.txt"), "grader_prompt": ref("grader.txt"), "tokenizer": tokenizer,
                "variables": {"ranking": "hybrid.v1", "chunker": "chunk.v2", "embedding": "embedding.v1",
                              "lexical": "lex.v1", "reranker": "off", "pack": {"budget": 4000}, "code": "c" * 40}}
    dump(root / "run.json", manifest)
    for q in questions:
        qid = q["id"]
        pack = f"[1] {qid}: material{pack_suffix}\n"
        (root / "packs" / f"{qid}.md").write_text(pack)
        pack_sha = sha256_file(root / "packs" / f"{qid}.md")
        for a in ("a1", "a2"):
            answer = ("The material does not contain enough information to answer this question."
                      if q["class"] == "trap" else "The answer cites [1].")
            if different_wording and a == "a2":
                answer += " Different phrasing."
            answer_path = root / f"answers_{a}" / f"{qid}.md"
            answer_path.write_text(answer)
            answer_sha = sha256_file(answer_path)
            request = root / f"requests_{a}" / f"{qid}.txt"
            request.write_text(answer_request(a_template, q, pack))
            dump(root / f"answers_{a}" / f"{qid}.json", {
                "schema": 3, "run_id": run_id, "question_id": qid, "answer_round": a,
                "execution_id": f"{run_id}/{qid}/{a}", "completed_at": "2026-10-21T09:30:00+08:00",
                "answer_sha256": answer_sha, "pack_sha256": pack_sha,
                "answerer_prompt_sha256": manifest["answerer_prompt"]["sha256"], "request_sha256": sha256_file(request),
                "answerer_model": models["answerer"], "answerer_settings": settings["answerer"],
            })
            judge_path = root / f"judge_prompts_{a}" / f"{qid}.txt"
            judge_path.write_text(grader_request(g_template, q, answer, pack))
            for j in ("j1", "j2"):
                dump(root / f"grades_{a}_{j}" / f"{qid}.json", {
                    "schema": 3, "run_id": run_id, "question_id": qid, "answer_round": a, "judge_round": j,
                    "execution_id": f"{run_id}/{qid}/{a}/{j}", "completed_at": "2026-10-21T10:00:00+08:00",
                    "score": score_for(q, a, j) if score_for else 2,
                    "refused": q["class"] == "trap", "fabricated_fact": False,
                    "fact_results": [{"fact_id": f["fact_id"], "supported": True,
                                      "evidence_ids": f["sufficient_evidence"][0]} for f in q["required_facts"]],
                    "answer_sha256": answer_sha, "pack_sha256": pack_sha,
                    "grader_prompt_sha256": sha256_file(judge_path), "judge_model": models["judge"], "judge_settings": settings["judge"],
                })
    return root
