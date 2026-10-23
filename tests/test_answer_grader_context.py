"""Mechanical prompt/provenance tests; synthetic records, no model scores."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "scripts"),
               str(Path(__file__).resolve().parent)]
from answer_eval.protocol import (EvaluationError, answer_request, grader_request,
                                  load_run, sha256_file)
from answer_eval_fixtures import dump, make_run

TEMPLATE = "Question: <QUESTION>\nFacts: <FACTS_JSON>\nActual material: <PACK>\nAnswer: <ANSWER>\n"
QUESTION = {"q": "What does the material establish?", "class": "single",
            "expect": [], "required_facts": []}


def test_actual_pack_is_present_and_changes_the_judge_input():
    first = grader_request(TEMPLATE, QUESTION, "An unchanged answer.", "Source A: one.")
    second = grader_request(TEMPLATE, QUESTION, "An unchanged answer.", "Source B: two.")
    assert "Source A: one." in first and "Source B: two." in second
    assert first != second


@pytest.mark.parametrize("slot", ["<QUESTION>", "<FACTS_JSON>", "<PACK>", "<ANSWER>"])
@pytest.mark.parametrize("count", [0, 2])
def test_every_grader_slot_is_required_exactly_once(slot, count):
    template = TEMPLATE.replace(slot, slot * count)
    with pytest.raises(EvaluationError, match="PACK"):
        grader_request(template, QUESTION, "Answer.", "Material.")


@pytest.mark.parametrize("pack", ["", "中文 [1] material\n", "<QUESTION> <FACTS_JSON> <PACK> <ANSWER>"])
def test_empty_unicode_and_placeholder_like_material_are_literal(pack):
    q = deepcopy(QUESTION); q["q"] = "Literal <PACK> in the source question?"
    answer = "Literal <FACTS_JSON> in the answer."
    rendered = grader_request(TEMPLATE, q, answer, pack)
    assert q["q"] in rendered and answer in rendered
    assert "Actual material: " + pack + "\nAnswer:" in rendered


def test_non_text_pack_is_not_silently_rendered_as_missing_context():
    with pytest.raises(EvaluationError, match="must be text"):
        grader_request(TEMPLATE, QUESTION, "Answer.", None)


def test_rebinding_pack_metadata_cannot_reuse_a_stale_judge_request(tmp_path):
    # All records here are explicitly synthetic, not an actual answer/judge run.
    run = make_run(tmp_path / "fixture")
    q = json.loads((run / "questions.jsonl").read_text().splitlines()[0])
    pack_path = run / "packs" / (q["id"] + ".md")
    pack_path.write_text("[1] Different actual supplied material.\n")
    pack_sha = sha256_file(pack_path)
    template = (run / "answerer.txt").read_text()
    for a in ("a1", "a2"):
        request = run / ("requests_" + a) / (q["id"] + ".txt")
        request.write_text(answer_request(template, q, pack_path.read_text()))
        answer_path = run / ("answers_" + a) / (q["id"] + ".json")
        answer = json.loads(answer_path.read_text())
        answer.update(pack_sha256=pack_sha, request_sha256=sha256_file(request))
        dump(answer_path, answer)
        for j in ("j1", "j2"):
            path = run / ("grades_" + a + "_" + j) / (q["id"] + ".json")
            grade = json.loads(path.read_text()); grade["pack_sha256"] = pack_sha
            dump(path, grade)
    with pytest.raises(EvaluationError, match="grader request is not"):
        load_run(run, allow_fixture=True)
