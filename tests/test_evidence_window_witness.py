"""search.v7-evidence-window-witness (preregistration evidence-window-witness-001).

Two evidence-side rules, both preregistered and simulated on dev before
implementation: within an admitted file the semantic primary yields to a
segment whose densest 200-char window holds >= 2 more distinct query units
(and at least as many body units); a hybrid file sharing no query word (zero
coverage, no FTS rank) is not admitted on z alone.
"""
from __future__ import annotations

import pytest

from goldmine.admission import AdmissionParameters, decide_admission
from goldmine.lexical import LexicalResult, query_units
from goldmine.retrieval_evidence import WINDOW_CHARS, WINDOW_POSITION_CAP, select_evidence, window_units
from goldmine.search import Searcher
from goldmine.store import FileRec, SegmentRec, Store
from test_search_offline import FixedBackend, evec


def units_for(query, phrases=()):
    return query_units(query, Searcher.terms(query), list(phrases))


# ----------------------------------------------------------- frozen matching rule

def test_window_counts_distinct_units_inside_one_window():
    units = units_for("connect timeout retries")
    text = "the connect timeout governs retries"          # all three within 200 chars
    assert window_units(text, units) == 3
    spread = "connect " + "x " * 150 + "timeout " + "y " * 150 + "retries"
    assert window_units(spread, units) == 1              # never two inside 200 chars
    assert window_units("", units) == 0 and window_units(text, []) == 0


def test_window_boundary_is_strictly_under_window_chars():
    units = units_for("alpha omega")
    assert window_units("alpha" + " " * (WINDOW_CHARS - 6) + "omega", units) == 2   # omega at 199
    assert window_units("alpha" + " " * (WINDOW_CHARS - 5) + "omega", units) == 1   # omega at 200


def test_only_the_last_latin_unit_matches_by_prefix():
    units = units_for("connect timeout")                 # 'timeout' carries the FTS prefix
    assert window_units("connect timeouts", units) == 2
    assert window_units("connections timeout", units) == 1   # 'connect' is whole-token only
    assert window_units("connect-timeout", units) == 2       # tokens split on non [0-9a-z]


def test_cjk_grams_and_phrases_match_by_substring():
    units = units_for("响应式状态 内存")
    assert window_units("组件的响应式状态会占用内存", units) == 2
    assert window_units("无关文本", units) == 0
    phrased = units_for("timeout", phrases=["connect timeout applies"])
    assert window_units("the connect timeout applies here", phrased) == 2


def test_position_cap_bounds_the_scan():
    units = units_for("alpha beta")
    text = ("alpha " * (WINDOW_POSITION_CAP + 40)) + "beta"
    # beta is beyond the first 64 alpha positions' 200-char reach; still counted alone
    assert window_units(text, units) >= 1


# ------------------------------------------------------------- primary dominance

@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "window.sqlite")
    yield value
    value.close()


def source(store, texts):
    fid = store.upsert_file(FileRec(path="/synthetic/doc.md", state="done", capability="content_indexed"))
    store.replace_segments(fid, [SegmentRec(kind="name", modality="none", text_for_fts="owned folder")] + [
        SegmentRec(kind="chunk", modality="none", loc=20 * i + 1, loc_end=20 * i + 8, text_for_fts=text)
        for i, text in enumerate(texts)])
    return store.get_file(fid), [r[0] for r in store.db.execute(
        "SELECT id FROM segments WHERE file=? AND kind!='name' ORDER BY id", (fid,))]


def lexical_for(file, query, pairs):
    return LexicalResult(segment_units={sid: frozenset(u) for sid, u in pairs},
                         segment_files={sid: file.id for sid, _ in pairs}, units=units_for(query))


def test_concentrated_challenger_demotes_the_semantic_top(store):
    query = "connect timeout applies attempt"
    topical = "This chapter is about timeouts in general. " * 6
    answer = "The connect timeout applies to each connection attempt to an address."
    file, (sem_top, dense) = source(store, [topical, answer])
    words = lexical_for(file, query, [(sem_top, {1}), (dense, {0, 1, 2, 3})])
    chosen = select_evidence(store, file, sem_top, words, [(sem_top, 5.0, .9), (dense, 3.5, .8)], query_text=query)
    assert chosen.primary == dense
    assert chosen.demoted_semantic_primary == sem_top and chosen.window_units == 4


def test_margin_below_two_keeps_the_semantic_primary(store):
    query = "connect timeout"
    file, (sem_top, other) = source(store, ["connect settings explained", "the connect timeout"])
    words = lexical_for(file, query, [(sem_top, {0}), (other, {0, 1})])
    chosen = select_evidence(store, file, sem_top, words, [(sem_top, 5.0, .9), (other, 3.2, .8)], query_text=query)
    assert chosen.primary == sem_top and chosen.demoted_semantic_primary is None


# ------------------------------------------- span-window-v2-001 tier-2 takeover

def test_margin_one_with_z_qualification_and_parity_demotes(store):
    """Tier 2: one extra window unit swaps two semantically comparable windows."""
    query = "connect timeout"
    file, (sem_top, other) = source(store, ["connect settings explained", "the connect timeout"])
    words = lexical_for(file, query, [(sem_top, {0}), (other, {0, 1})])
    chosen = select_evidence(store, file, sem_top, words, [(sem_top, 5.0, .9), (other, 4.0, .85)], query_text=query)
    assert chosen.primary == other and chosen.demoted_semantic_primary == sem_top


def test_margin_one_without_z_gate_keeps_the_semantic_primary(store):
    query = "connect timeout"
    file, (sem_top, other) = source(store, ["connect settings explained", "the connect timeout"])
    words = lexical_for(file, query, [(sem_top, {0}), (other, {0, 1})])
    chosen = select_evidence(store, file, sem_top, words, [(sem_top, 5.0, .9), (other, 2.5, .7)], query_text=query)
    assert chosen.primary == sem_top and chosen.demoted_semantic_primary is None


def test_margin_two_takes_over_even_without_any_z(store):
    """Tier 1 stays z-independent: a strong window lead needs no semantic score."""
    query = "connect timeout applies"
    file, (sem_top, other) = source(store, ["connect explained settings", "the connect timeout applies to calls"])
    words = lexical_for(file, query, [(sem_top, {0}), (other, {0, 1, 2})])
    chosen = select_evidence(store, file, sem_top, words, [(sem_top, 5.0, .9)], query_text=query)
    assert chosen.primary == other and chosen.demoted_semantic_primary == sem_top


def test_zless_global_max_shadows_a_qualified_margin_one_peer(store):
    """Single-challenger semantics: the (window, units, z) global max is the only
    candidate tested, so a z-less margin-1 max blocks a peer that tier 2 would
    have admitted (span-window-v2-001 shadowing invariant)."""
    query = "connect timeout applies"
    spread = "connect timeout filler " + "x " * 200 + "applies noted"
    texts = ["connect explained settings", "the connect timeout answered", spread]
    file, ids = source(store, texts)
    sem_top, other, zless = ids
    words = lexical_for(file, query, [(sem_top, {0}), (other, {0, 1}), (zless, {0, 1, 2})])
    chosen = select_evidence(store, file, sem_top, words, [(sem_top, 5.0, .9), (other, 4.0, .85)], query_text=query)
    # zless carries the best units at margin 1, fails tier 2 (no z), and no fallback scan
    assert chosen.primary == sem_top and chosen.demoted_semantic_primary is None


def test_challenger_needs_at_least_as_many_body_units(store):
    query = "alpha beta gamma delta"
    file, (sem_top, dense) = source(store, ["alpha beta gamma delta spread " + "z " * 200 + "alpha",
                                            "alpha beta gamma"])
    # dense has the better window (3 vs 1) but fewer body units (3 vs 4): semantic stays
    words = lexical_for(file, query, [(sem_top, {0, 1, 2, 3}), (dense, {0, 1, 2})])
    chosen = select_evidence(store, file, sem_top, words, [(sem_top, 5.0, .9)], query_text=query)
    assert chosen.primary == sem_top


def test_without_a_semantic_standout_the_old_ladder_is_unchanged(store):
    query = "alpha beta"
    file, (first, second) = source(store, ["alpha beta here", "alpha"])
    words = lexical_for(file, query, [(first, {0, 1}), (second, {0})])
    chosen = select_evidence(store, file, second, words, [(second, 2.0, .5)], query_text=query)
    assert chosen.primary == first and chosen.demoted_semantic_primary is None


def test_body_exact_still_outranks_window_dominance(store):
    from goldmine.lexical import ExactMatch
    query = "Q7-L4 connect timeout applies"
    file, (exact_row, dense) = source(store, ["ticket Q7-L4 filed", "connect timeout applies to each attempt"])
    exact = ExactMatch(1, exact_row, "body", "Q7-L4", "raw", "Q7-L4")
    words = LexicalResult(segment_units={exact_row: frozenset({0}), dense: frozenset({1, 2, 3})},
                          segment_files={exact_row: file.id, dense: file.id}, exact={file.id: exact}, units=units_for(query))
    chosen = select_evidence(store, file, dense, words, [(dense, 6.0, .9)], query_text=query)
    assert chosen.primary == exact_row


# ----------------------------------------------------------------- lexical witness

def test_hybrid_semantic_standout_requires_a_lexical_witness():
    assert decide_admission(scorer="hybrid", z=5.0, coverage=0.0, lexical_match=False) is None
    assert decide_admission(scorer="hybrid", z=5.0, coverage=0.0, lexical_match=True) == "semantic_standout"
    assert decide_admission(scorer="hybrid", z=5.0, coverage=0.1, lexical_match=False) == "semantic_standout"
    assert decide_admission(scorer="hybrid", z=2.0, coverage=0.1, lexical_match=True) is None
    # ablations keep their own rules
    assert decide_admission(scorer="semantic", z=5.0, coverage=0.0) == "semantic_standout"
    assert decide_admission(scorer="rrf", z=5.0, coverage=0.0, lexical_match=False) == "semantic_standout"
    assert decide_admission(scorer="lexical", z=5.0, lexical_match=False) is None
    # quorum and exact admissions are untouched by the witness rule
    assert decide_admission(scorer="hybrid", z=None, coverage=0.5) == "lexical_quorum"
    assert decide_admission(scorer="hybrid", exact=True, coverage=0.0) == "verified_exact"
    assert decide_admission(scorer="hybrid", z=5.0, coverage=0.0, lexical_match=True,
                            parameters=AdmissionParameters(semantic_segment_margin=0.25), segment_margin=0.1) is None


def test_zero_overlap_file_is_not_returned_by_hybrid_but_still_by_semantic(store):
    fid = store.upsert_file(FileRec(path="/synthetic/unrelated.txt", state="done", capability="content_indexed"))
    store.replace_segments(fid, [SegmentRec(kind="name", modality="none", text_for_fts="owned folder"),
                                 SegmentRec(kind="chunk", modality="text", loc=1, loc_end=3,
                                            text_for_fts="completely different vocabulary", vec=evec(0))])
    for i in range(25):
        other = store.upsert_file(FileRec(path=f"/fill/{i}.txt", state="done", capability="content_indexed"))
        store.replace_segments(other, [SegmentRec(kind="name", modality="none", text_for_fts="owned folder"),
                                       SegmentRec(kind="chunk", modality="text", loc=1, loc_end=1, text_for_fts="filler", vec=evec(1))])
    backend = FixedBackend(evec(0))
    assert Searcher(store, backend).search("marathon registration locker", scorer="hybrid") == []
    semantic = Searcher(store, backend).search("marathon registration locker", scorer="semantic")
    assert semantic and semantic[0].path == "/synthetic/unrelated.txt"
    witnessed = Searcher(store, backend).search("different marathon locker", scorer="hybrid")
    assert witnessed and witnessed[0].admission_reason == "semantic_standout"


def test_textless_vector_file_is_exempt_from_the_witness_rule(store):
    """A pure image (vector, no FTS body) can never share a word: still z-admitted."""
    fid = store.upsert_file(FileRec(path="/img/pic.png", kind="image", state="done", capability="content_indexed"))
    store.replace_segments(fid, [SegmentRec(kind="name", modality="none", text_for_fts="owned folder"),
                                 SegmentRec(kind="image", modality="image", loc=1, text_for_fts=None, vec=evec(0))])
    for i in range(25):
        other = store.upsert_file(FileRec(path=f"/fill/{i}.txt", state="done", capability="content_indexed"))
        store.replace_segments(other, [SegmentRec(kind="name", modality="none", text_for_fts="owned folder"),
                                       SegmentRec(kind="chunk", modality="text", loc=1, loc_end=1, text_for_fts="filler", vec=evec(1))])
    hits = Searcher(store, FixedBackend(evec(0))).search("qqq zzq", scorer="hybrid")
    assert len(hits) == 1 and hits[0].path == "/img/pic.png" and hits[0].admission_reason == "semantic_standout"
