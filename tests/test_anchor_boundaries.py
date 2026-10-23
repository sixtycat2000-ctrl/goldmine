"""Independent invariants around source identity, OFF paths and composition."""
from __future__ import annotations

from dataclasses import replace
import sqlite3

import pytest

from goldmine.anchor_admission import resolve_obligations
from goldmine.anchor_resolution import Resolver
from goldmine.application import Application
from goldmine.citations import read_evidence, resolve_citation
from goldmine.contracts import SearchRequest
from goldmine.deadline import RequestTimeout, request_cancellation, request_timeout
from goldmine.lexical import LEXICAL_VERSION
from goldmine.query_obligations import lexemes, plan_query, typed_normalize
from goldmine.search import Searcher, parse_filters
from goldmine.store import Store, SegmentRec
from test_anchor_contract import add, index, proof, search
from test_search_offline import FixedBackend, evec


@pytest.mark.parametrize("left,right", [
    ("-.5mA", "-.5MA"), ("5GHz", "5GHZ"), (".5m", "5m"),
    ("-5A", "5A"), ("1,000mA", "1000mA"), ("−5A", "-5A"),
    ("/owned/5mA.csv", "/owned/5MA.csv"), ("current 5mA", "current 5MA"),
])
def test_typed_gate_preserves_every_quantity_lexical_component(left, right):
    assert typed_normalize(left) != typed_normalize(right)


@pytest.mark.parametrize("surface", ["-.5mA", ".5mA", "1,000mA", "5GHz", ".NET", "１２３４"])
def test_raw_offsets_survive_nfkc_without_guessing_indices(surface):
    text = "prefix " + surface + " suffix"
    anchor = plan_query(text).anchors[0]
    assert text[anchor.start:anchor.end] == surface
    assert anchor.raw == surface


@pytest.mark.parametrize("query,body", [
    (".NET", "NET"), (".NET", ".NETCore"), ("5mA", "-.5mA"),
    ("5mA", ".5mA"), ("1,000mA", "1000mA"), ("-5mA", "5mA"),
])
def test_new_recall_does_not_accept_old_derived_punctuation_loss(index, query, body):
    add(index, body)
    assert search(index, query) == []


@pytest.mark.parametrize("query,source", [("5mA", ".5mA.csv"), ("5mA", "-5mA.csv"), ("5mA", "5MA.csv")])
def test_filename_component_recall_keeps_real_quantity_spelling(index, query, source):
    add(index, "irrelevant", path="/owned/" + source)
    assert search(index, query) == []


@pytest.mark.parametrize("path", ["/owned/wirefmt.py", "/owned/wirefmt/__init__.py"])
def test_python_one_level_actual_file_kind_and_symbol_are_both_required(index, path):
    add(index, "def encode_field(value): pass", path=path, kind="code")
    hit = search(index, "wirefmt.encode_field")[0]
    assert proof(hit)["bindings"][0]["component_segment_id"] != hit.segment_id
    with index.write_transaction():
        index.db.execute("UPDATE files SET kind='doc'")
    assert search(index, "wirefmt.encode_field") == []


def test_python_module_prefix_is_not_namespace_ownership(index):
    add(index, "def encode_field(value): pass", path="/owned/wirefmt_extra.py", kind="code")
    add(index, "def encode_field(value): pass", path="/owned/other.py", kind="code")
    assert search(index, "wirefmt.encode_field") == []


@pytest.mark.parametrize("query", ["load_config_v2", "pkg.v2.7", "under:bad/value", "`TX-81;XV-73`"])
def test_maximal_raw_surfaces_are_not_generic_and_or_delimiter_splits(query):
    planned = plan_query(query)
    assert len(planned.anchors) == 1
    anchor = planned.anchors[0]
    assert query[anchor.start:anchor.end] == anchor.raw


def test_comparison_grammar_is_closed_and_chinese_shared_scope_is_real(index):
    add(index, "MN-42 保留天数17", path="/owned/a.txt")
    add(index, "QV-86 保留天数29", path="/owned/b.txt")
    assert len(search(index, "比较 MN-42 与 QV-86 在 保留天数")) == 2
    # anchor.v2-intent: ambient _CODE anchors in prose are ordinary terms, so
    # the non-compare conjunction now admits both files via quorum (7694837).
    assert len(search(index, "MN-42 和 QV-86 保留天数")) == 2
    # The grammar stays closed for structural identities: "和" never splits
    # scopes — one clause stays soft while the compare grammar constrains.
    add(index, "MN_42 保留天数17", path="/owned/c.txt")
    add(index, "QV_86 保留天数29", path="/owned/d.txt")
    soft = search(index, "MN_42 和 QV_86 保留天数")
    # anchor.v2-intent: no single file owns both identities, so the one clause
    # demotes to soft (anchor_sibling_absent) instead of refusing.
    assert {"/owned/c.txt", "/owned/d.txt"} <= {h.path for h in soft}
    assert all("anchor_contract" not in h.why for h in soft)
    assert {h.why["anchor_contract_soft"]["soft_clauses"][0]["reason"] for h in soft} == {"anchor_sibling_absent"}
    split = search(index, "比较 MN_42 与 QV_86 在 保留天数")
    assert {tuple(proof(h)["clauses"]) for h in split} == {(0,), (1,)}


def test_shared_compare_identity_is_not_silently_discarded(index):
    add(index, "MN-42 RET-12 retention days", path="/owned/a.txt")
    add(index, "QV-86 retention days", path="/owned/b.txt")
    # anchor.v2-intent: ambient _CODE anchors in prose lose the per-operand
    # shared-identity veto; both files return via ordinary admission (7694837).
    compare = "compare MN-42 and QV-86 on RET-12 retention days"
    assert {h.path for h in search(index, compare)} == {"/owned/a.txt", "/owned/b.txt"}
    # The shared compare identity still binds each returned owner for
    # structural code shapes: the operand without it is demoted, not returned.
    add(index, "MN_42 RET_12 retention days", path="/owned/c.txt")
    add(index, "QV_86 retention days", path="/owned/d.txt")
    strict = "compare MN_42 and QV_86 on RET_12 retention days"
    hits = search(index, strict)
    assert {h.path for h in hits} == {"/owned/c.txt"}
    assert hits[0].why["anchor_contract_soft"]["soft_clauses"] == [{"clause_id": 1, "reason": "anchor_sibling_absent"}]


def test_same_owner_can_truthfully_represent_two_explicit_clauses_at_limit_one(index):
    add(index, "MN-42 charge limit QV-86 retention days")
    hits = search(index, "MN-42 charge limit; QV-86 retention days", limit=1)
    # anchor.v2-intent: ambient _CODE clauses are soft, so limit truncation
    # returns the ordinary top-1 without an anchor_contract block (7694837).
    assert len(hits) == 1 and "anchor_contract" not in hits[0].why
    assert hits[0].why["anchor_contract_soft"]["soft_clauses"][0]["reason"] == "ambient_only"
    # One file truthfully representing both explicit clauses is still pinned
    # by structural identities at limit one.
    add(index, "MN_42 charge limit QV_86 retention days", path="/owned/strict.txt")
    hit = search(index, "MN_42 charge limit; QV_86 retention days", limit=1)[0]
    assert hit.path == "/owned/strict.txt" and proof(hit)["clauses"] == [0, 1]


def test_phrase_is_body_proof_in_mixed_request_not_copied_name(index):
    add(index, "MN-42 unrelated body", path="/owned/signed receipt.txt")
    q, filters = parse_filters('MN-42 "signed receipt"')
    assert search(index, q, filters=filters) == []


@pytest.mark.parametrize("body", ["receipt signed", "signed\nreceipt", "signed unrelated receipt"])
def test_quote_is_contiguous_literal_not_fts_token_proximity(index, body):
    add(index, "MN-42 " + body)
    q, filters = parse_filters('MN-42 "signed receipt"')
    assert search(index, q, filters=filters) == []


def test_anchorless_quoted_phrase_cannot_be_semantically_rescued(index):
    add(index, "receipt signed", vector=evec(0))
    for i in range(25):
        add(index, "unrelated", path=f"/fill/{i}.txt", vector=evec(1))
    q, filters = parse_filters('"signed receipt"')
    assert Searcher(index, FixedBackend(evec(0))).search(q, filters=filters) == []


def test_pure_quoted_filename_is_real_metadata_not_body(index):
    path = "/owned/a b/Report 5mA.csv"
    add(index, "irrelevant", path=path)
    q, filters = parse_filters('"' + path + '"')
    hit = search(index, q, filters=filters)[0]
    assert hit.path == path and hit.segment == "name" and hit.loc is None


def test_pure_full_path_semicolon_is_not_cross_file_scope(index):
    path = "/owned/a;b/report.md"
    add(index, "irrelevant", path=path)
    assert search(index, path)[0].path == path


def test_backtick_body_literal_keeps_semicolon_and_complete_surface(index):
    add(index, "literal TX-81;XV-73 available")
    hit = search(index, "`TX-81;XV-73`")[0]
    assert proof(hit)["bindings"][0]["raw"] == "TX-81;XV-73"


def test_ordinary_long_anchorless_input_does_not_invent_size_or_identity_gate(index):
    query = " ".join(["harbor"] * 3000)
    add(index, "harbor")
    hit = search(index, query)[0]
    assert hit.admission_reason == "lexical_quorum" and "anchor_contract" not in hit.why


def test_duplicate_obligations_do_not_spend_distinct_anchor_budget(index):
    add(index, "ZT-84")
    assert search(index, " ".join(["ZT-84"] * 40))


def test_all_same_file_body_witnesses_can_span_rows_but_not_files(index):
    add(index, ["ZT-84 alpha", "beta gamma"])
    # anchor.v2-intent: the ambient _CODE clause is soft; the hit returns via
    # ordinary admission with soft disclosure instead of witness proofs (7694837).
    soft = search(index, "ZT-84 alpha beta gamma")
    assert soft and all("anchor_contract" not in h.why and "anchor_contract_soft" in h.why for h in soft)
    # The same-file witness span pin survives for structural identities.
    add(index, ["TX_84 alpha", "beta gamma"], path="/owned/strict.txt")
    hit = search(index, "TX_84 alpha beta gamma")[0]
    sids = {w["segment_id"] for w in proof(hit)["remainder_witnesses"]}
    assert len(sids) == 2
    assert all(read_evidence(index, sid).citation.path == hit.path for sid in sids)


def test_lookup_source_integrity_error_is_not_relabelled_unsupported(index):
    add(index, "ZT-84")
    with index.write_transaction():
        index.db.execute("UPDATE lookup_terms SET raw_term='counterfeit' WHERE source='body'")
    with pytest.raises(ValueError, match="lookup pointer"):
        search(index, "ZT-84")


def test_lookup_wrong_file_or_metadata_kind_fails_instead_of_fiction(index):
    other = add(index, "other", path="/owned/other.txt")
    add(index, "ZT-84")
    with index.write_transaction():
        index.db.execute("UPDATE lookup_terms SET file_id=? WHERE source='body'", (other,))
    with pytest.raises(ValueError, match="lookup pointer"):
        search(index, "ZT-84")


def test_required_upgrade_table_is_not_silently_optional(index):
    add(index, "ZT-84")
    with index.write_transaction():
        index.db.execute("DROP TABLE lookup_terms")
    with pytest.raises(sqlite3.OperationalError, match="lookup_terms"):
        search(index, "ZT-84")


def test_request_bound_aggregates_distinct_inspections_and_uses_extra_row(index):
    add(index, ["ZT-84"] * 1000)
    with index.read_snapshot():
        resolver = Resolver(index, {1})
        for _ in range(3):
            assert resolver.bind(plan_query("ZT-84").anchors[0])
        assert len(resolver.attempted) == 1000
        assert len(resolver.rows) == 1000


def test_off_limit0_and_lexical_never_use_vectors_or_embeddings(index, monkeypatch):
    add(index, "ZT-84")
    monkeypatch.setattr(index, "load_vectors", lambda: pytest.fail("unexpected vector load"))
    backend = FixedBackend(evec(0))
    monkeypatch.setattr(backend, "embed_texts", lambda texts: pytest.fail("unexpected embedding"))
    assert Searcher(index, backend).search("ZT-84", limit=0) == []
    assert Searcher(index, backend).search("ZT-84", scorer="lexical")
    assert Searcher(index, backend).search("ZT-840") == []


def test_valid_hybrid_uses_one_existing_query_embedding_and_one_vector_load(index, monkeypatch):
    add(index, "ZT-84 alpha", vector=evec(0))
    for i in range(25):
        add(index, "filler", path=f"/fill/{i}.txt", vector=evec(1))
    backend = FixedBackend(evec(0))
    calls, loads = [], []
    embed, load = backend.embed_texts, index.load_vectors
    monkeypatch.setattr(backend, "embed_texts", lambda texts: (calls.append(texts) or embed(texts)))
    monkeypatch.setattr(index, "load_vectors", lambda: (loads.append(1) or load()))
    assert Searcher(index, backend).search("ZT-84 alpha")
    assert calls == [["task: search result | query: ZT-84 alpha"]] and loads == [1]


def test_pure_semantic_keeps_gate_fallback_and_empty_support_despite_verified_anchor(index):
    add(index, ["semantic fallback", "ZT-84", "extra semantic"], vector=evec(0))
    for i in range(40):
        add(index, "filler", path=f"/fill/{i}.txt", vector=evec(1))
    hit = Searcher(index, FixedBackend(evec(0))).search("ZT-84", scorer="semantic", why=True)[0]
    assert hit.admission_reason == "semantic_standout" and not hit.supporting_segment_ids
    assert hit.excerpt == "semantic fallback" and hit.loc == 7
    assert "exact_source" not in hit.why
    assert Searcher(index, None).search("ZT-84", scorer="semantic") == []


def test_same_snapshot_name_body_vector_and_citation_survive_concurrent_writer(index):
    fid = add(index, "ZT-84 alpha", vector=evec(0))
    for i in range(25):
        add(index, "filler", path=f"/fill/{i}.txt", vector=evec(1))
    other = Store(index.path)
    backend = FixedBackend(evec(0))
    embed = backend.embed_texts
    def mutate(texts):
        other.replace_segments(fid, [SegmentRec(kind="chunk", modality="none", loc=99, text_for_fts="changed")])
        return embed(texts)
    backend.embed_texts = mutate
    try:
        with index.read_snapshot():
            hit = Searcher(index, backend).search("ZT-84 alpha", why=True)[0]
            assert hit.loc == 7 and read_evidence(index, hit.segment_id).text == "ZT-84 alpha"
        assert index.segment_text(hit.segment_id) is None
    finally:
        other.close()


def test_all_store_identity_and_schema_rows_unchanged_after_actual_search_and_cit_readback(index):
    add(index, "ZT-84 alpha", vector=evec(0))
    before = list(index.db.iterdump())
    for scorer in ("lexical", "hybrid", "semantic", "rrf"):
        hits = Searcher(index, FixedBackend(evec(0))).search("ZT-84 alpha", scorer=scorer)
        for hit in hits:
            cite = read_evidence(index, hit.segment_id).citation
            assert resolve_citation(index, cite.citation_id).citation == cite
    assert list(index.db.iterdump()) == before
    assert index.meta("lexical_version") == LEXICAL_VERSION == "lex.v2-exact"


def test_mid_sql_deadline_or_cancel_never_becomes_empty_success(index):
    add(index, "ZT-84")
    calls = []
    def cancel_later():
        calls.append(1)
        if len(calls) > 10:
            raise RequestTimeout("during bound verification")
    with pytest.raises(RequestTimeout, match="during bound verification"):
        with request_cancellation(cancel_later):
            search(index, "ZT-84")
    assert not index.db.in_transaction
    with pytest.raises(RequestTimeout):
        with request_timeout(0):
            search(index, "ZT-84")


def test_quality_final_limit_rechecks_original_owner_without_extra_retrieval(index):
    add(index, "MN-42 charge limit 17", path="/owned/a.txt")
    add(index, "QV-86 retention days 29", path="/owned/b.txt")
    app = Application(index.path)
    base = SearchRequest(query="charge limit MN-42; retention days QV-86", lexical_only=True,
                         mode="quality", limit=1, require_citations=True)
    result = app.search(base)
    # anchor.v2-intent: ambient _CODE clauses are soft (empty owners are
    # skipped by represented), so limit truncation returns the top-1 (7694837).
    assert len(result.hits) == 1
    assert len(app.search(replace(base, limit=2)).hits) == 2
    # The final-limit owner recheck still fires for constrained decisions.
    add(index, "MN_42 charge limit 17", path="/owned/c.txt")
    add(index, "QV_86 retention days 29", path="/owned/d.txt")
    strict = SearchRequest(query="charge limit MN_42; retention days QV_86", lexical_only=True,
                           mode="quality", limit=1, require_citations=True)
    result = app.search(strict)
    assert result.hits == [] and result.settings["anchor_contract"]["reason"] == "anchor_not_in_selected_limit"
    assert len(app.search(replace(strict, limit=2)).hits) == 2


def test_multiquery_expansion_cannot_rescue_missing_original_identity(index):
    add(index, "MN-42 charge limit 17", path="/owned/a.txt")
    app = Application(index.path)
    request = SearchRequest(query="compare MN-42 and QV-87 on charge limit", lexical_only=True,
                            mode="quality", multi_query=True, limit=10)
    result = app.search(request)
    # anchor.v2-intent: the absent QV-87 is an ambient _CODE, not an
    # obligation, so the query returns via ordinary admission (7694837).
    assert result.hits
    # A missing structural identity still refuses the original query even
    # when multiquery expansion produces rescuing subqueries.
    add(index, "MN_42 charge limit 17", path="/owned/c.txt")
    strict = SearchRequest(query="compare MN_42 and QX_87 on charge limit", lexical_only=True,
                           mode="quality", multi_query=True, limit=10)
    result = app.search(strict)
    assert result.hits == []
    assert result.settings["anchor_contract"]["reason"] == "anchor_unresolved"


@pytest.mark.parametrize("query,source", [("ZT-84", "编号：ZT-84"), ("-.5mA", "测量：-.5mA"),
                                         ("－.5mA", "-.5mA")])
def test_cjk_label_separator_and_fullwidth_sign_do_not_erase_identity(index, query, source):
    add(index, source)
    hit = search(index, query)[0]
    assert read_evidence(index, hit.segment_id).text == source


def test_only_supported_clause_bindings_are_associated_with_a_returned_owner(index):
    add(index, "MN-42 charge limit QV-86", path="/owned/a.txt")
    add(index, "QV-86 retention days", path="/owned/b.txt")
    hits = search(index, "MN-42 charge limit; QV-86 retention days")
    # anchor.v2-intent: ambient _CODE clauses never bind; the hits carry soft
    # disclosure with the ambient raws instead of owner proofs (7694837).
    assert {h.path for h in hits} == {"/owned/a.txt", "/owned/b.txt"}
    assert all("anchor_contract" not in h.why for h in hits)
    assert {tuple(h.why["anchor_contract_soft"]["ambient_anchors"]) for h in hits} == {("MN-42", "QV-86")}
    # The clause-owner binding association pin survives for structural identities.
    add(index, "MN_42 charge limit QV_86", path="/owned/c.txt")
    add(index, "QV_86 retention days", path="/owned/d.txt")
    strict = search(index, "MN_42 charge limit; QV_86 retention days")
    c = next(h for h in strict if h.path == "/owned/c.txt")
    assert proof(c)["clauses"] == [0]
    assert all(b["query_term"] == "MN_42" and b["clause_ids"] == [0] for b in proof(c)["bindings"])


def test_normalized_recall_and_original_text_cannot_disagree_on_source_witness(index):
    add(index, "ZT-84 alpha")
    with index.write_transaction():
        index.db.execute("UPDATE fts SET original='unrelated source text' WHERE body LIKE '%zt-84%'")
    with pytest.raises(ValueError, match="normalization is inconsistent"):
        search(index, "ZT-84 alpha")


def test_derived_key_must_really_be_a_form_of_its_declared_raw_source(index):
    add(index, "ZT-84")
    with index.write_transaction():
        index.db.execute("UPDATE lookup_terms SET normalized_term='false-84' WHERE source='body' AND match_type='raw'")
    with pytest.raises(ValueError, match="lookup pointer"):
        search(index, "false-84")


def test_duplicate_source_occurrences_have_bounded_request_work_and_why(index):
    add(index, ["ZT-84"] * 1000)
    diag = {}
    hit = search(index, " ".join(["ZT-84"] * 40), diagnostics=diag)[0]
    # anchor.v2-intent: the lexical pre-read (Resolver.read(preread=True))
    # fetches and caches the source rows without consuming the obligation
    # budget, so verifying the already-fetched rows marks none attempted (§C-4).
    assert diag["inspected_source_rows"] == 0
    assert diag["source_queries"] < 30  # batch rows/cache identical obligations, not1000×40 SQL
    assert len(proof(hit)["bindings"]) == 40


def test_anchorless_quoted_search_preserves_ordinary_unquoted_quorum_not_all_word_veto(index):
    add(index, "cat signed receipt")
    q, filters = parse_filters('cat absentword "signed receipt"')
    hit = search(index, q, filters=filters)[0]
    assert hit.admission_reason == "lexical_quorum" and hit.coverage < 1
    assert proof(hit)["bindings"] == []
    q, filters = parse_filters('cat; absentword "signed receipt"')
    assert search(index, q, filters=filters)


# Exhaust the compatibility class in this Python Unicode database. This is
# independent of the production lexer and is not an algorithm sign blacklist.
import unicodedata as _unicode
_NFKC_SIGNS = tuple(chr(i) for i in range(0x110000)
                    if _unicode.normalize("NFKC", chr(i)) in {"+", "-"}) + ("−",)


@pytest.mark.parametrize("sign", _NFKC_SIGNS, ids=lambda s: f"U+{ord(s):04X}")
@pytest.mark.parametrize("number", ["5", "５", "⁵"])
def test_all_nfkc_signed_metadata_components_cannot_become_unsigned(index, sign, number):
    path = "/owned/" + sign + number + "mA.csv"
    add(index, "irrelevant", path=path)
    assert search(index, "5mA") == []
    assert search(index, "/owned/5mA.csv") == []
    # The full real signed name remains searchable and keeps name provenance.
    query = _unicode.normalize("NFKC", sign + number + "mA.csv")
    hit = search(index, query)[0]
    assert hit.path == path and hit.segment == "name" and hit.loc is hit.loc_end is None
    assert hit.why["exact_match_type"] == "raw"


@pytest.mark.parametrize("filename", [".5mA.csv", "-.5mA.csv", "±5mA.csv", "unit_5mA.csv", "prefix-5mA.csv"])
def test_numeric_fragment_cannot_invent_a_complete_metadata_component(index, filename):
    add(index, "irrelevant", path="/owned/" + filename)
    assert search(index, "5mA") == []


def test_true_unsigned_numeric_metadata_component_and_letter_code_still_bind(index):
    add(index, "irrelevant", path="/owned/5mA.csv")
    assert search(index, "5mA")[0].segment == "name"
    add(index, "irrelevant", path="/owned/invoice-K7Q2LM.pdf")
    assert search(index, "K7Q2LM")[0].path == "/owned/invoice-K7Q2LM.pdf"
