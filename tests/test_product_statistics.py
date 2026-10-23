"""Paired estimates must use the same fixed metric denominators as summaries."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from answer_eval.protocol import EvaluationError
from product_eval.scoring import paired_changes, summarize


def rows():
    answer = {name: 0 for name in ("success@1", "success@5", "MRR", "nDCG@10", "all_required_facts@pack")}
    answer["noise_correct_empty"] = None
    noise = {"success@1": 0, "success@5": 0, "MRR": 0, "nDCG@10": None,
             "all_required_facts@pack": None, "noise_correct_empty": 0}
    return [{"id": "answer", "primary_class": "semantic", "source_group": "source:answer", "tags": [], "metrics": answer},
            *[{"id": f"noise-{i}", "primary_class": "noise", "source_group": f"noise:{i}", "tags": [], "metrics": dict(noise)} for i in range(30)]]


def test_thirty_noise_zeroes_do_not_dilute_one_answerable_paired_improvement():
    base = rows()
    candidate = copy.deepcopy(base)
    for name in ("success@1", "success@5", "MRR", "nDCG@10", "all_required_facts@pack"):
        candidate[0]["metrics"][name] = 1
    candidate[1]["metrics"]["noise_correct_empty"] = 1
    paired = paired_changes(base, candidate, reps=1000)
    for name in ("success@1", "success@5", "MRR", "nDCG@10", "all_required_facts@pack"):
        before, after = (summarize(data)["overall"][name] for data in (base, candidate))
        assert paired[name]["mean_delta"] == after["value"] - before["value"] == 1
        assert paired[name]["n_queries"] == after["n"] == before["n"] == 1
        assert paired[name]["n_source_groups"] == 1
    assert paired["noise_correct_empty"]["mean_delta"] == 1 / 30
    assert paired["noise_correct_empty"]["n_queries"] == 30


def test_paired_metric_classification_cannot_be_changed_after_scoring():
    base = rows()
    candidate = copy.deepcopy(base)
    candidate[0]["primary_class"] = "noise"
    with pytest.raises(EvaluationError, match="primary classes differ"):
        paired_changes(base, candidate, reps=1000)
