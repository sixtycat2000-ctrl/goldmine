"""Proof eligibility precedes the bound on4/5/6; actual bounds stay strict."""
from contextlib import closing
import sqlite3

import pytest

import goldmine.anchor_admission as admission
from goldmine.deadline import RequestTimeout
from goldmine.search import Searcher
from goldmine.store import Store
from legacy_read_fixtures import build


def docs(count=2001, *, body="reportstxt harbor", basename="rep-orts.txt"):
    return [{"path": f"/owned/declared-{i:04d}/{basename}", "text": body if i == 0 else []}
            for i in range(count)]


def run(store, query):
    diagnostics = {}
    hits = Searcher(store, None).search(query, scorer="lexical", diagnostics=diagnostics)
    return [(h.path, h.segment_id, h.score, h.coverage) for h in hits], diagnostics


@pytest.mark.parametrize("version", [4, 5, 6])
@pytest.mark.parametrize("query,expected", [("reportstxt", 1), ("rep-orts", 20)])
def test_ineligible_alias_and_raw_stems_do_not_veto_ordinary_admission(tmp_path, monkeypatch, version, query, expected):
    path = build(tmp_path / "index.sqlite", version, docs())
    with closing(Store(path, ro=True)) as store:
        actual, diag = run(store, query)
        # Isolate only optional prelookup on a fixture. This is a causal
        # comparator, not the implementation: mandatory search still executes.
        with monkeypatch.context() as patch:
            patch.setattr(admission, "_pure_metadata", lambda plan, resolver: [])
            isolated, _ = run(store, query)
        assert actual == isolated and len(actual) == expected
        assert diag["anchors"] == 0 and diag["reason"] is None


@pytest.mark.parametrize("version", [4, 5, 6])
def test_true_full_metadata_survives_many_proof_ineligible_keys(tmp_path, version):
    path = build(tmp_path / "index.sqlite", version, docs(body="harbor unrelated") +
                 [{"path": "/owned/actual/reportstxt", "text": []}])
    with closing(Store(path, ro=True)) as store:
        hits, diag = run(store, "reportstxt")
        assert len(hits) == 1 and hits[0][0] == "/owned/actual/reportstxt"
        assert diag["reason"] is None
        assert store.segment_row(hits[0][1])[:3] == ("name", None, None)


def test_successful_pure_metadata_only_supplements_actual_body_not_metadata_aliases(tmp_path):
    path = build(tmp_path / "index.sqlite", 6, [
        {"path": "/owned/ZT84"},
        {"path": "/owned/alias/ZT-84.txt"},
        {"path": "/owned/body.txt", "text": "ZT-84 harbor"},
    ])
    with closing(Store(path, ro=True)) as store:
        hits = Searcher(store, None).search("ZT84", scorer="lexical", why=True)
        assert {hit.path for hit in hits} == {"/owned/ZT84", "/owned/body.txt"}
        body = next(hit for hit in hits if hit.path.endswith("body.txt"))
        bindings = body.why["anchor_contract"]["bindings"]
        assert bindings[0]["field"] == "body" and bindings[0]["match_type"] == "alias"
        assert store.segment_text(body.segment_id) == "ZT-84 harbor"


@pytest.mark.parametrize("version", [4, 5, 6])
def test_true_complete_metadata_overflow_still_blocks(tmp_path, version):
    path = build(tmp_path / "index.sqlite", version, docs())
    with closing(Store(path, ro=True)) as store:
        hits, diag = run(store, "rep-orts.txt")
        assert hits == [] and diag["reason"] == "anchor_verification_bound"
        assert diag["inspected_source_rows"] <= 2000


@pytest.mark.parametrize("version", [4, 5, 6])
def test_mandatory_alias_is_not_removed_or_given_a_larger_budget(tmp_path, version):
    path = build(tmp_path / "tiny.sqlite", version, [{"text": "ZT-84 harbor"}])
    with closing(Store(path, ro=True)) as store:
        assert run(store, "ZT84")[0]
    many = [{"path": f"/owned/{i}.txt", "text": "ZT-84 harbor"} for i in range(2001)]
    path = build(tmp_path / "many.sqlite", version, many)
    with closing(Store(path, ro=True)) as store:
        hits, diag = run(store, "ZT84")
        assert hits == [] and diag["reason"] == "anchor_verification_bound"
        assert diag["inspected_source_rows"] <= 2000


@pytest.mark.parametrize("version", [4, 5, 6])
def test_real_full_title_path_and_unit_casing_not_broad_normalized_key(tmp_path, version):
    path = build(tmp_path / "index.sqlite", version, [
        {"path": "/owned/5MA.csv", "title": "5MA"},
        {"path": "/owned/full?%# 界面.md", "title": "ZT-84"},
    ])
    with closing(Store(path, ro=True)) as store:
        assert run(store, "5mA")[0] == []
        assert run(store, "/owned/full?%# 界面.md")[0]
        assert run(store, "ZT-84")[0]


def test_whole_metadata_raw_pointer_corruption_is_not_hidden_by_filter(tmp_path):
    path = build(tmp_path / "index.sqlite", 6, [{"path": "/owned/ZT-84.txt", "text": "harbor"},
                                               {"path": "/owned/other.txt", "text": "other"}])
    with closing(Store(path)) as writer:
        with writer.write_transaction():
            writer.db.execute("UPDATE lookup_terms SET file_id=2 WHERE source='basename' AND raw_term='ZT-84.txt'")
    with closing(Store(path, ro=True)) as store:
        with pytest.raises(ValueError, match="lookup pointer"):
            run(store, "ZT-84.txt")


def test_dangling_metadata_segment_errors_instead_of_disappearing_in_join(tmp_path):
    path = build(tmp_path / "index.sqlite", 6, [{"path": "/owned/ZT-84.txt", "text": "harbor"}])
    # Deliberate malformed test-owned artifact; do not defeat live writer
    # fences or manufacture a valid Store receipt for this corruption.
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE lookup_terms SET segment_id=999 WHERE source='basename' AND raw_term='ZT-84.txt'")
        db.commit()
    with closing(Store(path, ro=True)) as store:
        with pytest.raises(ValueError, match="missing stored segment"):
            run(store, "ZT-84.txt")


@pytest.mark.parametrize("version", [4, 5, 6])
def test_metadata_callback_deadline_keeps_original_error_and_cleans_request(tmp_path, monkeypatch, version):
    import goldmine.metadata_prelookup as metadata
    path = build(tmp_path / "index.sqlite", version, [{"path": "/owned/ZT-84.txt", "text": "harbor"}])
    with closing(Store(path, ro=True)) as store:
        with monkeypatch.context() as patch:
            def interrupt():
                raise RequestTimeout("inside metadata eligibility fixture")
            patch.setattr(metadata, "check_deadline", interrupt)
            with pytest.raises(RequestTimeout, match="inside metadata eligibility"):
                run(store, "ZT-84.txt")
        # The previous callback/error/allowed-source closure must not survive.
        assert run(store, "ZT-84.txt")[0]
        with pytest.raises(sqlite3.Error):
            store.db.execute("SELECT _gm_full_metadata_v1(1,1,'name',1,'p',NULL,1,'path','p','p','raw','p')").fetchone()
