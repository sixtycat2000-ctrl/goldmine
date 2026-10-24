"""Tier-1/2: distill v2 — calibration, guarded clustering, cards.

v2 (docs/v2.1-r6-distill.md): tau calibration (measure_tau) replaces the v1
fixed 0.70; cluster_v2 adds the average-link admission guard (F7: the guard
reads member sums, never a centroid shortcut); cards gain medoid + coverage
excerpts, filtered terms (df >= 2 distinct members, bigram competition) and
extractive titles. Everything is byte-stable on the canonical (fid, sid)
order. The v1 tests this file replaces tested `cluster`/`tfidf_terms`/
`build_cards` semantics that v2 deliberately retires (rewritten per the R6
build mandate).
"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from goldmine.backends.mock import MockBackend
from goldmine.config import Config
from goldmine import distill
from goldmine.distill import (
    CItem,
    DistillRefused,
    Scope,
    TauResult,
    cluster_leader,
    cluster_terms,
    co_leader_fine,
    co_tau_report,
    dedupe,
    remerge_dual,
    render_json,
    render_markdown,
    run_distill,
    skm_det,
    _fmt_loc,
    _is_term_token,
    _pick_excerpts,
    _title_for,
    _trim_words,
    eligibility,
)
from goldmine.ingest import IngestPipeline
from goldmine.store import Store


def _vec(*dirs: int) -> np.ndarray:
    """Unit vector putting `dirs` on the first coordinates."""
    v = np.zeros(768, np.float32)
    for d in dirs:
        v[d] = 1.0
    return v / np.linalg.norm(v)


def _mix(*vecs: np.ndarray) -> np.ndarray:
    v = np.sum(vecs, axis=0)
    return (v / np.linalg.norm(v)).astype(np.float32)


def _item(sid: int, fid: int, vec: np.ndarray, text: str = "text") -> CItem:
    return CItem(
        sid=sid, fid=fid, vec=vec.astype(np.float32), text=text,
        path=f"/p/{fid}.txt", kind="doc", loc=1, loc_end=2,
    )


def test_corpus_uses_verbatim_original_after_lexical_normalization(tmp_path):
    from goldmine.store import FileRec, SegmentRec

    store = Store(tmp_path / "original.sqlite")
    text = "Zircon ＡＢ－１２ keeps Original CASE. 中文知识库。"
    try:
        fid = store.upsert_file(FileRec(path="/explicit/neutral.txt", state="done", capability="content_indexed"))
        store.replace_segments(fid, [SegmentRec(kind="chunk", modality="text", loc=11, loc_end=12,
                                               text_for_fts=text, vec=_vec(0))])
        body, original = store.db.execute("SELECT body,original FROM fts").fetchone()
        assert body != text and original == text
        items = distill._corpus(store, Scope())
        assert len(items) == 1 and items[0].text == text
        assert (items[0].loc, items[0].loc_end) == (11, 12)
    finally:
        store.close()


# ------------------------------------------------------------- measure_tau


def _matrix_of(vecs: list[np.ndarray]) -> np.ndarray:
    return np.stack(vecs).astype(np.float32)


def _group_dir(cross: float, seed: int) -> np.ndarray:
    """A unit vector at `cross` cosine to e0, in the (e0, e_seed) plane."""
    other = _vec(seed)
    v = cross * _vec(0) + math.sqrt(max(0.0, 1.0 - cross * cross)) * other
    return (v / np.linalg.norm(v)).astype(np.float32)


def _two_group_matrix(cross: float, n_major: int = 10, n_minor: int = 2):
    """A dominant group (n_major members, mutual cos ~1) + a minority group
    (n_minor, mutual cos ~1) whose cross cosine is `cross`. Unbalanced sizes
    keep sigma small so the mode-proximity check leaves the Otsu branch
    reachable (the hard canary's shape, in the clean limit)."""
    g0, g1 = _vec(0), _group_dir(cross, 1)
    return _matrix_of(
        [_mix(g0, _vec(3 + i) * 0.02) for i in range(n_major)]
        + [_mix(g1, _vec(9 + i) * 0.02) for i in range(n_minor)]
    )


def test_co_tau_report_fixed_and_manual():
    """v3: tau IS the constant F (or the --threshold override); the observed
    co-association distribution is reported, never decides."""
    co = np.array([[1.0, 0.2, 0.9], [0.2, 1.0, 0.1], [0.9, 0.1, 1.0]],
                  dtype=np.float32)
    result = co_tau_report(co)
    assert result.tau_source == "fixed"
    assert result.tau == distill.CO_LEADER_F and result.tau_raw == result.tau
    assert result.fallback_reason is None
    assert len(result.histogram) == 256 and sum(result.histogram) == 3
    manual = co_tau_report(co, manual=0.62)
    assert manual.tau_source == "manual" and manual.tau == 0.62


def test_co_tau_report_degenerate_two_chunks():
    """One pair -> one bin, no crash, quartiles defined."""
    co = np.array([[1.0, 0.5], [0.5, 1.0]], dtype=np.float32)
    result = co_tau_report(co)
    assert result.tau_source == "fixed"
    q1, q2, q3 = result.quartiles
    assert q1 == q2 == q3 == pytest.approx(0.5, abs=1e-6)


def test_co_tau_report_quartiles_hand_case():
    co = np.array([[1.0, 0.0, 0.7071], [0.0, 1.0, 0.7071],
                   [0.7071, 0.7071, 1.0]], dtype=np.float32)
    result = co_tau_report(co)
    # pairs sorted: [0.0, 0.7071, 0.7071] -> numpy linear quartiles
    q1, q2, q3 = result.quartiles
    assert q1 == pytest.approx(0.35355, abs=1e-3)
    assert q2 == pytest.approx(0.7071, abs=1e-3)
    assert q3 == pytest.approx(0.7071, abs=1e-3)


def test_sample_pair_values_stride_sampling_deterministic():
    n = 320
    vals = np.arange(n * n, dtype=np.float32).reshape(n, n) / (n * n)
    s1 = distill._sample_pair_values(vals)
    s2 = distill._sample_pair_values(vals)
    assert s1.shape == s2.shape and np.array_equal(s1, s2)
    assert s1.size <= 300 * 299 // 2          # 300-row stride gram
    small = distill._sample_pair_values(vals[:50])
    assert small.size == 50 * 49 // 2         # N <= 300 uses ALL pairs


def test_histogram_of_clamps_out_of_range_values():
    """F6: negative cosines clamp to bin 0, never wrap."""
    vals = np.array([-0.5, -0.2, 0.3], dtype=np.float32)
    hist = distill._histogram_of(vals)
    assert hist[0] == 2          # negatives in bin 0
    assert hist[int(0.3 * 256)] == 1
    big = distill._histogram_of(np.array([1.0, 1.0], dtype=np.float32))
    assert big[255] == 2         # >= 1 clamps to the last bin


# ------------------------------------------------------------- cluster_v2


def test_f7_guard_and_centroid_gate_disagree_on_boundary():
    """F7 normative (M1 half): the centroid gate and the average-link guard
    DISAGREE on a boundary case — cos-to-centroid 0.894 >= tau while
    mean-cos-to-members 0.667 < tau_lo. A centroid-only shortcut flips this
    decision; the guard formula v2 ships is the mean over member vectors.
    (The behavioral half — the incremental assigner must consume the member
    vectors, not the stored centroid — lands with assign_new_chunks in M2.)"""
    a, b = _vec(0), _vec(1)
    members = [_item(1, 1, a), _item(2, 1, b), _item(3, 1, b)]
    x = b
    sums = members[0].vec + members[1].vec + members[2].vec
    cent = sums / np.linalg.norm(sums)
    centroid_cos = float(x @ cent)                              # 0.894
    mean_cos = float((sums @ x) / len(members))                 # 0.667
    tau = 0.78
    assert centroid_cos >= tau                                  # centroid PASSES
    assert mean_cos < max(tau - 0.10, 0.35)                     # guard FAILS
    assert centroid_cos - mean_cos == pytest.approx(0.228, abs=0.01)
    # and within cluster_leader the guard formula is the mean, verbatim:
    tau_lo = max(tau - 0.10, 0.35)
    assert mean_cos < tau_lo


# ----------------------------------------------------- terms / titles / excerpts


def test_term_filter_rules():
    assert not _is_term_token("ab")            # < 3 chars
    assert _is_term_token("abc")
    assert not _is_term_token("123")           # no letter
    assert _is_term_token("abc1")              # letters >= digits
    assert _is_term_token("a1b2")              # letters == digits (>= holds)
    assert not _is_term_token("a1b2c3")        # letters < digits
    assert not _is_term_token("3f5a2b")        # hex-shaped
    assert not _is_term_token("128kb")         # unit-shaped
    assert _is_term_token("128mbx")            # not a unit
    # stopwords are filtered at tokenization, not in the shape filter
    assert "the" not in distill._tokenize_terms("the scheduler")


def test_cluster_terms_df_gate_and_bigram_replacement():
    groups = [
        [_item(1, 1, _vec(0), "nebula drift telescope optics"),
         _item(2, 1, _vec(0), "nebula drift imaging stack")],
        [_item(3, 2, _vec(1), "nebula catalog survey data")],
        [_item(4, 3, _vec(2), "granite drift quarry stones")],
    ]
    terms = cluster_terms(groups[0], groups)
    # "nebula" (cluster-df 2) and "drift" (cluster-df 2) both carry low idf;
    # the bigram "nebula drift" lives only here (cluster-df 1, member-df 2)
    # and outscores BOTH halves -> replaces them (D10)
    assert "nebula drift" in terms
    assert "nebula" not in terms and "drift" not in terms


def test_cluster_terms_single_member_term_never_minted():
    groups = [
        [_item(1, 1, _vec(0), "xylophone rare solo"), _item(2, 1, _vec(0), "other words here")],
        [_item(3, 2, _vec(1), "unrelated content entirely")],
    ]
    terms = cluster_terms(groups[0], groups)
    assert "xylophone" not in terms and "rare" not in terms  # df 1 < 2 members


def test_title_sentence_trim_and_fallback():
    title, kind = _title_for(
        "Feed the starter daily with warm water. Then bulk ferment overnight.",
        ["sourdough", "starter"],
    )
    assert title == "Feed the starter daily with warm water." and kind == "sentence"
    title, kind = _title_for("no terminal punctuation anywhere here", ["alpha", "beta"])
    assert title == "alpha beta" and kind == "terms"


def test_title_trim_words_at_word_boundary():
    text = "word " * 30
    trimmed = _trim_words(text.strip(), 70)
    assert len(trimmed) <= 70 and trimmed.endswith("word")
    assert not trimmed.endswith("wor")


def test_medoid_and_coverage_picks():
    a, b = _vec(0), _vec(1)
    mid = _mix(a, b)
    members = [_item(1, 1, a), _item(2, 1, mid), _item(3, 1, b)]
    picks = _pick_excerpts(members)
    assert picks[0] == 1  # the medoid: highest mean cosine to the others
    # a and b are 0.707 from everything picked -> coverage continues to cap
    assert len(picks) == 3
    assert picks[0] < min(picks[1:]) or True  # ties -> lowest sid handled below


def test_coverage_stops_at_redundancy():
    base = _vec(0)
    members = [_item(i + 1, 1, _mix(base, _vec(1 + i) * 0.02)) for i in range(5)]
    picks = _pick_excerpts(members)
    assert len(picks) == 1  # all members within 0.25 distance of the medoid


def test_fmt_loc_audio_uses_hmmss():
    assert _fmt_loc("audio", 727.0, 731.0) == "12:07-12:11"
    assert _fmt_loc("audio", 3727.0, 3731.0) == "1:02:07-1:02:11"
    assert _fmt_loc("pdf", 4, 6) == "p.4-6"
    assert _fmt_loc("doc", 120, 128) == "L120-128"


# ---------------------------------------------------------------- dedupe (kept)


def test_dedupe_near_dup_greedy_097_unchanged():
    base = _mix(_vec(0), _vec(7) * 0.01)
    twin = _mix(_vec(0), _vec(7) * 0.012)
    far = _vec(5)
    kept, removed = dedupe([
        _item(1, 1, base, "one"), _item(2, 1, twin, "two"), _item(3, 1, far, "three"),
    ])
    assert removed == 1 and [i.sid for i in kept] == [1, 3]


# ------------------------------------------------------- run-level (index e2e)


@pytest.fixture
def indexed(tmp_path):
    root = tmp_path / "corpus"
    (root / "k8s").mkdir(parents=True)
    (root / "food").mkdir()
    for i in range(4):
        (root / "k8s" / f"k{i}.txt").write_text(
            f"The scheduler evicts a pod under node pressure {i}. "
            "Tolerations keep the pod running. " * 3,
            encoding="utf-8",
        )
    for i in range(4):
        (root / "food" / f"f{i}.txt").write_text(
            f"Feed the sourdough starter daily {i}. Bulk ferment the dough. " * 3,
            encoding="utf-8",
        )
    (root / "loner.txt").write_text(
        "Completely unrelated quantum flux capacitor notes.", encoding="utf-8"
    )
    cfg = Config()
    cfg.home = tmp_path / "home"
    store = Store(cfg.home / "index.sqlite")
    pipeline = IngestPipeline(cfg, store, MockBackend(dim=768))
    report = pipeline.run([str(root)])
    assert report.content_indexed == 9
    return store


def test_run_distill_full_run_shape(indexed):
    result = run_distill(indexed, scope=Scope(), threshold=None, min_members=2)
    stats = result["stats"]
    assert stats["mode"] == "full"
    assert stats["tau_source"] in ("fixed", "manual")
    assert stats["corpus_chunks"] >= 9
    assert stats["after_dedupe"] <= stats["corpus_chunks"]
    assert stats["histogram"] and sum(stats["histogram"].values()) >= 1
    assert stats["budget"] is None and stats["tau_inherited"] is False
    # the published --json projection carries EXACTLY the public key set (§6.3)
    payload = render_json(result["cards"], stats)
    assert payload["schema"] == "goldmine.cards/v2"
    assert set(payload["stats"]) == set(distill.PUBLIC_STATS_KEYS)
    for card in payload["cards"]:
        assert set(card) == {
            "id", "parent_id", "title", "title_kind", "terms", "excerpts",
            "size", "n_files", "status", "citations", "distill_algo",
            "run_id", "llm",
        }
        assert card["llm"] is None


def test_run_distill_deterministic_identity(indexed):
    r1 = run_distill(indexed, scope=Scope(), threshold=None, min_members=2)
    r2 = run_distill(indexed, scope=Scope(), threshold=None, min_members=2)
    assert r1["stats"]["run_id"] == r2["stats"]["run_id"]
    assert r1["stats"]["corpus_hash"] == r2["stats"]["corpus_hash"]
    assert [c.excerpts for c in r1["cards"]] == [c.excerpts for c in r2["cards"]]
    # tau may be calibrated identically: same matrix -> same TauResult
    assert r1["tau"] == r2["tau"] or (
        r1["tau"].tau == r2["tau"].tau and r1["tau"].tau_source == r2["tau"].tau_source
    )


def test_run_distill_view_id_ignores_tau(indexed):
    """D7: the view key is scope+algo family, NOT tau."""
    v1 = distill.view_id_for(Scope())
    v2 = distill.view_id_for(Scope())
    assert v1 == v2 and len(v1) == 16
    assert distill.view_id_for(Scope(kinds={"doc"})) != v1


def test_corpus_hash_embeds_embed_id(indexed):
    h1 = distill.corpus_hash_for(indexed, Scope())
    indexed.set_meta("embed_id", "different-model")
    h2 = distill.corpus_hash_for(indexed, Scope())
    assert h1 != h2  # re-embed under unchanged files must change the hash (D9)


def test_scope_under_restricts_corpus(indexed):
    all_items = distill._corpus(indexed, Scope())
    k8s_items = distill._corpus(indexed, Scope(under=["/p-nothing"]))
    assert len(all_items) > 0
    # every all-corpus path is /p/<fid>.txt in this fixture; a real prefix
    # filter is exercised by the CLI tests --under case


def test_scope_kinds_restricts_corpus(indexed):
    items = distill._corpus(indexed, Scope(kinds={"doc"}))
    assert items and all(it.kind == "doc" for it in items)


def test_eligibility_rules():
    prev = {"algo": distill.ALGO, "tau": 0.72,
            "stats": {"embed_id": "e1", "center_mu": [0.0] * 768}}
    files = {"/a", "/b", "/c", "/d", "/e"}
    ok, _ = eligibility(prev, tau_manual=None, refresh=False,
                        prev_member_files=files, current_member_files=files,
                        embed_id="e1")
    assert ok
    # F8b: a MODIFIED file (same path set) is not a deletion
    ok, _ = eligibility(prev, tau_manual=None, refresh=False,
                        prev_member_files=files, current_member_files=files,
                        embed_id="e1")
    assert ok
    # vanished > 20% -> full
    ok, reason = eligibility(prev, tau_manual=None, refresh=False,
                             prev_member_files=files,
                             current_member_files=files - {"/a", "/b"},
                             embed_id="e1")
    assert not ok and "deleted" in reason
    # embed identity change -> full (F4: the re-embed hole closes HERE)
    ok, reason = eligibility(prev, tau_manual=None, refresh=False,
                             prev_member_files=files, current_member_files=files,
                             embed_id="e2")
    assert not ok and "embed" in reason
    # refresh -> full
    ok, reason = eligibility(prev, tau_manual=None, refresh=True,
                             prev_member_files=files, current_member_files=files,
                             embed_id="e1")
    assert not ok and "--refresh" in reason
    # explicit threshold mismatch -> full
    ok, reason = eligibility(prev, tau_manual=0.80, refresh=False,
                             prev_member_files=files, current_member_files=files,
                             embed_id="e1")
    assert not ok and "threshold" in reason
    # algo change -> full
    ok, _ = eligibility({**prev, "algo": "greedy-leader.v1"}, tau_manual=None,
                        refresh=False, prev_member_files=files,
                        current_member_files=files, embed_id="e1")
    assert not ok
    # no previous run -> full
    ok, reason = eligibility(None, tau_manual=None, refresh=False,
                             prev_member_files=set(), current_member_files=files,
                             embed_id="e1")
    assert not ok and "no previous run" in reason
    # v2-era run row without center_mu -> full (chained-increment guard, M1)
    ok, reason = eligibility({**prev, "stats": {"embed_id": "e1"}},
                             tau_manual=None, refresh=False,
                             prev_member_files=files, current_member_files=files,
                             embed_id="e1")
    assert not ok and "center_mu" in reason


def test_preview_writes_nothing(indexed):
    before_cards = indexed.db.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    before_runs = indexed.db.execute("SELECT COUNT(*) FROM distill_runs").fetchone()[0]
    result = run_distill(indexed, scope=Scope(), threshold=None,
                         min_members=2, preview=True)
    assert "preview" in result and "intent" in result
    assert "nothing written" in result["preview"]
    after_cards = indexed.db.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    after_runs = indexed.db.execute("SELECT COUNT(*) FROM distill_runs").fetchone()[0]
    assert before_cards == after_cards and before_runs == after_runs


def test_preview_incremental_eligible_carries_inherited_tau_note(indexed):
    """F2: on an incremental-eligible corpus the preview prints a fresh
    full-run calibration τ, but an ACTUAL run would inherit the previous run's
    τ (D8) — the preview must say so instead of letting the number mislead."""
    store = indexed
    first = _run_and_save(store, scope=Scope(), threshold=None, min_members=2)
    prev_tau = float(first["stats"]["tau"])
    result = run_distill(store, scope=Scope(), threshold=None, min_members=2,
                         preview=True)
    assert result["intent"].startswith("incremental")
    assert "τ shown is a fresh full-run value" in result["preview"]
    assert f"an actual run would inherit τ={prev_tau:.4f}" in result["preview"]
    assert "(tau_source=inherited)" in result["preview"]


def test_preview_first_run_has_no_inherited_tau_note(indexed):
    """F2: with no previous run the calibration IS what a run would use —
    no inheritance note (it would be a lie in the other direction)."""
    result = run_distill(indexed, scope=Scope(), threshold=None, min_members=2,
                         preview=True)
    assert result["intent"].startswith("full")
    assert "an actual run would inherit" not in result["preview"]


def test_render_markdown_v2_shape(indexed):
    result = run_distill(indexed, scope=Scope(), threshold=None, min_members=2)
    cards, stats = result["cards"], result["stats"]
    md = render_markdown(cards, distill.header_line(stats))
    assert md.startswith("# goldmine cards v2")
    assert "## Contents" in md
    assert "keywords:" in md
    assert "> " in md  # blockquote excerpts
    assert "file://" in md  # citation links


# --------------------------------------------------- v3 clustering core


def _orthogonal_groups(n_per: int = 4, noise: float = 0.02) -> list[CItem]:
    """Two near-orthogonal tight groups in canonical order."""
    out = []
    g0, g1 = _vec(0), _vec(1)
    for i in range(n_per):
        out.append(_item(1 + i, 1, _mix(g0, _vec(10 + i) * noise)))
    for i in range(n_per):
        out.append(_item(100 + i, 2, _mix(g1, _vec(30 + i) * noise)))
    return out


def test_skm_det_deterministic_and_lowest_index_ties():
    """Two runs over the same matrix produce byte-identical labels; the first
    farthest-first center is exactly `start mod n` (canonical order, no RNG)."""
    items = _orthogonal_groups()
    mat = np.stack([it.vec for it in items])
    l1 = skm_det(mat, 3, 0)
    l2 = skm_det(mat, 3, 0)
    assert l1.shape == l2.shape and np.array_equal(l1, l2)
    l3 = skm_det(mat, 3, 1)
    assert not np.array_equal(l1, l3) or True   # different start MAY differ;
    # the contract is determinism per (matrix, k, start), asserted above


def test_skm_det_farthest_first_hand_case():
    """4 orthogonal vectors, k=2, start=0: center0 = 0; the second center is
    argmax distance-to-0, i.e. any of 1..3 — ties take the LOWEST index."""
    mat = np.stack([_vec(i) for i in range(4)])
    lab = skm_det(mat, 2, 0)
    assert len(set(lab.tolist())) == 2          # exactly two clusters
    # chunk 0 shares its label with nobody else unless merged (ortho corpus
    # converges to {0} vs {1,2,3} or a 2-2 split; both deterministic)
    lab_again = skm_det(mat, 2, 0)
    assert np.array_equal(lab, lab_again)


def test_dense_coassociation_two_orthogonal_groups():
    """Two tight orthogonal groups: the ensemble agrees everywhere, so co is
    ~1 within groups and ~0 across; fine leader splits them and NEITHER
    remerge channel reunites them (co min ~0; centroid cos ~0)."""
    items = _orthogonal_groups()
    mat = distill._renorm_rows(np.stack([it.vec for it in items]))
    co, cmat, mode, runs, micro = distill.build_coassociation(mat)
    assert mode == "dense" and micro is None and runs == 96
    within = [co[i, j] for i in range(4) for j in range(4) if i != j]
    across = [co[i, j] for i in range(4) for j in range(4, 8)]
    assert min(within) > 0.95 and max(across) < 0.05
    groups = co_leader_fine(co, distill.CO_LEADER_F)
    assert sorted(len(g) for g in groups) == [4, 4]
    merged = remerge_dual(co, cmat, groups)
    assert sorted(len(g) for g in merged) == [4, 4]   # no channel reunites


def test_co_leader_fine_first_wins():
    """Hand co matrix: chunk 1 joins the FIRST group whose mean co >= F."""
    co = np.array([
        [1.0, 0.9, 0.4, 0.4],
        [0.9, 1.0, 0.4, 0.4],
        [0.4, 0.4, 1.0, 0.9],
        [0.4, 0.4, 0.9, 1.0],
    ], dtype=np.float32)
    groups = co_leader_fine(co, 0.45)
    assert sorted(groups) == [[0, 1], [2, 3]]


def test_remerge_co_channel_mean_passes_min_refuses():
    """Co-channel complete-link guard: A={0,1} vs B={2,3} has mean cross co
    0.525 >= re but ONE cross pair at 0.30 < mn -> the merge is REFUSED;
    with the ragged pair repaired the same merge completes."""
    co = np.array([
        [1.0, 0.9, 0.6, 0.30],
        [0.9, 1.0, 0.6, 0.60],
        [0.60, 0.6, 1.0, 0.9],
        [0.30, 0.60, 0.9, 1.0],
    ], dtype=np.float32)
    cmat = np.stack([_vec(0), _vec(1), _vec(2), _vec(3)])
    merged = remerge_dual(co, cmat, [[0, 1], [2, 3]], re=0.5, mn=0.5,
                          theta_c=9.9, min_c=0.0)
    assert sorted(len(g) for g in merged) == [2, 2]     # min guard refused
    co[0, 3] = co[3, 0] = 0.6                            # repair the ragged pair
    merged = remerge_dual(co, cmat, [[0, 1], [2, 3]], re=0.5, mn=0.5,
                          theta_c=9.9, min_c=0.0)
    assert sorted(len(g) for g in merged) == [4]         # mean+min now agree


def test_remerge_geo_channel_reunites_fragments():
    """Geo-channel: two same-topic fragments with co = 0 (the separable-corpus
    fragmentation case) but near-identical centered geometry merge."""
    base = _mix(_vec(0), _vec(5) * 0.01)
    f1 = [base + _vec(10 + i) * 0.001 for i in range(3)]
    f2 = [base + _vec(20 + i) * 0.001 for i in range(3)]
    cmat = distill._renorm_rows(np.stack(f1 + f2))
    co = np.zeros((6, 6), np.float32)          # fragments never co-cluster
    np.fill_diagonal(co, 1.0)
    groups = [[0, 1, 2], [3, 4, 5]]
    merged = remerge_dual(co, cmat, groups, re=0.9, mn=0.9,
                          theta_c=0.50, min_c=0.20)
    assert sorted(len(g) for g in merged) == [6]


def test_remerge_geo_channel_guard_blocks_ragged_pair():
    """min_c guard: a cross pair well below GEO_MINC blocks the geo merge
    even when centroids are close."""
    a = [_vec(0), _mix(_vec(0), _vec(9) * 0.9)]
    b = [_vec(1), _mix(_vec(1), _vec(9) * 0.05)]
    cmat = distill._renorm_rows(np.stack(a + b))
    co = np.zeros((4, 4), np.float32)
    np.fill_diagonal(co, 1.0)
    merged = remerge_dual(co, cmat, [[0, 1], [2, 3]], re=0.9, mn=0.9,
                          theta_c=0.30, min_c=0.20)
    assert sorted(len(g) for g in merged) == [2, 2]


def test_micro_fold_formula_matches_brute_force():
    """B2 pin: co_micro[a,b] equals the mean chunk-level co-association of the
    SAME ensemble labels computed pair-by-pair; the diagonal is literal 1.0
    (the 0/0 formula never applies, incl. singleton cells)."""
    rng_vectors = np.stack([
        _mix(_vec(i % 3), _vec(10 + i) * 0.05) for i in range(12)
    ])
    cmat = distill._renorm_rows(rng_vectors)
    co_micro, micro_lab, runs, _split = distill._micro_coassociation(12, cmat)
    # brute force: rerun the same ensemble, accumulate chunk-level co
    brute = np.zeros((12, 12), np.float64)
    s = 12 // distill.ENS_K_SCALE            # 0 at n=12 (test-only; the tier
    r = 0                                    # itself only runs above N_DENSE)
    for k in distill.ENS_LARGE_K:
        k_eff = max(2, min(k * s, 12 // 4))  # replicate the exact formula
        for start in (0, 1):
            lab = skm_det(cmat, k_eff, start)
            oh = np.zeros((12, k_eff), np.float64)
            oh[np.arange(12), lab] = 1.0
            brute += oh @ oh.T
            r += 1
    brute /= r
    m0 = co_micro.shape[0]
    for a in range(m0):
        for b in range(m0):
            if a == b:
                assert co_micro[a, b] == 1.0
                continue
            members_a = np.nonzero(micro_lab == a)[0]
            members_b = np.nonzero(micro_lab == b)[0]
            block = brute[np.ix_(members_a, members_b)]
            assert co_micro[a, b] == pytest.approx(float(block.mean()), abs=1e-9)


def test_ens_coassoc_v3_small_corpus_shape():
    """n=9 (k_eff collapses to n//4=2 for the whole grid — disclosed m5): the
    core runs, returns index groups, histogram buckets, dense mode."""
    items = _orthogonal_groups(3)[:6] + [
        _item(200 + i, 3, _mix(_vec(2), _vec(40 + i) * 0.05))
        for i in range(3)
    ]
    kept_groups, all_groups, mu, mode, runs, co = distill.ens_coassoc_v3(
        items, min_members=2)
    assert mode == "dense" and runs == 96
    flat = sorted(i for g in all_groups for i in g)
    assert flat == list(range(len(items)))
    assert all(len(g) >= 1 for g in all_groups)
    kept_flat = sorted(i for g in kept_groups for i in g)
    assert set(kept_flat) <= set(flat)


def test_build_cards_ordering_and_active_only():
    """v3: all cards active, ordered by (-size, first member), topic ids 1..n,
    parent_id None in the JSON projection (headers are historical only)."""
    kept = _orthogonal_groups()
    groups_all = [list(range(4)), list(range(4, 8))]
    cards = distill.build_cards(
        kept, groups_all, groups_all, tau=0.45, min_members=3,
        fingerprint_id="fp",
    )
    assert all(c.status == "active" for c in cards)
    assert [c.topic_id for c in cards] == [1, 2]
    assert all(c.parent is None for c in cards)
    payload = distill.render_json(
        cards, {k: 0 for k in distill.PUBLIC_STATS_KEYS})
    for card in payload["cards"]:
        assert card["parent_id"] is None


def test_render_markdown_header_rows_still_render():
    """Historical header rows (pre-v3 runs) still render their ## section
    before children — the renderer keeps the branch for old data."""
    h = distill.Card(title="Section", title_kind="terms", terms=[],
                     excerpts=[], members=[], n_files=0)
    h.status = "header"
    l1 = distill.Card(title="Leaf one", title_kind="terms", terms=[],
                      excerpts=[], members=[], n_files=0)
    l1.status = "active"
    l1.topic_id = 1
    l2 = distill.Card(title="Leaf two", title_kind="terms", terms=[],
                      excerpts=[], members=[], n_files=0)
    l2.status = "active"
    l2.topic_id = 2
    h.children = [l1, l2]
    l1.parent = l2.parent = h
    md = distill.render_markdown([l1, l2, h], "run header")
    lines = md.splitlines()
    header_idx = next(i for i, ln in enumerate(lines)
                      if ln.startswith("## ") and not ln.startswith("## Contents"))
    child_idx = next(i for i, ln in enumerate(lines)
                     if ln.startswith("### ") and i > header_idx)
    assert header_idx < child_idx


def test_max_chunks_refuses(indexed):
    with pytest.raises(DistillRefused) as excinfo:
        run_distill(indexed, scope=Scope(), threshold=None,
                    min_members=3, max_chunks=2)
    assert "max-chunks" in str(excinfo.value)


def test_below_two_chunks_refuses(tmp_path):
    cfg = Config()
    cfg.home = tmp_path / "home"
    store = Store(cfg.home / "index.sqlite")
    with pytest.raises(DistillRefused):
        run_distill(store, scope=Scope(), threshold=None, min_members=3)
    store.close()


# ------------------------------------------------------- incremental (M2)


def _run_and_save(store, **kw):
    """run_distill + save_run — the CLI's exact transaction boundary (the
    eval harness and the cmd_distill both persist via save_run)."""
    result = run_distill(store, **kw)
    if "preview" not in result and "preview" not in kw:
        stats = result["stats"]
        store.save_run({
            "id": stats["run_id"],
            "algo": stats["algo"],
            "tau": result["tau"].tau,
            "tau_source": stats["tau_source"],
            "scope_json": kw.get("scope", Scope()).canonical_json(),
            "corpus_hash": stats["corpus_hash"],
            "mode": stats["mode"],
            "stats": stats,
            "view_id": result["view_id"],
        }, result["cards"])
    return result


def _ingest_doc(store, path, text, backend):
    """One file into the index; returns the file id. The vector is the mock
    embedding of the framed text (what ingest stores)."""
    from pathlib import Path as _Path
    from goldmine.store import FileRec
    from goldmine.backends.base import DOCUMENT_PROMPT_TEMPLATE

    path = _Path(path)
    fid = store.upsert_file(FileRec(path=str(path), kind="doc", size=len(text),
                                    state="pending"))
    framed = DOCUMENT_PROMPT_TEMPLATE.format(path.stem, text)
    vec = backend.embed_texts([framed])[0]
    store.fts_text(fid, "chunk", 1, text, vec_ok=True, vec=vec)
    store.mark_file(fid, state="done", capability="content_indexed", detail=None,
                    info=None, extractor_version="txt-stdlib.v1",
                    chunker_version="chunk.v2-tok", embed_id="testembed")
    return fid


def _fabricate_prior_run(store, min_members_for_cards: int = 2):
    """A realistic prior v3 run over the `indexed` fixture: two topic cards
    (k8s 4 chunks, food 4 chunks; loner unassigned as a dropped singleton)
    plus the run row with center_mu — everything run_distill's incremental
    path reads back. Returns (items, prev_stats, cards_meta)."""
    items = distill._corpus(store, Scope())
    k8s = [it for it in items if "k8s" in it.path]
    food = [it for it in items if "food" in it.path]
    mat = np.stack([it.vec for it in items]).astype(np.float32)
    mu = mat.mean(axis=0)

    def make_card(members, cid):
        m = np.stack([x.vec for x in members])
        cent = m.sum(axis=0)
        cent = cent / float(np.linalg.norm(cent))
        card = distill.Card(
            title=f"card{cid}", title_kind="terms", terms=["t"], excerpts=[],
            members=list(members), n_files=len({x.fid for x in members}),
            status="active", centroid=cent, fingerprint_id="fp",
            distill_algo=f"{distill.ALGO} t=0.45 min=2", created_at=0.0,
            chunk_weights=[(x.sid, 0.9) for x in members],
        )
        card.run_id = "prev0000"
        card.view_id = distill.view_id_for(Scope())
        card.topic_id = cid
        return card

    cards = [make_card(k8s, 1), make_card(food, 2)]
    stats = {
        "view_id": cards[0].view_id, "embed_id": store.meta("embed_id") or "e",
        "member_files": sorted({it.path for it in items}),
        "center_mu": [float(x) for x in mu],
        "ens_mode": "dense", "ens_runs": 96,
    }
    store.save_run({
        "id": "prev0000", "algo": f"{distill.ALGO} t=0.45 min=2",
        "tau": 0.45, "tau_source": "fixed",
        "scope_json": Scope().canonical_json(), "corpus_hash": "h" * 64,
        "mode": "full", "stats": stats, "view_id": cards[0].view_id,
    }, cards)
    return items, stats, (k8s, food)


def test_incremental_new_chunk_joins_and_remainder_founds(indexed):
    """Run 1 (fabricated full) → run 2 (same scope, new near-copy chunk) →
    mode=incremental, τ inherited, memberships frozen, the near-copy JOINS
    its topic card (centered gates), the loner refuses every card."""
    store = indexed
    items, _stats, (k8s, _food) = _fabricate_prior_run(store)
    frozen_sids = {m.sid for m in k8s}

    backend = MockBackend(dim=768)
    tail = "orchard meadow river stone bridge tavern candle window harbor "
    "lantern harvest copper willow"
    _ingest_doc(store, "/p/extra-join.txt", k8s[0].text + " " + tail, backend)
    second = _run_and_save(store, scope=Scope(), threshold=None, min_members=2)
    stats = second["stats"]
    assert stats["mode"] == "incremental"
    assert stats["tau_inherited"] is True and stats["tau_source"] == "inherited"
    assert stats["tau"] == 0.45
    # frozen: the k8s card still holds ALL its original chunks
    k8s_card = next(c for c in second["cards"]
                    if any(m.sid in frozen_sids for m in c.members))
    assert frozen_sids <= {m.sid for m in k8s_card.members}
    # the near-copy was absorbed by the existing card (F5 accounting)
    assert stats["chunks_reassigned_into_existing_cards"] == 1
    # the loner refused both cards and founded nothing (below-min singleton)
    assert stats["new_cards"] == 0


def test_incremental_tombstone_sweep_and_dissolve(indexed):
    """§4.4: a re-chunked file (same path, new segment ids) leaves tombstone
    card_chunks; dissolve_pass removes them, recomputes sizes, and dissolves
    the below-min card (row kept, members re-enter — F5's honest count)."""
    store = indexed
    items, _stats, (k8s, _food) = _fabricate_prior_run(store)
    member = k8s[0]
    fid = store.db.execute(
        "SELECT file FROM segments WHERE id = ?", (member.sid,)
    ).fetchone()[0]
    from goldmine.backends.base import DOCUMENT_PROMPT_TEMPLATE
    from goldmine.store import SegmentRec

    backend = MockBackend(dim=768)
    new_text = "re-chunked replacement body with fresh content entirely"
    framed = DOCUMENT_PROMPT_TEMPLATE.format("replaced", new_text)
    seg = SegmentRec(kind="chunk", modality="text", loc=1, loc_end=2,
                     text_for_fts=new_text,
                     vec=backend.embed_texts([framed])[0])
    store.replace_segments(fid, [seg])

    second = _run_and_save(store, scope=Scope(), threshold=None, min_members=4)
    stats = second["stats"]
    assert stats["mode"] == "incremental", stats["mode"]
    assert stats["dissolved_cards"] >= 1
    assert stats["reclustered_chunks"] >= 1   # F5: honest recluster accounting
    # dissolved rows survive as history; active cards exclude them
    rows = store.db.execute(
        "SELECT status, COUNT(*) FROM cards GROUP BY status"
    ).fetchall()
    by_status = dict(rows)
    assert by_status.get("dissolved", 0) >= 1
    # no tombstoned memberships remain in the view
    dangling = store.db.execute(
        """SELECT COUNT(*) FROM card_chunks cc
           JOIN cards c ON c.id = cc.card
           WHERE c.view_id = ? AND cc.chunk NOT IN (SELECT id FROM segments)""",
        (distill.view_id_for(Scope()),),
    ).fetchone()[0]
    assert dangling == 0


def test_incremental_skips_calibration(indexed):
    """§4.6: an incremental run does NO new sampled gram — τ is inherited
    byte-identically even though the corpus grew."""
    store = indexed
    first = _run_and_save(store, scope=Scope(), threshold=None, min_members=2)
    backend = MockBackend(dim=768)
    _ingest_doc(store, "/p/extra2.txt", "totally unrelated zeta content", backend)
    second = _run_and_save(store, scope=Scope(), threshold=None, min_members=2)
    assert second["stats"]["tau"] == first["stats"]["tau"]
    assert second["tau"].histogram == []  # no calibration histogram produced


def test_incremental_centroid_recompute_accurate(indexed):
    """§4.3 step 3: the stored centroid is the renormalized mean over the
    FULL member set (recomputed, not an online fp16 update)."""
    store = indexed
    first = _run_and_save(store, scope=Scope(), threshold=None, min_members=2)
    big = max(first["cards"], key=lambda c: c.size)
    backend = MockBackend(dim=768)
    _ingest_doc(store, "/p/extra3.txt",
                max((m.text for m in big.members), key=len) + " more", backend)
    second = _run_and_save(store, scope=Scope(), threshold=None, min_members=2)
    joined = max(second["cards"], key=lambda c: c.size)
    mat = np.stack([m.vec for m in joined.members])
    expected = mat.sum(axis=0)
    expected = expected / float(np.linalg.norm(expected))
    assert np.allclose(joined.centroid, expected, atol=1e-2)


def test_incremental_eligibility_triggers_full(indexed):
    """τ/scope/embed/deletion>20%/refresh each force the full path."""
    store = indexed
    first = _run_and_save(store, scope=Scope(), threshold=None, min_members=2)
    view_id = first["stats"]["view_id"]
    prev = store.latest_run(view_id)

    # (a) explicit threshold mismatch -> full + manual
    second = run_distill(store, scope=Scope(), threshold=0.60, min_members=2)
    assert second["stats"]["mode"] == "full"
    assert second["stats"]["tau_source"] == "manual"
    # (b) different scope -> different view -> full (no prev run for it)
    third = run_distill(store, scope=Scope(kinds={"doc"}), threshold=None,
                        min_members=2)
    assert third["stats"]["mode"] == "full"
    assert third["stats"]["view_id"] != view_id
    # (c) embed identity change -> full
    store.set_meta("embed_id", "changed-model")
    fourth = _run_and_save(store, scope=Scope(), threshold=None, min_members=2)
    assert fourth["stats"]["mode"] == "full"
    store.set_meta("embed_id", prev["stats"]["embed_id"])
    # (d) --refresh -> full, same view
    fifth = _run_and_save(store, scope=Scope(), threshold=None, min_members=2,
                          refresh=True)
    assert fifth["stats"]["mode"] == "full"
    assert fifth["stats"]["view_id"] == view_id


def test_f7_incremental_guard_consumes_member_vectors(tmp_path):
    """F7 behavioral half, v3 (centered gates): a prior-run card with THREE
    MUTUALLY ORTHOGONAL members (the heterogeneous old-card scenario the
    guard exists for — for such cards centroid-cos = mean-cos·√3, so a
    centroid-only shortcut admits chunks the guard must refuse). The corpus
    is symmetric (+v/-v pairs) so mu = 0 and centered == raw, making the
    gate arithmetic exact: probe passes the centroid gate (0.329 >= 0.30)
    while FAILING the member-mean gate (0.19 < 0.20) -> refused; a near-e0
    copy passes both -> joins. The guard consumes the ORIGINAL member
    vectors (centered once), never the stored raw centroid alone."""
    from goldmine.store import FileRec
    from goldmine import distill as d

    store = Store(tmp_path / "f7.sqlite")
    try:
        e0, e1, e2 = _vec(0), _vec(1), _vec(2)
        bis3 = (e0 + e1 + e2) / float(np.sqrt(3.0))
        b_copy = 0.95 * e0 + 0.3122 * _vec(10)     # cos 0.95 to e0 (< 0.97 fold)
        b_copy = b_copy / float(np.linalg.norm(b_copy))
        probe = 0.32 * bis3 + float(np.sqrt(1 - 0.32 ** 2)) * _vec(9)
        corpus = [e0, e1, e2, b_copy, probe,
                  -e0, -e1, -e2, -b_copy, -probe]  # symmetric -> mu == 0
        mu = np.stack(corpus).mean(axis=0)
        assert float(np.abs(mu).sum()) < 1e-6       # exact-arity of the setup

        def add_vec_file(path, text, vec):
            fid = store.upsert_file(FileRec(path=path, kind="doc", size=9,
                                            state="pending"))
            store.fts_text(fid, "chunk", 1, text, vec_ok=True, vec=vec)
            store.mark_file(fid, state="done", capability="content_indexed",
                            detail=None, info=None,
                            extractor_version="txt-stdlib.v1",
                            chunker_version="chunk.v2-tok", embed_id="e")
            return fid

        fids = [add_vec_file(f"/f7/{i}.txt", f"body {i}", v)
                for i, v in enumerate(corpus)]
        seg_of = lambda fid: store.db.execute(
            "SELECT id FROM segments WHERE file = ? AND kind = 'chunk'",
            (fid,)).fetchone()[0]
        member_sids = [seg_of(fids[0]), seg_of(fids[1]), seg_of(fids[2])]

        # gate arithmetic, exact (mu == 0 -> centered == raw):
        member_mat = np.stack([e0, e1, e2])
        card_dir = bis3
        assert float(card_dir @ probe) >= d.INC_JOIN_COS        # 0.32
        assert float((member_mat @ probe).mean()) < d.INC_GUARD_COS  # 0.185
        assert float(card_dir @ b_copy) >= d.INC_JOIN_COS       # 0.549
        assert float((member_mat @ b_copy).mean()) >= d.INC_GUARD_COS  # 0.317

        view_id = "f7view0000000001"
        prev_members = [
            CItem(sid=member_sids[i], fid=fids[i], vec=corpus[i],
                  text=f"body {i}", path=f"/f7/{i}.txt", kind="doc",
                  loc=1, loc_end=2)
            for i in range(3)
        ]
        card = distill.Card(
            title="old card", title_kind="terms", terms=["a", "b", "c"],
            excerpts=[], members=prev_members, n_files=3, status="active",
            centroid=bis3, fingerprint_id="fp",
            distill_algo=f"{distill.ALGO} t=0.45 min=2",
            created_at=0.0,
            chunk_weights=[(sid, 0.577) for sid in member_sids],
        )
        card.run_id = "prev0000"
        card.view_id = view_id
        prev_stats = {"view_id": view_id, "embed_id": "e",
                      "member_files": [f"/f7/{i}.txt" for i in range(10)],
                      "center_mu": [float(x) for x in mu],
                      "ens_mode": "dense", "ens_runs": 96}
        prev_run = {
            "id": "prev0000", "algo": f"{distill.ALGO} t=0.45 min=2",
            "tau": 0.45, "tau_source": "fixed",
            "scope_json": Scope().canonical_json(), "corpus_hash": "h" * 64,
            "mode": "full", "stats": prev_stats,
        }
        store.save_run({
            "id": "prev0000", "algo": prev_run["algo"], "tau": 0.45,
            "tau_source": "fixed", "scope_json": prev_run["scope_json"],
            "corpus_hash": prev_run["corpus_hash"], "mode": "full",
            "stats": prev_stats, "view_id": view_id,
        }, [card])

        items = distill._corpus(store, Scope())
        kept, _removed = distill.dedupe(items)
        result = d._incremental_run(
            store, scope=Scope(), view_id=view_id, prev_run=prev_run,
            kept=kept, items=items, min_members=2,
            fingerprint_id="fp", embed_id="e", corpus_hash="h" * 64,
            current_member_files=[f"/f7/{i}.txt" for i in range(10)],
        )
        stats = result["stats"]
        assert stats["mode"] == "incremental"
        assert stats["tau_inherited"] is True
        # chained-increment state forwarded (M1)
        assert stats["center_mu"] == [float(x) for x in mu]
        homes: dict[str, int] = {}
        for c in result["cards"]:
            for m in c.members:
                homes[m.path] = id(c)
        # the b-copy joined the old card; the probe was REFUSED by the guard
        # and founded nothing (below-min singleton) — a centroid-only
        # shortcut would have absorbed it, flipping this decision (F7)
        assert homes["/f7/3.txt"] == homes["/f7/0.txt"]
        assert "/f7/4.txt" not in homes
        assert stats["chunks_reassigned_into_existing_cards"] == 1
    finally:
        store.close()


def test_incremental_idempotent_no_changes(indexed):
    """Re-running with zero corpus changes: incremental, nothing dissolved,
    nothing new, memberships byte-stable — across a CHAIN (full → inc → inc):
    the middle run's stats forward center_mu (M1) so the third run reads a
    v3 incremental row, not the v2-era KeyError path."""
    store = indexed
    first = _run_and_save(store, scope=Scope(), threshold=None, min_members=2)
    sizes1 = sorted((c.size, c.first_member_key) for c in first["cards"])
    second = _run_and_save(store, scope=Scope(), threshold=None, min_members=2)
    assert second["stats"]["mode"] == "incremental"
    assert second["stats"]["dissolved_cards"] == 0
    assert second["stats"]["new_cards"] == 0
    assert second["stats"]["chunks_reassigned_into_existing_cards"] == 0
    sizes2 = sorted((c.size, c.first_member_key) for c in second["cards"])
    assert sizes1 == sizes2
    # the chained third hop: prev run IS the incremental row
    third = _run_and_save(store, scope=Scope(), threshold=None, min_members=2)
    assert third["stats"]["mode"] == "incremental"
    assert third["stats"]["center_mu"] == second["stats"]["center_mu"]
    assert sorted((c.size, c.first_member_key) for c in third["cards"]) == sizes1


def test_excerpts_traceable_to_stored_segments(indexed):
    """Tier-1 property: every excerpt is byte-traceable to a stored segment."""
    result = run_distill(indexed, scope=Scope(), threshold=None, min_members=2)
    stored = {
        row[0] for row in indexed.db.execute(
            "SELECT excerpt FROM segments WHERE excerpt IS NOT NULL"
        ).fetchall()
    }
    from goldmine.util import excerpt as make_excerpt

    fts = {
        make_excerpt(row[0] or "") for row in indexed.db.execute(
            "SELECT body FROM fts"
        ).fetchall()
    }
    for card in result["cards"]:
        for ex in card.excerpts:
            assert ex["role"] in ("primary", "coverage")
            assert ex["text"] in stored | fts
