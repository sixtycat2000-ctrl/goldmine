"""Thin tests for scripts/eval_distill.py (v2.1-r6 §7.5): the canary gates are
CI-ENFORCED on the mock backend, not just script-honored.

The heavyweight variant (main/hard) builds + ingests the 100-doc fixture —
module-scoped so one run serves several assertions. Variant `hard` is F1: the
real offline gate (v1 rules FAIL, v2 PASS on the filler-heavy fixture).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_distill.py"
_spec = importlib.util.spec_from_file_location("eval_distill", _SCRIPT)
eval_distill = importlib.util.module_from_spec(_spec)
sys.modules["eval_distill"] = eval_distill  # dataclasses resolves __module__
_spec.loader.exec_module(eval_distill)


@pytest.fixture(scope="module")
def main_home(tmp_path_factory):
    home = tmp_path_factory.mktemp("gm-eval-distill-main")
    report, ok = eval_distill.variant_main("mock", home)
    return report, ok


def test_fixture_shape_and_determinism():
    """F9: 100 = 8x10 planted + 12 noise + 8 twins; two builds byte-equal."""
    docs = eval_distill.build_corpus("main")
    assert len(docs) == 100
    topics = [d.topic for d in docs if d.topic]
    # 80 planted + 8 twins (twins carry their original's topic label)
    assert len(topics) == 88 and len(set(topics)) == 8
    noise = [d for d in docs if d.topic is None]
    assert len(noise) == 12
    twins = [d for d in docs if d.doc_id.endswith("-twin")]
    assert len(twins) == 8
    again = eval_distill.build_corpus("main")
    assert [d.text for d in docs] == [d.text for d in again]
    hard = eval_distill.build_corpus("hard")
    assert len(hard) == 100
    assert FILLER_SHARED in hard[0].text


FILLER_SHARED = "the general introduction of this document"


def test_ari_hand_case():
    """Contingency-table ARI against a hand-computed value."""
    # perfect agreement
    assert eval_distill._adjusted_rand_index(
        ["a", "a", "b", "b"], [1, 1, 2, 2],
    ) == pytest.approx(1.0)
    # one discordant pair of four: exactly chance level -> ARI = 0
    ari = eval_distill._adjusted_rand_index(["a", "a", "b", "b"], [1, 1, 1, 2])
    assert ari == pytest.approx(0.0)
    # fully anti-correlated -> negative
    assert eval_distill._adjusted_rand_index(
        ["a", "a", "b", "b"], [1, 2, 1, 2],
    ) == pytest.approx(-0.5)
    # the classic permutation-independent identity: ARI(x, x) = 1
    labels = ["a", "b", "a", "c", "c", "b"]
    assert eval_distill._adjusted_rand_index(labels, labels) == pytest.approx(1.0)


def test_purity_hand_case():
    """Purity = sum(size * dominant fraction) / sum(size), noise-labelled
    cards count their own dominant label."""

    class Card:
        status = "active"

        def __init__(self, members):
            self.members = members
            self.size = len(members)

    def item(sid, doc):
        from goldmine.distill import CItem
        import numpy as np

        return CItem(sid=sid, fid=1, vec=np.zeros(768, np.float32),
                     text="", path=f"/c/0000-{doc}.txt", kind="doc",
                     loc=1, loc_end=2)

    planted = {"alpha-doc": "alpha", "beta-doc": "beta"}
    kept = [item(1, "alpha-doc"), item(2, "alpha-doc"), item(3, "beta-doc")]
    cards = [Card([kept[0], kept[1], kept[2]])]  # 2 alpha + 1 beta in one card
    metrics = eval_distill.compute_metrics(cards, kept, planted)
    assert metrics["purity"] == pytest.approx(2 / 3, abs=1e-3)  # rounded 4dp


def test_main_variant_gates_pass_on_mock(main_home):
    report, ok = main_home
    assert ok is True
    assert all(report["gates"].values()), report["gates"]
    m = report["metrics"]
    assert m["purity"] >= 0.70 and m["ari"] >= 0.50
    assert m["recovery"] >= 6 and m["excerpt_fidelity"] == 1.0
    assert report["gates"]["determinism"] is True


@pytest.fixture(scope="module")
def hard_home(tmp_path_factory):
    home = tmp_path_factory.mktemp("gm-eval-distill-hard")
    report, ok = eval_distill.variant_hard("mock", home)
    return report, ok


def test_hard_variant_differential_is_the_real_gate(hard_home):
    """F1: v1 rules FAIL (one mega-card, purity ~0.12) and v2 rules PASS on
    the filler-heavy fixture — the algorithm, not the fixture, does the work."""
    report, ok = hard_home
    assert ok is True
    v1, v2 = report["v1_rules"], report["v2_rules"]
    assert v1["purity"] < 0.70 and v1["cards"] == 1      # v1 FAILS, as required
    assert v2["purity"] >= 0.70 and v2["ari"] >= 0.50    # v2 PASSES
    assert v2["recovery"] >= 6
    assert report["gates"]["v1_purity<0.70 (must FAIL)"] is True


def test_single_topic_variant_asserts_non_otsu(tmp_path_factory):
    """B9 normative assertion: the monothematic degenerate case never trusts
    the Otsu cut."""
    home = tmp_path_factory.mktemp("gm-eval-distill-single")
    report, ok = eval_distill.variant_single_topic("mock", home)
    assert ok is True
    assert report["gates"]["tau_source != otsu"] is True
    assert report["calibration"].startswith("tau ")


def test_micro_variant_runs_with_honest_stats(tmp_path_factory):
    home = tmp_path_factory.mktemp("gm-eval-distill-micro")
    report, ok = eval_distill.variant_micro("mock", home)
    assert ok is True
    stats = report["stats"]
    assert 2 <= stats["after_dedupe"] <= stats["corpus_chunks"]
    assert sum(stats["histogram"].values()) >= 1


def test_shuffle_variant_keeps_recovery(tmp_path_factory):
    """Order-robustness (06 §4.2): reversed ingest order, same gates."""
    home = tmp_path_factory.mktemp("gm-eval-distill-shuffle")
    report, ok = eval_distill.variant_shuffle("mock", home)
    assert ok is True
    assert report["metrics"]["recovery"] >= 6


def test_render_report_mentions_verdicts():
    report = {
        "variant": "hard",
        "backend": "mock",
        "calibration": "tau 0.8302 (raw 0.8302, source percentile)",
        "v1_rules": {"purity": 0.12, "cards": 1, "carded_chunks": 100,
                     "after_dedupe": 100},
        "v2_rules": {"purity": 0.88, "ari": 0.81, "recovery": 8,
                     "recovery_total": 8, "cards": 8},
        "gates": {"v1 must FAIL": True, "v2 must PASS": True},
    }
    text = eval_distill.render_report(report)
    assert "v1 rules (must FAIL)" in text
    assert "v2 rules (must PASS)" in text
    assert "[PASS]" in text
