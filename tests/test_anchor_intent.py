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
