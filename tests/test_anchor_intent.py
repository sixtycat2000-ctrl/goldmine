"""anchor.v2-intent mechanical fixtures (c1-quality-repair-design-001 §5.1).

Clause intent (C-INT byte path vs C-NL soft path), anchor strength and
path-plausibility grammar, ambient never-bind, demote disclosure, the
bracket and path-suffix recall tiers, scope-grammar softening for prose,
and verification-bound accounting. Synthetic mechanics only.
"""
from __future__ import annotations

import pytest

from goldmine.anchor_admission import guard_composed_hits, resolve_obligations
from goldmine.anchor_resolution import Resolver
from goldmine.query_obligations import (Anchor, QueryPlan, anchor_strength,
                                        path_plausible, plan_query)
from goldmine.search import Searcher, _terms_of, parse_filters
from goldmine.store import FileRec, SegmentRec, Store
from test_search_offline import FixedBackend, evec


@pytest.fixture
def index(tmp_path):
    store = Store(tmp_path / "intent.sqlite")
    yield store
    store.close()


def add(store, text, *, path="/owned/source.txt", kind="doc", vector=None, title=None):
    fid = store.upsert_file(FileRec(path=path, kind=kind, state="done", capability="content_indexed",
                                    info={"title": title} if title is not None else None))
    texts = [text] if isinstance(text, str) else text
    store.replace_segments(fid, [SegmentRec(kind="name", modality="none", text_for_fts="owned folder")] + [
        SegmentRec(kind="chunk", modality="text" if vector is not None else "none",
                   loc=7 + i, loc_end=7 + i, text_for_fts=t, excerpt=t, vec=vector)
        for i, t in enumerate(texts)])
    return fid


def search(store, query, **kwargs):
    return Searcher(store, None).search(query, scorer="lexical", why=True, **kwargs)


def proof(hit):
    return hit.why["anchor_contract"]


def all_files(store):
    return {row[0] for row in store.db.execute("SELECT id FROM files WHERE state='done'")}


def decide(store, query, phrases=()):
    with store.read_snapshot():
        return resolve_obligations(store, query, phrases, all_files(store), _terms_of)


# --------------------------------------------------------------- 1. strength


@pytest.mark.parametrize("query", [
    "1992", "2025", "3551", "B12", "DMT1", "M240681", "ISO-8859-1", "21st",
    "100m", "400mg", "ZT-84", "MN-42", "SHA-256", "CBC/CFB", "1/2000",
    "HTTP/2", "nmol/liter.", "a/b", "/", "statistics:", "Alcohol:",
])
def test_ambient_anchor_shapes_are_never_obligations(query):
    assert all(anchor_strength(a) == "ambient" for a in plan_query(query).anchors)


@pytest.mark.parametrize("query", [
    "uuid.uuid9", "ssl.TLSVersion.TLSv1_9", "ZIP128_LIMIT", "ctypes.c_int128",
    "defineModels", "SearchPanel", "iTregs", ".env.staging", ".NET",
    "/abs/ptrn", "./rel/q.rs", "../up/x.csv", "ctypes/__init__.py",
    "Lib/email/base64mime.py",
])
def test_obligatory_anchor_shapes_bind_prose_clauses(query):
    assert all(anchor_strength(a) == "obligatory" for a in plan_query(query).anchors)


def test_backtick_literal_and_path_kind_grammar_corners():
    # Backtick literals are always obligations, whatever the surface shape.
    literal = plan_query("`randomzz ZT-84`").anchors[0]
    assert literal.kind == "literal" and anchor_strength(literal) == "obligatory"
    # A path-plausible tail alone (x.py) obligates as a path-kind anchor;
    # slash-free plain-dotted names stay ambient per the dotted rule.
    assert path_plausible("x.py")
    assert anchor_strength(Anchor(0, 0, 0, 5, "x.py", "path")) == "obligatory"
    assert anchor_strength(plan_query("x.py").anchors[0]) == "ambient"


@pytest.mark.parametrize("value,expected", [
    ("x.py", True), ("/abs/ptrn", True), ("./rel/q.rs", True), ("../up/x.csv", True),
    ("ctypes/__init__.py", True), ("Lib/email/base64mime.py", True),
    ("CBC/CFB", False), ("1/2000", False), ("HTTP/2", False),
    ("nmol/liter.", False), ("a/b", False), ("/", False),
])
def test_path_plausibility_grammar_table(value, expected):
    assert path_plausible(value) is expected


# ------------------------------------------------- 2. ambient never binds


def test_ambient_code_in_prose_never_binds_or_promotes(index):
    add(index, "refund ZT-84 approved")
    for i in range(25):
        add(index, "filler words only", path=f"/fill/{i}.txt")
    decision = decide(index, "randomzz ZT-84")
    # Ambient anchors are never bound: no bindings, no exact promotion.
    assert decision.bindings == () and decision.exact == {}
    assert decision.soft_clauses == ((0, "ambient_only"),)
    assert decision.ambient_anchors == ("ZT-84",) and not decision.constrained
    hits = search(index, "randomzz ZT-84")
    assert hits and hits[0].admission_reason in ("lexical_quorum", "semantic_standout")
    assert hits[0].admission_reason != "verified_exact" and "exact_source" not in hits[0].why
    assert hits[0].why["anchor_contract_soft"]["ambient_anchors"] == ["ZT-84"]


def test_ambient_code_ranks_identically_to_an_anchorless_plan(index, monkeypatch):
    add(index, "refund ZT-84 approved")
    for i in range(25):
        add(index, "filler words only", path=f"/fill/{i}.txt")
    query = "randomzz ZT-84"
    real = search(index, query)
    import goldmine.anchor_admission as admission
    monkeypatch.setattr(admission, "plan_query",
                        lambda q, phrases=(): QueryPlan((), (), tuple(phrases), None))
    anchorless = search(index, query)
    monkeypatch.undo()
    # The mechanical form of semantics_claim iii: an ambient-only clause is
    # indistinguishable in admission and ranking from no anchors at all.
    assert [(h.path, h.score, h.admission_reason) for h in real] == \
           [(h.path, h.score, h.admission_reason) for h in anchorless]


# --------------------------------------------------- 3. demote disclosure


def test_demoted_clause_discloses_soft_reason_and_bindings(index):
    add(index, "BUILD_ZONE alpha", path="/owned/a.txt")
    add(index, "BUILD_ZONE beta", path="/owned/b.txt")
    decision = decide(index, "BUILD_ZONE alpha beta")
    assert decision.soft_clauses == ((0, "remainder_soft"),)
    assert sorted((b.file_id, b.query_term) for b in decision.soft_bindings) == \
           [(1, "BUILD_ZONE"), (2, "BUILD_ZONE")]
    assert decision.exact == {} and not decision.constrained
    hits = search(index, "BUILD_ZONE alpha beta")
    assert {h.path for h in hits} == {"/owned/a.txt", "/owned/b.txt"}
    for hit in hits:
        soft = hit.why["anchor_contract_soft"]
        assert soft["soft_clauses"] == [{"clause_id": 0, "reason": "remainder_soft"}]
        assert {b["query_term"] for b in soft["obligatory_bindings"]} == {"BUILD_ZONE"}
        assert hit.admission_reason != "verified_exact" and "exact_source" not in hit.why
        assert "anchor_contract" not in hit.why  # soft clauses never constrain
    # guard_composed_hits passes soft decisions through unchanged.
    assert guard_composed_hits(index, decision, hits) == hits


# ------------------------------------------------------ 4. C-INT unchanged


def test_pure_code_lookup_keeps_verified_exact_contract(index):
    add(index, "approved ZT-84 at L7")
    hit = search(index, "ZT-84")[0]
    assert hit.admission_reason == "verified_exact"
    assert proof(hit)["bindings"][0]["raw"] == "ZT-84"
    assert hit.why["exact_source"] == "body" and hit.why["exact_match_type"] == "raw"
    decision = decide(index, "ZT-84")
    assert decision.constrained and decision.bindings and not decision.soft_clauses


def test_bracketed_numeric_component_binds_with_name_provenance(index):
    add(index, "irrelevant", path="/owned/(3551).jpg")
    hit = search(index, "3551")[0]
    assert hit.segment == "name" and hit.loc is hit.loc_end is None
    assert hit.why["exact_source"] == "basename" and hit.why["exact_match_type"] == "raw"


@pytest.mark.parametrize("filename", ["±5mA.csv", "1.5mA", "1,5mA"])
def test_quantity_spelling_still_refuses_bracket_rule(index, filename):
    add(index, "irrelevant", path="/owned/" + filename)
    assert search(index, "5mA") == []


# ----------------------------------------------------- 5. path-suffix tier


def test_plausible_path_suffix_binds_below_whole_path_raw(index):
    add(index, "def encode(message): pass", path="/corpus/text/cpython/Lib/email/base64mime.py", kind="code")
    hit = search(index, "Lib/email/base64mime.py")[0]
    assert hit.path == "/corpus/text/cpython/Lib/email/base64mime.py"
    assert hit.why["exact_source"] == "path" and hit.why["exact_match_type"] == "path_suffix"
    assert hit.why["exact_priority"] == 3 and hit.admission_reason == "verified_exact"
    # A whole-path owner outranks the tier: the suffix scan never even runs
    # once an ordinary binding exists (design C-5c ordering).
    add(index, "whole path owner", path="Lib/email/base64mime.py")
    hits = search(index, "Lib/email/base64mime.py")
    assert [h.path for h in hits] == ["Lib/email/base64mime.py"]
    assert hits[0].why["exact_match_type"] == "raw" and hits[0].why["exact_priority"] == 0


def test_two_same_suffix_files_bind_deterministically(index):
    add(index, "one", path="/a/Lib/email/base64mime.py")
    add(index, "two", path="/b/Lib/email/base64mime.py")
    first = [h.path for h in search(index, "Lib/email/base64mime.py")]
    second = [h.path for h in search(index, "Lib/email/base64mime.py")]
    assert first == second == ["/a/Lib/email/base64mime.py", "/b/Lib/email/base64mime.py"]
    assert all(h.why["exact_match_type"] == "path_suffix" for h in
               search(index, "Lib/email/base64mime.py"))


def test_suffix_never_verifies_a_non_separator_substring(index):
    add(index, "decoy", path="/corpus/xLib/email/base64mime.py")
    assert search(index, "Lib/email/base64mime.py") == []


# ------------------------------------------------- 6. scope grammar for prose


def test_multi_semicolon_prose_is_one_soft_clause_not_ambiguity(index):
    add(index, "committee approved the budget in 2011 and the audit in 2012")
    assert plan_query("does the budget; and the audit; plus 2011 count").reason == "anchor_scope_ambiguous"
    diag = {}
    hits = search(index, "does the budget; and the audit; plus 2011 count", diagnostics=diag)
    assert hits and diag["reason"] != "anchor_scope_ambiguous"
    assert diag["soft_clauses"] == [{"clause_id": 0, "reason": "ambient_only"}]
    assert diag["ambient_anchors"] == ["2011"]


def test_one_semicolon_and_compare_grammar_unchanged_for_obligatory(index):
    add(index, "MN_42 charge limit retention days 17", path="/owned/a.txt")
    add(index, "QV_86 charge limit retention days 29", path="/owned/b.txt")
    hits = search(index, "MN_42 charge limit; QV_86 retention days")
    assert {tuple(proof(h)["clauses"]) for h in hits} == {(0,), (1,)}
    compare = search(index, "compare MN_42 and QV_86 on retention days")
    assert {h.path for h in compare} == {"/owned/a.txt", "/owned/b.txt"}
    assert all(proof(h)["clauses"] == [0] or proof(h)["clauses"] == [1] for h in compare)


# ---------------------------------------------------- 7. clause intent split


def test_pure_anchor_queries_follow_the_c_int_byte_path(index):
    add(index, "approved ZT-84 at L7", path="/owned/a.txt")
    add(index, "refund TX_81 signed", path="/owned/b.txt")
    pure = decide(index, "ZT-84")
    assert pure.reason is None and pure.owners and pure.bindings
    assert not pure.soft_clauses and not pure.ambient_anchors
    assert search(index, "ZT-84")[0].admission_reason == "verified_exact"
    # Anchor + prose is C-NL: the ambient shape goes soft, the structural
    # shape resolves through the ordinary clause machinery.
    ambient = decide(index, "ZT-84 refund policy")
    assert ambient.soft_clauses == ((0, "ambient_only"),) and not any(ambient.owners)
    structural = decide(index, "TX_81 refund signed")
    assert structural.owners and not structural.soft_clauses


def test_c_int_absence_still_hard_empties_but_cnl_absence_depends_on_strength(index):
    add(index, "approved ZT-84 at L7")
    assert decide(index, "ZT-840").reason == "anchor_unresolved"
    # Ambient absence never refuses; structural absence in prose does.
    assert decide(index, "randomzz ZT-840").reason is None
    assert decide(index, "randomzz QX_99").reason == "anchor_unresolved"


# ------------------------------------------------- 8. verification accounting


def test_preread_rows_do_not_consume_the_obligation_budget(index):
    add(index, ["ZT-84"] * 1000)
    with index.read_snapshot():
        ids = [r[0] for r in index.db.execute("SELECT id FROM segments WHERE kind!='name'")]
        anchor = plan_query("ZT-84").anchors[0]
        resolver = Resolver(index, {1})
        resolver.read(ids, preread=True)
        assert len(resolver.fetched) == 1000 and not resolver.attempted
        # Obligation verification of already-cached rows adds nothing.
        assert resolver.bind(anchor)
        assert not resolver.attempted
    with index.read_snapshot():
        resolver = Resolver(index, {1})
        assert resolver.bind(plan_query("ZT-84").anchors[0])
        # Without a preread, the obligation read still marks rows attempted.
        assert len(resolver.attempted) == 1000


# --------------------------------------- 9. quote-provenance (anchor.v3)

#: F1/F2/F3/F7/F8/F9/F11 prose: >= 200 normalized chars, only the appended
#: camel token is identity-shaped, so the clause is C-NL with exactly one
#: obligatory component-0 anchor.
_QUOTE_PROSE = (
    "the shipping manifest records every harbor departure with signed witnesses "
    "and countersigned receipts so the auditors can reconstruct each voyage "
    "without trusting the ledger alone and the same rule applies to every port "
    "office that stores the original stamped pages before the surveyors arrive"
)


def _cint_payload(decision):
    from dataclasses import asdict
    return {"plan": asdict(decision.plan),
            "owners": [sorted(v) for v in decision.owners],
            "bindings": [asdict(v) for v in decision.bindings],
            "witnesses": [{"clause_id": c, "file_id": f,
                           "values": [{"kind": k, "unit": u, "segment_id": s}
                                      for k, u, s in vals]}
                          for (c, f), vals in sorted(decision.witnesses.items())],
            "exact": {str(f): asdict(v) for f, v in sorted(decision.exact.items())},
            "reason": decision.reason, "inspected_rows": decision.inspected_rows,
            "queries": decision.queries, "source_capabilities": decision.source_capabilities}


def test_F1_quote_head_positive_demotes_to_soft(index):
    add(index, _QUOTE_PROSE + "\n\tNetFlix streaming note")
    query = _QUOTE_PROSE + " NetFlix"
    decision = decide(index, query)
    assert decision.soft_clauses == ((0, "quote_provenance"),)
    assert decision.owners == (frozenset(),) and not decision.constrained
    assert decision.bindings == () and decision.exact == {} and decision.reason is None
    hits = search(index, query)
    assert hits and hits[0].admission_reason in ("lexical_quorum", "semantic_standout")
    soft = hits[0].why["anchor_contract_soft"]
    assert {"clause_id": 0, "reason": "quote_provenance"} in soft["soft_clauses"]
    assert any(b["raw"] == "NetFlix" for b in soft["obligatory_bindings"])
    assert "anchor_contract" not in hits[0].why
    from goldmine.anchor_admission import guard_composed_hits
    assert guard_composed_hits(index, decision, hits, final=True) == hits


def test_F2_quote_tail_positive_demotes_to_soft(index):
    query = "NetFlix " + _QUOTE_PROSE.replace("harbor", "harbour")
    add(index, query)
    decision = decide(index, query)
    assert decision.soft_clauses == ((0, "quote_provenance"),)
    assert decision.owners == (frozenset(),) and not decision.constrained


def test_F3_sub200_run_stays_constrained(index):
    # The whole clause is stored verbatim, but the anchor sits mid-clause, so
    # neither the head (clause start -> first anchor end) nor the tail (last
    # anchor start -> clause end) reaches the 200-char floor.
    mid = len(_QUOTE_PROSE) // 2
    query = _QUOTE_PROSE[:mid] + " NetFlix " + _QUOTE_PROSE[mid:]
    add(index, query)
    decision = decide(index, query)
    assert decision.soft_clauses == () and decision.constrained
    assert decision.owners and all(decision.owners)
    hits = search(index, query)
    assert hits and hits[0].admission_reason == "verified_exact"


def test_F4_unbound_camel_red_line_defineModels_still_refuses(index):
    add(index, "vue macro documentation without the identifier")
    decision = decide(index, "explain defineModels lifecycle for vue macros")
    assert decision.reason == "anchor_unresolved"
    assert decision.soft_clauses == () and decision.bindings == ()
    assert search(index, "explain defineModels lifecycle for vue macros") == []


def test_F5_unbound_camel_red_line_SearchPanel_cjk_still_refuses(index):
    add(index, "公司组件的通用说明文档")
    decision = decide(index, "给出公司 SearchPanel 组件里所有计算属性的实际依赖关系图")
    assert decision.reason == "anchor_unresolved" and decision.soft_clauses == ()
    assert search(index, "给出公司 SearchPanel 组件里所有计算属性的实际依赖关系图") == []


def test_F6_c_int_fixture_payloads_byte_identical_to_frozen_baseline(index):
    import json as _json
    from pathlib import Path as _Path
    # Append the frozen fixture's content to each initial name row. Replacing
    # that row used to rely on SQLite recycling deleted IDs; IDs now stay spent.
    for path, text, title in (
        ("/owned/zt84.txt", "ZT-84 signed harbor charge record", None),
        ("/owned/kv.txt", "key_value_store.sqlite schema note", None),
        ("/owned/wheel.txt", "py-1.0.3 wheel listing", None),
        ("/owned/lh.txt", "LH-2209 flight manifest", None),
        ("/owned/k7.txt", "K7Q2LM activation code", None),
        ("/owned/tx81.txt", "TX-81 calibration sheet body", "TX-81 calibration sheet"),
    ):
        fid = index.upsert_file(FileRec(path=path, kind="doc", state="done",
            capability="content_indexed", info={"title": title} if title else None))
        index.fts_text(fid, "chunk", 7, text, False, loc_end=7, excerpt_text=text)
    baseline = _json.loads((_Path(__file__).parent / "anchor_intent_cint_baseline.json")
                           .read_text())
    for query, expected in baseline.items():
        got = _cint_payload(decide(index, query))
        assert _json.dumps(got, sort_keys=True) == _json.dumps(expected, sort_keys=True), query


def test_F7_partial_demotion_keeps_surviving_clause_constraint(index):
    quote_file = add(index, _QUOTE_PROSE + " NetFlix")
    zone_file = add(index, "BUILD_ZONE summary notes with retention words",
                    path="/owned/zone.txt")
    query = _QUOTE_PROSE + " NetFlix ; BUILD_ZONE summary notes"
    decision = decide(index, query)
    assert decision.soft_clauses == ((0, "quote_provenance"),)
    assert decision.owners == (frozenset(), frozenset({zone_file}))
    assert decision.constrained and decision.allowed == {zone_file}
    hits = search(index, query)
    assert hits and {h.path for h in hits} == {"/owned/zone.txt"}
    assert "anchor_contract" in hits[0].why
    assert {"clause_id": 0, "reason": "quote_provenance"} in \
        hits[0].why["anchor_contract_soft"]["soft_clauses"]


def test_F8_oversized_owner_page_never_proves_quoting(index):
    add(index, [_QUOTE_PROSE + " NetFlix"] + ["filler tail words"] * 2000)
    decision = decide(index, _QUOTE_PROSE + " NetFlix")
    assert decision.soft_clauses == () and decision.constrained and all(decision.owners)


def test_F9_owner_cap_is_conservative_both_ways(index, tmp_path):
    from goldmine.store import FileRec, SegmentRec, Store
    def corpus(n):
        store = Store(tmp_path / f"cap{n}.sqlite")
        for i in range(n):
            fid = store.upsert_file(FileRec(path=f"/q/{i}.txt", kind="doc", state="done",
                                            capability="content_indexed"))
            store.replace_segments(fid, [
                SegmentRec(kind="name", modality="none", text_for_fts="owned folder"),
                SegmentRec(kind="chunk", modality="none", loc=7, loc_end=7,
                           text_for_fts=_QUOTE_PROSE + " NetFlix", excerpt="x")])
        return store
    over = corpus(9)
    try:
        decision = decide(over, _QUOTE_PROSE + " NetFlix")
        assert decision.soft_clauses == () and decision.constrained
    finally:
        over.close()
    within = corpus(8)
    try:
        decision = decide(within, _QUOTE_PROSE + " NetFlix")
        assert decision.soft_clauses == ((0, "quote_provenance"),)
        assert decision.owners == (frozenset(),) and not decision.constrained
    finally:
        within.close()


def test_F10_quote_reads_are_preread_semantics_and_visible_in_queries(index, monkeypatch):
    from goldmine.anchor_resolution import Resolver
    add(index, _QUOTE_PROSE + " NetFlix")
    query = _QUOTE_PROSE + " NetFlix"
    fired = decide(index, query)
    assert fired.soft_clauses == ((0, "quote_provenance"),)
    # The quote block's reads are preread-semantics: zeroing quote_corpus
    # (conservative no-demote, no quote SQL at all) leaves the obligation
    # accounting identical while strictly lowering the source-query count.
    monkeypatch.setattr(Resolver, "quote_corpus", lambda self, file_id: None)
    silenced = decide(index, query)
    monkeypatch.undo()
    assert silenced.soft_clauses == () and silenced.constrained
    assert silenced.inspected_rows == fired.inspected_rows
    assert silenced.queries < fired.queries


def test_F11_case_flipped_stored_head_never_demotes(index):
    add(index, _QUOTE_PROSE.replace("harbor", "Harbor", 1) + " NetFlix",
        path="/owned/caseflip.txt")
    decision = decide(index, _QUOTE_PROSE + " NetFlix")
    assert decision.soft_clauses == () and decision.constrained and all(decision.owners)


def test_F12_phrase_carve_corner_keeps_owners_and_never_demotes(index):
    body = "the archive stores /owned/quote-path.txt verbatim 宏的说明以及 的内容"
    cid = add(index, body)
    from goldmine.search import parse_filters
    raw = 'defineLater 宏的说明以及 "/owned/quote-path.txt" 的内容'
    query, filters = parse_filters(raw)
    allowed = all_files(index)
    with index.read_snapshot():
        decision = resolve_obligations(index, query, filters.phrases, allowed, _terms_of)
    assert decision.reason is None
    assert decision.owners == (frozenset({cid}),)
    assert decision.soft_clauses == ()
    assert decision.constrained and decision.allowed == {cid}
