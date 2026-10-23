"""Stored evidence mechanics; supplied semantic scores are not model results."""
from __future__ import annotations

from dataclasses import replace

import pytest

from goldmine.lexical import ExactMatch, LexicalResult
from goldmine.retrieval_evidence import select_evidence
from goldmine.search import Searcher
from goldmine.store import FileRec, SegmentRec, Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "selection.sqlite")
    yield value
    value.close()


def source(store, *, title=None, filename="neutral.md", texts=None):
    fid = store.upsert_file(FileRec(path="/synthetic/" + filename, state="done", capability="content_indexed",
        info={"title": title} if title else None))
    store.replace_segments(fid, [SegmentRec(kind="name", modality="none", text_for_fts="owned folder")] + [
        SegmentRec(kind="chunk", modality="none", loc=20 * i + 1, loc_end=20 * i + 8, text_for_fts=text)
        for i, text in enumerate(texts or ["alpha beta gamma delta boilerplate", "alpha central fact", "alpha other fact"])])
    return store.get_file(fid), [r[0] for r in store.db.execute("SELECT id FROM segments WHERE file=? ORDER BY id", (fid,))]


def lexical(file, pairs, *, exact=None):
    return LexicalResult(segment_units={sid: frozenset(units) for sid, units in pairs},
        segment_files={sid: file.id for sid, _ in pairs}, exact={file.id: exact} if exact else {})


def test_semantic_primary_and_second_survive_duplicate_lexical_units(store):
    file, (name, boiler, best, other) = source(store)
    words = lexical(file, [(boiler, {0, 1, 2, 3}), (best, {0}), (other, {0})])
    selected = select_evidence(store, file, best, words,
        [(best, 6., .85), (other, 5., .82), (boiler, 3., .7)], query_text="alpha request")
    assert selected.primary == best
    assert selected.supporting == (other, boiler)
    assert selected.evidence_type == "content" and name not in selected.supporting


def test_lexical_and_semantic_ties_are_stable_and_support_has_two_slots(store):
    file, (_, first, second, third) = source(store)
    words = lexical(file, [(first, {0}), (second, {0}), (third, {1})])
    selected = select_evidence(store, file, second, words,
        [(second, 4., .8), (first, 4., .8), (third, 3., .7)])
    assert selected.primary == first
    assert selected.supporting == (second, third)
    assert len(set((selected.primary, *selected.supporting))) == 3


def test_body_exact_beats_different_page_semantic_peak(store):
    file, (_, actual, other, _) = source(store)
    exact = ExactMatch(1, actual, "body", "Q7-L4", "raw", "Q7-L4")
    words = lexical(file, [(actual, {0}), (other, {1})], exact=exact)
    selected = select_evidence(store, file, other, words, [(other, 6., .8)], query_text="Q7-L4")
    assert selected.primary == actual and selected.evidence_type == "content"
    assert selected.supporting == (other,)


@pytest.mark.parametrize("lookup_source", ["title", "basename", "path"])
def test_actual_name_row_never_claims_mixed_request_body(store, lookup_source):
    file, (name, body, _, _) = source(store, title="A9-Z8")
    exact = ExactMatch(1, name, lookup_source, "A9-Z8", "raw", "A9-Z8")
    words = lexical(file, [(body, {0, 1})], exact=exact)
    mixed = select_evidence(store, file, body, words, [(body, 6., .8)], query_text="A9-Z8 retained values")
    assert mixed.primary == body and mixed.evidence_type == "content"
    pure = select_evidence(store, file, body, words, [(body, 6., .8)], query_text="Ａ９－Ｚ８")
    assert pure.primary == name and pure.evidence_type == "name_metadata"
    assert pure.supporting == ()


def test_real_title_derived_row_is_metadata_and_mixed_query_selects_actual_body(store):
    file, ids = source(store, title="A9-Z8", texts=["retained values remain in the stored body"])
    title_row = store.db.execute("SELECT segment_id FROM lookup_terms WHERE source='title' AND normalized_term='a9-z8'").fetchone()
    assert title_row == (ids[0],)
    pure = Searcher(store, None).search("A9-Z8", scorer="lexical")[0]
    mixed = Searcher(store, None).search("A9-Z8 retained values", scorer="lexical")[0]
    assert pure.segment == "name" and pure.loc is None
    assert mixed.segment_id == ids[1] and mixed.loc == 1 and mixed.evidence_type == "content"


def test_standalone_filename_keeps_metadata_and_mixed_filename_has_real_body(store):
    file, ids = source(store, filename="manual-G7Y9.md", texts=["retained values remain in the stored body"])
    pure = Searcher(store, None).search("manual-G7Y9.md", scorer="lexical")[0]
    mixed = Searcher(store, None).search("manual-G7Y9.md retained values", scorer="lexical")[0]
    assert pure.segment_id == ids[0] and pure.evidence_type == "name_metadata"
    assert mixed.segment_id == ids[1] and mixed.evidence_type == "content"


def test_no_semantic_qualifier_preserves_lexical_fallback_and_no_fake_support(store):
    file, (_, first, second, third) = source(store)
    words = lexical(file, [(first, {0, 1}), (second, {0}), (third, {0})])
    selected = select_evidence(store, file, second, words, [(second, 2.99, .7), (third, float("nan"), .7)])
    assert selected.primary == first and selected.supporting == ()


def test_empty_body_cannot_displace_quotable_semantic_content(store):
    file, (_, empty, body) = source(store, texts=["", "complete visible source fact"])
    words = lexical(file, [])
    selected = select_evidence(store, file, empty, words, [(empty, 8., .9), (body, 4., .7)])
    assert selected.primary == body and selected.supporting == ()
    # Explicit semantic ablation preserves its own primary contract.
    semantic = select_evidence(store, file, empty, words, [(empty, 8., .9), (body, 4., .7)], semantic_only=True)
    assert semantic.primary == empty


def test_exact_remainder_noise_veto_is_not_removed_by_selection_change(store):
    source(store, texts=["refund AB-12 approved"])
    hits = Searcher(store, None).search("randomzz AB-12", scorer="lexical", why=True)
    # anchor.v2-intent: ambient _CODE anchors in prose lose the all-words
    # veto; the anchor file returns via ordinary quorum (7694837 reference).
    assert hits and hits[0].admission_reason == "lexical_quorum"
    assert "exact_source" not in hits[0].why and "anchor_contract_soft" in hits[0].why


def test_file_admission_order_and_scores_do_not_depend_on_evidence_selector(store, monkeypatch):
    from goldmine import search as module
    from goldmine.retrieval_evidence import EvidenceSelection
    first, _ = source(store, filename="first.md", texts=["alpha beta one", "alpha two"])
    second, _ = source(store, filename="second.md", texts=["alpha beta three"])
    searcher = Searcher(store, None)
    before = searcher.search("alpha beta", scorer="lexical")
    monkeypatch.setattr(module, "select_evidence", lambda store, file, fallback, *args, **kwargs: EvidenceSelection(fallback))
    fallback = searcher.search("alpha beta", scorer="lexical")
    features = lambda rows: [(r.path, r.score, r.z, r.cosine, r.coverage, r.admission_reason) for r in rows]
    assert len(before) == len(fallback) == 2 and features(before) == features(fallback)


@pytest.mark.parametrize("fallback_index", [1, 2, 3])
def test_pure_semantic_keeps_fallback_without_new_supports_while_hybrid_protects_support(store, fallback_index):
    file, ids = source(store)
    name, boiler, best, other = ids
    words = lexical(file, [(boiler, {0, 1, 2, 3}), (best, {0}), (other, {0})])
    semantic = [(best, 6., .85), (other, 5., .82), (boiler, 4., .75)]
    assert len(semantic) == 3 and all(z >= 3 for _, z, _ in semantic)
    selected = select_evidence(store, file, ids[fallback_index], words, semantic,
                               semantic_only=True, query_text="alpha request")
    assert selected.primary == ids[fallback_index]
    assert selected.supporting == ()
    assert selected.evidence_type == "content"
    from goldmine.citations import read_evidence
    assert read_evidence(store, selected.primary).text == store.segment_text(selected.primary)
    hybrid = select_evidence(store, file, ids[fallback_index], words, semantic, query_text="alpha request")
    assert hybrid.primary == best and hybrid.supporting == (other, boiler)
    assert name not in hybrid.supporting


@pytest.mark.parametrize("fallback_kind", ["name", "vector_only"])
def test_pure_semantic_name_or_actual_vector_only_fallback_keeps_type_and_no_support(store, fallback_kind):
    import numpy as np
    path = "/synthetic/ablation-type.png"
    fid = store.upsert_file(FileRec(path=path, kind="image", state="done", capability="content_indexed"))
    store.replace_segments(fid, [
        SegmentRec(kind="name", modality="none", text_for_fts="stored image name"),
        SegmentRec(kind="image", modality="image", text_for_fts=None, vec=np.eye(1, 768, dtype=np.float32)[0]),
        SegmentRec(kind="ocr", modality="none", loc=1, loc_end=1, text_for_fts="first genuine stored body"),
        SegmentRec(kind="ocr", modality="none", loc=2, loc_end=2, text_for_fts="second genuine stored body"),
        SegmentRec(kind="ocr", modality="none", loc=3, loc_end=3, text_for_fts="third genuine stored body"),
    ])
    file = store.get_file(fid)
    ids = [row[0] for row in store.db.execute("SELECT id FROM segments WHERE file=? ORDER BY id", (fid,))]
    fallback = ids[0] if fallback_kind == "name" else ids[1]
    if fallback_kind == "vector_only":
        assert store.db.execute("SELECT vec IS NOT NULL FROM segments WHERE id=?", (fallback,)).fetchone()[0]
        assert store.db.execute("SELECT rowid FROM fts WHERE rowid=?", (fallback,)).fetchone() is None
    semantic = [(ids[2], 6., .85), (ids[3], 5., .82), (ids[4], 4., .75)]
    selected = select_evidence(store, file, fallback, None, semantic, semantic_only=True)
    assert selected.primary == fallback and selected.supporting == ()
    assert selected.evidence_type == ("name_metadata" if fallback_kind == "name" else "vector_only")
    assert selected.media_evidence_type == ("name_metadata" if fallback_kind == "name" else "image_vector")
    from goldmine.citations import read_evidence
    stored = read_evidence(store, selected.primary)
    assert stored.citation.segment_id == fallback and stored.citation.evidence_type == selected.evidence_type
    assert stored.text == ("stored image name" if fallback_kind == "name" else None)
