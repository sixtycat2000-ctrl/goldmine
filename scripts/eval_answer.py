#!/usr/bin/env python3
"""Answer-material eval (v2.1 R4): emit packs -> answer (human-run Phase B)
-> grade -> baseline + gate. Spec: docs/v2.1-pack-eval.md §3, with the review
amendments F1-F11 applied.

The repo script NEVER calls an LLM (D14: no API in the repo — disk is the
interface). Phase B (answering) and the grader subagents are spawned by the
human/orchestrator; this tool prepares their inputs and consumes their
outputs:

    evals/answer/
      questions.jsonl        committed question set (§3.2 schema + F1 evidence)
      answerer_prompt.md     committed verbatim answerer prompt
      grader_prompt.md       committed verbatim grader prompt
      packs/<qid>.md/.json   Phase A output (pack + sidecar)
      lists/<qid>.md         Phase A --arm list comparison material
      answers/<qid>.md       Phase B round-1 answers (+ run.json: answerer id)
      answers2/<qid>.md      Phase B round-2 answers (variance run, F2)
      answers_list/<qid>.md  list-arm answers (advisory, F11)
      grade_jobs/<qid>.txt   ready-to-paste grader prompts (--grade)
      grade_jobs/<qid>.out   grader replies dropped back by the orchestrator
      grades/<qid>.json      Phase C grades (--grade --collect)
      grades2/<qid>.json     double-grades for the variance measurement (F2)
      grades_list/<qid>.json list-arm grades (advisory)

Gate discipline (F2/F3/F4/F5): --gate re-emits the packs and byte-compares
shas BEFORE touching grades (drifted pack -> exit 2); refuses when >2 answer
files differ from the baseline's recorded answer shas; mean score over the 16
ANSWERABLE questions only (traps report honest_refusal_rate, non-gating);
threshold derived from the measured round-1/round-2 waver rates so a no-change
re-run trips < 5% of the time.

Usage:
  python3 scripts/eval_answer.py --validate-questions
  python3 scripts/eval_answer.py --emit --backend llama [--arm list] [--out DIR]
  python3 scripts/eval_answer.py --emit --check-facts      # per-fact regex gate
  python3 scripts/eval_answer.py --grade [--jobs DIR]      # write grader prompts
  python3 scripts/eval_answer.py --grade --collect         # replies -> grades/
  python3 scripts/eval_answer.py --summary                 # report from grades/
  python3 scripts/eval_answer.py --gate                    # vs the baseline
  python3 scripts/eval_answer.py --update-baseline --yes   # requires variance
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from goldmine.backends import create_backend  # noqa: E402
from goldmine.config import Config  # noqa: E402
from goldmine.pack import (  # noqa: E402
    PackSettings,
    build_pack_from_hits,
)
from goldmine.render import _render_loc  # noqa: E402
from goldmine.search import Searcher, parse_filters  # noqa: E402
from goldmine.store import Store  # noqa: E402
from goldmine.util import excerpt as make_excerpt  # noqa: E402

EVALS = REPO_ROOT / "evals"
DEFAULT_DB = EVALS / "eval.sqlite"
DEFAULT_OUT = EVALS / "answer"
DEFAULT_BASELINE = EVALS / "answer_baseline.json"
DEFAULT_MANIFEST = EVALS / "corpus_manifest.json"
QUESTIONS_PATH = DEFAULT_OUT / "questions.jsonl"
ANSWERER_PROMPT = DEFAULT_OUT / "answerer_prompt.md"
GRADER_PROMPT = DEFAULT_OUT / "grader_prompt.md"

#: Phase A pins (§3.1) — overridable only TOGETHER via --scorer/--z-gate/
#: --limit/--context/--budget, and any override changes the fingerprint set
#: the baseline stores (an override run can never silently compare).
PACK_SETTINGS = {
    "scorer": "hybrid",
    "z_gate": 3.0,
    "limit": 10,
    "context": 1,
    "budget": 4000,
}
BUDGET_FLOOR, BUDGET_CEILING = 512, 32768
MAX_CONTEXT = 3

PROMPT_VERSIONS = {"answerer": "answerer.v2", "grader": "grader.v2"}
PROTOCOL_VERSION = "answer-eval/v2"
BASELINE_SCHEMA = 2
DEFAULT_MODEL_ID = "GLM-5.3-Flash (ZCode subagent)"

#: F2: a no-change re-run may trip the gate at most p_max of the time;
#: the threshold is raised until the measured waver rates clear that bar.
TRIP_P_MAX = 0.05
THRESHOLD_CANDIDATES = (0.10, 0.15, 0.20, 0.25, 0.30)
#: F2: --gate refuses the comparison when MORE than this many answer files'
#: shas differ from the baseline's record (deliberate re-run required).
MAX_DRIFTED_ANSWERS = 2
#: F2: --update-baseline requires a double-grade over at least this many
#: questions, covering EVERY round-1 borderline (score 1) question.
MIN_DOUBLE_GRADES = 12
ANSWERABLE_CLASSES = ("single", "multi", "cross")
TRAP_CLASS = "trap"
VALID_CLASSES = ANSWERABLE_CLASSES + (TRAP_CLASS,)

CLASSES_REQUIRING_EVIDENCE = VALID_CLASSES
REFUSAL_MARKER = "does not contain enough information"


# ------------------------------------------------------------------ questions


def load_questions(path: Path = QUESTIONS_PATH) -> list[dict]:
    """questions.jsonl -> list of dicts; `//` comment lines skipped."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("//"):
            continue
        out.append(json.loads(s))
    return out


def validate_questions(qs: list[dict]) -> list[str]:
    """Schema + label checks only (no packs needed). The evidence field (F1a)
    carries ONE regex per must_fact; for traps the regexes are LEAK patterns
    that must NOT match the emitted pack body."""
    problems: list[str] = []
    ids = [q.get("id") for q in qs]
    if len(qs) != 20:
        problems.append(f"expected 20 questions, got {len(qs)}")
    if len(set(ids)) != len(ids):
        problems.append("duplicate question ids")
    for i, qid in enumerate(ids):
        if qid != f"ans-{i + 1:03d}":
            problems.append(f"question {i}: unexpected id {qid!r}")
    for q in qs:
        qid = q.get("id", "?")
        cls = q.get("class")
        if cls not in VALID_CLASSES:
            problems.append(f"{qid}: unknown class {cls!r}")
        if bool(q.get("trap")) != (cls == TRAP_CLASS):
            problems.append(f"{qid}: trap flag disagrees with class {cls!r}")
        if not q.get("q"):
            problems.append(f"{qid}: empty question text")
        mf = q.get("must_facts") or []
        if not 1 <= len(mf) <= 4:
            problems.append(f"{qid}: must_facts must be 1..4, got {len(mf)}")
        ev = q.get("evidence") or []
        if len(ev) != len(mf):
            problems.append(
                f"{qid}: evidence must carry exactly one regex per must_fact "
                f"({len(mf)}), got {len(ev)}"
            )
        for e in ev:
            try:
                re.compile(e)
            except re.error as exc:
                problems.append(f"{qid}: bad evidence regex {e!r}: {exc}")
        if not q.get("sources"):
            problems.append(f"{qid}: no sources")
        if cls == TRAP_CLASS:
            if len(mf) < 2:
                problems.append(f"{qid}: trap needs >= 2 refusal checks")
        for key in ("nice_facts",):
            if not isinstance(q.get(key), list):
                problems.append(f"{qid}: {key} must be a list")
    return problems


# --------------------------------------------------------------------- packs


def open_index(db: Path, backend_name: str):
    """(store, backend) on the eval index, Phase A config (the .evalhome the
    corpus build uses — llama server/model paths live there)."""
    cfg = Config(home=str(db.parent / ".evalhome"))
    cfg.backend = backend_name
    store = Store(db)
    backend = create_backend(cfg, backend_name)
    return store, backend


def search_one(store, backend, question: dict, settings: dict):
    """The FIXED Phase A search: clean query -> hits under the pinned pack
    settings. One search per question; the pack builder consumes the hits."""
    clean, filters = parse_filters(question["q"])
    searcher = Searcher(store, backend)
    hits = searcher.search(
        clean,
        limit=settings["limit"],
        scorer=settings["scorer"],
        z_gate=settings["z_gate"],
        filters=filters,
        snippet_width=None,  # pack records carry full text; snippets are waste
    )
    return clean, filters, hits


def build_question_pack(store, backend, question: dict, settings: dict):
    """(pack_text, meta, list_text) for one question. list_text is the
    advisory list-arm material (numbered excerpts, no expansion, no budget —
    same [n] citation grammar so the answerer prompt is identical)."""
    clean, filters, hits = search_one(store, backend, question, settings)
    pack_settings = PackSettings(
        query=clean,
        scorer=settings["scorer"],
        z_gate=settings["z_gate"],
        limit=settings["limit"],
        context=settings["context"],
        budget=settings["budget"],
        filters=filters,
    )
    text, meta = build_pack_from_hits(store, backend, pack_settings, hits)
    list_text = render_list_arm(hits)
    return text, meta, list_text


def render_list_arm(hits) -> str:
    """The comparison arm's material (F11, advisory): the plain search render,
    but with [n] record numbers so the fixed answerer prompt works verbatim —
    top-`limit` hits, stored excerpts (240 chars, the human default), no
    neighbor expansion, no budget."""
    lines = []
    for h in hits:
        loc = _render_loc(h.kind, h.loc, h.loc_end)
        z_part = f" z {h.z:.2f}" if h.z is not None else ""
        lines.append(
            f"[{h.rank}] {h.path} ({h.kind}) {loc} · score {h.score:.2f}{z_part}"
        )
        body = make_excerpt(h.excerpt or "", 240)
        if body:
            lines.append(f'   "{body}"')
    return "\n".join(lines) + ("\n" if lines else "")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def emit(args) -> int:
    """Phase A: write packs/<qid>.md + sidecar (and lists/<qid>.md with
    --arm list) for every question; --check-facts enforces EVERY evidence
    regex (F1b) and exits 1 listing failures."""
    qs = load_questions(QUESTIONS_PATH)
    problems = validate_questions(qs)
    if problems:
        for p in problems:
            print(f"question set invalid: {p}", file=sys.stderr)
        return 2
    settings = _settings_from_args(args)
    out = Path(args.out)
    (out / "packs").mkdir(parents=True, exist_ok=True)
    if args.arm == "list":
        (out / "lists").mkdir(parents=True, exist_ok=True)
    store, backend = open_index(Path(args.db), args.backend)
    try:
        for q in qs:
            text, meta, list_text = build_question_pack(
                store, backend, q, settings
            )
            meta["question_id"] = q["id"]
            meta["class"] = q["class"]
            meta["trap"] = q["trap"]
            meta["pack_settings"] = settings
            pack_path = out / "packs" / f"{q['id']}.md"
            pack_path.write_text(text, encoding="utf-8")
            (out / "packs" / f"{q['id']}.json").write_text(
                json.dumps(meta, indent=1, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            if args.arm == "list":
                (out / "lists" / f"{q['id']}.md").write_text(
                    list_text, encoding="utf-8"
                )
            print(
                f"{q['id']} [{q['class']:6s}] {meta['used_tokens']:5d}/"
                f"{settings['budget']} tok  {len(meta['records'])} records  "
                f"skipped {meta['skipped_over_budget']}"
            )
    finally:
        backend.close()
        store.close()
    if args.arm == "list":
        print(f"emitted {len(qs)} packs + lists -> {out}")
    else:
        print(f"emitted {len(qs)} packs -> {out}/packs/")
    if args.check_facts:
        return check_facts_command(qs, out / "packs")
    return 0


# --------------------------------------------------------------- check-facts


def pack_body(pack_text: str) -> str:
    """The material half of a pack: everything AFTER the `#` header block
    (the header echoes the question text — matching against it would accept
    the question as its own evidence). Includes the sources footer: a fact
    may not leak through a path either."""
    lines = pack_text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("[1] "):
            return "\n".join(lines[i:])
    # zero-record pack: no material at all
    return ""


def check_facts_command(qs: list[dict], packs_dir: Path) -> int:
    """F1b: every evidence regex per question against the emitted pack text.
    Answerable: each regex MUST match. Traps: regexes are leak patterns and
    must NOT match. Prints the per-fact table; exit 1 listing failures."""
    failures: list[str] = []
    print(f"{'qid':8s} {'chk':4s} {'verdict':8s} pattern")
    for q in qs:
        pack_path = packs_dir / f"{q['id']}.md"
        body = pack_body(pack_path.read_text(encoding="utf-8"))
        for i, (fact, pattern) in enumerate(
            zip(q["must_facts"], q["evidence"]), start=1
        ):
            if q["trap"]:
                ok = re.search(pattern, body, re.DOTALL) is None
                kind = "absent"
            else:
                ok = re.search(pattern, body, re.DOTALL) is not None
                kind = "present"
            verdict = "PASS" if ok else "FAIL"
            print(f"{q['id']:8s} {i:<4d} {verdict:8s} [{kind}] {fact[:72]}")
            if not ok:
                failures.append(
                    f"{q['id']} check {i} ({kind}): {fact} — pattern {pattern!r}"
                )
    if failures:
        print(f"\ncheck-facts: {len(failures)} FAILURE(S):", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"\ncheck-facts: all {sum(len(q['must_facts']) for q in qs)} "
          f"fact checks PASS")
    return 0


# ------------------------------------------------------------ grader plumbing


def _strip_html_comment(text: str) -> str:
    return re.sub(r"<!--.*?-->\s*", "", text, flags=re.DOTALL)


def build_checks_block(q: dict) -> str:
    """The CHECKS slot (§3.4): answerable = must_facts verbatim, numbered;
    traps = TRAP header + refusal checks + F5's nice_fact adjudications
    folded in verbatim (they define what still counts as honest refusal)."""
    if not q["trap"]:
        return "\n".join(
            f"{i}. {fact}" for i, fact in enumerate(q["must_facts"], start=1)
        )
    lines = ["TRAP — the material is known not to cover this question."]
    for i, fact in enumerate(q["must_facts"], start=1):
        lines.append(f"{i}. {fact}")
    for nice in q.get("nice_facts", []):
        lines.append(f"Adjudication: {nice}")
    return "\n".join(lines)


def build_grader_prompt(q: dict, answer_text: str, template: str) -> str:
    """Fill the template's four tokens (§3.4 + F7 scoping lives in the
    template text; F5 adjudications ride <CHECKS>; F10's grader_note rides
    <GRADER_NOTE>, empty when the question carries none)."""
    note = q.get("grader_note", "")
    return (
        template.replace("<QUESTION>", q["q"])
        .replace("<GRADER_NOTE>", f"NOTE: {note}" if note else
                 "(no extra note for this question)")
        .replace("<CHECKS>", build_checks_block(q))
        .replace("<ANSWER>", answer_text)
    )


def grade_jobs(args) -> int:
    """Phase C step 1: write the ready-to-paste grader prompts."""
    qs = {q["id"]: q for q in load_questions(QUESTIONS_PATH)}
    answers_dir = Path(args.answers_dir)
    jobs = Path(args.jobs)
    jobs.mkdir(parents=True, exist_ok=True)
    template = _strip_html_comment(GRADER_PROMPT.read_text(encoding="utf-8"))
    n = 0
    for qid in sorted(qs):
        answer_path = answers_dir / f"{qid}.md"
        if not answer_path.is_file():
            print(f"skip {qid}: no answer at {answer_path}", file=sys.stderr)
            continue
        prompt = build_grader_prompt(
            qs[qid], answer_path.read_text(encoding="utf-8"), template
        )
        (jobs / f"{qid}.txt").write_text(prompt, encoding="utf-8")
        n += 1
    (jobs / "run.json").write_text(
        json.dumps({"judge": args.judge_model}, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {n} grader prompts -> {jobs}/ "
          f"(drop replies as {jobs}/<qid>.out, then --grade --collect)")
    return 0


def parse_grader_output(text: str, qid: str) -> dict:
    """Strict parse of a grader reply (§3.4): ONE JSON object, checks list
    complete, score in range — refuse ambiguous output for re-run."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```\s*$", "", stripped)
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end == -1 or "}" not in stripped:
        raise ValueError(f"{qid}: grader reply carries no JSON object")
    try:
        obj = json.loads(stripped[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"{qid}: grader reply is not valid JSON: {exc}") from exc
    checks = obj.get("checks")
    score = obj.get("score")
    if not isinstance(checks, list) or not checks:
        raise ValueError(f"{qid}: grader reply has no checks list")
    for c in checks:
        if not isinstance(c, dict) or "id" not in c or "pass" not in c:
            raise ValueError(f"{qid}: malformed check {c!r}")
    if score not in (0, 1, 2):
        raise ValueError(f"{qid}: score {score!r} out of range")
    if not isinstance(obj.get("reason"), str):
        raise ValueError(f"{qid}: missing reason")
    return obj


def collect(args) -> int:
    """Phase C step 2: parse grader replies into grade files."""
    qs = {q["id"]: q for q in load_questions(QUESTIONS_PATH)}
    answers_dir = Path(args.answers_dir)
    jobs = Path(args.jobs)
    grades_dir = Path(args.grades_dir)
    grades_dir.mkdir(parents=True, exist_ok=True)
    n, bad = 0, []
    for qid in sorted(qs):
        out_path = jobs / f"{qid}.out"
        if not out_path.is_file():
            continue
        answer_path = answers_dir / f"{qid}.md"
        if not answer_path.is_file():
            bad.append(f"{qid}: answer vanished ({answer_path})")
            continue
        try:
            obj = parse_grader_output(out_path.read_text(encoding="utf-8"), qid)
        except ValueError as exc:
            bad.append(str(exc))
            continue
        expected = len(qs[qid]["must_facts"])
        if len(obj["checks"]) != expected:
            bad.append(
                f"{qid}: {len(obj['checks'])} checks, expected {expected}"
            )
            continue
        grade = {
            "qid": qid,
            "score": obj["score"],
            "checks": obj["checks"],
            "reason": obj["reason"],
            "answer_sha256": sha256_file(answer_path),
            "grader_prompt_sha256": sha256_file(jobs / f"{qid}.txt"),
            "judge_model": args.judge_model,
        }
        (grades_dir / f"{qid}.json").write_text(
            json.dumps(grade, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        n += 1
    if bad:
        for b in bad:
            print(f"collect: {b}", file=sys.stderr)
        print(f"collect: {n} ok, {len(b)} refused — fix the .out files "
              "and re-run", file=sys.stderr)
        return 1
    print(f"collect: {n} grades -> {grades_dir}/")
    return 0


# ------------------------------------------------------------------ statistics


def _stats(qs: list[dict], grades_dir: Path) -> dict | None:
    """Read a complete grades dir -> {scores per qid, per-class, refusal}.
    None when incomplete (the caller decides whether that is fatal)."""
    grades = {}
    for q in qs:
        p = grades_dir / f"{q['id']}.json"
        if not p.is_file():
            return None
        grades[q["id"]] = json.loads(p.read_text(encoding="utf-8"))
    return grades


def summarize(qs: list[dict], grades: dict, baseline: dict | None) -> str:
    """The §3.6 statistics block (house honesty style)."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import eval_real  # noqa: E402  (binom_ci/bootstrap_ci reuse)

    answerable = [q["id"] for q in qs if q["class"] in ANSWERABLE_CLASSES]
    traps = [q["id"] for q in qs if q["class"] == TRAP_CLASS]
    scores = [grades[qid]["score"] for qid in answerable]
    mean = sum(scores) / len(scores) if scores else 0.0
    answered = sum(1 for qid in answerable if grades[qid]["score"] >= 1)
    refusals = sum(1 for qid in traps if grades[qid]["score"] == 2)
    label = "" if baseline and baseline.get("date") else "  [advisory-until-baseline]"
    lo, hi = eval_real.binom_ci(answered, len(answerable))
    rlo, rhi = eval_real.binom_ci(refusals, len(traps)) if traps else (0.0, 1.0)
    blo, bhi = eval_real.bootstrap_ci(scores)
    out = [
        f"n=20 → 1 question = {1 / len(qs):.4g} of the mean{label}",
        f"mean answerable score: {mean:.3f} "
        f"(95% bootstrap CI [{blo:.3f}, {bhi:.3f}], n={len(answerable)})",
        f"answered rate (score ≥ 1): {answered}/{len(answerable)} = "
        f"{answered / len(answerable):.3f} "
        f"(exact 95% CI [{lo:.3f}, {hi:.3f}])",
        f"honest refusal rate (trap score 2, non-gating): {refusals}/"
        f"{len(traps)} = {refusals / len(traps):.3f} "
        f"(exact 95% CI [{rlo:.3f}, {rhi:.3f}])",
    ]
    for cls in VALID_CLASSES:
        ids = [q["id"] for q in qs if q["class"] == cls]
        m = sum(grades[i]["score"] for i in ids) / len(ids) if ids else 0.0
        out.append(f"  class {cls:6s}: mean {m:.3f} (n={len(ids)}) — advisory")
    marker_hits = [
        qid for qid, g in grades.items()
        if REFUSAL_MARKER in _grade_reason_or_text(g)
    ]
    if marker_hits:
        out.append(f"  refusal-marker sanity signal: {len(marker_hits)} "
                   f"answer(s) carry the marker sentence (advisory)")
    return "\n".join(out)


def _grade_reason_or_text(grade: dict) -> str:
    return grade.get("reason", "") + "".join(
        c.get("quote", "") or "" for c in grade.get("checks", [])
    )


def summary_command(args) -> int:
    qs = load_questions(QUESTIONS_PATH)
    problems = validate_questions(qs)
    if problems:
        for p in problems:
            print(f"question set invalid: {p}", file=sys.stderr)
        return 2
    grades = _stats(qs, Path(args.grades_dir))
    if grades is None:
        print(f"grades incomplete under {args.grades_dir} — run --grade "
              "--collect for all 20 first", file=sys.stderr)
        return 2
    baseline = _load_baseline(args.baseline)
    print(summarize(qs, grades, baseline))
    return 0


# ------------------------------------------------------------------ baseline


def _load_baseline(path: Path) -> dict | None:
    p = Path(path)
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def empty_baseline(corpus_version: str) -> dict:
    """The committed scaffolding: everything knowable without a Phase B/C run
    is filled; every run-dependent field stays null until --update-baseline."""
    return {
        "schema": BASELINE_SCHEMA,
        "protocol_version": PROTOCOL_VERSION,
        "date": None,
        "models": {"answerer": None, "judge": None},
        "pack_settings": {**PACK_SETTINGS, "token_mode": None},
        "index": {
            "fingerprint_id": None, "model_id": None, "model_sha256": None,
            "dim": None, "chunker_version": None,
        },
        "questions_sha256": None,
        "prompt_versions": dict(PROMPT_VERSIONS),
        "corpus_version": corpus_version,
        "variance": None,
        "mean_score": None,
        "answered_rate": None,
        "honest_refusal_rate": None,
        "per_question": {},
        "per_class": None,
        "list_arm": None,
        "gate": {
            "threshold": 0.1,
            "derived": False,
            "rule": "mean answerable-score drop > threshold vs baseline -> exit 1",
            "n_answerable": 16,
            "trip_p_max": TRIP_P_MAX,
        },
    }


def _binom_tail_ge(k: int, n: int, p: float) -> float:
    """P(X >= k), X ~ Bin(n, p), exact via the complement tail."""
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i)
               for i in range(k, n + 1))


def derive_threshold(q_eff: float, n_answerable: int = 16) -> tuple[float, float, int]:
    """F2: smallest candidate threshold whose NO-CHANGE trip probability
    (>= ceil atoms flip under Bin(n, q_eff)) stays under TRIP_P_MAX. Returns
    (threshold, trip_p, atoms)."""
    for t in THRESHOLD_CANDIDATES:
        atoms = math.floor(round(t * n_answerable, 9)) + 1  # min a with a/n > t
        trip_p = _binom_tail_ge(atoms, n_answerable, q_eff)
        if trip_p < TRIP_P_MAX:
            return t, trip_p, atoms
    return THRESHOLD_CANDIDATES[-1], trip_p, atoms


def _settings_from_args(args) -> dict:
    """The pinned Phase A settings; flags exist for deliberate override runs
    (which change the fingerprint set — never silently comparable)."""
    s = dict(PACK_SETTINGS)
    for key, argname in (("budget", "budget"), ("limit", "limit"),
                         ("context", "context")):
        v = getattr(args, argname, None)
        if v is not None:
            s[key] = v
    if getattr(args, "z_gate", None) is not None:
        s["z_gate"] = args.z_gate
    if getattr(args, "scorer", None):
        s["scorer"] = args.scorer
    s["budget"] = max(BUDGET_FLOOR, min(BUDGET_CEILING, int(s["budget"])))
    s["context"] = min(int(s["context"]), MAX_CONTEXT)
    return s


def _emit_pack_shas(args, settings: dict) -> dict[str, tuple[str, dict]]:
    """Fresh emit of every question -> {qid: (pack_sha, meta)}; the gate's
    byte round-trip (F4) and update-baseline's pack pinning both need this."""
    qs = load_questions(QUESTIONS_PATH)
    store, backend = open_index(Path(args.db), args.backend)
    shas: dict[str, tuple[str, dict]] = {}
    try:
        for q in qs:
            text, meta, _ = build_question_pack(store, backend, q, settings)
            shas[q["id"]] = (sha256_bytes(text.encode("utf-8")), meta)
    finally:
        backend.close()
        store.close()
    return shas


def _index_fingerprints(db: Path) -> dict:
    store = Store(db)
    try:
        fp_json = store.meta("fingerprint_json") or "{}"
        try:
            fp = json.loads(fp_json)
        except (ValueError, TypeError):
            fp = {}
        chunkers = [
            r[0] for r in store.db.execute(
                "SELECT DISTINCT chunker_version FROM files "
                "WHERE chunker_version IS NOT NULL ORDER BY 1"
            )
        ]
        return {
            "fingerprint_id": store.meta("fingerprint_id"),
            "model_id": fp.get("model_id"),
            "model_sha256": fp.get("model_sha256"),
            "dim": fp.get("dim"),
            "chunker_version": "+".join(chunkers) if chunkers else None,
        }
    finally:
        store.close()


def _corpus_version() -> str:
    manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    return manifest["corpus_version"]


def _answers_model(answers_dir: Path) -> str | None:
    p = Path(answers_dir) / "run.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8")).get("answerer")


def update_baseline(args) -> int:
    """Full run + commit the baseline. REQUIRES the F2 variance data: 20
    round-2 answers (answers2/) and >= MIN_DOUBLE_GRADES round-2 grades
    (grades2/) covering every round-1 score-1 question."""
    qs = load_questions(QUESTIONS_PATH)
    problems = validate_questions(qs)
    if problems:
        for p in problems:
            print(f"question set invalid: {p}", file=sys.stderr)
        return 2
    answerable = [q["id"] for q in qs if q["class"] in ANSWERABLE_CLASSES]
    traps = [q["id"] for q in qs if q["class"] == TRAP_CLASS]
    out = Path(args.out)
    answers, answers2 = out / "answers", out / "answers2"
    grades, grades2 = out / "grades", out / "grades2"
    missing = []
    for q in qs:
        for d in (answers, answers2):
            if not (d / f"{q['id']}.md").is_file():
                missing.append(str(d.relative_to(out) / f"{q['id']}.md"))
        for d in (grades,):
            if not (d / f"{q['id']}.json").is_file():
                missing.append(str(d.relative_to(out) / f"{q['id']}.json"))
    if missing:
        print(f"update-baseline: incomplete run ({len(missing)} missing "
              "answer/grade files), e.g.:", file=sys.stderr)
        for m in missing[:5]:
            print(f"  {m}", file=sys.stderr)
        return 2
    a1 = _answers_model(answers)
    a2 = _answers_model(answers2)
    if not a1 or not a2:
        print("update-baseline: answers/run.json missing (record the answerer "
              "model id there) — see the spec §3 Phase B", file=sys.stderr)
        return 2
    if a1 != a2:
        print(f"update-baseline: answerer differs between rounds ({a1!r} vs "
              f"{a2!r}) — variance needs the SAME model twice", file=sys.stderr)
        return 2
    g1 = _stats(qs, grades)
    g2 = _stats(qs, grades2)
    if g2 is None:
        double = [
            q["id"] for q in qs if (grades2 / f"{q['id']}.json").is_file()
        ]
        if len(double) < MIN_DOUBLE_GRADES:
            print(f"update-baseline: variance needs >= {MIN_DOUBLE_GRADES} "
                  f"double-grades in grades2/, found {len(double)}",
                  file=sys.stderr)
            return 2
        g2 = {qid: json.loads((grades2 / f"{qid}.json").read_text())
              for qid in double}
    borderlines = {qid for qid in answerable if g1[qid]["score"] == 1}
    uncovered = borderlines - set(g2)
    if uncovered:
        print(f"update-baseline: double-grade must cover every borderline "
              f"(score 1) question, missing {sorted(uncovered)}", file=sys.stderr)
        return 2

    # -- variance (F2) ------------------------------------------------------
    n_flip = sum(
        1 for q in qs
        if sha256_file(answers / f"{q['id']}.md")
        != sha256_file(answers2 / f"{q['id']}.md")
    )
    answerer_flip = n_flip / len(qs)
    shared = sorted(set(g2) & set(g1))
    g_flip = sum(1 for qid in shared if g1[qid]["score"] != g2[qid]["score"])
    grader_flip = g_flip / len(shared) if shared else 0.0
    q_eff = 1.0 - (1.0 - answerer_flip) * (1.0 - grader_flip)
    threshold, trip_p, atoms = derive_threshold(q_eff)

    # -- pack round-trip: fresh emit must equal the packs on disk (F4) ------
    settings = {**PACK_SETTINGS}
    fresh = _emit_pack_shas(args, settings)
    drifted = []
    for q in qs:
        disk = out / "packs" / f"{q['id']}.md"
        if disk.is_file() and sha256_file(disk) != fresh[q["id"]][0]:
            drifted.append(q["id"])
    if drifted:
        print(f"update-baseline: packs on disk drifted from a fresh emit "
              f"({', '.join(drifted)}) — re-run --emit, then Phase B again "
              "(the answers no longer describe these packs)", file=sys.stderr)
        return 2

    token_mode = fresh[qs[0]["id"]][1]["token_mode"]
    baseline = empty_baseline(_corpus_version())
    baseline.update({
        "date": args.date,
        "models": {"answerer": a1, "judge": args.judge_model},
        "pack_settings": {**settings, "token_mode": token_mode},
        "index": _index_fingerprints(Path(args.db)),
        "questions_sha256": sha256_file(QUESTIONS_PATH),
        "variance": {
            "answerer_flip_rate": round(answerer_flip, 4),
            "grader_flip_rate": round(grader_flip, 4),
            "q_effective": round(q_eff, 4),
            "n_double_answered": len(qs),
            "n_double_graded": len(shared),
        },
        "mean_score": round(
            sum(g1[qid]["score"] for qid in answerable) / len(answerable), 4
        ),
        "answered_rate": {
            "value": round(
                sum(1 for qid in answerable if g1[qid]["score"] >= 1)
                / len(answerable), 4
            ),
            "n": len(answerable),
        },
        "honest_refusal_rate": {
            "value": round(
                sum(1 for qid in traps if g1[qid]["score"] == 2) / len(traps), 4
            ),
            "n": len(traps),
        },
        "per_question": {
            qid: {
                "score": g1[qid]["score"],
                "answer_sha256": sha256_file(answers / f"{qid}.md"),
                "pack_sha256": fresh[qid][0],
            }
            for qid in (q["id"] for q in qs)
        },
        "per_class": {
            cls: round(
                sum(g1[qid]["score"] for qid in (
                    q["id"] for q in qs if q["class"] == cls))
                / max(1, sum(1 for q in qs if q["class"] == cls)), 4
            )
            for cls in VALID_CLASSES
        },
        "list_arm": _list_arm(out, qs, answerable),
        "gate": {
            "threshold": threshold,
            "derived": True,
            "rule": "mean answerable-score drop > threshold vs baseline -> exit 1",
            "n_answerable": len(answerable),
            "trip_p_max": TRIP_P_MAX,
            "measured_no_change_trip_p": round(trip_p, 4),
            "trip_atoms": atoms,
        },
    })
    Path(args.baseline).write_text(
        json.dumps(baseline, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"baseline written: {args.baseline}")
    print(f"  variance: answerer_flip {answerer_flip:.2f}, grader_flip "
          f"{grader_flip:.2f} -> q_eff {q_eff:.3f}")
    print(f"  gate: threshold {threshold} (no-change trip p = {trip_p:.3f} "
          f"< {TRIP_P_MAX}, {atoms}-atom wire)")
    print(f"  mean answerable score {baseline['mean_score']} · "
          f"answered {baseline['answered_rate']['value']:.3f} · "
          f"honest refusal {baseline['honest_refusal_rate']['value']:.3f} "
          f"(non-gating)")
    return 0


def _list_arm(out: Path, qs: list[dict], answerable: list[str]) -> dict | None:
    """F11: the advisory list arm — filled when answers_list/ + grades_list/
    are complete for the answerable questions; never gating."""
    lists_answers = out / "answers_list"
    lists_grades = out / "grades_list"
    ids = answerable
    if not all((lists_answers / f"{qid}.md").is_file() for qid in ids):
        return None
    if not all((lists_grades / f"{qid}.json").is_file() for qid in ids):
        return None
    scores = {
        qid: json.loads((lists_grades / f"{qid}.json").read_text())["score"]
        for qid in ids
    }
    return {
        "mean_score": round(sum(scores.values()) / len(ids), 4),
        "answered_rate": round(
            sum(1 for v in scores.values() if v >= 1) / len(ids), 4
        ),
        "n": len(ids),
        "note": "advisory: single round, never gates",
    }


# ----------------------------------------------------------------- the gate


def gate(args) -> int:
    """--gate (F2/F3/F4/F5): invalidation set -> pack sha round-trip ->
    answer-sha drift -> answerable-mean threshold. Exit 2 = not comparable
    (fix deliberately), 1 = material regression, 0 = green."""
    baseline = _load_baseline(args.baseline)
    if baseline is None:
        print(f"no baseline at {args.baseline} — run --update-baseline --yes "
              "first (advisory until then)", file=sys.stderr)
        return 2
    if not baseline.get("date") or not baseline.get("per_question"):
        print("baseline is empty scaffolding (no Phase B run recorded) — "
              "nothing to compare; all runs stay advisory", file=sys.stderr)
        return 2
    qs = load_questions(QUESTIONS_PATH)
    problems = validate_questions(qs)
    if problems:
        for p in problems:
            print(f"question set invalid: {p}", file=sys.stderr)
        return 2
    answerable = [q["id"] for q in qs if q["class"] in ANSWERABLE_CLASSES]
    out = Path(args.out)

    # 0. fresh emit — the current packs AND the current token mode (the
    # settings comparison includes token_mode, so it must be known first)
    settings = {**PACK_SETTINGS}
    fresh = _emit_pack_shas(args, settings)
    token_mode = fresh[qs[0]["id"]][1]["token_mode"]

    # 1. invalidation set (§3.5 + F3) — never compare across these
    mismatches = []
    idx = _index_fingerprints(Path(args.db))
    for key, current in idx.items():
        if baseline["index"].get(key) != current:
            mismatches.append(f"index.{key}: baseline "
                              f"{baseline['index'].get(key)!r} != current {current!r}")
    b_settings = dict(baseline["pack_settings"])
    if b_settings.get("token_mode") is None:
        b_settings.pop("token_mode")
    if b_settings != {**settings, "token_mode": token_mode}:
        mismatches.append(f"pack_settings: baseline {b_settings} != current "
                          f"{ {**settings, 'token_mode': token_mode} }")
    if baseline["questions_sha256"] != sha256_file(QUESTIONS_PATH):
        mismatches.append("questions_sha256 changed")
    if baseline["prompt_versions"] != PROMPT_VERSIONS:
        mismatches.append(f"prompt_versions: baseline "
                          f"{baseline['prompt_versions']} != current {PROMPT_VERSIONS}")
    if baseline["corpus_version"] != _corpus_version():
        mismatches.append(f"corpus_version: baseline "
                          f"{baseline['corpus_version']} != current "
                          f"{_corpus_version()}")
    answerer = _answers_model(out / "answers")
    if not answerer:
        mismatches.append("answers/run.json missing (cannot verify who answered)")
    elif answerer != baseline["models"]["answerer"]:
        mismatches.append(f"models.answerer: baseline "
                          f"{baseline['models']['answerer']!r} != current {answerer!r}")
    judges = {
        json.loads((out / "grades" / f"{q['id']}.json").read_text()).get("judge_model")
        for q in qs if (out / "grades" / f"{q['id']}.json").is_file()
    }
    bad_judges = {j for j in judges if j and j != baseline["models"]["judge"]}
    if bad_judges:
        mismatches.append(f"models.judge: grades carry {sorted(bad_judges)} "
                          f"!= baseline {baseline['models']['judge']!r}")
    if mismatches:
        print("gate: NOT COMPARABLE — fingerprint/protocol drift (exit 2):",
              file=sys.stderr)
        for m in mismatches:
            print(f"  - {m}", file=sys.stderr)
        print("re-run Phase B deliberately, then --update-baseline --yes",
              file=sys.stderr)
        return 2

    # 2. pack sha round-trip (F4) — the fresh emit byte-compares FIRST
    drifted = [
        qid for qid in (q["id"] for q in qs)
        if qid in baseline["per_question"]
        and baseline["per_question"][qid]["pack_sha256"] != fresh[qid][0]
    ]
    if drifted:
        print("gate: NOT COMPARABLE — packs drifted from the baseline's "
              "recorded bytes (exit 2):", file=sys.stderr)
        for qid in drifted:
            print(f"  - {qid}", file=sys.stderr)
        print("ranking/chunker/backend/tokenizer drift subsumed here: "
              "re-emit, re-run Phase B, --update-baseline --yes",
              file=sys.stderr)
        return 2

    # 3. answer-sha drift (F2) — the scores describe ONE answer run
    moved = [
        qid for qid in (q["id"] for q in qs)
        if not (out / "answers" / f"{qid}.md").is_file()
        or sha256_file(out / "answers" / f"{qid}.md")
        != baseline["per_question"][qid]["answer_sha256"]
    ]
    if len(moved) > MAX_DRIFTED_ANSWERS:
        print(f"gate: NOT COMPARABLE — {len(moved)} answer files differ from "
              f"the baseline's run (> {MAX_DRIFTED_ANSWERS}) (exit 2):",
              file=sys.stderr)
        for qid in moved[:10]:
            print(f"  - {qid}", file=sys.stderr)
        print("re-run Phase B deliberately, then --update-baseline --yes",
              file=sys.stderr)
        return 2
    if moved:
        print(f"note: {len(moved)} answer file(s) re-answered since the "
              "baseline (<= " + str(MAX_DRIFTED_ANSWERS) + ", allowed): "
              + ", ".join(moved), file=sys.stderr)

    # 4. the comparison (F5): mean over the 16 answerable only
    grades = _stats(qs, out / "grades")
    if grades is None:
        print("grades incomplete — run --grade --collect for all 20 first",
              file=sys.stderr)
        return 2
    threshold = baseline["gate"]["threshold"]
    base_mean = baseline["mean_score"]
    scores = {qid: grades[qid]["score"] for qid in answerable}
    current = sum(scores.values()) / len(scores)
    drop = base_mean - current
    traps = [q["id"] for q in qs if q["class"] == TRAP_CLASS]
    refusal_now = sum(1 for qid in traps if grades[qid]["score"] == 2) / len(traps)
    print(f"gate: mean answerable score {current:.3f} vs baseline "
          f"{base_mean:.3f} (drop {drop:+.3f}, threshold {threshold}, "
          f"n={len(answerable)})")
    print(f"honest refusal rate {refusal_now:.3f} vs baseline "
          f"{baseline['honest_refusal_rate']['value']:.3f} (non-gating)")
    if drop > threshold:
        per_q = {
            qid: scores[qid] - baseline["per_question"][qid]["score"]
            for qid in answerable
        }
        fallen = sorted((pq for pq in per_q.items() if pq[1] < 0),
                        key=lambda kv: kv[1])
        print("gate: FAIL — mean drop exceeds the threshold; per-question "
              "drops:", file=sys.stderr)
        for qid, d in fallen:
            print(f"  - {qid}: {baseline['per_question'][qid]['score']} -> "
                  f"{scores[qid]} ({d:+d})", file=sys.stderr)
        return 1
    print("gate: green")
    return 0


# --------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--validate-questions", action="store_true",
                    help="schema + label checks only")
    ap.add_argument("--emit", action="store_true",
                    help="Phase A: write packs/ (+ lists/ with --arm list)")
    ap.add_argument("--check-facts", action="store_true",
                    help="with --emit: enforce every evidence regex; exit 1 "
                         "on failures")
    ap.add_argument("--arm", choices=["pack", "list"], default=None,
                    help="with --emit: also write lists/<qid>.md (advisory "
                         "comparison arm)")
    ap.add_argument("--grade", action="store_true",
                    help="Phase C: write grader prompts (--jobs); with "
                         "--collect, parse replies into grades/")
    ap.add_argument("--collect", action="store_true",
                    help="with --grade: parse <jobs>/<qid>.out -> grades/")
    ap.add_argument("--summary", action="store_true",
                    help="report from grades/ alone")
    ap.add_argument("--gate", action="store_true",
                    help="compare the current run against the baseline")
    ap.add_argument("--update-baseline", action="store_true",
                    help="full run + commit the baseline (needs --yes and "
                         "the F2 variance data)")
    ap.add_argument("--yes", action="store_true",
                    help="confirm a baseline write")
    ap.add_argument("--backend", default="llama",
                    help="emit/gate backend (default llama)")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--jobs", default=str(DEFAULT_OUT / "grade_jobs"))
    ap.add_argument("--answers-dir", default=str(DEFAULT_OUT / "answers"))
    ap.add_argument("--grades-dir", default=str(DEFAULT_OUT / "grades"))
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    ap.add_argument("--budget", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--context", type=int, default=None)
    ap.add_argument("--z-gate", type=float, default=None)
    ap.add_argument("--scorer", default=None)
    ap.add_argument("--judge-model", default=DEFAULT_MODEL_ID)
    ap.add_argument("--date", default=None,
                    help="baseline date stamp (update-baseline; default today)")
    args = ap.parse_args(argv)

    if args.date is None:
        import datetime

        args.date = datetime.date.today().isoformat()
    if args.validate_questions:
        qs = load_questions(QUESTIONS_PATH)
        problems = validate_questions(qs)
        if problems:
            for p in problems:
                print(f"question set invalid: {p}", file=sys.stderr)
            return 2
        print(f"questions: {len(qs)} ok "
              f"({sum(1 for q in qs if q['trap'])} traps, "
              f"{sum(len(q['must_facts']) for q in qs)} fact atoms)")
        return 0
    if args.emit:
        return emit(args)
    if args.grade and args.collect:
        return collect(args)
    if args.grade:
        return grade_jobs(args)
    if args.summary:
        return summary_command(args)
    if args.gate:
        return gate(args)
    if args.update_baseline:
        if not args.yes:
            print("--update-baseline needs --yes", file=sys.stderr)
            return 2
        return update_baseline(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
