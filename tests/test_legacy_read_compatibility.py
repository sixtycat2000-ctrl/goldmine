"""Read-only4/5 support executes C1; real source proof is never switched off."""
from contextlib import closing
from dataclasses import replace
import hashlib
import sqlite3

import pytest

from goldmine import migrations
from goldmine.application import Application
from goldmine.citations import read_evidence, resolve_citation
from goldmine.contracts import ApplicationError, SearchRequest
from goldmine.deadline import RequestTimeout, request_cancellation, request_timeout
from goldmine.search import Searcher, parse_filters
from goldmine.store import Store
from legacy_read_fixtures import build, fixture_writer, logical


def search(path, query, **options):
    with closing(Store(path, ro=True)) as store:
        return Searcher(store, None).search(query, scorer="lexical", why=True, **options)


@pytest.mark.parametrize("version", [4, 5, 6, "current"])
def test_ordinary_supported_read_no_backend_or_index_mutation(tmp_path, version):
    path = build(tmp_path / "index.sqlite", version, [{"text": "harbor receipt ordinary evidence"}])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    def forbidden():
        pytest.fail("lexical read constructed a backend")
    result = Application(path, forbidden).search(SearchRequest(query="harbor receipt", lexical_only=True))
    assert result.hits
    # v6+ carry the current lexical derivation; "current" is the live TARGET
    assert result.settings["lexical_upgrade_required"] is (
        version != "current" and version < 6
    )
    assert result.settings["source_capabilities"]["schema_version"] == (
        migrations.TARGET if version == "current" else version
    )
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("version", [4, 5])
@pytest.mark.parametrize("query,text,expected", [
    ("ZT-84", "ZT-84 retained harbor", True), ("ZT84", "ZT-84 retained harbor", True),
    ("zt84", "ZT-84 retained harbor", False), ("ZT-840", "ZT-84 retained harbor", False),
    ("loadconfig", "load_config", False), ("pkg.v2.7", "pkg.v2.6", False),
    (".NET", "NET", False), (".5m", "5m", False),
    ("25m", "2.5m", False), ("5mA", "5MA", False),
])
def test_actual_complete_legacy_sources_and_typed_alias_rules(tmp_path, version, query, text, expected):
    path = build(tmp_path / "index.sqlite", version, [{"text": text}])
    # loadconfig is ordinary text rather than a hard anchor; use the maximal
    # literal syntax to demand exactly this complete code spelling.
    if query == "loadconfig":
        query = "`loadconfig`"
    hits = search(path, query)
    assert bool(hits) is expected
    if expected:
        assert hits[0].segment == "chunk" and hits[0].loc == 7
        assert hits[0].why["anchor_contract"]["bindings"][0]["raw"] in text


@pytest.mark.parametrize("version", [4, 5])
def test_old_nfc_original_and_modern_normalized_original_return_actual_raw(tmp_path, version):
    original = "A\u0301 ZT-84 harbor"
    path = build(tmp_path / "index.sqlite", version, [{"text": original}])
    hit = search(path, "ZT-84")[0]
    with closing(Store(path, ro=True)) as store:
        actual = read_evidence(store, hit.segment_id)
        assert actual.text == original
        assert resolve_citation(store, actual.citation.citation_id) == actual
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE fts SET original='counterfeit' WHERE rowid=?", (hit.segment_id,))
        db.commit()
    with pytest.raises(ValueError, match="normalization"):
        search(path, "ZT-84")


@pytest.mark.parametrize("version", [4, 5])
@pytest.mark.parametrize("sign", ["+", "-", "−", "＋", "﹢", "﹣", "－"])
def test_legacy_metadata_does_not_strip_any_numeric_sign(tmp_path, version, sign):
    path = build(tmp_path / "index.sqlite", version, [{"path": "/owned/" + sign + "5mA.csv", "text": "unrelated"}])
    assert search(path, "5mA") == []
    pure = search(path, sign + "5mA.csv")
    assert pure and pure[0].segment == "name" and pure[0].loc is None


@pytest.mark.parametrize("version", [4, 5])
def test_pure_actual_metadata_does_not_supply_mixed_body_predicates(tmp_path, version):
    path = build(tmp_path / "index.sqlite", version, [{"path": "/owned/界面?%# 测量.md", "title": "ZT-84"}])
    assert search(path, "/owned/界面?%# 测量.md")[0].segment == "name"
    assert search(path, "ZT-84")[0].segment == "name"
    assert search(path, "ZT-84 signed harbor") == []


@pytest.mark.parametrize("version", [4, 5])
def test_real_metadata_owner_has_distinct_actual_body_predicate_witness(tmp_path, version):
    path = build(tmp_path / "index.sqlite", version, [{"title": "ZT-84", "text": "signed harbor receipt"}])
    hits = search(path, "ZT-84 signed harbor")
    # anchor.v2-intent: the ambient _CODE title identity in prose is soft; the
    # hit returns via ordinary admission without an owner proof (7694837).
    assert hits and all("anchor_contract" not in h.why for h in hits)
    assert all("anchor_contract_soft" in h.why for h in hits)
    # The distinct title/body witness pin survives for a structural identity.
    strict = build(tmp_path / "strict.sqlite", version, [{"title": "ZT_84", "text": "signed harbor receipt"}])
    hit = search(strict, "ZT_84 signed harbor")[0]
    assert hit.segment == "chunk" and hit.loc == 7
    proof = hit.why["anchor_contract"]
    assert proof["bindings"][0]["field"] == "title"
    with closing(Store(strict, ro=True)) as store:
        assert read_evidence(store, proof["bindings"][0]["segment_id"]).citation.evidence_type == "name_metadata"
        assert all(read_evidence(store, item["segment_id"]).citation.evidence_type == "content"
                   for item in proof["remainder_witnesses"])


@pytest.mark.parametrize("version", [5, 6])
def test_modern_raw_quantity_body_keeps_suffix_case_after_normalized_recall(tmp_path, version):
    path = build(tmp_path / "index.sqlite", version, [{"text": "5mA signed harbor"}])
    hit = search(path, "5mA")[0]
    assert hit.why["anchor_contract"]["bindings"][0]["raw"] == "5mA"
    assert search(path, "5MA") == []
    with closing(Store(path, ro=True)) as store:
        assert store.segment_text(hit.segment_id) == "5mA signed harbor"


@pytest.mark.parametrize("version", [4, 5])
def test_legacy_exact_phrase_requires_single_contiguous_actual_segment(tmp_path, version):
    path = build(tmp_path / "index.sqlite", version, [{"text": ["ZT-84 signed receipt", "unrelated"]}])
    for text in ('"signed receipt"', 'ZT-84 "signed receipt"'):
        query, filters = parse_filters(text)
        assert search(path, query, filters=filters)
    split = build(tmp_path / "split.sqlite", version, [{"text": ["ZT-84 signed", "receipt"]}])
    for text in ('"signed receipt"', 'ZT-84 "signed receipt"'):
        query, filters = parse_filters(text)
        assert search(split, query, filters=filters) == []


@pytest.mark.parametrize("version", [4, 5])
def test_no_other_file_or_copied_name_or_folder_can_supply_remainder(tmp_path, version):
    path = build(tmp_path / "index.sqlite", version, [
        {"path": "/owned/a.txt", "text": "ZT-84 harbor", "folder": "randomzz signed receipt"},
        {"path": "/owned/b.txt", "text": "randomzz signed receipt"},
    ])
    hits = search(path, "ZT-84 randomzz")
    # anchor.v2-intent: ambient _CODE anchors lose the same-file remainder
    # veto; the anchor file returns via ordinary quorum (7694837).
    assert hits and all(h.admission_reason != "verified_exact" for h in hits)
    assert all("exact_source" not in h.why and "anchor_contract_soft" in h.why for h in hits)
    # The quoted phrase stays a hard existence obligation and picks its true
    # owner even when the ambient anchor points elsewhere.
    query, filters = parse_filters('ZT-84 "signed receipt"')
    assert {h.path for h in search(path, query, filters=filters)} == {"/owned/b.txt"}
    # A structural identity keeps the veto as a disclosed soft demote that is
    # never verified exact, and the phrase cannot be lent to the anchor owner.
    strict = build(tmp_path / "strict.sqlite", version, [
        {"path": "/owned/a.txt", "text": "ZT_84 harbor", "folder": "randomzz signed receipt"},
        {"path": "/owned/b.txt", "text": "randomzz signed receipt"},
    ])
    strict_hits = search(strict, "ZT_84 randomzz")
    # anchor.v2-intent: the veto survives as remainder_soft on the real owner,
    # never as a verified exact promotion.
    assert "/owned/a.txt" in {h.path for h in strict_hits}
    assert all("exact_source" not in h.why for h in strict_hits)
    assert all(h.why["anchor_contract_soft"]["soft_clauses"] == [{"clause_id": 0, "reason": "remainder_soft"}]
               for h in strict_hits if h.path == "/owned/a.txt")
    query, filters = parse_filters('ZT_84 "signed receipt"')
    strict_phrase = search(strict, query, filters=filters)
    # anchor.v2-intent: the demoted clause cannot constrain, so the folder
    # words may surface a ordinarily too — but never as a verified proof.
    assert "/owned/b.txt" in {h.path for h in strict_phrase}
    assert all("exact_source" not in h.why and h.admission_reason != "verified_exact" for h in strict_phrase)


@pytest.mark.parametrize("version", [4, 5])
def test_compare_and_semicolon_keep_true_separate_owners_and_limit(tmp_path, version):
    path = build(tmp_path / "index.sqlite", version, [
        {"path": "/owned/a.txt", "text": "MN-42 harbor charge"},
        {"path": "/owned/b.txt", "text": "QV-86 harbor charge"},
    ])
    query = "compare MN-42 and QV-86 on harbor charge"
    # anchor.v2-intent: ambient _CODE compare clauses are soft; ordinary
    # admission returns both files (7694837 reference).
    assert len(search(path, query, limit=2)) == 2
    # Soft clauses have empty owners, so the final limit recheck never fires.
    assert len(search(path, query, limit=1)) == 1
    assert len(search(path, "MN-42 harbor; QV-86 charge")) == 2
    assert len(search(path, "MN-42 QV-86 harbor charge")) == 2
    # The separate-owner and limit pins survive for structural identities.
    strict = build(tmp_path / "strict.sqlite", version, [
        {"path": "/owned/a.txt", "text": "MN_42 harbor charge"},
        {"path": "/owned/b.txt", "text": "QV_86 harbor charge"},
    ])
    strict_query = "compare MN_42 and QV_86 on harbor charge"
    assert len(search(strict, strict_query, limit=2)) == 2
    assert search(strict, strict_query, limit=1) == []
    assert len(search(strict, "MN_42 harbor; QV_86 charge")) == 2
    # anchor.v2-intent: one clause with identities on different files demotes
    # softly (anchor_sibling_absent) instead of refusing.
    conjunction = search(strict, "MN_42 QV_86 harbor charge")
    assert {h.path for h in conjunction} == {"/owned/a.txt", "/owned/b.txt"}
    assert all("exact_source" not in h.why for h in conjunction)
    assert all(h.why["anchor_contract_soft"]["soft_clauses"] == [{"clause_id": 0, "reason": "anchor_sibling_absent"}]
               for h in conjunction)


@pytest.mark.parametrize("version", [4, 5])
def test_qualified_python_and_actual_kind_are_required(tmp_path, version):
    path = build(tmp_path / "index.sqlite", version, [
        {"path": "/owned/wirefmt.py", "kind": "code", "text": "def encode_field(value): pass"},
    ])
    assert search(path, "wirefmt.encode_field")
    assert search(path, "wirefmt.Mode.encode_field") == []
    if version == 4:
        with closing(sqlite3.connect(path)) as db:
            db.execute("UPDATE files SET kind='doc'"); db.commit()
    else:
        with fixture_writer(path, version) as writer:
            with writer.write_transaction():
                writer.db.execute("UPDATE files SET kind='doc'")
    assert search(path, "wirefmt.encode_field") == []


@pytest.mark.parametrize("version", [4, 5])
def test_cjk_owner_grams_and_complete_quote_are_actual_body(tmp_path, version):
    path = build(tmp_path / "index.sqlite", version, [{"text": ["ZT-84 保留天数17", "签名", "回执"]}])
    assert search(path, "ZT-84 保留天数")
    query, filters = parse_filters('ZT-84 "签名回执"')
    assert search(path, query, filters=filters) == []
    # Native4 has no ordinary CJK recall index. Keep its existing ranking
    # limitation: the supported Latin anchor supplies recall, while the
    # quote still needs actual contiguous CJK source proof.
    query, filters = parse_filters('ZT-84 "保留天数"')
    assert search(path, query, filters=filters)


@pytest.mark.parametrize("version", [4, 5])
def test_supported_read_stays_in_filter_snapshot_and_cancels(tmp_path, version):
    path = build(tmp_path / "index.sqlite", version, [{"text": "ZT-84 harbor"}])
    query, filters = parse_filters("ZT-84 under:/other")
    assert search(path, query, filters=filters) == []
    with pytest.raises(RequestTimeout):
        with request_timeout(0):
            search(path, "ZT-84")
    def cancel():
        raise RequestTimeout("fixture cancellation")
    with pytest.raises(RequestTimeout, match="cancellation"):
        with request_cancellation(cancel):
            search(path, "ZT-84")


def test_body_only4_all_returned_readers_use_actual_body_and_do_not_write(tmp_path):
    path = build(tmp_path / "body-only.sqlite", 4, [{"text": ["ZT-84 harbor evidence", "later whole body"]}], body_only=True)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    app = Application(path)
    result = app.search(SearchRequest(query="ZT-84", lexical_only=True, pack=True, context=1, require_citations=True))
    assert result.pack_text and "later whole body" in result.pack_text
    assert result.settings["source_capabilities"]["original_provenance"] == "stored_body_only"
    with closing(Store(path, ro=True)) as store:
        hit = result.hits[0]
        actual = read_evidence(store, hit.segment_id)
        assert actual.text == "ZT-84 harbor evidence"
        assert resolve_citation(store, actual.citation.citation_id) == actual
        assert store.segment_text(hit.segment_id) == actual.text
        assert not any(name == "original" for _, name, *_ in store.db.execute("PRAGMA table_info(fts)"))
    assert app.status().counts.files == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("version,table", [
    (5, "fts_cjk"), (6, "fts_cjk"), (6, "lookup_terms"), ("current", "lookup_terms"),
])
def test_missing_promised_derived_table_is_not_legacy_fallback(tmp_path, version, table):
    path = build(tmp_path / "index.sqlite", version, [{"text": "ZT-84 harbor"}])
    with closing(sqlite3.connect(path)) as db:
        db.execute(f"DROP TABLE {table}"); db.commit()
    for query in ("harbor", "ZT-84"):
        with pytest.raises(sqlite3.OperationalError, match=table):
            search(path, query)


@pytest.mark.parametrize("version", [4, 5, 6, "current"])
def test_contradictory_lexical_profile_is_an_error(tmp_path, version):
    path = build(tmp_path / "index.sqlite", version, [{"text": "ZT-84"}])
    # Raw test-owned mutation; production reads never repair it.
    if version == 4:
        with closing(sqlite3.connect(path)) as db:
            db.execute("INSERT INTO meta VALUES('lexical_version','lex.v2-exact')"); db.commit()
    elif version == "current":
        with closing(Store(path)) as writer:
            writer.set_meta("lexical_version", "counterfeit")
    else:
        with fixture_writer(path, version) as writer:
            writer.set_meta("lexical_version", "counterfeit")
    with pytest.raises(sqlite3.OperationalError, match="inconsistent"):
        search(path, "ZT-84")


@pytest.mark.parametrize("version", [4, 5])
def test_missing_uuid_does_not_get_created_by_ordinary_read(tmp_path, version):
    path = build(tmp_path / "index.sqlite", version, [{"text": "harbor receipt"}], uuid=None)
    # Migration5 legitimately createsUUID; remove it with its actual writer
    # before closing this explicitly constructed fixture.
    if version == 5:
        with fixture_writer(path, version) as writer:
            writer.set_meta("index_uuid", None)
    app = Application(path)
    assert app.search(SearchRequest(query="harbor", lexical_only=True)).hits
    with pytest.raises(ApplicationError, match="reindex lexical"):
        app.search(SearchRequest(query="harbor", lexical_only=True, require_citations=True))
    with closing(Store(path, ro=True)) as store:
        assert store.meta("index_uuid") is None


@pytest.mark.parametrize("version", [5, 6])
def test_missing_original_is_not_body_only_legacy4(tmp_path, version):
    path = build(tmp_path / "index.sqlite", version, [{"text": "ZT-84 harbor"}])
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute("SELECT rowid,name,body FROM fts").fetchall()
        sql = db.execute("SELECT sql FROM sqlite_schema WHERE name='fts'").fetchone()[0]
        db.execute("DROP TABLE fts")
        db.execute(sql.replace("name, body, original UNINDEXED", "name, body"))
        db.executemany("INSERT INTO fts(rowid,name,body) VALUES(?,?,?)", rows)
        db.commit()
    with pytest.raises(sqlite3.OperationalError, match="original"):
        search(path, "ZT-84")


@pytest.mark.parametrize("version", [4, 5, 6])
def test_reader_has_no_ddl_dml_source_files_or_extra_backend(tmp_path, monkeypatch, version):
    from pathlib import Path
    path = build(tmp_path / "index.sqlite", version, [{"text": ["ZT-84 harbor", "later receipt"]}])
    with closing(Store(path, ro=True)) as store:
        before = logical(store.db)
        rejected = []
        writes = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                  sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_TEMP_TABLE,
                  sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_CREATE_TEMP_VIEW,
                  sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_DROP_TABLE}
        def authorize(action, *args):
            if action in writes:
                rejected.append((action, args))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        store.db.set_authorizer(authorize)
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_bytes", lambda *_: pytest.fail("source/model file read"))
            assert Searcher(store, None).search("ZT-84", scorer="lexical")
        store.db.set_authorizer(None)
        assert rejected == [] and logical(store.db) == before


@pytest.mark.parametrize("version", [4, 5])
def test_missing_hard_identity_cannot_start_vector_or_embedding_rescue(tmp_path, monkeypatch, version):
    from test_search_offline import FixedBackend, evec
    path = build(tmp_path / "index.sqlite", version, [{"text": "ZT-84 harbor"}])
    with closing(Store(path, ro=True)) as store:
        # anchor.v2-intent: an ambient _CODE miss is ordinary admission, which
        # legitimately reaches the vector path (7694837 reference).
        assert Searcher(store, None).search("ZT-840 harbor", scorer="lexical")
        backend = FixedBackend(evec(0))
        patch = monkeypatch.context()
        with patch as guard:
            guard.setattr(store, "load_vectors", lambda: pytest.fail("missing anchor loaded vectors"))
            guard.setattr(backend, "embed_texts", lambda *_: pytest.fail("missing anchor embedded"))
            # A missing structural identity still refuses before any
            # vector/embedding work starts.
            for scorer in ("lexical", "hybrid", "semantic", "rrf"):
                assert Searcher(store, backend).search("ZT_840 harbor", scorer=scorer) == []


@pytest.mark.parametrize("version", [4, 5])
def test_unique_source_budget_is_shared_across_repeated_routes(tmp_path, version):
    from goldmine.anchor_resolution import Resolver
    from goldmine.query_obligations import plan_query
    path = build(tmp_path / "index.sqlite", version, [{"text": "ZT-84 harbor receipt"}])
    with closing(Store(path, ro=True)) as store:
        with store.read_snapshot():
            resolver = Resolver(store, {1})
            anchor = plan_query("ZT84").anchors[0]
            assert resolver.bind(anchor)
            count = len(resolver.attempted)
            assert resolver.bind(anchor)
            assert resolver.remainder(1, "harbor receipt", [], Searcher.terms)
            assert len(resolver.attempted) == count <= 2


@pytest.mark.parametrize("version", [4, 5])
def test_same_reader_snapshot_survives_separate_legitimate_migration(tmp_path, version):
    path = build(tmp_path / "index.sqlite", version, [{"text": "A\u0301 ZT-84 harbor"}])
    with closing(Store(path, ro=True)) as reader:
        with reader.read_snapshot():
            assert reader.db.execute("PRAGMA user_version").fetchone()[0] == version
            assert Searcher(reader, None).search("ZT-84", scorer="lexical")
            # This is a separate authorized fixture writer, not reader mutation.
            Store(path).close()
            assert reader.db.execute("PRAGMA user_version").fetchone()[0] == version
            assert Searcher(reader, None).search("ZT-84", scorer="lexical")
        assert reader.db.execute("PRAGMA user_version").fetchone()[0] == migrations.TARGET
        assert Searcher(reader, None).search("ZT-84", scorer="lexical")


@pytest.mark.parametrize("version", [4, 5])
def test_32_anchors_and_33_overflow_use_existing_bound(tmp_path, version):
    anchors = [f"ZT-{n:03d}" for n in range(33)]
    path = build(tmp_path / "index.sqlite", version, [{"text": " ".join(anchors)}])
    assert search(path, " ".join(anchors[:32]))
    assert search(path, " ".join(anchors)) == []


@pytest.mark.parametrize("version", [4, 5])
def test_sparse_ids_and_real_vector_only_gap_do_not_invent_content(tmp_path, version):
    from test_search_offline import evec
    path = build(tmp_path / "index.sqlite", version, [{"text": ["ZT-84 harbor", "later receipt"]}])
    def mutate(db):
        db.execute("UPDATE segments SET id=100 WHERE id=2")
        db.execute("UPDATE fts SET rowid=100 WHERE rowid=2")
        if version == 5:
            db.execute("UPDATE fts_cjk SET rowid=100 WHERE rowid=2")
        db.execute("INSERT INTO segments(id,file,kind,modality,loc,vec) VALUES(200,1,'chunk','image',9,?)",
                   (evec(0).astype('<f2').tobytes(),))
    if version == 4:
        with closing(sqlite3.connect(path)) as db:
            mutate(db); db.commit()
    else:
        with fixture_writer(path, version) as writer:
            with writer.write_transaction():
                mutate(writer.db)
    with closing(Store(path, ro=True)) as store:
        before = logical(store.db)
        hit = Searcher(store, None).search("ZT84", scorer="lexical")[0]
        assert hit.segment_id == 100 and hit.loc == 7
        assert read_evidence(store, 200).citation.evidence_type == "vector_only"
        assert read_evidence(store, 200).text is None
        assert store.segment_text(200) is None
        assert logical(store.db) == before


@pytest.mark.parametrize("upgrade", [False, True])
def test_actual_latency_prepare_prefix_only_upgrades_owned_copy(tmp_path, monkeypatch, upgrade):
    import os
    from pathlib import Path
    import shutil
    import sys
    # Standalone runs do not inherit scripts/ from other test modules.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import search_bench.session as session_module
    from acceptance_lib.evidence import read_command_run
    source = build(tmp_path / "original.sqlite", 4, [{"text": "A\u0301 ZT-84 harbor"}])
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    model, server = tmp_path / "fixture-not-model", tmp_path / "fixture-not-server"
    model.write_text("fixture only; never executed")
    server.write_text("fixture only; never executed")
    project = Path(__file__).resolve().parents[1]
    out = tmp_path / "session"
    session = session_module.Session(out, project, model, server, sys.executable, "controlled")
    original_popen = session_module.subprocess.Popen
    stopped = []
    class StopBeforeServe(Exception):
        pass
    def spawn(argv, *args, **kwargs):
        if argv == [session.python, "-m", "goldmine.cli", "serve"]:
            stopped.append(argv)
            raise StopBeforeServe
        return original_popen(argv, *args, **kwargs)
    monkeypatch.setattr(session_module.subprocess, "Popen", spawn)
    try:
        with pytest.raises(StopBeforeServe):
            session.prepare(source, upgrade=upgrade)
    finally:
        session.close()
    assert len(stopped) == 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    with closing(Store(session.db, ro=True)) as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == (migrations.TARGET if upgrade else 4)
        assert store.segment_text(2) == "A\u0301 ZT-84 harbor"
    manifest = out / "commands/prepare-lexical/manifest.json"
    assert manifest.exists() is upgrade
    if upgrade:
        _, receipt, _ = read_command_run(manifest)
        assert receipt["exit_code"] == 0 and not receipt["timed_out"]
        assert receipt["argv"][-5:] == ["reindex", "lexical", "--db", str(session.db), "--json"]
    # Preserve exact raw paths/bytes for report replay, not a relocated success.
    if os.environ.get("COMPAT_EXTRA_ARTIFACTS"):
        target = Path(os.environ["COMPAT_EXTRA_ARTIFACTS"]) / ("prepare-upgrade" if upgrade else "prepare-reference")
        shutil.copytree(out, target)
