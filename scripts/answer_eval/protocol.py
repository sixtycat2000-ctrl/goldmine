"""Strict, read-only provenance checks for the answer-eval/v3 disk protocol."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

PROTOCOL = "answer-eval/v3"
THRESHOLDS = (0.10, 0.15, 0.20, 0.25, 0.30)
ROUNDS = (("a1", "j1"), ("a1", "j2"), ("a2", "j1"), ("a2", "j2"))
CLASSES = ("single", "multi", "cross", "trap")


class EvaluationError(ValueError):
    """Invalid/missing provenance is exit 2, not an observed quality failure."""

    def __init__(self, message: str, *, status: str = "NOT_COMPARABLE"):
        super().__init__(message)
        self.status = status


def sha256_file(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise EvaluationError(f"missing/unreadable input: {path}: {exc}", status="MISSING") from exc


def canonical_sha256(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise EvaluationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json(text: str, label: str):
    def invalid_constant(token):
        raise EvaluationError(f"{label}: nonfinite JSON number {token}")
    try:
        result = json.loads(text, object_pairs_hook=_pairs, parse_constant=invalid_constant)
        def finite(value):
            if isinstance(value, dict):
                for item in value.values():
                    finite(item)
            elif isinstance(value, list):
                for item in value:
                    finite(item)
            elif isinstance(value, float) and not math.isfinite(value):
                raise EvaluationError(f"{label}: nonfinite JSON number")
        finite(result)
        return result
    except (ValueError, TypeError) as exc:
        raise EvaluationError(f"{label}: invalid JSON: {exc}") from exc


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise EvaluationError(f"missing/unreadable UTF-8 input: {path}: {exc}", status="MISSING") from exc


def read_json(path: Path):
    return parse_json(read_text(path), str(path))


def fields(obj, required: set[str], label: str, optional=frozenset()):
    if not isinstance(obj, dict):
        raise EvaluationError(f"{label}: require an object")
    missing, unknown = required - obj.keys(), obj.keys() - required - optional
    if missing or unknown:
        raise EvaluationError(f"{label}: missing={sorted(missing)}, unknown={sorted(unknown)}")


def nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise EvaluationError(f"{label}: require a nonempty string")


def sha(value, label):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise EvaluationError(f"{label}: require lowercase SHA256")


def timestamp(value, label):
    nonempty(value, label)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationError(f"{label}: require ISO8601 with timezone") from exc
    if stamp.tzinfo is None:
        raise EvaluationError(f"{label}: timezone is required")
    return stamp


def inside(root: Path, relative: str) -> Path:
    nonempty(relative, "artifact path")
    part = PurePosixPath(relative)
    if part.is_absolute() or ".." in part.parts or "\\" in relative:
        raise EvaluationError(f"artifact must be a run-relative path: {relative!r}")
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise EvaluationError(f"artifact escapes run directory: {relative!r}")
    return target


def reference(root: Path, obj, label) -> Path:
    fields(obj, {"path", "sha256"}, label)
    sha(obj["sha256"], label + ".sha256")
    path = inside(root, obj["path"])
    if sha256_file(path) != obj["sha256"]:
        raise EvaluationError(f"{label}: SHA256 does not match {path}")
    return path


def model(value, label):
    fields(value, {"model_id", "model_sha256"}, label)
    nonempty(value["model_id"], label + ".model_id")
    sha(value["model_sha256"], label + ".model_sha256")


def tokenizer(value):
    fields(value, {"model_id", "sha256", "mode"}, "tokenizer")
    nonempty(value["model_id"], "tokenizer.model_id")
    sha(value["sha256"], "tokenizer.sha256")
    if value["mode"] not in ("tokenize", "estimate"):
        raise EvaluationError("tokenizer.mode must be tokenize or estimate")


def validate_location(value, kind, evidence_type):
    if value is None and evidence_type == "name_metadata":
        return
    fields(value, {"unit", "start", "end"}, "evidence.location")
    unit, start, end = value["unit"], value["start"], value["end"]
    allowed = {"pdf": "page", "audio": "second", "video": "second"}
    if unit != allowed.get(kind, "line"):
        raise EvaluationError(f"location unit {unit!r} incompatible with kind {kind!r}")
    numeric = lambda n: type(n) in (int, float) and math.isfinite(n)
    if not numeric(start) or not numeric(end) or start > end:
        raise EvaluationError("invalid/nonfinite location range")
    if unit in ("line", "page"):
        if type(start) is not int or type(end) is not int or start < 1:
            raise EvaluationError("page/line locations must be integers starting at 1")
    elif start < 0:
        raise EvaluationError("time locations cannot be negative")


def validate_question(q):
    fields(q, {"id", "class", "q", "source_group", "expect", "required_facts"}, "question")
    if not isinstance(q["id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", q["id"]):
        raise EvaluationError("question.id must be a safe unique identifier")
    if q["class"] not in CLASSES:
        raise EvaluationError(f"{q['id']}: invalid answer class")
    nonempty(q["q"], "question.q")
    nonempty(q["source_group"], "question.source_group")
    if not isinstance(q["expect"], list) or not isinstance(q["required_facts"], list):
        raise EvaluationError("expect and required_facts must be lists")
    evidence_ids = set()
    for e in q["expect"]:
        fields(e, {"evidence_id", "path", "kind", "grade", "evidence_type", "location", "span"},
               "expect", {"storage_field"})
        nonempty(e["evidence_id"], "evidence_id")
        if e["evidence_id"] in evidence_ids:
            raise EvaluationError("duplicate evidence_id")
        evidence_ids.add(e["evidence_id"])
        nonempty(e["path"], "evidence.path")
        p = PurePosixPath(e["path"])
        if p.is_absolute() or ".." in p.parts or "\\" in e["path"]:
            raise EvaluationError("evidence.path must be corpus-relative")
        if e["kind"] not in ("pdf", "audio", "video", "doc", "code", "image", "screenshot", "web"):
            raise EvaluationError("unknown evidence kind")
        if e["evidence_type"] not in ("content_text", "ocr_text", "asr_text", "name_metadata"):
            raise EvaluationError("unknown evidence_type")
        if type(e["grade"]) is not int or not 0 <= e["grade"] <= 3:
            raise EvaluationError("grade must be an integer in 0..3")
        if e["evidence_type"] == "name_metadata":
            nonempty(e.get("storage_field"), "metadata storage_field")
        elif "storage_field" in e:
            raise EvaluationError("storage_field is only valid for metadata evidence")
        validate_location(e["location"], e["kind"], e["evidence_type"])
        s = e["span"]
        fields(s, {"start_char", "end_char", "text", "sha256"}, "span")
        nonempty(s["text"], "span.text")
        if unicodedata.normalize("NFC", s["text"]) != s["text"] or "\r" in s["text"]:
            raise EvaluationError("span text must use frozen NFC/LF source normalization")
        if (type(s["start_char"]) is not int or type(s["end_char"]) is not int
                or s["start_char"] < 0 or s["end_char"] - s["start_char"] != len(s["text"])):
            raise EvaluationError("span must use exact, zero-based half-open Unicode character offsets")
        sha(s["sha256"], "span.sha256")
        if hashlib.sha256(s["text"].encode("utf-8")).hexdigest() != s["sha256"]:
            raise EvaluationError("span text SHA mismatch")
    facts = set()
    for f in q["required_facts"]:
        fields(f, {"fact_id", "description", "sufficient_evidence"}, "required_fact")
        nonempty(f["fact_id"], "fact_id")
        nonempty(f["description"], "fact.description")
        if f["fact_id"] in facts:
            raise EvaluationError("duplicate fact_id")
        facts.add(f["fact_id"])
        alternatives = f["sufficient_evidence"]
        if not isinstance(alternatives, list) or not alternatives:
            raise EvaluationError("facts require nonempty sufficient_evidence alternatives")
        for alternative in alternatives:
            if (not isinstance(alternative, list) or not alternative
                    or any(not isinstance(x, str) for x in alternative)
                    or len(set(alternative)) != len(alternative)
                    or not set(alternative) <= evidence_ids):
                raise EvaluationError("fact alternative must reference distinct, known evidence IDs")
    if q["class"] == "trap":
        if q["expect"] or q["required_facts"]:
            raise EvaluationError("trap expect and required_facts must be empty")
    elif not facts or not evidence_ids:
        raise EvaluationError("answerable questions require facts and evidence")
    elif q["class"] == "single" and len(facts) != 1:
        raise EvaluationError("single questions require exactly one fact")
    elif q["class"] in ("multi", "cross") and len(facts) < 2:
        raise EvaluationError("multi/cross questions require at least two facts")
    if q["class"] == "cross" and len({e["path"] for e in q["expect"]}) < 2:
        raise EvaluationError("cross questions require evidence from at least two source files")


def answer_request(template: str, question: dict, pack: str) -> str:
    slots = ("<question text verbatim>", "<pack file contents verbatim>")
    if any(template.count(slot) != 1 for slot in slots):
        raise EvaluationError("answerer template must contain each documented slot exactly once")
    replacements = dict(zip(slots, (question["q"], pack)))
    return re.sub("|".join(re.escape(s) for s in slots), lambda m: replacements[m[0]], template)


def grader_request(template: str, question: dict, answer: str, pack: str) -> str:
    if not isinstance(pack, str):
        raise EvaluationError("grader material pack must be text")
    slots = ("<QUESTION>", "<FACTS_JSON>", "<PACK>", "<ANSWER>")
    if any(template.count(slot) != 1 for slot in slots):
        raise EvaluationError("grader template must contain QUESTION, FACTS_JSON, PACK, ANSWER once")
    facts = {k: question[k] for k in ("class", "expect", "required_facts")}
    replacements = dict(zip(slots, (question["q"], canonical_json(facts), pack, answer)))
    return re.sub("|".join(re.escape(s) for s in slots), lambda m: replacements[m[0]], template)


@dataclass(frozen=True)
class AnswerRun:
    root: Path
    manifest: dict
    manifest_sha256: str
    preregistration: dict
    questions: dict[str, dict]
    scores: dict[str, list[int]]
    answers: dict[str, list[dict]]
    grades: dict[str, list[dict]]
    packs: dict[str, str]
    execution_ids: frozenset[str]


def _directory_ids(root: Path, name: str, suffix: str, expected: set[str]):
    path = inside(root, name)
    if not path.is_dir():
        raise EvaluationError(f"missing directory: {path}", status="MISSING")
    actual = {p.name.removesuffix(suffix) for p in path.iterdir() if p.name.endswith(suffix)}
    if actual != expected:
        raise EvaluationError(f"{name}: incomplete or extra question IDs; missing={sorted(expected-actual)}, extra={sorted(actual-expected)}", status="MISSING" if expected-actual else "NOT_COMPARABLE")


def _load_preregistration(root, m, started):
    p = read_json(reference(root, m["preregistration"], "preregistration"))
    fields(p, {"schema", "protocol", "registered_at", "questions_sha256", "corpus_sha256",
               "source_groups_sha256", "models", "settings", "answerer_prompt_sha256",
               "grader_prompt_sha256", "tokenizer", "score_scale", "practical_drop_threshold",
               "trap_min_rate", "bootstrap_unit", "bootstrap_seed", "bootstrap_reps"}, "preregistration")
    if type(p["schema"]) is not int or p["schema"] != 3 or p["protocol"] != PROTOCOL:
        raise EvaluationError("preregistration requires answer-eval/v3 schema3")
    if timestamp(p["registered_at"], "registered_at") > started:
        raise EvaluationError("preregistration was registered after the run started")
    for key, value in (("questions_sha256", m["questions"]["sha256"]),
                       ("corpus_sha256", m["corpus"]["sha256"]),
                       ("source_groups_sha256", m["source_groups_sha256"]),
                       ("answerer_prompt_sha256", m["answerer_prompt"]["sha256"]),
                       ("grader_prompt_sha256", m["grader_prompt"]["sha256"]),
                       ("models", m["models"]), ("settings", m["settings"]), ("tokenizer", m["tokenizer"])):
        if p[key] != value:
            raise EvaluationError(f"preregistration.{key} does not match this run")
    if p["score_scale"] != [0, 2] or any(type(x) is not int for x in p["score_scale"]):
        raise EvaluationError("score_scale must be [0,2], unnormalized")
    if type(p["practical_drop_threshold"]) not in (int, float) or p["practical_drop_threshold"] not in THRESHOLDS:
        raise EvaluationError("practical threshold must be preregistered in 0.10/0.15/0.20/0.25/0.30")
    if type(p["trap_min_rate"]) not in (int, float) or p["trap_min_rate"] != 0.90:
        raise EvaluationError("trap_min_rate is fixed at 0.90")
    if (p["bootstrap_unit"] != "source_group" or type(p["bootstrap_seed"]) is not int
            or type(p["bootstrap_reps"]) is not int or not 1000 <= p["bootstrap_reps"] <= 100000):
        raise EvaluationError("require source_group bootstrap, integer seed, 1000..100000 reps")
    return p


def load_run(path: str | Path, *, allow_fixture: bool = False) -> AnswerRun:
    root = Path(path).resolve()
    m = read_json(inside(root, "run.json"))
    fields(m, {"schema", "protocol", "run_id", "evidence_mode", "started_at", "completed_at",
               "questions", "corpus", "preregistration", "source_groups_sha256", "models", "settings",
               "answerer_prompt", "grader_prompt", "tokenizer", "variables"}, "run")
    if type(m["schema"]) is not int or m["schema"] != 3 or m["protocol"] != PROTOCOL:
        raise EvaluationError("legacy schema is historical only; answer-eval/v3 provenance is required")
    nonempty(m["run_id"], "run_id")
    if m["evidence_mode"] not in ("real", "fixture"):
        raise EvaluationError("evidence_mode must be real or fixture")
    if m["evidence_mode"] == "fixture" and not allow_fixture:
        raise EvaluationError("fixture scores are mechanical diagnostics, never real evidence", status="MISSING")
    started = timestamp(m["started_at"], "started_at")
    completed = timestamp(m["completed_at"], "completed_at")
    if completed < started:
        raise EvaluationError("run completed before it started")
    fields(m["models"], {"answerer", "judge"}, "models")
    fields(m["settings"], {"answerer", "judge"}, "settings")
    for role in ("answerer", "judge"):
        model(m["models"][role], "models." + role)
        if not isinstance(m["settings"][role], dict):
            raise EvaluationError(f"settings.{role} must be an object")
    tokenizer(m["tokenizer"])
    fields(m["variables"], {"ranking", "chunker", "embedding", "lexical", "reranker", "pack", "code"}, "variables")
    for name, value in m["variables"].items():
        if value is None or value == "" or value == {}:
            raise EvaluationError(f"variables.{name}: must record an actual identity/setting")
    questions_path = reference(root, m["questions"], "questions")
    reference(root, m["corpus"], "corpus")  # Upstream corpus audit owns the source/label audit.
    a_template = read_text(reference(root, m["answerer_prompt"], "answerer_prompt"))
    g_template = read_text(reference(root, m["grader_prompt"], "grader_prompt"))
    questions = {}
    for line in read_text(questions_path).splitlines():
        if not line.strip():
            continue
        q = parse_json(line, "question")
        validate_question(q)
        if q["id"] in questions:
            raise EvaluationError("duplicate question_id")
        questions[q["id"]] = q
    counts = {c: sum(q["class"] == c for q in questions.values()) for c in CLASSES}
    if any(counts[c] < 20 for c in CLASSES):
        raise EvaluationError(f"require >=20 single/multi/cross/trap each; found {counts}", status="MISSING")
    group_hash = canonical_sha256({qid: q["source_group"] for qid, q in questions.items()})
    if m["source_groups_sha256"] != group_hash:
        raise EvaluationError("source_groups_sha256 does not match question group mapping")
    p = _load_preregistration(root, m, started)
    expected = set(questions)
    _directory_ids(root, "packs", ".md", expected)
    for a in ("a1", "a2"):
        for folder, extension in ((f"answers_{a}", ".md"), (f"answers_{a}", ".json"),
                                  (f"requests_{a}", ".txt"), (f"judge_prompts_{a}", ".txt")):
            _directory_ids(root, folder, extension, expected)
    for a, j in ROUNDS:
        _directory_ids(root, f"grades_{a}_{j}", ".json", expected)
    scores, answers, grades, packs = {}, {}, {}, {}
    execution_ids = set()

    def execution(record, label):
        nonempty(record["execution_id"], label + ".execution_id")
        if record["execution_id"] in execution_ids:
            raise EvaluationError("execution_id reused: rounds/questions require independent jobs")
        execution_ids.add(record["execution_id"])
        if record["run_id"] != m["run_id"]:
            raise EvaluationError(label + ": record belongs to another run")

    for qid, q in sorted(questions.items()):
        pack_path = inside(root, f"packs/{qid}.md")
        pack, pack_sha = read_text(pack_path), sha256_file(pack_path)
        packs[qid] = pack_sha
        answers[qid], grades[qid], scores[qid] = [], [], []
        for a in ("a1", "a2"):
            answer_path = inside(root, f"answers_{a}/{qid}.md")
            text, answer_sha = read_text(answer_path), sha256_file(answer_path)
            nonempty(text, "answer text")
            answer = read_json(inside(root, f"answers_{a}/{qid}.json"))
            fields(answer, {"schema", "run_id", "question_id", "answer_round", "execution_id", "completed_at",
                            "answer_sha256", "pack_sha256", "answerer_prompt_sha256", "request_sha256",
                            "answerer_model", "answerer_settings"}, "answer")
            execution(answer, "answer")
            request_path = inside(root, f"requests_{a}/{qid}.txt")
            expected_request = answer_request(a_template, q, pack)
            if read_text(request_path) != expected_request:
                raise EvaluationError(f"{qid}/{a}: answerer request is not the frozen question/template/owned pack")
            expected_values = {"schema": 3, "question_id": qid, "answer_round": a,
                               "answer_sha256": answer_sha, "pack_sha256": pack_sha,
                               "answerer_prompt_sha256": m["answerer_prompt"]["sha256"],
                               "request_sha256": sha256_file(request_path),
                               "answerer_model": m["models"]["answerer"],
                               "answerer_settings": m["settings"]["answerer"]}
            if type(answer["schema"]) is not int or any(answer[k] != v for k, v in expected_values.items()):
                raise EvaluationError(f"{qid}/{a}: answer provenance mismatch")
            stamp = timestamp(answer["completed_at"], "answer.completed_at")
            if not started <= stamp <= completed:
                raise EvaluationError("answer completion lies outside its run")
            answers[qid].append(answer)
            judge_path = inside(root, f"judge_prompts_{a}/{qid}.txt")
            if read_text(judge_path) != grader_request(g_template, q, text, pack):
                raise EvaluationError(f"{qid}/{a}: grader request is not the frozen facts/template/owned answer")
            for j in ("j1", "j2"):
                grade = read_json(inside(root, f"grades_{a}_{j}/{qid}.json"))
                fields(grade, {"schema", "run_id", "question_id", "answer_round", "judge_round", "execution_id",
                               "completed_at", "score", "refused", "fabricated_fact", "fact_results",
                               "answer_sha256", "pack_sha256", "grader_prompt_sha256", "judge_model", "judge_settings"}, "grade")
                execution(grade, "grade")
                values = {"schema": 3, "question_id": qid, "answer_round": a, "judge_round": j,
                          "answer_sha256": answer_sha, "pack_sha256": pack_sha,
                          "grader_prompt_sha256": sha256_file(judge_path),
                          "judge_model": m["models"]["judge"], "judge_settings": m["settings"]["judge"]}
                if type(grade["schema"]) is not int or any(grade[k] != v for k, v in values.items()):
                    raise EvaluationError(f"{qid}/{a}/{j}: grade provenance mismatch")
                g_stamp = timestamp(grade["completed_at"], "grade.completed_at")
                if not stamp <= g_stamp <= completed:
                    raise EvaluationError("grade completion predates answer or lies outside run")
                if type(grade["score"]) is not int or grade["score"] not in (0, 1, 2):
                    raise EvaluationError("grade score must be integer 0/1/2; booleans are invalid")
                if type(grade["refused"]) is not bool or type(grade["fabricated_fact"]) is not bool:
                    raise EvaluationError("refused/fabricated_fact booleans are required")
                fact_results = grade["fact_results"]
                if not isinstance(fact_results, list):
                    raise EvaluationError("fact_results must be a complete list")
                fact_ids = []
                evidence_ids = {e["evidence_id"] for e in q["expect"]}
                for fact in fact_results:
                    fields(fact, {"fact_id", "supported", "evidence_ids"}, "fact_result")
                    nonempty(fact["fact_id"], "fact_result.fact_id")
                    if type(fact["supported"]) is not bool or not isinstance(fact["evidence_ids"], list):
                        raise EvaluationError("fact_results require supported bool and evidence_ids list")
                    if (any(not isinstance(x, str) for x in fact["evidence_ids"])
                            or len(set(fact["evidence_ids"])) != len(fact["evidence_ids"])
                            or not set(fact["evidence_ids"]) <= evidence_ids
                            or (fact["supported"] and not fact["evidence_ids"])):
                        raise EvaluationError("fact result must cite known evidence IDs")
                    fact_ids.append(fact["fact_id"])
                if (len(set(fact_ids)) != len(fact_ids)
                        or set(fact_ids) != {f["fact_id"] for f in q["required_facts"]}):
                    raise EvaluationError("missing/duplicate/unknown fact_results")
                definitions = {f["fact_id"]: f for f in q["required_facts"]}
                for fact in fact_results:
                    if fact["supported"] and not any(
                        set(option) <= set(fact["evidence_ids"])
                        for option in definitions[fact["fact_id"]]["sufficient_evidence"]
                    ):
                        raise EvaluationError("supported fact is missing a required conjunct of its evidence")
                if q["class"] != "trap" and grade["score"] == 2 and not all(f["supported"] for f in fact_results):
                    raise EvaluationError("score 2 contradicts unsupported required facts")
                grades[qid].append(grade)
                scores[qid].append(grade["score"])
    return AnswerRun(root, m, sha256_file(root / "run.json"), p, questions, scores,
                     answers, grades, packs, frozenset(execution_ids))
