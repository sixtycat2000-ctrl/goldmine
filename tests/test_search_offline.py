"""Tier-1/2: fusion math — z-scores, tiny-group fallback, lookup inversion, RRF,
coverage^1.5, kind hints, file collapse, missing vectors, bm25 direction.

Hand-computed case: 1 target file (vec e0) + 25 filler files (vec e1); query e0.
26 vector rows, scores = [1, 0 x25]: mean 1/26, centered target 25/26,
std = sqrt(n-1)/n = 5/26  =>  z_target = (25/26)/(5/26) = 5.0 exactly.
General rule used below: one high score among n rows gives z = sqrt(n-1).
"""

from __future__ import annotations

import numpy as np
import pytest

from goldmine.backends.base import (
    Capability,
    EmbeddingBackend,
    NotSupported,
)
from goldmine.search import KIND_WORDS, STOPWORDS, Searcher
from goldmine.store import FileRec, SegmentRec, Store

# one high cosine among 26 vector rows -> z = sqrt(26 - 1) = 5.0
Z_TARGET = 5.0


def evec(i: int) -> np.ndarray:
    v = np.zeros(768, dtype=np.float32)
    v[i] = 1.0
    return v


class FixedBackend(EmbeddingBackend):
    backend_id = "mock"

    def __init__(self, vec: np.ndarray, dim: int = 768):
        self.vec = np.asarray(vec, dtype=np.float32)
        self._dim = dim

    @property
    def dim(self):
        return self._dim

    def capability(self):
        return Capability(text=True, image=False, audio=False)

    def fingerprint_fields(self):
        return {"model_id": "fixed", "model_sha256": "n/a", "dim": self._dim,
                "prompts": "v1", "pooling": "none", "normalization": "l2"}

    def embed_texts(self, texts):
        return np.stack([self.vec for _ in texts])

    def embed_image(self, path):
        raise NotSupported

    def embed_audio_wav(self, wav):
        raise NotSupported

    def describe(self):
        return {"id": "mock", "available": True}


def add_file(store: Store, path: str, texts: list[str], vecs: list[np.ndarray | None],
             kind: str = "doc", capability: str = "content_indexed",
             modality: str = "text") -> int:
    fid = store.upsert_file(FileRec(path=path, kind=kind, size=10, state="pending"))
    store.mark_file(fid, state="done", capability=capability, detail=None, info=None,
                    extractor_version="txt-stdlib.v1")
    segs = [SegmentRec(kind="name", modality="none", excerpt=path.split("/")[-1],
                       text_for_fts=f"folder words {path}")]
    for i, (text, vec) in enumerate(zip(texts, vecs)):
        segs.append(
            SegmentRec(kind="chunk", modality=modality if vec is not None else "none",
                       loc=i + 1, loc_end=i + 1, excerpt=text,
                       text_for_fts=text, vec=vec)
        )
    store.replace_segments(fid, segs)
    return fid


@pytest.fixture
def haystack(tmp_path):
    """1 target (needle doc, vec e0) + 25 fillers (vec e1)."""
    store = Store(tmp_path / "s.sqlite")
    add_file(store, "/docs/needle-target.txt", ["needle unique sentence"], [evec(0)])
    for i in range(25):
        add_file(store, f"/fill/filler{i}.txt", ["filler text here"], [evec(1)])
    yield store
    store.close()


# ------------------------------------------------------------- static math


def test_zscores_centering_and_std_floor():
    scores = np.array([1.0, 0.0, 0.0])
    mods = np.array(["text", "text", "text"])
    z = Searcher.zscores(scores, mods)
    mean, std = 1.0 / 3.0, np.sqrt(2.0 / 9.0)
    expected = (scores - mean) / max(std, 0.02)
    assert np.allclose(z, expected, atol=1e-12)


def test_zscores_std_floor_constant_scores():
    z = Searcher.zscores(np.array([5.0, 5.0, 5.0, 5.0]), np.array(["text"] * 4))
    assert np.allclose(z, 0.0)  # std 0 -> floor 0.02; centered mean is 0 anyway


def test_zscores_tiny_group_uses_global_mean():
    """Groups < 20 fall back to the global mean (V3 tiny-group fix)."""
    scores = np.array([1.0] * 20 + [0.0, 0.0, 0.0])
    mods = np.array(["text"] * 20 + ["image"] * 3)
    z = Searcher.zscores(scores, mods)
    global_mean = scores.mean()  # 20/23
    # image rows are centered by the GLOBAL mean, text rows by their own mean (1.0)
    assert np.allclose(z[:20], z[0])       # all text identical
    assert np.allclose(z[20:], z[20])      # all image identical
    assert z[0] > z[20]                    # above-average group ranks higher
    # the image centered value is exactly score - global_mean, scaled globally
    centered_img = 0.0 - global_mean
    centered_text = 1.0 - 1.0
    all_centered = np.concatenate([np.full(20, centered_text), np.full(3, centered_img)])
    std = max(all_centered.std(), 0.02)
    assert np.allclose(z, (all_centered - all_centered.mean()) / std)


def test_zscores_single_audio_segment_among_text():
    """Corpus shape after media wiring: 1 audio vector among many text rows.
    The singleton group must use the global mean and stay finite/sane."""
    scores = np.array([1.0] + [0.0] * 50)
    mods = np.array(["audio"] + ["text"] * 50)
    z = Searcher.zscores(scores, mods)
    assert np.all(np.isfinite(z))
    assert z[0] > 0                        # the lone audio match stands out
    assert z[0] == pytest.approx(np.sqrt(50), rel=1e-6)  # one high among 51


def test_hybrid_tiny_audio_group_qualifies(haystack):
    """End-to-end: 1 audio segment among 26 text vectors — search must not
    choke on the singleton modality group and the audio file can qualify."""
    add_file(haystack, "/aud/one-shot.wav", ["needle unique audio"], [evec(0)],
             kind="audio", capability="content_indexed", modality="audio")
    hits = Searcher(haystack, FixedBackend(evec(0))).search("needle", z_gate=2.0)
    assert hits, "singleton audio group must not break fusion"
    by_path = {h.path: h for h in hits}
    assert "/aud/one-shot.wav" in by_path
    assert "/docs/needle-target.txt" in by_path
    audio_hit = by_path["/aud/one-shot.wav"]
    assert audio_hit.z is not None and audio_hit.z >= 2.0
    assert audio_hit.kind == "audio"
    assert np.all(np.isfinite([h.z for h in hits]))


def test_is_lookup():
    assert Searcher.is_lookup(["K7Q2LM"])
    assert Searcher.is_lookup(["TRX50"])
    assert Searcher.is_lookup(["4815162342"])          # long number
    assert Searcher.is_lookup(["K7Q2LM", "TRX50"])     # every term qualifies
    # v2.1 (01-search F3): ANY qualifying term fires — 'LH-2209' tokenizes to
    # ['lh', '2209'] and the digit-bearing '2209' now carries the query.
    # A/B (n=146 llama): lookup MRR 0.594 -> 0.609, no query regressed.
    assert Searcher.is_lookup(["lh", "2209"])
    assert Searcher.is_lookup(["K7Q2LM", "roasting"])  # mixed: the code term fires
    assert not Searcher.is_lookup([])
    assert not Searcher.is_lookup(["needle"])
    assert not Searcher.is_lookup(["note"])            # letters only


def test_is_lookup_py_version_string_known_gap():
    """real-053 regression pin ('py-1.0.3'): _terms_of drops the single-char
    digit runs (1/0/3), leaving ['py'] — no digit-bearing term, so lookup
    weights cannot fire. This is the documented residual gap of the ANY-term
    trigger (the fix helps the LH-2209 class, real-058/061); changing it
    means touching _terms_of's digit-run rule — a v2.1 G1 decision, not an
    accident to drift into."""
    assert Searcher.terms("py-1.0.3") == ["py"]
    assert not Searcher.is_lookup(Searcher.terms("py-1.0.3"))


def test_rrf_hand_values():
    fused = Searcher.rrf([1, 2, 3], [1, 2], k=60)
    assert fused[1] == pytest.approx(1 / 61 + 1 / 61)   # rank 1 in both lists
    assert fused[2] == pytest.approx(1 / 62 + 1 / 62)
    assert fused[3] == pytest.approx(1 / 63)            # only in list a
    best = max(fused, key=fused.get)
    assert best == 1


def test_terms_stopwords_dedup():
    terms = Searcher.terms("The scheduler and the SCHEDULER evicts a pod")
    assert terms == ["scheduler", "evicts", "pod"]  # lowercase, deduped, stopwords gone
    assert "pdf" not in Searcher.terms("find my pdf of taxes")  # kind word dropped


def test_kind_hints():
    assert Searcher.kind_hints("screenshot of the error") == {"image"}
    assert Searcher.kind_hints("my notes on k8s") == {"doc"}
    assert Searcher.kind_hints("a website about tea") == {"webpage"}
    assert Searcher.kind_hints("nothing here") == set()


# ------------------------------------------------------------ fusion (hybrid)


def test_hybrid_hand_computed_score(haystack):
    backend = FixedBackend(evec(0))
    hits = Searcher(haystack, backend).search("needle", z_gate=2.0, limit=10)
    assert hits, "the needle file must qualify"
    top = hits[0]
    assert top.path == "/docs/needle-target.txt"
    assert top.z == pytest.approx(Z_TARGET, rel=1e-6)
    assert top.cosine == pytest.approx(1.0, rel=1e-6)
    assert top.coverage == pytest.approx(1.0)
    # score = 1.0*z + 2.0*coverage^1.5 = 3.5355 + 2.0
    assert top.score == pytest.approx(Z_TARGET + 2.0, rel=1e-4)
    assert hits[0].file_status == "content_indexed"
    assert hits[0].scorer == "hybrid"
    # the 25 fillers do not qualify (z=-1.9, coverage 0)
    assert all(h.path != "/fill/filler0.txt" for h in hits)


def test_lookup_inverts_weights(haystack):
    """Lookup query ('TRX50' has digits + letters): (w_m, w_c) = (0.5, 4.0)."""
    # add a lookup-target: vec e2, so 27 vector rows -> z = sqrt(26)
    add_file(haystack, "/docs/trx-target.txt", ["TRX50 datasheet pinout"], [evec(2)])
    backend = FixedBackend(evec(2))
    hits = Searcher(haystack, backend).search("TRX50", z_gate=2.0, limit=10)
    assert hits
    top = hits[0]
    assert top.path == "/docs/trx-target.txt"
    z_trx = np.sqrt(26)  # one high score among 27 rows
    # lookup weights: 0.5 * max(z, 0) + 4.0 * coverage^1.5 (coverage = 1.0)
    assert top.score == pytest.approx(0.5 * z_trx + 4.0, rel=1e-4)
    # and the non-lookup weights would give a different total:
    assert top.score != pytest.approx(1.0 * z_trx + 2.0, rel=1e-2)


def test_kind_hint_flat_not_stacked(haystack):
    hits = Searcher(haystack, FixedBackend(evec(0))).search(
        "needle document screenshot", z_gate=2.0, limit=10
    )
    top = hits[0]
    # both "document" and "screenshot" hint; the file is kind doc -> +1.0 ONCE
    assert top.score == pytest.approx(Z_TARGET + 2.0 + 1.0, rel=1e-3)


def test_coverage_power_15(haystack):
    """coverage^1.5: full coverage scores 2.0; half coverage scores 2*0.5^1.5.
    Query vectors are orthogonal to every doc vector, so qualification is purely
    lexical (coverage >= 0.5 quorum)."""
    add_file(haystack, "/cov/quarter.txt", ["alpha beta gamma delta"], [evec(3)])
    add_file(haystack, "/cov/one.txt", ["alpha nothing else"], [evec(4)])
    hits = Searcher(haystack, FixedBackend(evec(5))).search(
        "alpha beta", z_gate=2.0, limit=10
    )
    by_path = {h.path: h for h in hits}
    assert by_path["/cov/quarter.txt"].coverage == pytest.approx(1.0)
    assert by_path["/cov/quarter.txt"].score == pytest.approx(2.0, rel=1e-4)
    assert by_path["/cov/one.txt"].coverage == pytest.approx(0.5)  # quorum edge: qualifies
    assert by_path["/cov/one.txt"].score == pytest.approx(2.0 * 0.5 ** 1.5, rel=1e-4)
    assert by_path["/cov/one.txt"].z == pytest.approx(0.0)  # scored, stood out not at all


def test_zero_vector_file_qualifies_on_coverage(haystack):
    """Verdict 3: a file with zero vectors can still qualify; z/cosine are None."""
    add_file(haystack, "/lex/digits.txt", ["needle 4815162342 998877"], [None])
    hits = Searcher(haystack, FixedBackend(evec(0))).search(
        "needle", z_gate=2.0, limit=10
    )
    digit_hit = next(h for h in hits if h.path == "/lex/digits.txt")
    assert digit_hit.z is None
    assert digit_hit.cosine is None
    assert digit_hit.coverage == pytest.approx(1.0)
    # score = 0 (no meaning) + 2.0 * 1.0^1.5
    assert digit_hit.score == pytest.approx(2.0, rel=1e-6)


def test_file_collapse_picks_meaning_segment(tmp_path):
    store = Store(tmp_path / "c.sqlite")
    add_file(
        store,
        "/docs/two-chunks.txt",
        ["needle unique sentence", "unrelated filler words"],
        [evec(0), evec(2)],
    )
    for i in range(25):
        add_file(store, f"/fill/f{i}.txt", ["filler text here"], [evec(1)])
    hits = Searcher(store, FixedBackend(evec(0))).search("needle", z_gate=2.0)
    assert len(hits) == 1  # collapsed to file level
    assert hits[0].loc == 1  # the MEANING segment (chunk 1), not the keyword-only one


def test_file_collapse_keyword_segment_when_z_below_gate(tmp_path):
    """z < gate but coverage qualifies: hit points at the KEYWORD segment."""
    store = Store(tmp_path / "k.sqlite")
    # query vector orthogonal to everything -> all z = 0, no z-gate candidates
    add_file(store, "/kw/target.txt", ["needle alpha", "beta needle here"], [evec(0), evec(0)])
    for i in range(25):
        add_file(store, f"/kw/f{i}.txt", ["unrelated content"], [evec(i % 20 + 1)])
    hits = Searcher(store, FixedBackend(evec(30))).search("needle alpha", z_gate=2.0)
    target = [h for h in hits if h.path == "/kw/target.txt"]
    assert target
    h = target[0]
    assert h.z is not None and h.z < 2.0
    # coverage is per FILE: both terms appear somewhere in the file's rows
    assert h.coverage == pytest.approx(1.0)
    # collapse points at the keyword segment (chunk containing both terms)
    assert h.loc == 1


def test_scorers_lexical_semantic_rrf(haystack):
    s = Searcher(haystack, FixedBackend(evec(0)))
    lex = s.search("needle", scorer="lexical")
    assert lex and lex[0].path == "/docs/needle-target.txt"
    assert lex[0].z is None and lex[0].scorer == "lexical"
    assert lex[0].score == pytest.approx(2.0 * 1.0, rel=1e-4)  # w_c * cov^1.5

    sem = s.search("needle", scorer="semantic", z_gate=2.0)
    assert sem and sem[0].score == pytest.approx(Z_TARGET, rel=1e-3)

    rrf_hits = s.search("needle", scorer="rrf")
    assert rrf_hits and rrf_hits[0].path == "/docs/needle-target.txt"


def test_kinds_filter(haystack):
    add_file(haystack, "/img/picture.png", [], [], kind="image", capability="metadata_only")
    hits = Searcher(haystack, FixedBackend(evec(0))).search("needle", kinds={"image"})
    assert hits == []
    hits = Searcher(haystack, FixedBackend(evec(0))).search("needle", kinds={"doc"})
    assert hits and hits[0].path == "/docs/needle-target.txt"


def test_failed_files_never_surface_in_results(haystack):
    """P1-5 (belt): even when a failed file's stale segments linger in the
    store, candidate generation excludes state='failed' rows."""
    fid = add_file(haystack, "/docs/failed-needle.txt", ["needle unique sentence"], [evec(0)])
    haystack.mark_file(
        fid, state="failed", capability="failed", detail="boom",
        info=None, extractor_version=None,
    )
    hits = Searcher(haystack, FixedBackend(evec(0))).search("needle unique sentence")
    assert hits
    assert all(h.file_status != "failed" for h in hits)
    assert all("failed-needle" not in h.path for h in hits)
    # ...and the healthy needle still wins
    assert hits[0].path == "/docs/needle-target.txt"
    # the filter also applies under a kinds filter
    hits = Searcher(haystack, FixedBackend(evec(0))).search("needle", kinds={"doc"})
    assert all(h.file_status != "failed" for h in hits)


def test_zero_hits_is_empty_not_error(haystack):
    # query vector orthogonal to every stored vector AND no lexical overlap
    hits = Searcher(haystack, FixedBackend(evec(700))).search("zzzq")
    assert hits == []


def test_no_backend_hybrid_falls_back_to_coverage(haystack):
    """Without a backend the hybrid scorer still runs its lexical half: files
    qualify on coverage, z/cosine are None."""
    hits = Searcher(haystack, None).search("needle", scorer="hybrid")
    assert hits and hits[0].path == "/docs/needle-target.txt"
    assert hits[0].z is None and hits[0].cosine is None
    assert hits[0].score == pytest.approx(2.0, rel=1e-4)
    # pure lexical baseline works too
    hits = Searcher(haystack, None).search("needle", scorer="lexical")
    assert hits and hits[0].path == "/docs/needle-target.txt"


def test_stopwords_and_kindwords_frozen_sets():
    assert isinstance(STOPWORDS, frozenset)
    assert KIND_WORDS["pdf"] == "pdf"
    assert KIND_WORDS["screenshots"] == "image"


# --------------------------------------------- vectorized aggregation parity


def test_vectorized_aggregation_matches_python_loop():
    """04 §5 companion fix: _aggregate_by_file must reproduce the per-row
    Python loop it replaced EXACTLY (to 1e-9), including the tie-break (the
    FIRST row achieving a file's max z wins, because the old loop compared
    strictly `z > best[fid][0]`). 1k random rows over ~120 files + crafted
    exact ties."""
    rng = np.random.default_rng(7)
    n = 1000
    file_ids = rng.integers(0, 120, size=n)
    zvals = rng.standard_normal(n)                      # float64 like zscores()
    scores = rng.standard_normal(n).astype(np.float32)  # float32 like matmul
    seg_ids = rng.permutation(n).astype(np.int64)
    # craft exact duplicate maxima within a few files to pin the tie-break
    for fid in (3, 17, 42):
        rows = np.flatnonzero(file_ids == fid)[:4]
        if len(rows) >= 2:
            zvals[rows] = 1.2345
            scores[rows] = -0.75

    # reference: verbatim port of the v1 per-row loop (search.py:155-160)
    best_ref: dict[int, tuple[float, float, int]] = {}
    z_ref: dict[int, float] = {}
    for row in range(n):
        fid = int(file_ids[row])
        z = float(zvals[row])
        if fid not in best_ref or z > best_ref[fid][0]:
            best_ref[fid] = (z, float(scores[row]), int(seg_ids[row]))
        z_ref[fid] = max(z_ref.get(fid, -np.inf), z)

    best, z_by_file = Searcher._aggregate_by_file(
        np.asarray(file_ids, dtype=np.int64), zvals, scores, seg_ids
    )
    # keys must be PYTHON ints: sqlite3 binds np.int64 as a BLOB, which would
    # silently make every downstream get_file()/mark_file() miss its row
    assert all(type(f) is int for f in best)
    assert all(type(f) is int for f in z_by_file)
    assert set(best) == set(best_ref)
    assert set(z_by_file) == set(z_ref)
    for fid in best_ref:
        assert z_by_file[fid] == pytest.approx(z_ref[fid], abs=1e-9)
        z, cosine, seg = best[fid]
        assert z == pytest.approx(best_ref[fid][0], abs=1e-9)
        assert cosine == pytest.approx(best_ref[fid][1], abs=1e-9)
        assert seg == best_ref[fid][2], f"tie-break mismatch on file {fid}"


def test_vectorized_aggregation_empty_and_single():
    empty = np.zeros(0, dtype=np.int64)
    best, z_by_file = Searcher._aggregate_by_file(
        empty, np.zeros(0), np.zeros(0, dtype=np.float32), empty
    )
    assert best == {} and z_by_file == {}
    best, z_by_file = Searcher._aggregate_by_file(
        np.array([9], dtype=np.int64),
        np.array([2.5]),
        np.array([0.5], dtype=np.float32),
        np.array([777], dtype=np.int64),
    )
    assert best == {9: (2.5, 0.5, 777)} and z_by_file == {9: 2.5}
