"""Unit tests for scripts/eval_answer.py (R4 harness) — offline, no network.

Covers: question-schema validation (incl. the F1 evidence-per-fact rule), the
check-facts failure path (present + leak patterns), the grader-output parser
(valid / malformed / score out of range), the F2 threshold derivation
historical behavior rejection, and the v3 adapter paths. Historical test node
IDs stay stable; variance-derived thresholds and non-gating traps are now
explicitly rejected under the reviewed v3 contract.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import eval_answer as ea  # noqa: E402
from answer_eval import compute_score_variance, load_run, write_baseline  # noqa: E402
from answer_eval_fixtures import edit, make_run  # noqa: E402


# ---------------------------------------------------------------- fixtures


def q_answerable(qid="ans-001", must=None, evidence=None):
    return {
        "id": qid, "class": "single", "trap": False,
        "q": "what is the answer",
        "must_facts": must or ["The answer states the widget weighs 3 kg"],
        "evidence": evidence or [r"weighs 3 kg"],
        "nice_facts": [], "sources": ["evals/corpus_snapshot/x.md:L1-2"],
        "notes": "test",
    }


def q_trap(qid="ans-017"):
    return {
        "id": qid, "class": "trap", "trap": True,
        "q": "how do I export",
        "must_facts": ["The answer refuses", "The answer invents nothing"],
        "evidence": [r"(?i)\bcsv\b", r"(?i)export (results|to)"],
        "nice_facts": ["piping stdout is fine when labeled"],
        "sources": ["evals/corpus_snapshot/x.md:L1-2"], "notes": "test",
    }


def pack_for(body: str) -> str:
    return (
        "# goldmine answer pack v1\n"
        "# query: what is the answer\n"
        "# search: scorer=hybrid z_gate=3 limit=10 context=1 budget=4000 mode=tokenize\n"
        "# index: ff3dfbc47659dccb embeddinggemma-2-Q8_0 dim=768\n"
        "# the material below is verbatim from the index; cite sources as [n]\n"
        "\n"
        "[1] /tmp/x.md (doc) L1-2 · score 5.00 (z 4.00, cov 1.00)\n"
        + body + "\n"
        "# sources\n# [1] /tmp/x.md L1-2\n"
        "# pack: 1 records · 1 hits · 0 duplicates folded · 0 also folded · "
        "0 skipped (budget) · used 20/4000 tokens (tokenize)\n"
    )


# ---------------------------------------------------------------- validation


def test_validate_questions_accepts_the_committed_set():
    qs = ea.load_questions()
    assert len(qs) == 20
    assert ea.validate_questions(qs) == []
    assert sum(len(q["must_facts"]) for q in qs) == 50
    assert sum(1 for q in qs if q["trap"]) == 4


def test_validate_questions_requires_evidence_per_fact():
    q = q_answerable()
    problems = ea.validate_questions([q])
    # the synthetic single-question list trips the 20-count rule only
    assert all("evidence" not in p for p in problems)
    q["evidence"] = []
    problems = ea.validate_questions([q])
    assert any("evidence" in p for p in problems)
    q["evidence"] = ["(", "("]  # wrong count AND bad regex
    problems = ea.validate_questions([q])
    assert any("evidence" in p and "exactly one regex" in p for p in problems)
    assert any("bad evidence regex" in p for p in problems)


def test_validate_questions_rejects_trap_flag_mismatch():
    q = q_answerable()
    q["trap"] = True
    assert any("trap flag" in p for p in ea.validate_questions([q]))


# --------------------------------------------------------------- check-facts


def test_check_facts_present_and_leak_paths(tmp_path, capsys):
    qs = [q_answerable(), q_trap()]
    packs = tmp_path / "packs"
    packs.mkdir()
    (packs / "ans-001.md").write_text(pack_for("The widget weighs 3 kg [1]."))
    (packs / "ans-017.md").write_text(pack_for("nothing about that here"))
    assert ea.check_facts_command(qs, packs) == 0
    out = capsys.readouterr().out
    assert out.count("PASS") == 4

    # answerable failure: fact missing from the body
    (packs / "ans-001.md").write_text(pack_for("no fact here"))
    assert ea.check_facts_command(qs, packs) == 1
    err = capsys.readouterr().err
    assert "FAILURE" in err and "ans-001 check 1" in err

    # trap failure: the leak pattern matches the body
    (packs / "ans-001.md").write_text(pack_for("The widget weighs 3 kg [1]."))
    (packs / "ans-017.md").write_text(pack_for("use the export results menu"))
    assert ea.check_facts_command(qs, packs) == 1
    err = capsys.readouterr().err
    assert "ans-017 check 2" in err


def test_check_facts_header_echo_is_never_evidence(tmp_path, capsys):
    """The question text in `# query:` can't satisfy an evidence regex (F1)."""
    q = q_answerable(must=["The answer mentions zebra Force Nine"],
                     evidence=[r"zebra Force Nine"])
    packs = tmp_path / "packs"
    packs.mkdir()
    # body empty; the phrase appears ONLY in the header query echo
    (packs / "ans-001.md").write_text(pack_for("").replace(
        "# query: what is the answer", "# query: zebra Force Nine"))
    assert ea.check_facts_command([q], packs) == 1


# ------------------------------------------------------------ grader parsing


def test_parse_grader_output_valid():
    obj = ea.parse_grader_output(
        '{"checks": [{"id": 1, "pass": true, "quote": "3 kg"}], '
        '"score": 2, "reason": "all good"}', "ans-001")
    assert obj["score"] == 2


def test_parse_grader_output_rejects_garbage():
    with pytest.raises(ValueError):
        ea.parse_grader_output("I think the answer is good, score 2", "ans-001")
    with pytest.raises(ValueError):
        ea.parse_grader_output('{"checks": [], "score": 1, "reason": "x"}', "ans-001")
    with pytest.raises(ValueError):
        ea.parse_grader_output(
            '{"checks": [{"id": 1, "pass": true}], "score": 3, "reason": "x"}',
            "ans-001")


def test_grader_prompt_slots_and_trap_adjudications():
    q = q_trap()
    q["grader_note"] = "ignore the stale diagram"
    template = ("QUESTION: <QUESTION>\n<GRADER_NOTE>\nCHECKS:\n<CHECKS>\n"
                "ANSWER:\n<ANSWER>\n")
    prompt = ea.build_grader_prompt(q, "the reply", template)
    assert "how do I export" in prompt
    assert "NOTE: ignore the stale diagram" in prompt
    assert "TRAP — the material is known not to cover this question." in prompt
    assert "Adjudication: piping stdout is fine when labeled" in prompt
    assert "<CHECKS>" not in prompt and "<ANSWER>" not in prompt
    answerable = q_answerable()
    prompt = ea.build_grader_prompt(answerable, "r", template)
    assert "1. The answer states the widget weighs 3 kg" in prompt
    assert "TRAP" not in prompt


# ------------------------------------------------------- threshold derivation


def test_binom_tail_ge_known_values():
    """Historical node ID: binomial model must no longer imply gate guarantees."""
    stats = compute_score_variance({"q": [2, 2, 2, 2]})
    assert stats["p_score"] == 0
    assert stats["false_positive_bound"] is None
    assert not hasattr(ea, "_binom_tail_ge")


def test_derive_threshold_low_waver_keeps_010(tmp_path):
    """The .10 default comes from preregistration, never a zero-flip estimate."""
    run = make_run(tmp_path / "run")
    result = load_run(run, allow_fixture=True)
    assert result.preregistration["practical_drop_threshold"] == 0.10
    assert "threshold" not in compute_score_variance({"q": [2, 2, 2, 2]})


def test_derive_threshold_raises_when_waver_too_high(tmp_path):
    """Historical node ID: high variation may not raise the registered wire."""
    run = make_run(tmp_path / "run", score_for=lambda q, a, j: 0 if a == "a2" else 2)
    result = load_run(run, allow_fixture=True)
    assert result.preregistration["practical_drop_threshold"] == 0.10
    assert compute_score_variance(result.scores)["calibration_status"] == "uncalibrated"


# ----------------------------------------------------------------- the gate


def _write_baseline(path: Path, **over):
    base = json.loads((REPO_ROOT / "evals/answer_baseline.json").read_text())
    base.update({
        "date": "2026-10-19",
        "models": {"answerer": "GLM-test", "judge": "GLM-test"},
        "pack_settings": {**ea.PACK_SETTINGS, "token_mode": "tokenize"},
        "index": {"fingerprint_id": "fp1", "model_id": "m", "model_sha256": "s",
                  "dim": 768, "chunker_version": "chunk.v2-tok"},
        "questions_sha256": ea.sha256_file(ea.QUESTIONS_PATH),
        "variance": {"answerer_flip_rate": 0.0, "grader_flip_rate": 0.0,
                     "q_effective": 0.0, "n_double_answered": 20,
                     "n_double_graded": 12},
        "mean_score": 2.0,
        "honest_refusal_rate": {"value": 1.0, "n": 4},
        "per_question": {},
        "gate": {"threshold": 0.1, "derived": True,
                 "rule": "mean answerable-score drop > threshold vs baseline -> exit 1",
                 "n_answerable": 16, "trip_p_max": 0.05},
    })
    base.update(over)
    path.write_text(json.dumps(base, indent=1) + "\n")


def _write_run(tmp: Path, scores_by_qid, answer_sha="a" * 64, pack_sha="p" * 64):
    """A complete answers/ + grades/ + packs/ + answers/run.json fixture."""
    qs = ea.load_questions()
    (tmp / "answers").mkdir(exist_ok=True)
    (tmp / "grades").mkdir(exist_ok=True)
    (tmp / "packs").mkdir(exist_ok=True)
    (tmp / "answers" / "run.json").write_text(
        json.dumps({"answerer": "GLM-test"}) + "\n")
    for q in qs:
        (tmp / "answers" / f"{q['id']}.md").write_text(f"answer {q['id']}\n")
        score = scores_by_qid.get(q["id"], 2)
        checks = [{"id": i, "pass": True, "quote": "q"} for i in
                  range(1, len(q["must_facts"]) + 1)]
        (tmp / "grades" / f"{q['id']}.json").write_text(json.dumps({
            "qid": q["id"], "score": score, "checks": checks,
            "reason": "ok", "answer_sha256": answer_sha,
            "grader_prompt_sha256": "g" * 64, "judge_model": "GLM-test",
        }) + "\n")
        (tmp / "packs" / f"{q['id']}.md").write_text(f"pack {q['id']}\n")


def _gate_args(tmp: Path, baseline: Path) -> argparse.Namespace:
    return argparse.Namespace(
        baseline=str(baseline), out=str(tmp), db="/nonexistent.sqlite",
        backend="mock",
    )


@pytest.fixture
def patched_index(tmp_path, monkeypatch):
    """Legacy input must be rejected before any model/index probing."""
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy gate attempted model/index access")
    monkeypatch.setattr(ea, "open_index", forbidden)
    yield


def test_gate_empty_baseline_refuses(tmp_path, patched_index):
    baseline = tmp_path / "b.json"
    _write_baseline(baseline)  # date set, but per_question empty
    assert ea.gate(_gate_args(tmp_path, baseline)) == 2


def test_gate_fingerprint_invalidation_exit2(tmp_path, patched_index):
    baseline = tmp_path / "b.json"
    _write_baseline(baseline)
    _write_run(tmp_path, {})
    # tamper one fingerprint field
    b = json.loads(baseline.read_text())
    b["index"]["chunker_version"] = "chunk.v1"
    baseline.write_text(json.dumps(b))
    assert ea.gate(_gate_args(tmp_path, baseline)) == 2


def test_gate_models_invalidation_exit2(tmp_path, patched_index):
    baseline = tmp_path / "b.json"
    _write_baseline(baseline)
    _write_run(tmp_path, {}, answer_sha="a" * 64)
    b = json.loads(baseline.read_text())
    b["models"]["answerer"] = "some-other-model"
    baseline.write_text(json.dumps(b))
    assert ea.gate(_gate_args(tmp_path, baseline)) == 2


def test_gate_pack_sha_drift_exit2(tmp_path, patched_index):
    baseline = tmp_path / "b.json"
    _write_baseline(baseline)
    _write_run(tmp_path, {}, pack_sha="stale" + "p" * 60)
    b = json.loads(baseline.read_text())
    for qid in b["per_question"] or []:
        pass
    # baseline per_question must pin the STALE pack shas (what --update-baseline recorded)
    qs = ea.load_questions()
    b["per_question"] = {
        q["id"]: {"score": 2, "answer_sha256": ea.sha256_bytes(f"answer {q['id']}\n".encode()),
                  "pack_sha256": ea.sha256_bytes(b"drifted-pack")}
        for q in qs
    }
    baseline.write_text(json.dumps(b))
    rc, err = capsys_err(lambda: ea.gate(_gate_args(tmp_path, baseline)))
    assert rc == 2 and "NOT_COMPARABLE" in err


def capsys_err(fn):
    import contextlib
    import io

    err, out = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        rc = fn()
    return rc, err.getvalue() + out.getvalue()


def test_gate_answer_drift_over_two_exit2(tmp_path, patched_index):
    baseline = tmp_path / "b.json"
    _write_baseline(baseline)
    _write_run(tmp_path, {}, answer_sha="fresh" + "a" * 59)
    qs = ea.load_questions()
    b = json.loads(baseline.read_text())
    b["per_question"] = {
        q["id"]: {"score": 2, "answer_sha256": "old" + "a" * 61,
                  "pack_sha256": ea.sha256_bytes(f"pack {q['id']}\n".encode())}
        for q in qs
    }
    baseline.write_text(json.dumps(b))
    rc, err = capsys_err(lambda: ea.gate(_gate_args(tmp_path, baseline)))
    assert rc == 2 and "NOT_COMPARABLE" in err


def test_gate_two_answer_drifts_allowed_and_atom_wire_trips(tmp_path, patched_index):
    """Historical node ID: v3 uses complete independent jobs and exact .10 wire."""
    base_run = make_run(tmp_path / "base", run_id="base", mode="real")
    baseline = tmp_path / "baseline.json"
    write_baseline(base_run, baseline, yes=True)
    candidate = make_run(tmp_path / "candidate", run_id="candidate", mode="real",
                         score_for=lambda q, a, j: 1 if int(q["id"][2:]) < 7 else 2)
    rc, output = capsys_err(lambda: ea.gate(_gate_args(candidate, baseline)))
    assert rc == 1 and "mean drop" in output
    # Six one-point losses among 60 equals .10 and must not fail by rounding.
    for a in ("a1", "a2"):
        for j in ("j1", "j2"):
            edit(candidate / f"grades_{a}_{j}/q-006.json", lambda g: g.update(score=2))
    rc, output = capsys_err(lambda: ea.gate(_gate_args(candidate, baseline)))
    assert rc == 0 and "PASS" in output


def test_trap_refusal_rate_reported_not_gating(tmp_path, patched_index):
    """Historical node ID: the reviewed contract REQUIRES a separate trap gate."""
    base_run = make_run(tmp_path / "base", run_id="base", mode="real")
    baseline = tmp_path / "baseline.json"
    write_baseline(base_run, baseline, yes=True)
    candidate = make_run(tmp_path / "candidate", run_id="candidate", mode="real")
    for i in range(60, 80):
        edit(candidate / f"grades_a1_j1/q-{i:03d}.json", lambda g: g.update(refused=False))
    rc, output = capsys_err(lambda: ea.gate(_gate_args(candidate, baseline)))
    assert rc == 1 and "trap refusal 0/20" in output


# ---------------------------------------------------------- update-baseline


def test_update_baseline_requires_variance(tmp_path, monkeypatch):
    out = tmp_path / "answer"
    (out / "answers").mkdir(parents=True)
    (out / "grades").mkdir(parents=True)
    (out / "packs").mkdir(parents=True)
    qs = ea.load_questions()
    monkeypatch.setattr(ea, "QUESTIONS_PATH", REPO_ROOT / "evals/answer/questions.jsonl")
    args = argparse.Namespace(out=str(out), db="/nonexistent", yes=True,
                              judge_model="GLM-test", date="2026-10-19",
                              baseline=str(tmp_path / "b.json"))
    rc = ea.update_baseline(args)
    assert rc == 2  # incomplete run refused


def test_derive_threshold_matches_reviewer_math():
    """The original math ignored shared run shocks; it is no longer gate code."""
    stats = compute_score_variance({f"q{i}": [2, 2, 2, 2] for i in range(60)})
    assert stats["calibration_status"] == "uncalibrated"
    assert stats["false_positive_bound"] is None
    assert not hasattr(ea, "derive_threshold")
