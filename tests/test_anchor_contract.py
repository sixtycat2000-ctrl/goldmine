"""C002's 49 preregistered cases: actual stored rows, fixed manual vectors.

Synthetic mechanics only. No model, external source, query corpus or quality run.
"""
from __future__ import annotations

import pytest

from goldmine.citations import read_evidence
from goldmine.query_obligations import plan_query
from goldmine.search import Searcher, parse_filters
from goldmine.store import FileRec, SegmentRec, Store
from test_search_offline import FixedBackend, evec


@pytest.fixture
def index(tmp_path):
    store = Store(tmp_path / "anchor.sqlite")
    yield store
    store.close()


def add(store, text, *, path="/owned/source.txt", kind="doc", title=None, vector=None):
    fid = store.upsert_file(FileRec(path=path, kind=kind, state="done", capability="content_indexed",
                                    info={"title": title} if title is not None else None))
    texts = [text] if isinstance(text, str) else text
    store.replace_segments(fid, [SegmentRec(kind="name", modality="none", text_for_fts="owned folder")] + [
        SegmentRec(kind="chunk", modality="text" if vector is not None else "none",
                   loc=7 + i, loc_end=7 + i, text_for_fts=t, excerpt=t, vec=vector)
        for i, t in enumerate(texts)
    ])
    return fid


def search(store, query, **kwargs):
    return Searcher(store, None).search(query, scorer="lexical", why=True, **kwargs)


def proof(hit):
    return hit.why["anchor_contract"]


def test_C1_01_real_literal_body_and_opaque_citation(index):
    add(index, "approved ZT-84 at L7")
    hit = search(index, "ZT-84")[0]
    actual = read_evidence(index, hit.segment_id)
    assert hit.segment == "chunk" and hit.loc == 7 and hit.cosine is None
    assert actual.text == "approved ZT-84 at L7"
    assert actual.citation.segment_id == hit.segment_id
    assert proof(hit)["bindings"][0]["raw"] == "ZT-84"


@pytest.mark.parametrize("query,text", [
    pytest.param("ZT-840", "approved ZT-84", id="C1-02"),
    pytest.param("-.5m", ".5m and -5m", id="C1-05"),
    pytest.param("pkg.v2_7", "pkg.v2_6 and pkg v2 7", id="C1-06"),
    pytest.param("wirefmt.encode_fields", "def encode_field(value): pass", id="C1-16"),
    pytest.param("wirefmt.Mode.NEW_VALUE", "class Mode: NEW_VALUE = 1", id="C1-17"),
    pytest.param("zt84", "ZT-84", id="C1-44"),
    pytest.param("84ZT", "84-ZT", id="C1-45"),
    pytest.param("Q2", "Q-2", id="C1-46"),
    pytest.param("ZT84", "ZT_84", id="C1-48"),
])
def test_complete_missing_identity_cannot_be_semantically_rescued(index, query, text):
    add(index, text, path="/owned/wirefmt.py", kind="code", vector=evec(0))
    for i in range(25):
        add(index, "filler", path=f"/fill/{i}.txt", vector=evec(1))
    backend = FixedBackend(evec(0))
    calls = []
    original = backend.embed_texts
    backend.embed_texts = lambda texts: (calls.append(texts) or original(texts))
    for scorer in ("lexical", "hybrid", "rrf", "semantic"):
        assert Searcher(index, backend).search(query, scorer=scorer) == []
    assert calls == []  # refusal precedes the existing query embedding


def test_C1_03_raw_ranks_before_disclosed_complete_alias(index):
    add(index, "ZT-84", path="/owned/alias.txt")
    add(index, "ZT84", path="/owned/raw.txt")
    hits = search(index, "ZT84")
    assert [h.path for h in hits] == ["/owned/raw.txt", "/owned/alias.txt"]
    assert [h.why["exact_match_type"] for h in hits] == ["raw", "alias"]
    assert hits[1].why["exact_string"] == "ZT-84"


def test_C1_04_leading_decimal_only_binds_actual_decimal_owner(index):
    add(index, "5m", path="/owned/wrong.txt")
    add(index, ".5m", path="/owned/right.txt")
    hits = search(index, ".5m")
    assert [h.path for h in hits] == ["/owned/right.txt"]
    assert hits[0].why["exact_string"] == ".5m"


def test_C1_07_fullwidth_normalization_preserves_original_body(index):
    add(index, "approved ZT-84")
    hit = search(index, "ＺＴ－８４")[0]
    assert hit.why["exact_match_type"] == "raw"
    assert read_evidence(index, hit.segment_id).text == "approved ZT-84"


def test_C1_08_cjk_adjacent_anchor_raw_offsets_and_remainder(index):
    query = "记录ZT-84"
    anchor = plan_query(query).anchors[0]
    assert query[anchor.start:anchor.end] == anchor.raw == "ZT-84"
    assert plan_query(query).clauses[0].remainder.strip() == "记录"
    add(index, query)
    hit = search(index, query)[0]
    assert proof(hit)["remainder_witnesses"][0]["unit"] == "记录"
    assert read_evidence(index, hit.segment_id).text == query


def test_C1_09_decimal_scripts_are_not_transliterated(index):
    add(index, "1234", path="/owned/ascii.txt")
    add(index, "١٢٣٤", path="/owned/arabic.txt")
    assert [h.path for h in search(index, "١٢٣٤")] == ["/owned/arabic.txt"]


def test_C1_10_chinese_number_words_remain_ordinary_prose(index):
    add(index, "三十万连接")
    assert plan_query("三十万连接").anchors == ()
    hit = search(index, "三十万连接")[0]
    assert hit.admission_reason == "lexical_quorum" and "anchor_contract" not in hit.why


def test_C1_11_camel_surface_is_not_a_prefix(index):
    add(index, "defineBuffers", path="/owned/wrong.txt")
    add(index, "defineBuffer", path="/owned/right.txt")
    assert [h.path for h in search(index, "defineBuffer")] == ["/owned/right.txt"]


def test_C1_12_camel_brand_can_bind_real_ordinary_document_word(index):
    add(index, "javascript compatibility")
    hit = search(index, "JavaScript")[0]
    assert proof(hit)["bindings"][0]["raw"] == "javascript"
    assert hit.kind == "doc"  # lexical identity never claims code-definition ownership


def test_C1_13_soft_hyphen_partial_does_not_veto_other_channel(index):
    add(index, "client-side", path="/owned/one.txt", vector=evec(0))
    add(index, "request settings", path="/owned/two.txt", vector=evec(1))
    for i in range(25):
        add(index, "filler", path=f"/fill/{i}.txt", vector=evec(1))
    query = "client-side per-request settings"
    assert not plan_query(query).anchors
    hits = Searcher(index, FixedBackend(evec(0))).search(query)
    assert any(h.path == "/owned/one.txt" and h.admission_reason == "semantic_standout" for h in hits)


def test_C1_14_title_case_prose_hyphens_are_not_camel_identifiers(index):
    query = "State-of-the-Art notes"
    assert plan_query(query).anchors == ()
    add(index, "State-of-the-Art notes")
    hit = search(index, query)[0]
    assert hit.admission_reason == "lexical_quorum" and "exact_match_type" not in hit.why


def test_C1_15_qualified_python_reconstruction_records_both_true_witnesses(index):
    add(index, "def encode_field(value): pass", path="/owned/wirefmt.py", kind="code")
    hit = search(index, "wirefmt.encode_field")[0]
    binding = proof(hit)["bindings"][0]
    assert binding["match_type"] == "qualified_components" and binding["raw"] == "encode_field"
    body = read_evidence(index, binding["segment_id"])
    name = read_evidence(index, binding["component_segment_id"])
    assert body.citation.segment_kind == "chunk" and name.citation.segment_kind == "name"
    assert binding["component_raw"] == "wirefmt.py" and name.citation.path == hit.path
    assert "wirefmt.encode_field" not in body.text


def test_C1_18_pure_title_identity_uses_actual_name_and_null_location(index):
    add(index, "unrelated prose", title="TX-81")
    hit = search(index, "TX-81")[0]
    assert hit.segment == "name" and hit.evidence_type == "name_metadata"
    assert hit.loc is hit.loc_end is None and hit.why["exact_source"] == "title"


def test_C1_19_metadata_cannot_supply_body_predicates(index):
    add(index, "unrelated prose", path="/owned/refund-policy/TX-81.txt", title="TX-81")
    assert search(index, "TX-81 refund policy") == []


def test_C1_20_mixed_metadata_identity_and_predicate_keep_distinct_true_fields(index):
    add(index, "refund policy available", title="TX-81")
    hit = search(index, "TX-81 refund policy")[0]
    binding = proof(hit)["bindings"][0]
    assert binding["field"] == "title"
    assert read_evidence(index, binding["segment_id"]).citation.segment_kind == "name"
    assert hit.segment == "chunk" and hit.loc == 7
    assert all(read_evidence(index, w["segment_id"]).citation.segment_kind == "chunk"
               for w in proof(hit)["remainder_witnesses"])


def test_C1_21_whole_literal_path_precedes_heuristic_space_percent_scope(index):
    path = "/owned/a b/预算 100%_table.sqlite"
    add(index, "unrelated body", path=path)
    for query in (path, path.rsplit("/", 1)[-1]):
        hit = search(index, query)[0]
        assert hit.path == path and hit.segment == "name" and hit.loc is None
        assert hit.why["exact_priority"] == 0


def test_C1_22_same_basename_keeps_distinct_stable_files(index):
    add(index, "alpha", path="/owned/a/report.md")
    add(index, "beta", path="/owned/b/report.md")
    paths = [h.path for h in search(index, "report.md")]
    assert paths == [h.path for h in search(index, "report.md")]
    assert set(paths) == {"/owned/a/report.md", "/owned/b/report.md"}


def test_C1_23_unrelated_file_cannot_lend_random_remainder_to_real_anchor(index):
    add(index, "refund ZT-84 approved", vector=evec(0))
    add(index, "randomzz", path="/owned/unrelated.txt", vector=evec(1))
    for i in range(25):
        add(index, "filler", path=f"/fill/{i}.txt", vector=evec(1))
    assert Searcher(index, FixedBackend(evec(0))).search("randomzz ZT-84") == []


def test_C1_24_same_identity_files_cannot_pool_half_predicates(index):
    add(index, "ZT-84 alpha", path="/owned/a.txt")
    add(index, "ZT-84 beta", path="/owned/b.txt")
    assert search(index, "alpha beta ZT-84") == []


def test_C1_25_explicit_clauses_bind_their_own_distinct_files(index):
    add(index, "MN-42 charge limit 17", path="/owned/a.txt")
    add(index, "QV-86 retention days 29", path="/owned/b.txt")
    hits = search(index, "charge limit MN-42; retention days QV-86")
    assert {h.path for h in hits} == {"/owned/a.txt", "/owned/b.txt"}
    assert {tuple(proof(h)["clauses"]) for h in hits} == {(0,), (1,)}


@pytest.mark.parametrize("query,a,b", [
    pytest.param("charge limit MN-42; randomzz QV-86", "MN-42 charge limit 17 randomzz", "QV-86 retention days 29", id="C1-26"),
    pytest.param("charge limit MN-42; retention days QV-87", "MN-42 charge limit 17", "QV-86 retention days 29", id="C1-27"),
    pytest.param("MN-42 charge limit and QV-86 retention days", "MN-42 charge limit 17", "QV-86 retention days 29", id="C1-28"),
    pytest.param("compare MN-42 and QV-86 on retention days", "MN-42 retention days 17", "QV-86 charge limit 29", id="C1-30"),
])
def test_cross_file_absence_and_scope_cannot_be_completed_by_global_union(index, query, a, b):
    add(index, a, path="/owned/a.txt")
    add(index, b, path="/owned/b.txt")
    assert search(index, query) == []


def test_C1_29_compare_shared_remainder_must_exist_in_each_owner(index):
    add(index, "MN-42 retention days 17", path="/owned/a.txt")
    add(index, "QV-86 retention days 29", path="/owned/b.txt")
    hits = search(index, "compare MN-42 and QV-86 on retention days")
    assert {h.path for h in hits} == {"/owned/a.txt", "/owned/b.txt"}


def test_C1_31_quoted_phrase_cannot_bridge_source_segments(index):
    add(index, ["MN-42 signed", "receipt"])
    q, filters = parse_filters('MN-42 "signed receipt"')
    assert search(index, q, filters=filters) == []


def test_C1_32_phrase_literal_and_identity_share_true_owner(index):
    add(index, "MN-42 signed receipt")
    q, filters = parse_filters('MN-42 "signed receipt"')
    hit = search(index, q, filters=filters)[0]
    assert any(w["kind"] == "phrase" and w["unit"] == "signed receipt" for w in proof(hit)["remainder_witnesses"])


def test_C1_33_extracted_phrase_positions_are_not_guessed_across_clauses(index):
    add(index, "MN-42 signed receipt", path="/owned/a.txt")
    add(index, "QV-86 signed receipt", path="/owned/b.txt")
    q, filters = parse_filters('MN-42; QV-86 "signed receipt"')
    diag = {}
    assert search(index, q, filters=filters, diagnostics=diag) == []
    assert diag["reason"] == "anchor_scope_ambiguous"


def test_C1_34_filters_exclude_even_genuine_identity_owner(index):
    add(index, "MN-42", path="/owned/a/source.txt")
    q, filters = parse_filters("MN-42 under:/owned/b")
    assert search(index, q, filters=filters) == []


def test_C1_35_limit_cannot_silently_drop_one_required_owner(index):
    add(index, "MN-42 charge limit 17", path="/owned/a.txt")
    add(index, "QV-86 retention days 29", path="/owned/b.txt")
    diag = {}
    assert search(index, "charge limit MN-42; retention days QV-86", limit=1, diagnostics=diag) == []
    assert diag["reason"] == "anchor_not_in_selected_limit"


def test_C1_36_declared_bounds_and_cancellation_have_no_semantic_fallback(index):
    from goldmine.deadline import RequestTimeout, request_cancellation
    add(index, "ZT-84")
    diag = {}
    assert search(index, " ".join(f"CODE-{i}" for i in range(33)), diagnostics=diag) == []
    assert diag["reason"] == "anchor_verification_bound"
    add(index, ["XX-72"] * 2001, path="/owned/many.txt")
    assert search(index, "XX-72", diagnostics=diag) == []
    assert diag["reason"] == "anchor_verification_bound" and diag["inspected_source_rows"] <= 2000
    def cancelled():
        raise RequestTimeout("fixture cancellation")
    with pytest.raises(RequestTimeout, match="fixture cancellation"):
        with request_cancellation(cancelled):
            search(index, "ZT-84")


def test_C1_37_semantic_remainder_is_unimplemented(index):
    add(index, "ZT-84 remove inherited settings", vector=evec(0))
    diag = {}
    assert Searcher(index, FixedBackend(evec(0))).search("ZT-84 erase inherited options", diagnostics=diag) == []
    assert diag["reason"] == "remainder_unsupported" and diag["semantic_remainder"] == "not_implemented"


def test_C1_38_private_unanchored_measurement_keeps_old_uncalibrated_route(index):
    add(index, "generic related docs", vector=evec(0))
    for i in range(25):
        add(index, "filler", path=f"/fill/{i}.txt", vector=evec(1))
    query = "private deployment measurement"
    assert not plan_query(query).anchors
    hit = Searcher(index, FixedBackend(evec(0))).search(query, why=True)[0]
    assert hit.admission_reason == "semantic_standout" and "anchor_contract" not in hit.why


def test_C1_39_whole_title_lookup_cannot_fold_numeric_suffix_case(index):
    add(index, "unrelated", title="-.5MA")
    assert search(index, "-.5mA") == []


def test_C1_40_full_path_lookup_cannot_fold_numeric_filename_unit_case(index):
    add(index, "unrelated", path="/owned/5MA.csv")
    assert search(index, "/owned/5mA.csv") == []


def test_C1_41_quoted_phrase_cannot_fold_numeric_unit_case(index):
    add(index, "current 5GHZ")
    q, filters = parse_filters('"current 5GHz"')
    assert search(index, q, filters=filters) == []


@pytest.mark.parametrize("query,text", [
    pytest.param("loadconfig", "load_config and load-config", id="C1-42"),
    pytest.param("loadconfigv2", "load_config_v2 and load-config-v2", id="C1-43"),
])
def test_general_separator_alias_is_not_a_verified_identity(index, query, text):
    add(index, text)
    hits = search(index, query)
    assert not hits  # no real FTS match either; unsafe lookup key alone cannot admit


def test_C1_47_complete_raw_casefold_is_not_compact_alias(index):
    add(index, "ZT-84")
    hit = search(index, "zt-84")[0]
    assert hit.why["exact_match_type"] == "raw"


def test_C1_49_nfkc_before_safe_alias_eligibility(index):
    add(index, "ZT-84")
    hit = search(index, "ＺＴ８４")[0]
    assert hit.why["exact_match_type"] == "alias" and hit.why["exact_string"] == "ZT-84"
