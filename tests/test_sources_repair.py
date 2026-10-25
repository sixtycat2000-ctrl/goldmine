"""End-to-end source management and targeted repair, with real SQLite state."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path

import numpy as np
import pytest

from goldmine.backends.mock import MockBackend
from goldmine.cli import main
from goldmine.config import Config
from goldmine.distill import DistillRefused, Scope, _corpus, run_distill
from goldmine.ingest import IngestPipeline
from goldmine.repair import execute_repairs, plan_repairs
from goldmine.sources import forget_sources, list_sources, restore_sources
from goldmine.store import FileRec, SegmentRec, Store


@pytest.fixture
def corpus(tmp_home, tmp_path):
    cfg = Config()
    cfg.home = tmp_home / "gmhome"
    root = tmp_path / "chosen"
    root.mkdir()
    db = tmp_path / "index.sqlite"
    with closing(Store(db)) as store:
        yield cfg, root, db, store


def index(cfg, store, roots, *, lexical=False, **kw):
    return IngestPipeline(cfg, store, None if lexical else MockBackend(), lexical_only=lexical).run(
        [str(root) for root in roots], **kw)


def test_missing_vector_repair_uses_stored_text_and_preserves_other_vectors(corpus, monkeypatch):
    cfg, root, _, store = corpus
    a, b = root / "a.txt", root / "b.txt"
    a.write_text("orchard banana cultivation")
    b.write_text("copper gasket inspection")
    index(cfg, store, [root], lexical=True)
    index(cfg, store, [a])
    before = store.db.execute("SELECT id,vec FROM segments WHERE vec IS NOT NULL").fetchall()
    original = store.db.execute("SELECT rowid,original,body FROM fts ORDER BY rowid").fetchall()
    monkeypatch.setattr(IngestPipeline, "_extract", lambda *args, **kwargs: pytest.fail("text backfill must not re-extract"))
    plan = plan_repairs(store, cfg, MockBackend(), missing_vectors=True)
    assert [(p["path"], p["action"]) for p in plan] == [(str(b), "backfill")]
    result = execute_repairs(store, cfg, MockBackend(), plan)
    assert result["vectors_added"] == 1 and result["files_backfilled"] == 1 and result["failures"] == []
    assert store.db.execute("SELECT id,vec FROM segments WHERE id=?", (before[0][0],)).fetchone() == before[0]
    assert store.db.execute("SELECT rowid,original,body FROM fts ORDER BY rowid").fetchall() == original
    assert plan_repairs(store, cfg, MockBackend(), missing_vectors=True) == []


def test_repair_plan_cli_is_read_only_and_does_not_start_model(corpus, monkeypatch, capsys):
    cfg, root, db, store = corpus
    path = root / "notes.txt"
    path.write_text("orchard banana cultivation")
    index(cfg, store, [root], lexical=True)
    before = store.db.execute("SELECT * FROM meta ORDER BY key").fetchall()
    class NoStartup(MockBackend):
        def startup(self):
            pytest.fail("repair --plan must not start a model")
    monkeypatch.setattr("goldmine.commands.repair.create_backend", lambda *args: NoStartup())
    assert main(["repair", "--db", str(db), "--plan", "--missing-vectors", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["plan"][0]["reasons"] == ["missing_vectors"]
    assert store.db.execute("SELECT * FROM meta ORDER BY key").fetchall() == before
    assert store.counts().vectors == 0


def test_failed_only_cli_does_not_refresh_successful_changed_files(corpus, capsys):
    from conftest import make_docx

    cfg, root, db, store = corpus
    okay, broken = root / "okay.txt", root / "broken.docx"
    okay.write_text("orchard banana cultivation")
    broken.write_bytes(b"not a zip")
    assert index(cfg, store, [root], lexical=True).failed == 1
    old_rows = store.db.execute("SELECT * FROM segments WHERE file=?", (store.find_by_path(str(okay)).id,)).fetchall()
    okay.write_text("changed but not selected for retry")
    make_docx(broken, ["the repaired quarterly report"])
    assert main(["repair", "--db", str(db), "--failed-only", "--lexical-only", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [item["path"] for item in result["plan"]] == [str(broken)]
    assert store.find_by_path(str(broken)).state == "done"
    assert store.db.execute("SELECT * FROM segments WHERE file=?", (store.find_by_path(str(okay)).id,)).fetchall() == old_rows


def test_repair_scope_and_unavailable_source_are_explicit(corpus):
    cfg, root, _, store = corpus
    a, b = root / "a.txt", root / "b.txt"
    for path in (a, b):
        path.write_text("orchard banana cultivation")
    index(cfg, store, [root], lexical=True)
    a.unlink()
    plan = plan_repairs(store, cfg, MockBackend(), [str(a)], missing_vectors=True)
    assert len(plan) == 1 and plan[0]["action"] == "unavailable"
    result = execute_repairs(store, cfg, MockBackend(), plan)
    assert len(result["failures"]) == 1
    assert store.counts().files == 2  # an unavailable source is never implicitly forgotten


def test_failed_backfill_retains_stored_evidence_for_retry(corpus):
    from goldmine.backends.base import EmbeddingError

    cfg, root, _, store = corpus
    path = root / "note.txt"
    path.write_text("orchard banana cultivation")
    index(cfg, store, [root], lexical=True)
    class Failed(MockBackend):
        def embed_texts(self, texts):
            raise EmbeddingError("unavailable embedding")
    backend = Failed()
    plan = plan_repairs(store, cfg, backend, missing_vectors=True)
    result = execute_repairs(store, cfg, backend, plan)
    assert len(result["failures"]) == 1
    assert store.find_by_path(str(path)).capability == "content_indexed"
    assert store.db.execute("SELECT count(*) FROM fts WHERE fts MATCH 'banana'").fetchone()[0] == 1
    assert plan_repairs(store, cfg, MockBackend(), missing_vectors=True)


def test_lexical_audio_is_selected_for_media_upgrade(corpus):
    cfg, root, _, store = corpus
    path = root / "speech.wav"
    path.write_bytes(b"opaque audio")
    index(cfg, store, [root], lexical=True)
    plan = plan_repairs(store, cfg, MockBackend(multimodal=True), missing_vectors=True)
    assert len(plan) == 1 and "media_upgrade" in plan[0]["reasons"]
    assert plan[0]["action"] == "reingest"


def test_asr_tool_upgrade_has_same_decision_in_plan_and_plain_ingest(corpus, monkeypatch):
    from goldmine.chunker import Chunk
    from goldmine.ingest.extraction import _Outcome, _Piece

    cfg, root, _, store = corpus
    path = root / "speech.wav"
    path.write_bytes(b"opaque audio")
    index(cfg, store, [root], lexical=True)
    rec = store.find_by_path(str(path))
    store.mark_file(rec.id, state="done", capability="metadata_only", detail="ASR unavailable",
                    info={"audio_routing": "none:no_capability", "asr_status": "unavailable"},
                    extractor_version=None, chunker_version=rec.chunker_version)
    monkeypatch.setattr("goldmine.ingest.asr.resolve_tools", lambda cfg: object())
    plan = plan_repairs(store, cfg, MockBackend())
    assert len(plan) == 1 and plan[0]["reasons"] == ["asr_retry"]
    monkeypatch.setattr(IngestPipeline, "_extract", lambda *args, **kwargs: _Outcome(
        capability="content_indexed", extractor_version="asr-whisper.v2",
        chunks=[_Piece(Chunk("a recovered speech transcript", 0, 2), True, kind="audio")],
        info={"asr_status": "complete", "asr_segments": 1}))
    report = index(cfg, store, [root])
    assert report.content_indexed == 1 and report.unchanged == 0
    assert plan_repairs(store, cfg, MockBackend()) == []


def test_forget_directory_removes_cards_and_prevents_reingest_until_restore(corpus):
    from test_store import _v2_card, _v2_run

    cfg, root, _, store = corpus
    private, sibling = root / "private", root / "private-other"
    for folder in (private, sibling):
        folder.mkdir()
        (folder / "note.txt").write_text("orchard banana cultivation")
    index(cfg, store, [root])
    rec = store.find_by_path(str(private / "note.txt"))
    sid = store.db.execute("SELECT id FROM segments WHERE file=? AND kind='chunk'", (rec.id,)).fetchone()[0]
    card = _v2_card("run", "view", [sid])
    card.excerpts_json = json.dumps([{"path": rec.path, "text": "orchard banana cultivation"}])
    store.save_run(_v2_run("run", "view"), [card])
    report = forget_sources(store, [str(private)])
    assert report["files_removed"] == report["cards_removed"] == 1
    assert store.find_by_path(rec.path) is None
    assert store.cards(status=None) == []
    assert store.db.execute("SELECT count(*) FROM card_chunks").fetchone()[0] == 0
    assert (private / "note.txt").is_file()  # only the index is changed
    index(cfg, store, [root])
    assert store.find_by_path(rec.path) is None
    assert store.find_by_path(str(sibling / "note.txt")) is not None
    restore_sources(store, [str(private)])
    index(cfg, store, [root])
    assert store.find_by_path(rec.path) is not None


def test_forget_clears_historical_excerpt_even_after_membership_was_removed(corpus):
    from test_store import _v2_card, _v2_run

    cfg, root, _, store = corpus
    path = root / "retired-notes.txt"
    path.write_text("orchard banana cultivation")
    index(cfg, store, [root])
    sid = store.db.execute("SELECT id FROM segments WHERE kind='chunk'").fetchone()[0]
    card = _v2_card("run", "view", [sid])
    card.excerpts_json = json.dumps([{"path": str(path), "text": "old text"}])
    store.save_run(_v2_run("run", "view"), [card])
    path.write_text("new text with no original facts")
    index(cfg, store, [root])
    assert store.view_member_chunk_ids("view") == set()
    assert forget_sources(store, [str(path)])["cards_removed"] == 1
    assert store.cards(status=None) == []


def test_source_cli_dry_run_and_remove_restore(corpus, capsys):
    cfg, root, db, store = corpus
    path = root / "name with spaces.txt"
    path.write_text("orchard banana cultivation")
    index(cfg, store, [root], lexical=True)
    before = store.db.execute("SELECT * FROM meta ORDER BY key").fetchall()
    assert main(["forget", str(path), "--db", str(db), "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["files_removed"] == 1
    assert store.db.execute("SELECT * FROM meta ORDER BY key").fetchall() == before
    assert main(["sources", "remove", str(root), "--db", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["files_removed"] == 1
    assert main(["sources", "list", "--db", str(db), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["roots"] == [] and payload["forgotten"] == [str(root)]
    assert main(["sources", "restore", str(root), "--db", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["restored"] == [str(root)]


def test_forget_url_scope_uses_origin_and_path_boundaries(corpus, monkeypatch):
    from goldmine.ingest.crawl import CrawledPage

    cfg, _, _, store = corpus
    urls = ["https://example.invalid/docs/one", "https://example.invalid/docs-other/two",
            "https://other.invalid/docs/one"]
    pages = [CrawledPage(url, url, 200, "<p>orchard banana cultivation</p>", "sha", 1) for url in urls]
    monkeypatch.setattr("goldmine.ingest.crawl.crawl", lambda *args: iter(pages))
    index(cfg, store, ["https://example.invalid"])
    assert forget_sources(store, ["https://example.invalid/docs"])["files_removed"] == 1
    index(cfg, store, ["https://example.invalid"])
    assert store.find_by_path(urls[0]) is None
    assert all(store.find_by_path(url) is not None for url in urls[1:])


def test_crawler_never_fetches_forgotten_nested_url(monkeypatch):
    from types import SimpleNamespace
    from goldmine.ingest.crawl import CrawlBounds, crawl

    fetched = []
    class Fetcher:
        def __init__(self, **kwargs):
            pass
        def fetch(self, url):
            fetched.append(url)
            html = '<a href="/private/note">private</a><a href="/public/note">public</a>' if url.endswith("/") else "<p>ordinary page</p>"
            return SimpleNamespace(ok=True, status=200, html=html, url=url, final_url=url,
                                   detail=None, bytes_read=len(html), sha256="sha", fetched_at=1)
    monkeypatch.setattr("goldmine.ingest.crawl.Fetcher", Fetcher)
    pages = list(crawl(["https://example.invalid/"], CrawlBounds(max_depth=1, respect_robots=False),
                       ["https://example.invalid/private"]))
    assert fetched == ["https://example.invalid/", "https://example.invalid/public/note"]
    assert len(pages) == 2


def test_distill_cap_preflight_happens_before_vector_decode(corpus, monkeypatch):
    _, root, _, store = corpus
    for i in range(5):
        vector = np.zeros(768, np.float32)
        vector[i] = 1
        fid = store.upsert_file(FileRec(path=str(root / f"{i}.txt"), state="done", capability="content_indexed"))
        store.replace_segments(fid, [SegmentRec(kind="chunk", modality="text", text_for_fts=f"topic {i}", vec=vector)])
    monkeypatch.setattr(np, "frombuffer", lambda *args, **kwargs: pytest.fail("cap must be checked before decoding vectors"))
    with pytest.raises(DistillRefused, match="max-chunks"):
        run_distill(store, scope=Scope(), max_chunks=2)


def test_distill_scoped_full_and_incremental_do_not_load_global_vectors(corpus, monkeypatch):
    from test_distill import _run_and_save

    _, root, _, store = corpus
    for i in range(12):
        vector = np.zeros(768, np.float32)
        vector[i] = 1
        folder = "target" if i < 4 else "outside"
        fid = store.upsert_file(FileRec(path=str(root / folder / f"{i}.txt"), state="done", capability="content_indexed", mtime=100))
        store.replace_segments(fid, [SegmentRec(kind="chunk", modality="text", text_for_fts=f"topic distinct {i}", vec=vector)])
    monkeypatch.setattr(store, "load_vectors", lambda: pytest.fail("scoped distill must not load global vectors"))
    scope = Scope(under=[str(root / "target")], after=90)
    assert len(_corpus(store, scope)) == 4
    first = _run_and_save(store, scope=scope, min_members=1, max_chunks=4)
    second = _run_and_save(store, scope=scope, min_members=1, max_chunks=4)
    assert first["stats"]["mode"] == "full" and second["stats"]["mode"] == "incremental"
    with pytest.raises(DistillRefused, match="found 0"):
        run_distill(store, scope=Scope(under=[str(root / "target")], after=101), max_chunks=4)
