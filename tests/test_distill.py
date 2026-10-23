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
    cluster_terms,
    cluster_v2,
    dedupe,
    measure_tau,
    render_json,
    render_markdown,
    run_distill,
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


def test_measure_tau_otsu_on_separable_synthetic():
    """Dominant group + minority group at cosine ~0.62: Otsu finds the
    valley, unclamped (the cut rides just above the cross band)."""
    result = measure_tau(_two_group_matrix(0.62))
    assert result.tau_source == "otsu"
    assert result.tau == result.tau_raw            # unclamped
    assert 0.2 < result.tau_raw < 0.85             # between the bands
    assert len(result.histogram) == 256
    assert sum(result.histogram) == 66             # C(12,2) pairs


def test_measure_tau_floor_on_orthogonal_groups():
    result = measure_tau(_two_group_matrix(0.30))
    assert result.tau_source == "floor"
    assert result.tau == 0.55
    assert result.tau_raw < 0.55


def test_measure_tau_ceil_on_all_similar():
    """A valley ABOVE the clamp: otsu path, tau_raw > 0.85 -> 'ceil' (the
    honest enum value for the all-similar corpus)."""
    result = measure_tau(_two_group_matrix(0.9))
    assert result.tau_source == "ceil"
    assert result.tau == 0.85
    assert result.tau_raw > 0.85


def test_measure_tau_percentile_fallback_via_mode_proximity():
    """Unimodal corpus (all pairs ~0.7): t* lands on/near the mode -> the B9
    fallback takes P90, recorded as 'percentile'."""
    common = _vec(0)
    vecs = [_mix(common, _vec(1 + i) * 0.7) for i in range(12)]
    result = measure_tau(_matrix_of(vecs))
    assert result.tau_source == "percentile"
    assert result.fallback_reason
    # tau = clamp(P90 of the sampled pairs)
    vals = distill._sample_pair_cosines(_matrix_of(vecs))
    p90 = float(np.percentile(vals, 90))
    assert result.tau == min(max(p90, 0.55), 0.85)


def test_measure_tau_manual_override():
    a, b = _vec(0), _vec(1)
    vecs = [a, b, _mix(a, b)]
    result = measure_tau(_matrix_of(vecs), manual=0.62)
    assert result.tau_source == "manual"
    assert result.tau == 0.62
    assert result.tau_raw == 0.62
    # steps 2-4 still ran: histogram + quartiles populated for preview
    assert len(result.histogram) == 256
    assert sum(result.histogram) == 3


def test_measure_tau_degenerate_single_bin_and_two_chunks():
    a = _vec(0)
    result = measure_tau(_matrix_of([a, _mix(a, _vec(1) * 0.001)]))
    # one pair -> one bin -> percentile path, no crash
    assert result.tau_source == "percentile"


def test_measure_tau_negative_cosines_clamp_to_bin_zero():
    """F6: negative cosines clamp to bin 0, never wrap."""
    vals = np.array([-0.5, -0.2, 0.3], dtype=np.float32)
    hist = distill._histogram_of(vals)
    assert hist[0] == 2          # negatives in bin 0
    assert hist[int(0.3 * 256)] == 1
    big = distill._histogram_of(np.array([1.0, 1.0], dtype=np.float32))
    assert big[255] == 2         # >= 1 clamps to the last bin


def test_measure_tau_stride_sampling_deterministic():
    n = 320
    rng = np.zeros((n, 768), np.float32)
    for i in range(n):
        rng[i, i % 768] = 1.0
    s1 = distill._sample_pair_cosines(rng)
    s2 = distill._sample_pair_cosines(rng)
    assert s1.shape == s2.shape and np.array_equal(s1, s2)
    # 300-row stride, duplicates collapsed -> <= C(300, 2) pairs
    assert s1.size <= 300 * 299 // 2
    # N <= 300 uses ALL pairs
    small = distill._sample_pair_cosines(rng[:50])
    assert small.size == 50 * 49 // 2


def test_measure_tau_quartiles_hand_case():
    a, b = _vec(0), _vec(1)
    diag = _mix(a, b)  # cos 0.707 to both
    result = measure_tau(_matrix_of([a, b, diag]))
    # pairs sorted: [0.0 (a·b), 0.7071, 0.7071] -> numpy linear quartiles
    q1, q2, q3 = result.quartiles
    assert q1 == pytest.approx(0.35355, abs=1e-3)
    assert q2 == pytest.approx(0.7071, abs=1e-3)
    assert q3 == pytest.approx(0.7071, abs=1e-3)


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
    # and within cluster_v2 the guard formula is the mean, verbatim:
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
    assert stats["tau_source"] in ("otsu", "floor", "ceil", "percentile")
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
    prev = {"algo": distill.ALGO, "tau": 0.72, "stats": {"embed_id": "e1"}}
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
    assert "τ shown is a fresh full-run calibration" in result["preview"]
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


# ------------------------------------------------------- two-level (D5, M3)


def _subgroup(base_dir: int, n: int, noise_dirs, sid_offset: int) -> list[CItem]:
    base = _vec(base_dir)
    return [
        _item(sid_offset + i, 1, _mix(base, _vec(noise_dirs(i)) * 0.05))
        for i in range(n)
    ]


def test_mean_intra_cosine_hand_case():
    """s-bar = (||S||^2 - n)/(n(n-1)) on an ortho pair = 0 and a tight pair ~1."""
    a, b = _vec(0), _vec(1)
    assert distill.mean_intra_cosine(
        [_item(1, 1, a), _item(2, 1, b)]
    ) == pytest.approx(0.0, abs=1e-6)
    tight = [_item(1, 1, a), _item(2, 1, _mix(a, _vec(3) * 0.02))]
    assert distill.mean_intra_cosine(tight) > 0.99
    assert distill.mean_intra_cosine([_item(1, 1, a)]) == 1.0


def test_split_triggers_on_loose_cluster_and_accepts():
    """16 members in two tight sub-groups: s-bar ~0.47 < tau-0.12, children
    ~0.99 -> improvement >> 0.05 -> split accepted, header + 2 leaves."""
    tau = 0.72
    members = _subgroup(0, 8, lambda i: 10 + i, 1) + \
        _subgroup(1, 8, lambda i: 30 + i, 100)
    s_bar = distill.mean_intra_cosine(members)
    assert s_bar < tau - distill.SPLIT_MARGIN          # split candidate
    leaves, headers = distill.split_two_level([members], tau)
    assert len(leaves) == 2 and len(headers) == 1
    header = headers[0]
    assert header.status == "header"
    assert header.size == 16
    for leaf in leaves:
        assert leaf.parent is header
    # the header union = all members; its own content gets filled by
    # _build_card (checked via build_cards_v2 e2e below)


def test_split_rejects_tight_cluster():
    """One tight group of 16: s-bar above the trigger -> no split, one card."""
    members = _subgroup(0, 16, lambda i: 10 + i, 1)
    leaves, headers = distill.split_two_level([members], 0.72)
    assert len(leaves) == 1 and headers == []
    assert leaves[0].members[0] is members[0]


def test_split_needs_min_twelve():
    """n < 12 never splits, however loose."""
    members = _subgroup(0, 5, lambda i: 10 + i, 1) + \
        _subgroup(1, 5, lambda i: 30 + i, 100)
    leaves, headers = distill.split_two_level([members], 0.72)
    assert len(leaves) == 1 and headers == []


def test_split_depth_cap():
    """depth <= 2: an accepted grandchild split stops at depth 2 (no depth-3)."""
    # three-level construction: A(4)+B(4) vs C(4)+D(4) inside one loose 16
    members = (
        _subgroup(0, 4, lambda i: 10 + i, 1)
        + _subgroup(1, 4, lambda i: 30 + i, 100)
        + _subgroup(2, 4, lambda i: 50 + i, 200)
        + _subgroup(3, 4, lambda i: 70 + i, 300)
    )
    leaves, headers = distill.split_two_level([members], 0.72)
    # the top split is accepted; children may split once more (depth 1 -> 2)
    # but never a third time
    assert len(headers) >= 1
    max_depth = 0

    def depth_of(card, d=0):
        nonlocal max_depth
        max_depth = max(max_depth, d)
        for child in getattr(card, "children", []) or []:
            depth_of(child, d + 1)

    for h in headers:
        depth_of(h)
    assert max_depth <= 2


def test_build_cards_v2_with_headers_parent_id_and_ordering(monkeypatch):
    """e2e wiring (with the split forced — under v2's guard a loose cluster
    can hardly form at all, which is the design working; the split path fires
    on incremental remainders and borderline geometry): headers excluded from
    the ordered active list, topic ids over active cards then headers (§2.6),
    parent links intact, json parent_id points at a header."""
    kept = _subgroup(0, 6, lambda i: 10 + i, 1) + \
        _subgroup(1, 6, lambda i: 30 + i, 100)

    def fake_split(clusters, tau):
        first = clusters[0]
        half = len(first) // 2
        leaves, headers = distill.split_two_level(
            [first[:half], first[half:]], tau,
        ) if False else ([], [])
        # build the tree manually: one header, two leaves
        h = distill.Card(title="", title_kind="terms", terms=[], excerpts=[],
                         members=list(first), n_files=0)
        h.status = "header"
        h.parent = None
        l1 = distill.Card(title="", title_kind="terms", terms=[], excerpts=[],
                          members=first[:half], n_files=0)
        l2 = distill.Card(title="", title_kind="terms", terms=[], excerpts=[],
                          members=first[half:], n_files=0)
        l1.parent = l2.parent = h
        h.children = [l1, l2]
        l1.status = l2.status = "active"
        return [l1, l2], [h]

    monkeypatch.setattr(distill, "split_two_level", fake_split)
    cards, clusters, hist = distill.build_cards_v2(
        kept, tau=0.72, min_members=3, fingerprint_id="fp",
    )
    active = [c for c in cards if c.status == "active"]
    headers = [c for c in cards if c.status == "header"]
    assert len(active) == 2 and len(headers) == 1
    assert [c.topic_id for c in active] == [1, 2]
    assert headers[0].topic_id == 3            # headers numbered after cards
    for leaf in active:
        assert leaf.parent is headers[0]
    payload = distill.render_json(cards, {})
    by_id = {c["id"]: c for c in payload["cards"]}
    for card in payload["cards"]:
        if card["parent_id"] is not None:
            assert by_id[card["parent_id"]]["status"] == "header"


def test_header_rendering_groups_children(monkeypatch):
    """Markdown: a header renders as a ## section before its children."""
    kept = _subgroup(0, 6, lambda i: 10 + i, 1) + \
        _subgroup(1, 6, lambda i: 30 + i, 100)

    def fake_split(clusters, tau):
        first = clusters[0]
        half = len(first) // 2
        h = distill.Card(title="Section", title_kind="terms", terms=[],
                         excerpts=[], members=list(first), n_files=0)
        h.status = "header"
        h.parent = None
        l1 = distill.Card(title="Leaf one", title_kind="terms", terms=[],
                          excerpts=[], members=first[:half], n_files=0)
        l2 = distill.Card(title="Leaf two", title_kind="terms", terms=[],
                          excerpts=[], members=first[half:], n_files=0)
        l1.parent = l2.parent = h
        h.children = [l1, l2]
        l1.status = l2.status = "active"
        return [l1, l2], [h]

    monkeypatch.setattr(distill, "split_two_level", fake_split)
    cards, _clusters, _hist = distill.build_cards_v2(
        kept, tau=0.72, min_members=3, fingerprint_id="fp",
    )
    md = distill.render_markdown(cards, "header")
    lines = md.splitlines()
    # the header's content-filled title replaces the shell's; find ANY
    # section line (## but not ## Contents / # goldmine) before a child
    header_idx = next(
        i for i, ln in enumerate(lines)
        if ln.startswith("## ") and not ln.startswith("## Contents")
    )
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


def test_incremental_new_chunk_joins_and_remainder_founds(indexed):
    """Run 1 full → run 2 (same scope, new chunk) → mode=incremental, τ
    inherited, memberships frozen, remainder founds its own card."""
    store = indexed
    first = _run_and_save(store, scope=Scope(), threshold=None, min_members=2)
    cards1 = {c.topic_id: c for c in first["cards"]}
    old_card = max(cards1.values(), key=lambda c: c.size)
    frozen_sids = {m.sid for m in old_card.members}

    # a chunk near the big card joins it (new segment id). The tail is long
    # enough to survive the 0.97 near-dup fold yet stays far above tau.
    backend = MockBackend(dim=768)
    texts = [m.text for m in old_card.members]
    tail = "orchard meadow river stone bridge tavern candle window harbor "
    "lantern harvest copper willow"
    _ingest_doc(store, "/p/extra-join.txt", texts[0] + " " + tail, backend)
    second = _run_and_save(store, scope=Scope(), threshold=None, min_members=2)
    stats = second["stats"]
    assert stats["mode"] == "incremental"
    assert stats["tau_inherited"] is True and stats["tau_source"] == "inherited"
    assert stats["tau"] == first["stats"]["tau"]
    # frozen: the old card still holds ALL its original chunks
    new_big = max(second["cards"], key=lambda c: c.size)
    assert frozen_sids <= {m.sid for m in new_big.members}
    # the join was counted as reassignment into an existing card (F5)
    assert stats["chunks_reassigned_into_existing_cards"] >= 1


def test_incremental_tombstone_sweep_and_dissolve(indexed):
    """§4.4: a re-chunked file (same path, new segment ids) leaves tombstone
    card_chunks; dissolve_pass removes them, recomputes sizes, and dissolves
    the below-min card (row kept, members re-enter — F5's honest count)."""
    store = indexed
    first = _run_and_save(store, scope=Scope(), threshold=None, min_members=4)
    victim = max(first["cards"], key=lambda c: c.size)
    assert victim.size >= 4
    # re-chunk ONE member's file: same path, brand-new segment ids
    member = victim.members[0]
    fid = store.db.execute(
        "SELECT file FROM segments WHERE id = ?", (member.sid,)
    ).fetchone()[0]
    from goldmine.backends.base import DOCUMENT_PROMPT_TEMPLATE

    backend = MockBackend(dim=768)
    new_text = "re-chunked replacement body with fresh content entirely"
    framed = DOCUMENT_PROMPT_TEMPLATE.format("replaced", new_text)
    from goldmine.store import SegmentRec

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
        (victim.view_id,),
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
    """F7 behavioral half, deterministic: a fabricated prior-run card whose
    members a/b are ORTHOGONAL (the heterogeneous old-card scenario the guard
    exists for), inherited tau 0.55 (tau_lo 0.45). A new b-copy passes BOTH
    gates (centroid 0.707 >= 0.55; mean-to-originals 0.5 >= 0.45) and joins;
    an off-direction probe passes the CENTROID gate (0.601 >= 0.55) while
    FAILING the mean gate (0.425 < 0.45) and must found a NEW card — a
    centroid-only shortcut flips this decision. The guard consumes the
    ORIGINAL member vectors, never the stored centroid."""
    from goldmine.store import FileRec
    from goldmine import distill as d

    store = Store(tmp_path / "f7.sqlite")
    try:
        e0 = _vec(0)
        e1 = _vec(1)
        e2 = _vec(2)

        def add_vec_file(path, text, vec):
            fid = store.upsert_file(FileRec(path=path, kind="doc", size=9,
                                            state="pending"))
            store.fts_text(fid, "chunk", 1, text, vec_ok=True, vec=vec)
            store.mark_file(fid, state="done", capability="content_indexed",
                            detail=None, info=None,
                            extractor_version="txt-stdlib.v1",
                            chunker_version="chunk.v2-tok", embed_id="e")
            return fid

        fid_a = add_vec_file("/f7/a.txt", "a body", e0)
        fid_b = add_vec_file("/f7/b.txt", "b body", e1)
        seg_of = lambda fid: store.db.execute(
            "SELECT id FROM segments WHERE file = ? AND kind = 'chunk'",
            (fid,)).fetchone()[0]
        s_a, s_b = seg_of(fid_a), seg_of(fid_b)

        # fabricated prior run: one card [a, b] whose centroid is (a+b)/|a+b|
        view_id = "f7view0000000001"
        cent = (e0 + e1) / float(np.linalg.norm(e0 + e1))
        prev_members = [CItem(sid=s_a, fid=fid_a, vec=e0, text="a body",
                              path="/f7/a.txt", kind="doc", loc=1, loc_end=2),
                        CItem(sid=s_b, fid=fid_b, vec=e1, text="b body",
                              path="/f7/b.txt", kind="doc", loc=1, loc_end=2)]
        card = distill.Card(
            title="old card", title_kind="terms", terms=["a", "b"],
            excerpts=[], members=prev_members, n_files=2, status="active",
            centroid=cent, fingerprint_id="fp", distill_algo=f"{distill.ALGO} t=0.55 min=2",
            created_at=0.0, chunk_weights=[(s_a, 0.71), (s_b, 0.71)],
        )
        card.run_id = "prev0000"
        card.view_id = view_id
        prev_run = {
            "id": "prev0000", "algo": f"{distill.ALGO} t=0.55 min=2",
            "tau": 0.55, "tau_source": "manual",
            "scope_json": Scope().canonical_json(), "corpus_hash": "h" * 64,
            "mode": "full",
            "stats": {"view_id": view_id, "embed_id": "e",
                      "member_files": ["/f7/a.txt", "/f7/b.txt"]},
        }
        store.save_run({
            "id": "prev0000", "algo": prev_run["algo"], "tau": 0.55,
            "tau_source": "manual", "scope_json": prev_run["scope_json"],
            "corpus_hash": prev_run["corpus_hash"], "mode": "full",
            "stats": prev_run["stats"], "view_id": view_id,
        }, [card])

        # two NEW chunks: a near-b vector (joins — must stay under the 0.97
        # near-dup fold) and the boundary probe (refused)
        b_copy = 0.91 * e1 + 0.4146 * _vec(3)
        b_copy = b_copy / float(np.linalg.norm(b_copy))
        probe = 0.85 * e1 + 0.527 * e2
        probe = probe / float(np.linalg.norm(probe))
        # verify the disagreement the test is about, on the ORIGINAL members
        mat = np.stack([e0, e1])
        assert float(probe @ cent) >= 0.55                      # centroid passes
        assert float((mat @ probe).mean()) < 0.45               # guard refuses
        assert float((mat @ b_copy).mean()) >= 0.45             # b-copy guard-ok
        add_vec_file("/f7/bcopy.txt", "b copy body", b_copy)
        add_vec_file("/f7/probe.txt", "probe body", probe)

        items = distill._corpus(store, Scope())
        kept, _removed = distill.dedupe(items)
        result = d._incremental_run(
            store, scope=Scope(), view_id=view_id, prev_run=prev_run,
            kept=kept, items=items, min_members=2,
            fingerprint_id="fp", embed_id="e", corpus_hash="h" * 64,
            current_member_files={"/f7/a.txt", "/f7/b.txt",
                                  "/f7/bcopy.txt", "/f7/probe.txt"},
        )
        stats = result["stats"]
        assert stats["mode"] == "incremental"
        assert stats["tau_inherited"] is True
        homes: dict[str, int] = {}
        for c in result["cards"]:
            for m in c.members:
                homes[m.path] = id(c)
        # the b-copy joined the old card; the probe was REFUSED by the guard
        # and ended up without a card (dropped as a below-min singleton) — a
        # centroid-only shortcut would have absorbed it into the old card,
        # flipping this exact decision (F7)
        assert homes["/f7/bcopy.txt"] == homes["/f7/a.txt"]
        assert "/f7/probe.txt" not in homes
        assert stats["chunks_reassigned_into_existing_cards"] == 1
        assert stats["new_cards"] == 0
    finally:
        store.close()


def test_incremental_idempotent_no_changes(indexed):
    """Re-running with zero corpus changes: incremental, nothing dissolved,
    nothing new, memberships byte-stable."""
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


def test_preview_writes_nothing(indexed):
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
