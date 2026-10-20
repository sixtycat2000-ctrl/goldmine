"""Shared service boundaries: stored evidence, coherent generations, no writes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from uuid import uuid4

import numpy as np
import pytest

from goldmine import fingerprints
from goldmine.application import Application
from goldmine.backends.mock import MockBackend
from goldmine.citations import read_evidence
from goldmine.contracts import ApplicationError, BudgetTooSmall, ContextRequest, SearchRequest
from goldmine.pack import PackSettings, build_pack_from_hits
from goldmine.search import Searcher, parse_filters
from goldmine.store import FileRec, SegmentRec, Store, connect_index


def _file(store, path="/explicit/notes.txt", text="needle original evidence", *, vec=None, info=None):
    fid = store.upsert_file(FileRec(
        path=path, state="done", capability="content_indexed", sha256="stored-file-sha", info=info,
    ))
    store.replace_segments(fid, [
        SegmentRec(kind="name", modality="none", text_for_fts="explicit folder"),
        SegmentRec(kind="chunk", modality="text" if vec is not None else "none",
                   loc=1, loc_end=2, excerpt=text, text_for_fts=text, vec=vec),
    ])
    return fid


@pytest.fixture
def indexed(tmp_path):
    writer = Store(tmp_path / "index.sqlite")
    writer.set_meta("index_uuid", str(uuid4()))
    _file(writer)
    yield writer
    writer.close()


def _request(**kwargs):
    return SearchRequest(query="needle", scorer="lexical", lexical_only=True, **kwargs)


def test_lexical_application_matches_existing_search_and_pack(indexed):
    # This is behavioral compatibility with the existing canonical algorithms,
    # not a new implementation of their scoring or rendering in a test.
    for context in (0, 1, 3):
        for limit in (0, 1, 20):
            request = _request(context=context, limit=limit, pack=True)
            expected_hits = Searcher(indexed, None).search("needle", scorer="lexical", limit=limit)
            expected_text, expected_meta = build_pack_from_hits(
                indexed, None, PackSettings(query="needle", scorer="lexical", limit=limit, context=context), expected_hits,
                citation_id_of=lambda sid: read_evidence(indexed, sid).citation.citation_id,
            )
            result = Application(indexed.path).search(request)
            assert result.hits == expected_hits
            assert result.pack_text == expected_text
            assert result.pack_meta["tokenizer"] == "goldmine.char-estimate/v1"
            assert {k:v for k,v in result.pack_meta.items() if k != "tokenizer"} == expected_meta


def test_filters_are_already_parsed_and_lexical_never_constructs_backend(indexed):
    def forbidden_factory():
        pytest.fail("lexical-only request constructed a model backend")

    query, filters = parse_filters("needle under:/explicit")
    result = Application(indexed.path, forbidden_factory).search(
        SearchRequest(query=query, filters=filters, lexical_only=True),
    )
    assert [h.path for h in result.hits] == ["/explicit/notes.txt"]


def test_read_only_search_and_status_preserve_content_and_schema(indexed):
    before = list(indexed.db.iterdump())
    app = Application(indexed.path)
    app.search(_request(pack=True, context=1))
    status = app.status()
    assert status.counts.files == 1
    assert status.capabilities["opaque_citations"]
    assert list(indexed.db.iterdump()) == before


def test_missing_read_only_index_does_not_create_parent(tmp_path):
    path = tmp_path / "absent-parent" / "index.sqlite"
    with pytest.raises(ApplicationError, match="index does not exist") as exc:
        Application(path).search(_request())
    assert exc.value.kind == "no_index"
    with pytest.raises(sqlite3.OperationalError):
        Store(path, ro=True)
    assert not path.parent.exists()


@pytest.mark.parametrize("name", ["index?literal.sqlite", "index#literal.sqlite", "index%25.sqlite", "中文 空格.sqlite"])
def test_read_only_uri_opens_exact_literal_path(tmp_path, name):
    path = tmp_path / name
    writer = Store(path)
    writer.set_meta("selected_file", name)
    writer.close()
    before = {p.name for p in tmp_path.glob("*") if not p.name.endswith(("-wal", "-shm"))}
    con = connect_index(path, ro=True)
    try:
        assert con.execute("SELECT value FROM meta WHERE key='selected_file'").fetchone()[0] == name
        assert con.execute("PRAGMA database_list").fetchone()[2] == str(path.resolve())
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO meta VALUES('bad','write')")
    finally:
        con.close()
    assert {p.name for p in tmp_path.glob("*") if not p.name.endswith(("-wal", "-shm"))} == before


def test_read_only_old_schema_never_migrates(indexed):
    indexed.db.execute("PRAGMA user_version=3")
    indexed.db.commit()
    before = list(indexed.db.iterdump())
    reader = Store(indexed.path, ro=True)
    try:
        assert reader.db.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        reader.close()
    with pytest.raises(ApplicationError) as exc:
        Application(indexed.path).status()
    assert exc.value.kind == "missing_capability"
    assert list(indexed.db.iterdump()) == before
    assert indexed.db.execute("PRAGMA user_version").fetchone()[0] == 3


def test_legacy_cli_search_needs_no_uuid_but_citation_api_does(indexed):
    indexed.set_meta("index_uuid", None)
    app = Application(indexed.path)
    assert app.search(_request()).hits
    with pytest.raises(ApplicationError, match="reindex lexical") as exc:
        app.search(_request(require_citations=True))
    assert exc.value.kind == "missing_capability"
    assert indexed.meta("index_uuid") is None


def test_canonical_digest_includes_metadata_and_preserves_key_order_independence(indexed):
    with indexed.write_transaction():
        indexed.db.execute("UPDATE files SET info=?", ('{"z":{"b":2,"a":1},"a":"中文"}',))
    sid = indexed.db.execute("SELECT id FROM segments WHERE kind='chunk'").fetchone()[0]
    first = read_evidence(indexed, sid)
    c = first.citation
    payload = ["gm-citation/v1", c.file_id, c.path, c.source, "stored-file-sha", c.segment_id,
               "chunk", "none", 1.0, 2.0, "content", {"a":"中文", "z":{"a":1,"b":2}}, first.text]
    expected = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    assert c.content_sha256 == expected
    with indexed.write_transaction():
        indexed.db.execute("UPDATE files SET info=?", ('{"a":"中文","z":{"a":1,"b":2}}',))
    assert read_evidence(indexed, sid).citation.citation_id == c.citation_id
    with indexed.write_transaction():
        indexed.db.execute("UPDATE files SET info=?", ('{"a":"changed"}',))
    with pytest.raises(ApplicationError) as exc:
        Application(indexed.path).get(c.citation_id)
    assert exc.value.kind == "stale_citation"


@pytest.mark.parametrize("bad_id", [
    "/etc/passwd", "https://example.com", "SELECT * FROM files", "gm2:a:1:abcd",
    "gm1:00000000-0000-0000-0000-000000000000:9223372036854775808:0123456789abcdef",
])
def test_get_rejects_non_opaque_or_overflow_ids(indexed, bad_id):
    with pytest.raises(ApplicationError) as exc:
        Application(indexed.path).get(bad_id)
    assert exc.value.kind in ("invalid_params", "stale_citation")


def test_metadata_only_and_vector_only_get_do_not_invent_a_body(indexed):
    fid = indexed.upsert_file(FileRec(
        path="/中文/照片.jpg", kind="image", state="done", capability="metadata_only",
        info={"camera":"stored-camera", "taken":"2026-01-01"},
    ))
    indexed.replace_segments(fid, [
        SegmentRec(kind="name", modality="none", text_for_fts=""),
        SegmentRec(kind="image", modality="image", vec=np.eye(1, 768, dtype=np.float32)[0]),
    ])
    for sid in [r[0] for r in indexed.db.execute("SELECT id FROM segments WHERE file=?", (fid,))]:
        evidence = read_evidence(indexed, sid)
        result = Application(indexed.path).get(evidence.citation.citation_id)
        assert result.evidence_type in ("name_metadata", "vector_only")
        assert "no quotable document body" in result.context_text
        assert "stored-camera" in result.context_text
        assert "/中文/照片.jpg" in result.context_text
        assert result.used_tokens <= 2000


def test_citation_get_and_context_use_stored_text_not_excerpt(indexed):
    with indexed.write_transaction():
        indexed.db.execute("UPDATE segments SET excerpt='presentation only' WHERE kind='chunk'")
    app = Application(indexed.path)
    citation = app.search(_request(require_citations=True)).citations[0]
    got = app.get(citation.citation_id)
    context = app.context(ContextRequest([citation.citation_id, citation.citation_id]))
    assert "needle original evidence" in got.context_text
    assert "presentation only" not in got.context_text
    assert len(context.citations) == 1


def test_search_pack_and_citation_share_snapshot_during_concurrent_replace(tmp_path):
    writer = Store(tmp_path / "race.sqlite")
    writer.set_meta("index_uuid", str(uuid4()))
    qvec = np.zeros(768, dtype=np.float32); qvec[0] = 1
    distractor = np.zeros(768, dtype=np.float32); distractor[1] = 1
    for i in range(25):
        _file(writer, f"/explicit/filler{i}.txt", "distractor", vec=distractor)
    fid = _file(writer, "/explicit/last.txt", "old original evidence", vec=qvec)

    class ReplaceDuringEmbedding(MockBackend):
        def embed_texts(self, texts):
            writer.replace_segments(fid, [
                SegmentRec(kind="chunk", modality="text", loc=17, loc_end=17,
                           excerpt="NEW unrelated text", text_for_fts="NEW unrelated text", vec=distractor),
            ])
            return qvec.reshape(1, -1)

    try:
        app = Application(writer.path, ReplaceDuringEmbedding)
        result = app.search(SearchRequest(
            query="old evidence", scorer="semantic", pack=True, context=1, require_citations=True,
        ))
        assert result.hits[0].cosine == pytest.approx(1)
        assert result.hits[0].excerpt == "old original evidence"
        assert "old original evidence" in result.pack_text
        assert "NEW unrelated" not in result.pack_text
        with pytest.raises(ApplicationError) as exc:
            app.get(result.citations[0].citation_id)
        assert exc.value.kind == "stale_citation"
        assert writer.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        writer.close()


def test_backend_closes_on_fingerprint_failure_and_exception(indexed):
    backend = MockBackend()
    closed = []
    backend.close = lambda: closed.append(True)
    indexed.set_meta("fingerprint_id", "incompatible")
    indexed.set_meta("fingerprint_json", "{}")
    with pytest.raises(fingerprints.FingerprintMismatch):
        Application(indexed.path, lambda: backend).search(SearchRequest("needle"))
    assert closed == [True]


def test_expired_request_never_constructs_backend(indexed):
    calls = []
    with pytest.raises(ApplicationError) as exc:
        Application(indexed.path, lambda: calls.append(True)).search(SearchRequest("needle", timeout_s=0))
    assert exc.value.kind == "timeout"
    assert calls == []


def test_long_empty_envelope_is_typed_budget_error(indexed):
    with pytest.raises(BudgetTooSmall):
        Application(indexed.path).search(replace(_request(pack=True, budget=512), query="x" * 8192))


def test_full_neighbor_parts_reach_adapter(indexed):
    result = Application(indexed.path).search(_request(context=1))
    assert result.contexts[1].parts[0]["role"] == "hit"
    assert result.contexts[1].parts[0]["text"] == "needle original evidence"


def test_sql_deadline_is_typed_and_next_request_still_reads(indexed, monkeypatch):
    app = Application(indexed.path)
    original = Store.counts

    def expensive_count(store):
        return store.db.execute(
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) "
            "SELECT sum(x) FROM n"
        ).fetchone()

    monkeypatch.setattr(Store, "counts", expensive_count)
    with pytest.raises(ApplicationError) as exc:
        app.status(timeout_s=0.01)
    assert exc.value.kind == "timeout"
    monkeypatch.setattr(Store, "counts", original)
    assert app.status().counts.files == 1
