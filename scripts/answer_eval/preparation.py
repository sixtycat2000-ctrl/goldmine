"""Closed, disk-only prerequisites for preparing the existing v3 answer runs.

This module does not authenticate a publisher, execute inference or score runs.
The source audit and the future engine-specific execution bridge are external.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .protocol import (CLASSES, PROTOCOL, EvaluationError, _load_preregistration,
                       answer_request, canonical_sha256, fields, grader_request,
                       model, parse_json, read_json, read_text, sha, sha256_file,
                       timestamp, tokenizer, validate_question)

ARMS = ("reference_LIST", "reference_PACK", "candidate_PACK")
EXPECTED = {"answer_completions": 480, "judge_completions": 960}


def file_ref(ref, label, *, verify=True):
    fields(ref, {"path", "sha256"}, label)
    sha(ref["sha256"], label + ".sha256")
    if not isinstance(ref["path"], str) or not Path(ref["path"]).is_absolute():
        raise EvaluationError(label + ": explicit absolute input required")
    path = Path(ref["path"]).resolve()
    if verify and sha256_file(path) != ref["sha256"]:
        raise EvaluationError(label + ": input bytes changed")
    return path


@dataclass(frozen=True)
class Preparation:
    path: Path
    plan: dict
    questions: dict
    corpus: dict
    common: dict | None
    missing: tuple[str, ...]

    @property
    def status(self):
        return "NOT_READY" if self.missing else "PREPARATION_INPUT_DESCRIPTORS_COMPLETE"


def _questions(path):
    questions = {}
    for line in read_text(path).splitlines():
        if not line.strip():
            continue
        q = parse_json(line, "question")
        validate_question(q)
        if q["id"] in questions:
            raise EvaluationError("duplicate question ID")
        questions[q["id"]] = q
    if Counter(q["class"] for q in questions.values()) != Counter({c: 20 for c in CLASSES}):
        raise EvaluationError("preparation requires exactly80 new questions,20 per class")
    return questions


def protected_roots(plan):
    roots = [Path(plan["bundle_root"]).resolve()]
    roots += [(Path(v["build_receipt"]["path"]).resolve().parent / "corpus").resolve()
              for v in plan["materials"].values() if isinstance(v, dict) and "build_receipt" in v]
    return roots


def owned_output(plan, output):
    """Resolve aliases and reject overlaps before a capture or worker can write."""
    output = Path(output).resolve()
    if any(output.is_relative_to(source) or source.is_relative_to(output)
           for source in protected_roots(plan)):
        raise EvaluationError("material output overlaps a protected source/indexed corpus tree")
    return output


def _common(plan, questions, *, verify_runtime):
    c = plan["common"]
    keys = {"models", "settings", "tokenizer", "answerer_prompt", "grader_prompt",
            "preregistration", "runtime"}
    fields(c, keys, "common")
    missing = ["common." + key for key in sorted(keys) if c[key] is None]
    if missing:
        return None, missing
    fields(c["models"], {"answerer", "judge"}, "models")
    fields(c["settings"], {"answerer", "judge"}, "settings")
    for role in ("answerer", "judge"):
        model(c["models"][role], role)
        if not isinstance(c["settings"][role], dict) or not c["settings"][role]:
            raise EvaluationError("explicit nonempty exact engine settings required")
    tokenizer(c["tokenizer"])
    a_path = file_ref(c["answerer_prompt"], "answerer_prompt")
    g_path = file_ref(c["grader_prompt"], "grader_prompt")
    # Reuse the reviewed literal renderers, not legacy --emit/--grade substitutions.
    probe = next(iter(questions.values()))
    answer_request(read_text(a_path), probe, "")
    grader_request(read_text(g_path), probe, "", "")
    reg_path = file_ref(c["preregistration"], "preregistration")
    m = {key: c[key] for key in ("models", "settings", "tokenizer", "answerer_prompt", "grader_prompt")}
    m.update({key: plan["data"][key] for key in ("questions", "corpus")})
    m["source_groups_sha256"] = canonical_sha256({i: q["source_group"] for i, q in questions.items()})
    m["preregistration"] = {"path": reg_path.name, "sha256": c["preregistration"]["sha256"]}
    # Exact existing v3 allowed thresholds/group bootstrap/scale; no new gate.
    _load_preregistration(reg_path.parent, m, timestamp(plan["registered_at"], "plan.registered_at"))
    runtime = c["runtime"]
    fields(runtime, {"answerer", "judge", "tokenizer_file"}, "runtime")
    for role in ("answerer", "judge"):
        fields(runtime[role], {"engine", "model_file"}, "runtime." + role)
        for name, ref in runtime[role].items():
            file_ref(ref, "runtime." + role + "." + name, verify=verify_runtime)
        if runtime[role]["model_file"]["sha256"] != c["models"][role]["model_sha256"]:
            raise EvaluationError("local model payload SHA must equal registered model SHA")
    file_ref(runtime["tokenizer_file"], "tokenizer_file", verify=verify_runtime)
    if runtime["tokenizer_file"]["sha256"] != c["tokenizer"]["sha256"]:
        raise EvaluationError("actual tokenizer payload differs from preregistration")
    return c, []


def load_preparation(path, *, require_ready=False, verify_runtime=False):
    path = Path(path).resolve()
    p = read_json(path)
    fields(p, {"schema", "protocol", "registered_at", "data", "bundle_root", "common", "materials", "runs"}, "preparation plan")
    if type(p["schema"]) is not int or p["schema"] != 1 or p["protocol"] != PROTOCOL:
        raise EvaluationError("preparation wraps existing answer-eval/v3 only")
    if timestamp(p["registered_at"], "plan.registered_at") > datetime.now(timezone.utc):
        raise EvaluationError("plan registration cannot be in the future")
    fields(p["data"], {"questions", "corpus", "source_map", "amendment", "review", "reviewed_corpus", "review_package", "adoption"}, "data")
    paths = {key: file_ref(ref, "data." + key) for key, ref in p["data"].items()}
    qs, corpus, review = _questions(paths["questions"]), read_json(paths["corpus"]), read_json(paths["review"])
    if (corpus.get("evidence_mode") != "real" or len(corpus.get("sources", [])) != 122
            or review.get("state") != "INDEPENDENT_SOURCE_SEMANTIC_CLOSURE_ACCEPTABLE_DRAFT002"
            or review.get("remaining_required_source_findings") != []):
        raise EvaluationError("requires the closed draft002 source audit and full122 sources")
    draft, adopted, package = (read_json(paths[key]) for key in ("reviewed_corpus", "adoption", "review_package"))
    allowed = {"state", "frozen_at", "reviewers", "data_freeze_scope"}
    if (corpus.get("state") != "FROZEN" or
            {k: v for k, v in corpus.items() if k not in allowed} != {k: v for k, v in draft.items() if k not in allowed}
            or adopted.get("state") != "SOURCE_DATA_ADOPTED_NO_REAL_JOBS"
            or adopted.get("answer_corpus_source_and_annotation_content_unchanged") is not True
            or any(adopted.get(key) != p["data"][name]["sha256"] for key, name in (
                ("answer_questions_sha256", "questions"), ("answer_corpus_sha256", "corpus"),
                ("answer_draft_corpus_sha256", "reviewed_corpus"), ("answer_review_package_sha256", "review_package")))
            or not any(row == {"path": "review.json", "sha256": p["data"]["review"]["sha256"]} for row in package.get("artifacts", []))):
        raise EvaluationError("data adoption must preserve the exact independently reviewed source/annotation content")
    for key, name in (("questions", "questions.draft.jsonl"), ("corpus", "corpus.draft.json"),
                      ("source_map", "source-map.json"), ("amendment", "data-amendment-registration.json")):
        pin_key = "reviewed_corpus" if key == "corpus" else key
        if review.get("input_pins", {}).get(name) != p["data"][pin_key]["sha256"]:
            raise EvaluationError("closed source review does not bind " + key)
    if corpus["question_source_map"]["sha256"] != p["data"]["source_map"]["sha256"] or corpus["data_amendment"]["sha256"] != p["data"]["amendment"]["sha256"]:
        raise EvaluationError("corpus/source amendment lineage differs")
    fields(p["materials"], {"reference", "candidate"}, "materials")
    material_roles = [p["materials"][key] for key in ("reference", "candidate")]
    if all(isinstance(v, dict) and "build_receipt" in v for v in material_roles):
        build_paths = [file_ref(v["build_receipt"], "shared build receipt", verify=False) for v in material_roles]
        if build_paths[0] != build_paths[1]:
            raise EvaluationError("three arms require the same physical built corpus and metadata receipt")
    fields(p["runs"], set(ARMS), "runs")
    roots = []
    for arm, name in p["runs"].items():
        if not isinstance(name, str) or not Path(name).is_absolute():
            raise EvaluationError("run destinations must be explicit absolute paths")
        roots.append(Path(name).resolve())
    if len(set(roots)) != 3 or any(a in b.parents for a in roots for b in roots if a != b):
        raise EvaluationError("three fresh disjoint owned run directories required")
    if any(root.is_relative_to(protected) for root in roots for protected in protected_roots(p)):
        raise EvaluationError("run outputs must stay outside source/assets/indexed corpus trees")
    common, missing = _common(p, qs, verify_runtime=verify_runtime)
    missing += ["materials." + role for role, value in p["materials"].items() if value is None]
    result = Preparation(path, p, qs, corpus, common, tuple(missing))
    if require_ready and result.missing:
        raise EvaluationError("NOT_READY: " + ", ".join(result.missing), status="MISSING")
    return result


def readiness(path):
    prep = load_preparation(path)
    return {"schema": 1, "protocol": PROTOCOL, "status": prep.status,
            "missing": list(prep.missing), "questions": 80, "classes": {c: 20 for c in CLASSES},
            "arms": list(ARMS), "planned_completions": EXPECTED,
            "actual_answer_completions": 0, "actual_judge_completions": 0,
            "execution_bridge": "MISSING: no engine-specific runner configured; descriptors only",
            "runtime_assets_checked": False,
            "material_source_index_verification": "DEFERRED to the fixed material command",
            "release_eligible": False, "model_jobs_performed": False}
