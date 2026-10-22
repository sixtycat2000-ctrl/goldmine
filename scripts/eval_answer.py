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

Historical v2 outputs remain readable for advisory summaries. Formal gates
now use the independent disk-only answer-eval/v3 protocol: preregistered
practical drop threshold, two answer rounds with two judgments each, complete
SHA provenance and source-group paired resampling. No variance-derived false
positive claim is made; old v2 baselines cannot pass a formal gate.

Usage:
  python3 scripts/eval_answer.py --validate-questions
  python3 scripts/eval_answer.py --emit --backend llama [--arm list] [--out DIR]
  python3 scripts/eval_answer.py --emit --check-facts      # per-fact regex gate
  python3 scripts/eval_answer.py --grade [--jobs DIR]      # write grader prompts
  python3 scripts/eval_answer.py --grade --collect         # replies -> grades/
  python3 scripts/eval_answer.py --summary                 # report from grades/
  python3 scripts/eval_answer.py --validate-run RUN --json
  python3 scripts/eval_answer.py --compare BASE_RUN CANDIDATE_RUN --json
  python3 scripts/eval_answer.py --gate --out RUN --baseline V3_RECEIPT
  python3 scripts/eval_answer.py --update-baseline --yes --out RUN
"""

from __future__ import annotations

import argparse
import hashlib
import json
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
DEFAULT_BASELINE = EVALS / "answer_v3" / "baseline.json"
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
DEFAULT_MODEL_ID = "GLM-5.3-Flash (ZCode subagent)"

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
    if len(qs) != 17:
        problems.append(f"expected 17 questions, got {len(qs)}")
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
    if type(score) is not int or score not in (0, 1, 2):
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
    label = "  [historical-v2 advisory; not release evidence]"
    lo, hi = eval_real.binom_ci(answered, len(answerable))
    rlo, rhi = eval_real.binom_ci(refusals, len(traps)) if traps else (0.0, 1.0)
    blo, bhi = eval_real.bootstrap_ci(scores)
    out = [
        f"n={len(qs)} → 1 question = {1 / len(qs):.4g} of the mean{label}",
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
              f"--collect for all {len(qs)} first", file=sys.stderr)
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


def update_baseline(args) -> int:
    """Record a fully validated v3 reference; never overwrite historical data."""
    from answer_eval.cli import command
    return command("baseline", run=args.out, output=args.baseline,
                   yes=getattr(args, "yes", False), as_json=getattr(args, "json", False))


def gate(args) -> int:
    """Verify a v3 receipt and gate its run; schema2 is historical only."""
    from answer_eval.cli import command
    return command("gate", base=args.baseline, candidate=args.out,
                   diagnostic=getattr(args, "diagnostic", False),
                   as_json=getattr(args, "json", False))


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
    ap.add_argument("--compare", nargs=2, metavar=("BASE_RUN", "CANDIDATE_RUN"),
                    help="compare two independent, complete answer-eval/v3 runs")
    ap.add_argument("--validate-run", metavar="RUN", help="validate v3 completeness and job provenance")
    ap.add_argument("--json", action="store_true", help="structured v3 report")
    ap.add_argument("--diagnostic", action="store_true",
                    help="allow fixture/self comparison; never accepted as release evidence")
    ap.add_argument("--gate", action="store_true",
                    help="verify a v3 baseline receipt and run; legacy baselines are rejected")
    ap.add_argument("--update-baseline", action="store_true",
                    help="create a new v3 reference receipt (needs --yes and complete real grades)")
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
                    help="accepted legacy option; v3 receipts use recorded run timestamps")
    args = ap.parse_args(argv)

    actions = (args.validate_questions, args.emit, args.grade, args.summary,
               bool(args.compare), bool(args.validate_run), args.gate, args.update_baseline)
    if sum(actions) > 1:
        ap.error("choose exactly one evaluation action")

    if args.date is None:
        import datetime

        args.date = datetime.date.today().isoformat()
    if args.compare or args.validate_run:
        from answer_eval.cli import command
        if args.compare:
            return command("compare", base=args.compare[0], candidate=args.compare[1],
                           diagnostic=args.diagnostic, as_json=args.json)
        return command("validate", run=args.validate_run, diagnostic=args.diagnostic, as_json=args.json)
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
