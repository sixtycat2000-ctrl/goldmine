"""Independent oracles for admission-preserving aggregate materialization.

These are hermetic mechanism cases, not product-eval questions. The scalar
oracle predates the pruning predicate: it visits every row and every file.
The frozen vectorized oracle also preserves the old nonfinite behavior, which
this optimization is deliberately not authorized to repair.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path

import numpy as np
import pytest

from goldmine.admission import AdmissionParameters, admit_files
from goldmine.application import Application
from goldmine.backends.base import Capability, EmbeddingBackend, NotSupported
from goldmine.contracts import SearchRequest
from goldmine.search import Searcher, parse_filters
from goldmine.store import FileRec, SegmentRec, Store


def _scalar_all_files(file_ids, zvals, scores, seg_ids):
    """Independent finite, first-row-max oracle; no gate/retention logic."""
    best = {}
    for file_id, z, score, sid in zip(file_ids, zvals, scores, seg_ids):
        file_id, z = int(file_id), float(z)
        if file_id not in best or z > best[file_id][0]:
            best[file_id] = z, float(score), int(sid)
    best = dict(sorted(best.items()))
    return best, {fid: row[0] for fid, row in best.items()}


def _frozen_all_files(file_ids, zvals, scores, seg_ids, **_ignored):
    """Unmodified 1cd2e9d aggregate, ignoring candidate-only keyword args."""
    if file_ids.size == 0:
        return {}, {}
    order = np.argsort(file_ids, kind="stable")
    fids = file_ids[order]
    z = np.asarray(zvals, dtype=np.float64)[order]
    starts = np.flatnonzero(np.r_[True, fids[1:] != fids[:-1]])
    max_z = np.maximum.reduceat(z, starts)
    sizes = np.diff(np.r_[starts, len(fids)])
    hit_rows = np.flatnonzero(z == np.repeat(max_z, sizes))
    firsts = np.r_[True, fids[hit_rows][1:] != fids[hit_rows][:-1]]
    best_rows = order[hit_rows[firsts]]
    group_fids = fids[starts]
    best = {
        int(f): (float(mz), float(sc), int(si))
        for f, mz, sc, si in zip(
            group_fids, max_z, np.asarray(scores)[best_rows], seg_ids[best_rows]
        )
    }
    return best, {int(f): float(mz) for f, mz in zip(group_fids, max_z)}


def _float_records(aggregate):
    """Compare nonfinite and signed-zero values without NaN != NaN noise."""
    best, maxima = aggregate
    return (
        [(fid, float(z).hex(), float(score).hex(), sid)
         for fid, (z, score, sid) in best.items()],
        [(fid, float(z).hex()) for fid, z in maxima.items()],
    )


def test_default_aggregate_is_complete_and_keeps_original_first_maximum():
    # The smallest segment ID is intentionally not the first maximum row.
    files = np.array([2**60 + 9, 7, 7, 31, 7, 2**60 + 9], dtype=np.int64)
    z = np.array([-2.0, 3.0, 3.0, -4.0, 2.0, -2.0])
    scores = np.array([.1, .2, .9, .4, .5, .6], dtype=np.float32)
    ids = np.array([90, 80, 1, 30, 40, 2], dtype=np.int64)
    expected = _scalar_all_files(files, z, scores, ids)
    assert Searcher._aggregate_by_file(files, z, scores, ids) == expected
    assert expected[0][7][2] == 80
    assert expected[0][2**60 + 9][2] == 90
    assert set(expected[0]) == {7, 31, 2**60 + 9}


def test_pruning_keeps_boundary_files_and_full_subthreshold_retained_records():
    files = np.array([91, 11, 31, 71, 51, 11, 91], dtype=np.int64)
    z = np.array([np.nextafter(3., -np.inf), -7., 3., np.nextafter(3., np.inf),
                  -4., -7., np.nextafter(3., -np.inf)])
    scores = np.array([.19, .11, .31, .71, .51, .99, .98])
    ids = np.array([90, 80, 30, 70, 50, 1, 2], dtype=np.int64)
    full, full_z = _scalar_all_files(files, z, scores, ids)
    actual, actual_z = Searcher._aggregate_by_file(
        files, z, scores, ids, minimum_z=3., retain_files={11, 2**61 + 1},
    )
    assert set(actual) == {11, 31, 71}
    assert actual == {fid: full[fid] for fid in (11, 31, 71)}
    assert actual_z == {fid: full_z[fid] for fid in (11, 31, 71)}
    assert actual[11] == (-7., .11, 80)


def test_one_file_cannot_crowd_another_admissible_file_out_of_a_global_row_window():
    files = np.r_[np.full(200, 7, dtype=np.int64), np.array([31], dtype=np.int64)]
    z = np.r_[np.linspace(4., 9., 200), 3.]
    scores = z / 10.
    ids = np.arange(1, len(files) + 1, dtype=np.int64)
    expected = _scalar_all_files(files, z, scores, ids)
    actual = Searcher._aggregate_by_file(files, z, scores, ids, minimum_z=3.)
    assert actual == expected
    assert actual[0][31] == (3., .3, 201)


def test_empty_vectors_do_not_invent_records_from_lexical_file_ids():
    ids = np.array([], dtype=np.int64)
    values = np.array([], dtype=np.float64)
    assert Searcher._aggregate_by_file(ids, values, values, ids,
                                      minimum_z=-3., retain_files={1, 99}) == ({}, {})


@pytest.mark.parametrize("scorer", ["hybrid", "rrf", "semantic", "lexical"])
@pytest.mark.parametrize("quorum", [0., .5, 1.])
@pytest.mark.parametrize("margin", [None, .5])
def test_randomized_complete_admission_and_records_match_scalar_oracle(scorer, quorum, margin):
    rng = np.random.default_rng(20261104)
    parameters = AdmissionParameters(coverage_quorum=quorum, semantic_segment_margin=margin)
    gates = (-3., 0., 1., 3., 9.)
    levels = np.array([-8., -3., -1., 0., 1., np.nextafter(3., -np.inf),
                       3., np.nextafter(3., np.inf), 4., 10.])
    for case in range(35):
        n_files = int(rng.integers(1, 24))
        universe = np.array([2**59 + i * 1009 for i in range(n_files + 2)], dtype=np.int64)
        # Two additional IDs have no vector, but can enter through metadata.
        files = rng.choice(universe[:n_files], size=int(rng.integers(0, 130)))
        z = rng.choice(levels, size=len(files))
        scores = rng.uniform(-1., 1., size=len(files)).astype(np.float32)
        ids = rng.permutation(np.arange(100, 100 + len(files), dtype=np.int64))
        best, full_z = _scalar_all_files(files, z, scores, ids)
        word_ids = set(map(int, rng.choice(universe, size=int(rng.integers(n_files + 2)), replace=False)))
        coverage_ids = set(map(int, rng.choice(universe, size=int(rng.integers(n_files + 2)), replace=False)))
        exact_ids = set(map(int, rng.choice(universe, size=int(rng.integers(n_files + 2)), replace=False)))
        name_ids = set(map(int, rng.choice(universe, size=int(rng.integers(n_files + 2)), replace=False)))
        rejected = set(map(int, rng.choice(universe, size=int(rng.integers(n_files + 2)), replace=False)))
        word_rank = {fid: i + 1 for i, fid in enumerate(sorted(word_ids))}
        coverage = {fid: float(rng.choice([-1., 0., .25, .5, 1., np.nan, np.inf])) for fid in coverage_ids}
        exact = {fid: object() for fid in exact_ids}
        margins = {int(fid): rng.choice([None, 0., .49, .5, .51, np.inf, np.nan]) for fid in universe}
        textless = set(map(int, rng.choice(universe, size=int(rng.integers(n_files + 2)), replace=False)))
        gate = gates[case % len(gates)]
        kwargs = dict(scorer=scorer, coverage=coverage, word_rank=word_rank, exact=exact,
                      rejected_exact=rejected, z_gate=gate, parameters=parameters,
                      segment_margins=margins, textless=textless,
                      name_hits=name_ids if scorer == "hybrid" else set())
        expected = admit_files(z_by_file=full_z, **kwargs)
        retain = word_ids | coverage_ids | exact_ids | (name_ids if scorer == "hybrid" else set())
        # Quorum zero is intentionally a caller policy, not a hidden gate arm.
        minimum = None if scorer == "hybrid" and quorum == 0 else gate
        actual_best, actual_z = Searcher._aggregate_by_file(
            files, z, scores, ids, minimum_z=minimum, retain_files=retain,
        )
        assert admit_files(z_by_file=actual_z, **kwargs) == expected, (scorer, quorum, margin, case)
        for fid in expected.keys() & best.keys():
            assert actual_best[fid] == best[fid], (scorer, quorum, margin, case, fid)
            assert actual_z[fid] == full_z[fid]
        assert all(actual_best[fid] == best[fid] for fid in actual_best)
        assert actual_z == {fid: full_z[fid] for fid in actual_z}
        assert Searcher._aggregate_by_file(files, z, scores, ids) == (best, full_z)


@pytest.mark.parametrize("z,gate", [
    ([np.nan, 0., 4.], 3.),
    ([np.inf, 0., 4.], 3.),
    ([-np.inf, 0., 4.], 3.),
    ([1., 0., 4.], np.nan),
    ([1., 0., 4.], np.inf),
    ([1., 0., 4.], -np.inf),
    ([-0., 0., -0.], 0.),
])
def test_nonfinite_fallback_and_signed_zero_preserve_frozen_original(z, gate):
    files = np.array([1, 2, 3], dtype=np.int64)
    values = np.array(z, dtype=np.float64)
    scores = np.array([.1, .2, .9])
    ids = np.array([10, 20, 30], dtype=np.int64)
    expected = _frozen_all_files(files, values, scores, ids)
    actual = Searcher._aggregate_by_file(files, values, scores, ids,
                                       minimum_z=gate, retain_files={1})
    assert _float_records(actual) == _float_records(expected)


def test_all_nan_input_preserves_the_original_exception_instead_of_repairing_it():
    arrays = (np.array([1, 2]), np.array([np.nan, np.nan]),
              np.array([.1, .2]), np.array([10, 20]))
    with pytest.raises(IndexError) as old:
        _frozen_all_files(*arrays)
    with pytest.raises(type(old.value)):
        Searcher._aggregate_by_file(*arrays, minimum_z=3., retain_files={1})


def _vec(axis):
    vector = np.zeros(768, dtype=np.float32)
    vector[axis] = 1.
    return vector


class _FixedBackend(EmbeddingBackend):
    backend_id = "mock"

    @property
    def dim(self):
        return 768

    def capability(self):
        return Capability(True, False, False)

    def fingerprint_fields(self):
        return dict(model_id="aggregate-oracle", model_sha256="none", dim=768,
                    prompts="fixed", pooling="none", normalization="l2")

    def embed_texts(self, texts):
        return np.stack([_vec(0) for _ in texts])

    def embed_image(self, path, *, budget=140):
        raise NotSupported

    def embed_audio_wav(self, wav):
        raise NotSupported

    def describe(self):
        return {"id": "mock", "available": True}


class _DeterministicRanker:
    def rank(self, query, documents):
        return [.9 if "answer" in text else .1 for text in documents], {"model": "hermetic-oracle"}

    def close(self):
        pass


@dataclass
class _Index:
    path: Path
    root: Path
    ids: dict[str, int]
    vector_rows: int


@pytest.fixture
def semantic_index(tmp_path):
    root = tmp_path / "chosen"
    root.mkdir()
    path = tmp_path / "search.sqlite"
    store = Store(path)
    ids = {}

    def add(key, filename, texts, axes, *, kind="doc", modality="text", source="file"):
        source_path = str(root / filename) if source == "file" else filename
        if source == "file":
            file = Path(source_path)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("synthetic source exists", encoding="utf-8")
        fid = store.upsert_file(FileRec(path=source_path, source=source, kind=kind,
                               state="done", capability="content_indexed", sha256=f"source-{key}"))
        segments = [SegmentRec(kind="name", modality="none", text_for_fts=filename,
                               excerpt=filename)]
        for i, (text, axis) in enumerate(zip(texts, axes)):
            segments.append(SegmentRec(
                kind=("image" if modality == "image" else "audio") if text is None else "chunk",
                modality=modality if axis is not None else "none", loc=10 * i + 1,
                loc_end=10 * i + 9, text_for_fts=text, excerpt=text,
                vec=_vec(axis) if axis is not None else None,
            ))
        store.replace_segments(fid, segments)
        ids[key] = fid

    add("primary", "guides/primary.md", [
        "alpha beta gamma answer one. Stored evidence explains snapshots.",
        "gamma delta answer two. Complete original text supports the second clause.",
        "An adjacent original note preserves context ordering.",
    ], [0, 0, None])
    add("weak", "guides/weak.md", ["alpha beta gamma lexical support with a weak semantic vector."], [1])
    add("named", "alpha-beta-gamma.md", ["quartz orchard notation"], [1])
    add("exact", "manual-LH-2209.md", ["LH-2209 alpha compatibility manual."], [1])
    add("cjk", "notes/缓存.md", ["缓存队列顺序与事务快照。原文中的完整证据。"], [0])
    add("image", "pictures/scene.png", [None], [0], kind="image", modality="image")
    add("audio", "recordings/scene.wav", [None], [0], kind="audio", modality="audio")
    add("metadata", "alpha-metadata.md", ["alpha beta gamma evidence without any vector."], [None])
    add("web", "https://example.test/manual", ["alpha beta gamma web answer evidence."], [0], source="url")
    add("deleted", "guides/deleted.md", ["alpha beta gamma deleted source."], [0])
    for n in range(80):
        add(f"filler{n}", f"padding/filler-{n}.md", ["orchard quartz unrelated padding prose."], [1])
    store.set_meta("index_uuid", "12345678-1234-1234-1234-123456789abc")
    store.set_meta("roots_json", json.dumps([str(root)]))
    vector_rows = store.db.execute("SELECT count(*) FROM segments WHERE vec IS NOT NULL").fetchone()[0]
    store.close()
    (root / "guides/deleted.md").unlink()
    return _Index(path, root, ids, vector_rows)


def _request_for(case, index):
    base = SearchRequest(query="alpha beta gamma", pack=True, context=2, why=True,
                         require_citations=True, limit=12, budget=8000, timeout_s=30.)
    if case in ("semantic", "rrf"):
        return replace(base, scorer=case)
    if case == "lexical":
        return replace(base, scorer="lexical", lexical_only=True)
    if case == "path_filter":
        query, filters = parse_filters(f"alpha beta gamma under:{index.root / 'guides'}")
        return replace(base, query=query, filters=filters)
    if case == "site_filter":
        query, filters = parse_filters("alpha beta gamma site:example.test")
        return replace(base, query=query, filters=filters)
    if case == "exact":
        return replace(base, query="LH-2209 alpha")
    if case == "cjk":
        return replace(base, query="缓存队列顺序")
    if case == "image_filter":
        return replace(base, scorer="semantic", kinds={"image", "audio"})
    if case == "negative_gate":
        return replace(base, z_gate=-2.)
    if case == "quality_multi":
        return replace(base, query="alpha beta; gamma delta", mode="quality", multi_query=True)
    if case == "quality_rerank":
        return replace(base, mode="quality", rerank="local")
    return base


@pytest.mark.parametrize("case", [
    "hybrid", "semantic", "rrf", "lexical", "path_filter", "site_filter",
    "exact", "cjk", "image_filter", "negative_gate", "quality_multi", "quality_rerank",
])
def test_full_hits_why_pack_citations_and_context_equal_unpruned_path(semantic_index, monkeypatch, case):
    request = _request_for(case, semantic_index)
    factory = _DeterministicRanker if case == "quality_rerank" else None
    before = semantic_index.path.read_bytes()
    candidate = Application(semantic_index.path, _FixedBackend, reranker_factory=factory).search(request)
    with monkeypatch.context() as original:
        original.setattr(Searcher, "_aggregate_by_file", staticmethod(_frozen_all_files))
        expected = Application(semantic_index.path, _FixedBackend, reranker_factory=factory).search(request)
    assert candidate.hits, case
    assert asdict(candidate) == asdict(expected), case
    assert candidate.pack_text and candidate.citations
    assert candidate.contexts
    assert semantic_index.ids["deleted"] not in {citation.file_id for citation in candidate.citations}
    assert semantic_index.path.read_bytes() == before


def test_zero_quorum_caller_retains_implicit_coverage_files(semantic_index, monkeypatch):
    request = replace(_request_for("hybrid", semantic_index), query="zzzzmissing")
    params = AdmissionParameters(coverage_quorum=0.)
    observed = []
    actual_aggregate = Searcher._aggregate_by_file

    def observe(*args, **kwargs):
        observed.append(kwargs.get("minimum_z"))
        return actual_aggregate(*args, **kwargs)

    monkeypatch.setattr(Searcher, "_aggregate_by_file", staticmethod(observe))
    candidate = Application(semantic_index.path, _FixedBackend, admission_parameters=params).search(request)
    assert observed == [None]
    with monkeypatch.context() as original:
        original.setattr(Searcher, "_aggregate_by_file", staticmethod(_frozen_all_files))
        expected = Application(semantic_index.path, _FixedBackend, admission_parameters=params).search(request)
    assert len(candidate.hits) == request.limit
    assert any("padding/filler-" in hit.path for hit in candidate.hits)
    assert asdict(candidate) == asdict(expected)


def test_name_only_eligibility_keeps_semantic_record_outside_bounded_recall(semantic_index, monkeypatch):
    import goldmine.search as module

    named = semantic_index.ids["named"]
    collect = module.collect_lexical

    def recall_without_named(*args, **kwargs):
        result = collect(*args, **kwargs)
        # Simulate this file lying beyond the bounded ordinary recall window.
        # Name eligibility is independently read from the whole stored name.
        result.word_rank.pop(named, None)
        result.coverage.pop(named, None)
        result.keyword_segment.pop(named, None)
        omitted = {sid for sid, fid in result.segment_files.items() if fid == named}
        for sid in omitted:
            result.segment_files.pop(sid, None)
            result.segment_units.pop(sid, None)
        return result

    monkeypatch.setattr(module, "collect_lexical", recall_without_named)
    request = _request_for("hybrid", semantic_index)
    candidate = Application(semantic_index.path, _FixedBackend).search(request)
    with monkeypatch.context() as original:
        original.setattr(Searcher, "_aggregate_by_file", staticmethod(_frozen_all_files))
        expected = Application(semantic_index.path, _FixedBackend).search(request)
    named_hit = next(hit for hit in candidate.hits if hit.path.endswith("alpha-beta-gamma.md"))
    assert named_hit.admission_reason == "name_quorum"
    assert named_hit.z is not None and named_hit.z < request.z_gate
    assert named_hit.cosine == 0.
    assert asdict(candidate) == asdict(expected)


@pytest.mark.parametrize("small_group", [1, 19, 20, 21])
def test_pruning_leaves_full_population_modality_normalization_unchanged(small_group):
    rng = np.random.default_rng(4711)
    scores = rng.uniform(-1., 1., size=40 + small_group)
    modalities = np.array(["text"] * 40 + ["image"] * small_group)
    z = Searcher.zscores(scores, modalities)
    files = np.array([1] * 40 + [2] * small_group, dtype=np.int64)
    ids = np.arange(1, len(files) + 1, dtype=np.int64)
    before = z.tobytes()
    full, full_z = _scalar_all_files(files, z, scores, ids)
    reduced, reduced_z = Searcher._aggregate_by_file(files, z, scores, ids,
                                                    minimum_z=3., retain_files={2})
    assert z.tobytes() == before
    assert reduced[2] == full[2] and reduced_z[2] == full_z[2]


def test_filtered_search_still_normalizes_every_vector_row(semantic_index, monkeypatch):
    observed = []
    normalize = Searcher.zscores

    def all_rows(scores, modalities):
        observed.append((len(scores), tuple(modalities)))
        return normalize(scores, modalities)

    monkeypatch.setattr(Searcher, "zscores", staticmethod(all_rows))
    request = _request_for("image_filter", semantic_index)
    result = Application(semantic_index.path, _FixedBackend).search(request)
    assert result.hits
    assert observed[0][0] == semantic_index.vector_rows
    assert set(observed[0][1]) == {"text", "image", "audio"}
