"""Admission reasons describe explicit evidence gates, never probability."""

import json
import hashlib
from pathlib import Path

import numpy as np
import pytest

from goldmine.admission import AdmissionParameters, SEARCH_BEHAVIOR_VERSION, decide_admission
from goldmine.search import Searcher, parse_filters
from goldmine.store import FileRec, SegmentRec, Store
from test_search_offline import FixedBackend, evec


@pytest.fixture
def index(tmp_path):
    store = Store(tmp_path / "admission.sqlite")
    yield store
    store.close()


def add(store, path, texts, vectors=None, kind="doc"):
    vectors = vectors or [None] * len(texts)
    fid = store.upsert_file(FileRec(path=path, kind=kind, state="done", capability="content_indexed"))
    store.replace_segments(fid, [SegmentRec(kind="name", modality="none", text_for_fts=path)] + [
        SegmentRec(kind="chunk", modality="text" if vec is not None else "none", loc=i + 1, loc_end=i + 1,
                   text_for_fts=text, vec=vec) for i, (text, vec) in enumerate(zip(texts, vectors))
    ])


def vector_with_cosine(cosine):
    vec = evec(1) * np.sqrt(1 - cosine ** 2)
    vec[0] = cosine
    return vec


def test_highest_unrelated_cosine_does_not_itself_admit(index):
    add(index, "/docs/one.txt", ["unrelated apple topic"], [vector_with_cosine(0.005)])
    for i in range(25):
        add(index, f"/docs/fill-{i}.txt", ["unrelated filler"], [evec(1)])
    assert Searcher(index, FixedBackend(evec(0))).search("spacecraftx", why=True) == []


def test_many_weak_segments_cannot_stack_into_quorum(index):
    add(index, "/docs/long.txt", ["unrelated filler"] * 150,
        [vector_with_cosine(float(x)) for x in np.linspace(0, 0.005, 150)])
    assert Searcher(index, FixedBackend(evec(0))).search("spacecraftx", why=True) == []


def test_exact_anchor_and_random_remaining_word_are_not_equivalent(index):
    add(index, "/docs/source.txt", ["refund LC-8492 approved"])
    hit = Searcher(index, None).search("LC-8492", why=True)[0]
    assert hit.admission_reason == hit.why["admission_reason"] == "verified_exact"
    assert hit.why["search_behavior_version"] == SEARCH_BEHAVIOR_VERSION
    # anchor.v2-intent: the ambient _CODE anchor in prose no longer vetoes the
    # random word; the file returns via ordinary quorum with soft disclosure (7694837).
    mixed = Searcher(index, None).search("nonsensezz LC-8492", why=True)
    assert mixed and mixed[0].admission_reason == "lexical_quorum"
    assert "exact_source" not in mixed[0].why and "anchor_contract_soft" in mixed[0].why


def test_vectorless_lexical_quorum_and_filtered_empty_results(index):
    add(index, "/docs/source.txt", ["beacon harbor approved"])
    hit = Searcher(index, None).search("beacon harbor", why=True)[0]
    assert hit.admission_reason == "lexical_quorum" and hit.z is None
    query, filters = parse_filters("beacon under:/other/path")
    assert Searcher(index, None).search(query, filters=filters) == []


def test_cjk_noise_empty_and_real_cjk_body_eligible(index):
    add(index, "/docs/source.txt", ["中文知识库管理"])
    assert Searcher(index, None).search("宇宙飞船维修协议") == []
    assert Searcher(index, None).search("知识库管理")[0].admission_reason == "lexical_quorum"


def test_one_modality_tiny_group_remains_finite_and_gated(index):
    add(index, "/docs/source.txt", ["unrelated"], [evec(0)])
    assert Searcher(index, FixedBackend(evec(0))).search("nothingmatched") == []
    assert decide_admission(scorer="semantic", z=float("nan")) is None
    assert decide_admission(scorer="semantic", z=float("inf")) is None


def test_optional_margin_is_explicit_and_never_changes_control():
    args = dict(scorer="hybrid", z=4.0, coverage=0, segment_margin=0.01)
    assert decide_admission(**args) == "semantic_standout"
    assert decide_admission(**args, parameters=AdmissionParameters(semantic_segment_margin=0.25)) is None
    assert decide_admission(**{**args, "segment_margin": 0.3}, parameters=AdmissionParameters(semantic_segment_margin=0.25)) == "semantic_standout"
    assert decide_admission(**args, exact=True, parameters=AdmissionParameters(semantic_segment_margin=0.25)) == "verified_exact"


def test_baseline_ablation_rules_retained():
    assert decide_admission(scorer="lexical", lexical_match=True, coverage=0.1) == "lexical_quorum"
    assert decide_admission(scorer="hybrid", lexical_match=True, coverage=0.1) is None
    assert decide_admission(scorer="rrf", lexical_match=True, coverage=0.1) == "lexical_quorum"
    assert decide_admission(scorer="semantic", exact=True, z=0) is None


def test_candidate_registration_is_finite_and_does_not_claim_calibration():
    registered = json.loads((Path(__file__).parents[1] / "evals/search_parameters.json").read_text())
    # Historical numeric trials retain their original v3 bytes/status.
    assert registered["search_behavior_version"] == "search.v3-evidence-control"
    structural = json.loads((Path(__file__).parents[1] / "evals/anchor_algorithm.json").read_text())
    assert structural["search_behavior_version"] == SEARCH_BEHAVIOR_VERSION == "search.v5-anchor-intent"
    assert structural["algorithm_version"] == "anchor.v2-intent"
    assert structural["status"] == "UNVALIDATED" and structural["quality_results"] is None
    assert structural["semantic_remainder"] == "not_implemented"
    # The v1 registration is preserved verbatim as immutable history.
    historical_entry = structural["history"][0]
    assert historical_entry["algorithm_version"] == "anchor.v1-lexical"
    assert historical_entry["search_behavior_version"] == "search.v4-anchor-contract"
    assert historical_entry["parameter_registration"]["sha256"] == \
        hashlib.sha256((Path(__file__).parents[1] / "evals/search_parameters.json").read_bytes()).hexdigest()
    registration = structural["parameter_registration"]
    assert registration["sha256"] == hashlib.sha256((Path(__file__).parents[1] / registration["path"]).read_bytes()).hexdigest()
    assert registration["historical_search_behavior_version"] == "search.v4-anchor-contract"
    assert not structural["noise_target_attainment_claim"]
    assert 1 <= len(registered["candidates"]) <= 3
    assert registered["default"] == "control"
    control = next(c for c in registered["candidates"] if c["id"] == "control")
    assert control["z_gate"] == 3.0 and control["coverage_quorum"] == 0.5
    assert control["semantic_segment_margin"] is None
    assert registered["selected"] is None and registered["status"] in ("MISSING", "FAIL")
    assert "holdout" in registered["selection"]
    if registered["status"] == "FAIL":
        assert registered["failed"] and registered["selection_evidence"]["holdout_exposures"] == 0
    else:
        assert registered["missing"]


@pytest.mark.parametrize("values", [{"coverage_quorum": float("nan")}, {"coverage_quorum": -1},
                                    {"semantic_segment_margin": -0.1}, {"semantic_segment_margin": float("inf")}])
def test_invalid_experimental_parameters_rejected(values):
    with pytest.raises(ValueError):
        AdmissionParameters(**values)
