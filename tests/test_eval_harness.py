"""Thin test for scripts/eval_relevance.py (TEST_PLAN §5).

Asserts the harness RUNS and produces all four scorer rows; the one quality
guardrail is hybrid R@5 >= 0.9 with the mock backend (lexical realism comes
from FTS, mock vectors are bag-of-token aligned by construction). Absolute
quality comparisons between scorers are REPORTED, never asserted.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_relevance.py"
_spec = importlib.util.spec_from_file_location("eval_relevance", _SCRIPT)
eval_relevance = importlib.util.module_from_spec(_spec)
sys.modules["eval_relevance"] = eval_relevance  # dataclasses resolves __module__
_spec.loader.exec_module(eval_relevance)


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    home = tmp_path_factory.mktemp("gm-eval")
    return eval_relevance.run_eval(home, backend_name="mock")


def test_all_four_scorer_rows_present(results):
    for scorer in ("hybrid", "lexical", "semantic", "rrf"):
        assert scorer in results
        assert set(results[scorer]["overall"]) == {"R@1", "R@5", "MRR"}
        assert "lookup" in results[scorer] and "non_lookup" in results[scorer]


def test_hybrid_r5_guardrail_with_mock(results):
    assert results["hybrid"]["overall"]["R@5"] >= 0.9


def test_zero_hit_query_is_honest_empty(results):
    # lexical must be empty (no term matches); semantic-gated scorers may
    # surface z-gate candidates by design -- no scorer may crash
    assert results["_zero_hit"]["hits"]["lexical"] == 0
    assert set(results["_zero_hit"]["hits"]) == {"hybrid", "lexical", "semantic", "rrf"}


def test_corpus_shape_deterministic():
    docs = eval_relevance.build_corpus()
    assert len(docs) == 35  # 30 topical + 3 near-dup twins + 2 cross-topic
    again = eval_relevance.build_corpus()
    assert [d.text for d in docs] == [d.text for d in again]
    lookup_docs = [d for d in docs if d.doc_id in eval_relevance.LOOKUP_TOKENS]
    assert len(lookup_docs) == 5


def test_render_table_mentions_all_scorers(results):
    table = eval_relevance.render_table(results)
    for scorer in ("hybrid", "lexical", "semantic", "rrf"):
        assert scorer in table
    assert "zero-hit" in table


@pytest.mark.eval
def test_clustering_sensitivity_reported(tmp_path):
    """τ sensitivity (DESIGN_DECISIONS §11): prints cluster counts; passes
    unless the pipeline errors. Quality judgments stay reported, not asserted.
    v2 (R6): the manual thresholds run through run_distill as
    tau_source='manual'; the calibrated run is the new baseline row."""
    from goldmine.backends.mock import MockBackend
    from goldmine.config import Config
    from goldmine.distill import Scope, run_distill
    from goldmine.ingest import IngestPipeline
    from goldmine.store import Store

    home = tmp_path / "sens"
    corpus_dir = home / "corpus"
    corpus_dir.mkdir(parents=True)
    for doc in eval_relevance.build_corpus():
        (corpus_dir / f"{doc.doc_id}.txt").write_text(doc.text, encoding="utf-8")
    cfg = Config(home=str(home))
    store = Store(home / "eval.sqlite")
    backend = MockBackend(dim=768)
    IngestPipeline(cfg, store, backend).run([str(corpus_dir)])
    counts = {}
    for tau in (0.60, 0.70, 0.80):
        result = run_distill(store, scope=Scope(), threshold=tau, min_members=3)
        counts[tau] = result["stats"]["clusters_kept"]
        assert result["stats"]["tau_source"] == "manual"
        print(f"tau={tau}: {result['stats']['clusters_kept']} clusters "
              f"(dropped {result['stats']['clusters_dropped_small']})")
    calibrated = run_distill(store, scope=Scope(), threshold=None, min_members=3)
    print(f"calibrated: tau={calibrated['stats']['tau']} "
          f"({calibrated['stats']['tau_source']}) -> "
          f"{calibrated['stats']['clusters_kept']} clusters")
    assert set(counts) == {0.60, 0.70, 0.80}
    store.close()
    backend.close()
