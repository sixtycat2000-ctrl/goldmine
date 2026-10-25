"""Regression cases for scoped rebuilds, identity recovery and stale evidence."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import subprocess
import sys
import textwrap

import numpy as np
import pytest

from goldmine import fingerprints
from goldmine.backends.base import BackendStartupError, EmbeddingError
from goldmine.backends.mock import MockBackend
from goldmine.config import Config
from goldmine.ingest import IngestPipeline
from goldmine.ingest.crawl import CrawledPage
from goldmine.search import Searcher, parse_filters
from goldmine.store import FileRec, SegmentRec, Store


@pytest.fixture
def library(tmp_path):
    cfg = Config()
    cfg.home = tmp_path / "home"
    root = tmp_path / "sources"
    root.mkdir()
    with closing(Store(tmp_path / "index.sqlite")) as store:
        yield cfg, root, store


def ingest(cfg, store, roots, *, seed=0, lexical=False, **kw):
    return IngestPipeline(cfg, store, None if lexical else MockBackend(seed=seed), lexical_only=lexical).run(
        [str(root) for root in roots], **kw)


def file_rows(store, path):
    rec = store.find_by_path(str(path))
    return store.db.execute("SELECT id,kind,vec FROM segments WHERE file=? ORDER BY id", (rec.id,)).fetchall()


def test_scoped_reindex_retains_other_directory_and_its_card_memberships(library):
    from test_store import _v2_card, _v2_run

    cfg, root, store = library
    paths = []
    for name in ("a", "b"):
        folder = root / name
        folder.mkdir()
        path = folder / "notes.txt"
        path.write_text(f"orchard banana cultivation in {name}")
        paths.append(path)
    ingest(cfg, store, [root])
    before = file_rows(store, paths[1])
    sid = next(sid for sid, kind, _ in before if kind == "chunk")
    store.save_run(_v2_run("run", "view"), [_v2_card("run", "view", [sid])])
    report = ingest(cfg, store, [paths[0].parent], reindex=True)
    assert report.content_indexed == 1
    assert file_rows(store, paths[1]) == before
    assert store.view_member_chunk_ids("view") == {sid}
    assert ingest(cfg, store, [paths[1].parent]).unchanged == 1


def test_model_change_reindex_expands_to_all_recorded_sources(library):
    cfg, root, store = library
    dirs = [root / name for name in ("a", "b")]
    for folder in dirs:
        folder.mkdir()
        (folder / "note.txt").write_text("orchard banana cultivation")
    ingest(cfg, store, dirs)
    report = ingest(cfg, store, [dirs[0]], seed=1, reindex=True)
    assert report.content_indexed == 2
    assert store.counts().vectors == 2
    assert {row[0] for row in store.db.execute("SELECT embed_id FROM files")} == {fingerprints.live_embed_id(MockBackend(seed=1))}
    assert store.meta("fingerprint_id") == fingerprints.fingerprint_id(fingerprints.build(MockBackend(seed=1)))


def test_model_change_refuses_unavailable_recorded_root_without_clearing_vectors(library):
    cfg, root, store = library
    a, b = root / "a", root / "b"
    for folder in (a, b):
        folder.mkdir()
        (folder / "note.txt").write_text("orchard banana cultivation")
    ingest(cfg, store, [a, b])
    before = store.db.execute("SELECT * FROM segments ORDER BY id").fetchall()
    old_fp = store.meta("fingerprint_id")
    b.rename(root / "offline")
    with pytest.raises(fingerprints.FingerprintMismatch, match="unavailable"):
        ingest(cfg, store, [a], seed=1, reindex=True)
    assert store.db.execute("SELECT * FROM segments ORDER BY id").fetchall() == before
    assert store.meta("fingerprint_id") == old_fp


def test_reindex_startup_failure_preserves_index(library):
    cfg, root, store = library
    (root / "note.txt").write_text("orchard banana cultivation")
    ingest(cfg, store, [root])
    before = store.db.execute("SELECT * FROM segments ORDER BY id").fetchall()
    class Broken(MockBackend):
        def startup(self):
            raise BackendStartupError("cannot start", "use a working backend")
    with pytest.raises(BackendStartupError):
        IngestPipeline(cfg, store, Broken(seed=1)).run([str(root)], reindex=True)
    assert store.db.execute("SELECT * FROM segments ORDER BY id").fetchall() == before


def test_crash_after_first_commit_keeps_identity_and_fences_different_model(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    for i in range(2):
        (root / f"file{i}.txt").write_text(f"orchard banana cultivation {i}")
    code = textwrap.dedent("""
        import os, sys
        from pathlib import Path
        from goldmine.config import Config
        from goldmine.store import Store
        from goldmine.ingest import IngestPipeline
        from goldmine.backends.mock import MockBackend
        class Crash(MockBackend):
            def embed_texts(self, texts):
                self.calls = getattr(self, 'calls', 0) + 1
                if self.calls == 2:
                    os._exit(73)
                return super().embed_texts(texts)
        cfg = Config(); cfg.home = Path(sys.argv[1]) / 'home'
        store = Store(Path(sys.argv[1]) / 'crash.sqlite')
        IngestPipeline(cfg, store, Crash()).run([sys.argv[2]])
    """)
    child = subprocess.run([sys.executable, "-c", code, str(tmp_path), str(root)],
                           cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
    assert child.returncode == 73, child.stderr
    cfg = Config()
    cfg.home = tmp_path / "home"
    with closing(Store(tmp_path / "crash.sqlite")) as store:
        assert store.counts().vectors == 1
        assert store.meta("fingerprint_id") is not None
        assert json.loads(store.meta("roots_json")) == [str(root)]
        with pytest.raises(fingerprints.FingerprintMismatch):
            ingest(cfg, store, [root], seed=1)
        assert store.counts().vectors == 1
        report = ingest(cfg, store, [root])
        assert report.unchanged == 1 and store.counts().vectors == 2


def test_unknown_legacy_vectors_cannot_be_blessed_by_a_new_model(library):
    cfg, root, store = library
    (root / "note.txt").write_text("orchard banana cultivation")
    ingest(cfg, store, [root])
    store.set_meta("fingerprint_id", None)
    with pytest.raises(fingerprints.FingerprintMismatch):
        ingest(cfg, store, [root], seed=1)
    assert store.counts().vectors == 1


def test_plain_ingest_promotes_lexical_text_without_churning_numeric_content(library):
    cfg, root, store = library
    (root / "note.txt").write_text("orchard banana cultivation")
    (root / "numbers.txt").write_text("123456789 9988776655")
    ingest(cfg, store, [root], lexical=True)
    report = ingest(cfg, store, [root])
    assert report.content_indexed == 1 and report.unchanged == 1
    assert store.counts().vectors == 1
    assert ingest(cfg, store, [root]).unchanged == 2


def test_unsupported_transition_removes_body_vectors_and_memberships(library):
    from test_store import _v2_card, _v2_run

    cfg, root, store = library
    path = root / "code.py"
    path.write_text("orchard banana cultivation")
    ingest(cfg, store, [root], code=True)
    sid = store.db.execute("SELECT id FROM segments WHERE kind='chunk'").fetchone()[0]
    store.save_run(_v2_run("run", "view"), [_v2_card("run", "view", [sid])])
    report = ingest(cfg, store, [root], code=False)
    assert report.unsupported == 1
    assert store.counts().vectors == 0
    assert Searcher(store, None).search("banana", scorer="lexical") == []
    assert store.view_member_chunk_ids("view") == set()


def test_rechunk_ids_do_not_retarget_card_members_and_empty_cards_dissolve(library):
    from test_store import _v2_card, _v2_run

    cfg, root, store = library
    path = root / "note.txt"
    path.write_text("orchard banana cultivation")
    ingest(cfg, store, [root])
    sid = store.db.execute("SELECT id FROM segments WHERE kind='chunk'").fetchone()[0]
    store.save_run(_v2_run("run", "view"), [_v2_card("run", "view", [sid])])
    path.write_text("satellite cryptography and unrelated transmission")
    ingest(cfg, store, [root])
    assert store.segment_text(sid) is None
    assert store.view_member_chunk_ids("view") == set()
    assert store.dissolve_pass("view", 3).dissolved == 1
    assert store.cards() == []
    old_max = store.db.execute("SELECT max(id) FROM segments").fetchone()[0]
    store.delete_file(store.find_by_path(str(path)).id)
    fresh = store.upsert_file(FileRec(path=str(root / "fresh.txt")))
    assert store.db.execute("SELECT min(id) FROM segments WHERE file=?", (fresh,)).fetchone()[0] > old_max


@pytest.mark.parametrize("empty", [False, True])
def test_recrawl_replaces_old_evidence_and_records_one_failure(library, monkeypatch, empty):
    cfg, _, store = library
    url = "https://example.invalid/notes"
    page = CrawledPage(url, url, 200, "<p>orchard banana cultivation</p>", "old", 1)
    monkeypatch.setattr("goldmine.ingest.crawl.crawl", lambda *args: iter([page]))
    ingest(cfg, store, [url])
    if empty:
        page.html, page.sha256 = "<html></html>", "new"
        report = ingest(cfg, store, [url])
        assert report.metadata_only == 1 and report.failed == 0
    else:
        class Failed(MockBackend):
            def embed_texts(self, texts):
                raise EmbeddingError("failed once")
        report = IngestPipeline(cfg, store, Failed()).run([url])
        assert report.failed == 1 and store.find_by_path(url).state == "failed"
    assert Searcher(store, None).search("banana", scorer="lexical") == []


@pytest.mark.parametrize("query,target_text", [("banana", "apple orchard banana cultivation"), ("退款期限", "退款期限四十五天")])
def test_scope_filters_precede_global_fts_candidate_limit(library, query, target_text):
    _, _, store = library
    with store.write_transaction():
        for i in range(1003):
            fid = store.upsert_file(FileRec(path=f"/noise/{i}.txt", state="done", capability="content_indexed"))
            store.replace_segments(fid, [SegmentRec(kind="chunk", modality="none", text_for_fts=query)])
        target = store.upsert_file(FileRec(path="/target/answer.txt", state="done", capability="content_indexed"))
        store.replace_segments(target, [SegmentRec(kind="chunk", modality="none", text_for_fts=target_text)])
    clean, filters = parse_filters(query + " under:/target")
    hits = Searcher(store, None).search(clean, filters=filters, scorer="lexical")
    assert [hit.path for hit in hits] == ["/target/answer.txt"]


def test_site_filter_uses_hostname_boundary_including_subdomains(library):
    _, _, store = library
    paths = ["https://example.com/page", "https://sub.example.com/page", "https://evil-example.com/page",
             "https://wrong.invalid/path/example.com", "https://example.com@evil.invalid/page"]
    for path in paths:
        fid = store.upsert_file(FileRec(path=path, source="url", kind="webpage", state="done", capability="content_indexed"))
        store.replace_segments(fid, [SegmentRec(kind="chunk", modality="none", text_for_fts="banana cultivation")])
    query, filters = parse_filters("banana site:example.com")
    assert {hit.path for hit in Searcher(store, None).search(query, filters=filters, scorer="lexical")} == set(paths[:2])


def test_status_cli_receives_live_media_capability(library, monkeypatch, capsys):
    from goldmine.cli import main

    cfg, root, store = library
    (root / "note.txt").write_text("orchard banana cultivation")
    IngestPipeline(cfg, store, MockBackend(multimodal=True)).run([str(root)])
    monkeypatch.setattr("goldmine.backends.create_backend", lambda *args: MockBackend(multimodal=True))
    monkeypatch.setattr("goldmine.commands.inspection.probe_all", lambda cfg: {})
    assert main(["status", "--db", str(store.path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["capabilities"]["visual_search"] is True


def test_reset_reindex_without_backend_preserves_existing_evidence(library, monkeypatch, capsys):
    from goldmine.cli import main
    from goldmine.backends import BackendUnavailable

    cfg, root, store = library
    (root / "note.txt").write_text("orchard banana cultivation")
    ingest(cfg, store, [root])
    before = store.db.execute("SELECT * FROM segments ORDER BY id").fetchall()
    def unavailable(*args):
        raise BackendUnavailable("missing backend")
    monkeypatch.setattr("goldmine.cli._make_backend", unavailable)
    assert main(["reset", "--reindex", "--db", str(store.path)]) == 4
    assert store.db.execute("SELECT * FROM segments ORDER BY id").fetchall() == before
