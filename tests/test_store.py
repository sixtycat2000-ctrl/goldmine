"""Tier-2: store DDL, fp16 roundtrip, atomic replace, unchanged, prune, counts, FTS.

Also (storage critique 03): the VectorTable cache (F2), the connection factory
pragmas (F6), and cached-vs-fresh search equivalence.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from goldmine.store import FileRec, SegmentRec, Store, connect_index


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "index.sqlite")
    yield s
    s.close()


def _orthogonal_vec(i: int) -> np.ndarray:
    v = np.zeros(768, dtype=np.float32)
    v[i % 768] = 1.0
    return v


def test_ddl_creates_clean(store):
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == Store.SCHEMA_VERSION
    assert store.meta("fingerprint_id") is None
    tables = {
        r[0]
        for r in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
    }
    assert {"meta", "files", "segments", "fts", "cards", "card_chunks"} <= tables
    # WAL + foreign_keys enforced
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert store.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_rejects_newer_schema(tmp_path):
    s = Store(tmp_path / "a.sqlite")
    s.db.execute("PRAGMA user_version = 99")
    s.db.commit()
    s.close()
    with pytest.raises(RuntimeError, match="newer"):
        Store(tmp_path / "a.sqlite")


def test_meta_roundtrip(store):
    store.set_meta("k", "v1")
    assert store.meta("k") == "v1"
    store.set_meta("k", "v2")
    assert store.meta("k") == "v2"
    store.set_meta("k", None)
    assert store.meta("k") is None


def test_upsert_file_writes_name_row(store):
    fid = store.upsert_file(
        FileRec(path="/tmp/deep/dir/notes.txt", kind="doc", size=10, state="pending")
    )
    rows = store.db.execute(
        "SELECT s.kind, fts.name, fts.body FROM segments s JOIN fts ON fts.rowid = s.id "
        "WHERE s.file = ?",
        (fid,),
    ).fetchall()
    assert len(rows) == 1
    kind, name, body = rows[0]
    assert kind == "name"
    assert name == "notes.txt"
    assert "deep" in body and "dir" in body


def test_find_and_unchanged(store):
    fid = store.upsert_file(
        FileRec(path="/a/b.txt", kind="doc", size=5, mtime=100.0, state="pending")
    )
    store.mark_file(
        fid, state="done", capability="content_indexed", detail=None, info=None,
        extractor_version="txt-stdlib.v1",
    )
    rec = store.find_by_path("/a/b.txt")
    assert rec is not None and rec.state == "done"
    assert rec.extractor_version == "txt-stdlib.v1"
    assert not store.unchanged("/a/b.txt", 5, "wrong-hash")
    with store.write_transaction():
        store.db.execute("UPDATE files SET sha256 = 'abc' WHERE id = ?", (fid,))
    store.db.commit()
    assert store.unchanged("/a/b.txt", 5, "abc")
    assert not store.unchanged("/a/b.txt", 6, "abc")
    assert store.unchanged("/a/missing.txt", 1, "x") is False


def test_replace_segments_atomic(store):
    fid = store.upsert_file(FileRec(path="/a/doc.md", kind="doc", size=9, state="pending"))
    v0 = _orthogonal_vec(0)
    store.replace_segments(
        fid,
        [
            SegmentRec(kind="name", modality="none", text_for_fts="folder words"),
            SegmentRec(kind="chunk", modality="text", loc=1, loc_end=2,
                       excerpt="old", text_for_fts="old content", vec=v0),
        ],
    )
    old = store.db.execute(
        "SELECT id FROM segments WHERE file = ? ORDER BY id", (fid,)
    ).fetchall()

    class Boom(Exception):
        pass

    original = Store._insert_segment
    calls = {"n": 0}

    def flaky(self, fid, seg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Boom("crash mid-transaction")
        return original(self, fid, seg)

    Store._insert_segment = flaky
    try:
        with pytest.raises(Boom):
            store.replace_segments(
                fid,
                [SegmentRec(kind="chunk", modality="text", text_for_fts="new", vec=_orthogonal_vec(1))],
            )
    finally:
        Store._insert_segment = original

    # rollback: the OLD segments are intact, nothing new leaked in
    now = store.db.execute("SELECT id FROM segments WHERE file = ? ORDER BY id", (fid,)).fetchall()
    assert now == old
    bodies = [r[0] for r in store.db.execute(
        "SELECT fts.body FROM fts JOIN segments s ON s.id = fts.rowid WHERE s.file = ?", (fid,)
    ).fetchall()]
    assert "old content" in bodies and "new" not in bodies


def test_fp16_vectors_roundtrip_through_load_vectors(store):
    fid = store.upsert_file(FileRec(path="/a/v.txt", kind="doc", size=1, state="pending"))
    vecs = [_orthogonal_vec(0), _orthogonal_vec(1)]
    store.replace_segments(
        fid,
        [
            SegmentRec(kind="chunk", modality="text", loc=1, loc_end=1,
                       text_for_fts="one", vec=vecs[0]),
            SegmentRec(kind="chunk", modality="text", loc=2, loc_end=2,
                       text_for_fts="two", vec=vecs[1]),
        ],
    )
    table = store.load_vectors()
    assert len(table.ids) == 2
    assert table.matrix.shape == (2, 768)
    assert table.matrix.dtype == np.float32
    assert np.allclose(table.matrix[0], vecs[0], atol=1e-3)
    assert table.file_ids == [fid, fid]
    assert table.modalities == ["text", "text"]


def test_delete_file_cascades(store):
    fid = store.upsert_file(FileRec(path="/a/gone.txt", kind="doc", size=1, state="pending"))
    store.replace_segments(
        fid, [SegmentRec(kind="chunk", modality="none", text_for_fts="x")]
    )
    store.delete_file(fid)
    assert store.find_by_path("/a/gone.txt") is None
    assert store.db.execute("SELECT COUNT(*) FROM segments WHERE file = ?", (fid,)).fetchone()[0] == 0


def _fts_orphans(store) -> int:
    return store.db.execute(
        "SELECT COUNT(*) FROM fts WHERE rowid NOT IN (SELECT id FROM segments)"
    ).fetchone()[0]


def test_delete_file_clears_fts_rows(store):
    """P0: FTS5 has no FK cascade — delete_file must clear fts explicitly, or
    the orphaned rows collide when segment ids are reused."""
    fid = store.upsert_file(FileRec(path="/a/gone.txt", kind="doc", size=1, state="pending"))
    store.replace_segments(
        fid, [SegmentRec(kind="chunk", modality="none", text_for_fts="orphan me")]
    )
    store.delete_file(fid)
    assert _fts_orphans(store) == 0


def test_prune_under_clears_fts_rows(store):
    fid = store.upsert_file(FileRec(path="/roots/a/keep.txt", kind="doc", size=1, state="pending"))
    del fid
    gone = store.upsert_file(FileRec(path="/roots/b/gone.txt", kind="doc", size=1, state="pending"))
    store.replace_segments(
        gone, [SegmentRec(kind="chunk", modality="none", text_for_fts="pruned text")]
    )
    assert store.prune_under(["/roots/a"]) == 1
    assert _fts_orphans(store) == 0


def test_upsert_name_refresh_leaves_no_fts_orphans(store):
    """Repeated re-ingest refreshes the name row; the doomed segment's fts row
    must go with it (segment-id reuse would otherwise hit a UNIQUE collision)."""
    fid = store.upsert_file(FileRec(path="/a/notes.txt", kind="doc", size=1, state="pending"))
    store.replace_segments(
        fid, [SegmentRec(kind="chunk", modality="none", text_for_fts="body text")]
    )
    for _ in range(3):
        store.upsert_file(FileRec(path="/a/notes.txt", kind="doc", size=2, state="pending"))
    assert _fts_orphans(store) == 0
    rows = store.db.execute(
        "SELECT COUNT(*) FROM segments WHERE file = ? AND kind = 'name'", (fid,)
    ).fetchone()[0]
    assert rows == 1


def test_delete_content_segments_keeps_name_row(store):
    """P1-5 helper: a failed re-ingest drops searchable content but keeps the
    name row so the file stays findable by its name keywords."""
    fid = store.upsert_file(FileRec(path="/a/report.txt", kind="doc", size=1, state="pending"))
    store.replace_segments(
        fid,
        [
            SegmentRec(kind="name", modality="none", text_for_fts="folder words"),
            SegmentRec(kind="chunk", modality="none", text_for_fts="stale zanzibar body"),
        ],
    )
    store.delete_content_segments(fid)
    assert _fts_orphans(store) == 0
    kinds = [r[0] for r in store.db.execute(
        "SELECT kind FROM segments WHERE file = ?", (fid,)
    ).fetchall()]
    assert kinds == ["name"]
    assert not store.search_fts('"zanzibar"')


def test_prune_under(store):
    store.upsert_file(FileRec(path="/roots/a/keep.txt", kind="doc", size=1, state="pending"))
    store.upsert_file(FileRec(path="/roots/b/gone.txt", kind="doc", size=1, state="pending"))
    store.upsert_file(FileRec(path="https://x.dev/page", source="url", kind="webpage",
                              size=1, state="pending"))
    store.upsert_file(FileRec(path="https://other.dev/page", source="url", kind="webpage",
                              size=1, state="pending"))
    removed = store.prune_under(["/roots/a", "https://x.dev"])
    assert removed == 2
    assert store.find_by_path("/roots/a/keep.txt") is not None
    assert store.find_by_path("https://x.dev/page") is not None
    assert store.find_by_path("/roots/b/gone.txt") is None


def test_fts_ordering_bm25_ascending(store):
    """Normative: bm25 is lower-is-better; search_fts returns best first."""
    for i, text in enumerate(
        ["gold gold gold gold rare", "gold hint", "gold gold gold gold gold gold gold gold"]
    ):
        fid = store.upsert_file(FileRec(path=f"/f{i}.txt", kind="doc", size=1, state="pending"))
        store.replace_segments(
            fid, [SegmentRec(kind="chunk", modality="none", text_for_fts=text)]
        )
    rows = store.search_fts('"gold"', limit=10)
    assert len(rows) == 3
    ranks = [r.rank for r in rows]
    assert ranks == sorted(ranks)          # ascending = best first
    assert all(r.rank < 0 for r in rows)   # bm25 values are negative
    # the row with the MOST golds has the LOWEST (best) bm25
    best = store.search_fts('"gold"', limit=1)[0]
    assert "gold gold gold gold gold" in best.body


def test_coverage(store):
    for i, text in enumerate(["alpha beta", "alpha gamma", "delta"]):
        fid = store.upsert_file(FileRec(path=f"/c{i}.txt", kind="doc", size=1, state="pending"))
        store.replace_segments(fid, [SegmentRec(kind="chunk", modality="none", text_for_fts=text)])
    fids = [store.find_by_path(f"/c{i}.txt").id for i in range(3)]
    cov = store.coverage(['"alpha"', '"beta"'], fids)
    assert cov[fids[0]] == 1.0     # both terms
    assert cov[fids[1]] == 0.5     # one of two
    assert fids[2] not in cov


def test_fts_text_helper(store):
    fid = store.upsert_file(FileRec(path="/x/y.txt", kind="doc", size=1, state="pending"))
    sid = store.fts_text(fid, "chunk", 3, "hello world", vec_ok=False, loc_end=4,
                         excerpt_text="hello world")
    kind, loc, loc_end, exc = store.segment_row(sid)
    assert kind == "chunk" and loc == 3 and loc_end == 4
    rows = store.search_fts('"hello"', limit=5)
    assert rows and rows[0].segment_id == sid


def test_counts_grid(store):
    a = store.upsert_file(FileRec(path="/k/a.txt", kind="doc", size=3, state="pending"))
    store.mark_file(a, state="done", capability="content_indexed", detail=None,
                    info=None, extractor_version="txt-stdlib.v1")
    store.replace_segments(a, [SegmentRec(kind="chunk", modality="text",
                                          text_for_fts="abc", vec=_orthogonal_vec(3))])
    b = store.upsert_file(FileRec(path="/k/b.png", kind="image", size=9, state="pending"))
    store.mark_file(b, state="done", capability="metadata_only", detail="no image backend",
                    info=None, extractor_version=None)
    c = store.upsert_file(FileRec(path="/k/c.pem", kind="doc", size=1, state="pending"))
    store.mark_file(c, state="done", capability="unsupported", detail="key or password file",
                    info=None, extractor_version=None)
    counts = store.counts()
    assert counts.files == 3
    assert counts.vectors == 1
    assert counts.totals["content_indexed"] == 1
    assert counts.totals["metadata_only"] == 1
    assert counts.totals["unsupported"] == 1
    assert counts.by_kind["doc"]["content_indexed"] == 1
    assert counts.by_kind["image"]["metadata_only"] == 1
    assert store.reasons("metadata_only") == [("no image backend", 1)]


def test_pending_and_failures(store):
    a = store.upsert_file(FileRec(path="/p/a.txt", kind="doc", size=1, state="pending"))
    del a
    b = store.upsert_file(FileRec(path="/p/b.txt", kind="doc", size=1, state="pending"))
    store.mark_file(b, state="failed", capability="failed", detail="timeout after 60s",
                    info=None, extractor_version=None)
    assert [r.path for r in store.pending()] == ["/p/a.txt"]
    fails = store.failures()
    assert len(fails) == 1 and fails[0].detail == "timeout after 60s"


def _v2_card(run_id: str, view_id: str, sids: list[int], *, title="alpha"):
    """A minimal v2 Card-shaped object for save_run (duck-typed)."""
    import numpy as np

    from goldmine.util import pack_fp16

    class Card:
        pass

    card = Card()
    card.run_id = run_id
    card.view_id = view_id
    card.title = title
    card.title_kind = "terms"
    card.terms_json = '["alpha","beta"]'
    card.excerpts_json = "[]"
    card.size = len(sids)
    card.n_files = 2
    card.status = "active"
    card.parent = None
    card.centroid = np.zeros(768, np.float32)
    card.fingerprint_id = "abc123"
    card.distill_algo = "leader-avglink.v2 t=0.72 min=3"
    card.created_at = 0.0
    card.chunk_weights = [(sid, 1.0) for sid in sids]
    return card


def _v2_run(run_id: str, view_id: str, mode: str = "full") -> dict:
    return {
        "id": run_id,
        "algo": "leader-avglink.v2 t=0.72 min=3",
        "tau": 0.72,
        "tau_source": "percentile",
        "scope_json": "{}",
        "corpus_hash": "cafe" * 16,
        "mode": mode,
        "stats": {"view_id": view_id, "run_id": run_id},
        "view_id": view_id,
    }


def test_cards_roundtrip_v2_save_run(store):
    """v2 (R6 §3.4): save_run persists runs + cards + chunks in ONE
    transaction; per-view replace; F3 repeat-identical runs collapse."""
    fid = store.upsert_file(FileRec(path="/c/doc.md", kind="doc", size=1, state="pending"))
    sid1 = store.fts_text(fid, "chunk", 1, "one", vec_ok=False)
    sid2 = store.fts_text(fid, "chunk", 2, "two", vec_ok=False)

    view_a = "viewaaaa"
    store.save_run(_v2_run("run-1", view_a), [_v2_card("run-1", view_a, [sid1, sid2])])
    rows = store.cards()
    assert len(rows) == 1
    assert rows[0].size == 2 and rows[0].fingerprint_id == "abc123"
    assert rows[0].view_id == view_a and rows[0].run_id == "run-1"
    assert rows[0].title == "alpha" and rows[0].status == "active"
    weights = store.db.execute("SELECT chunk, weight FROM card_chunks").fetchall()
    assert len(weights) == 2
    runs = store.db.execute("SELECT COUNT(*) FROM distill_runs").fetchone()[0]
    assert runs == 1

    # F3: repeat identical run (same id) updates in place — no IntegrityError
    store.save_run(_v2_run("run-1", view_a), [_v2_card("run-1", view_a, [sid1])])
    assert store.db.execute("SELECT COUNT(*) FROM distill_runs").fetchone()[0] == 1
    assert store.cards()[0].size == 1

    # per-view replace: another view's cards survive untouched
    view_b = "viewbbbb"
    store.save_run(_v2_run("run-2", view_b), [_v2_card("run-2", view_b, [sid2])])
    active_views = {
        r[0] for r in store.db.execute(
            "SELECT view_id FROM cards WHERE status = 'active'"
        ).fetchall()
    }
    assert active_views == {view_a, view_b}   # two live views coexist
    assert len(store.cards()) == 2
    assert {r.run_id for r in store.cards()} == {"run-1", "run-2"}

    # replacing view a with nothing clears just view a
    store.save_run(_v2_run("run-3", view_a), [])
    assert store.db.execute(
        "SELECT COUNT(*) FROM cards WHERE view_id = ?", (view_a,)
    ).fetchone()[0] == 0
    assert store.cards()[0].view_id == view_b


def test_load_view_cards_and_latest_run(store):
    import numpy as np

    fid = store.upsert_file(FileRec(path="/c/doc.md", kind="doc", size=1, state="pending"))
    sid1 = store.fts_text(fid, "chunk", 1, "one", vec_ok=False)
    view = "viewcccc"
    card = _v2_card("run-9", view, [sid1])
    card.centroid = np.full(768, 0.25, np.float32)
    from goldmine.util import pack_fp16

    card.centroid = card.centroid / np.linalg.norm(card.centroid)
    store.save_run(_v2_run("run-9", view), [card])
    loaded = store.load_view_cards(view)
    assert len(loaded) == 1
    assert loaded[0].members == [sid1]
    assert loaded[0].centroid is not None
    assert np.allclose(loaded[0].centroid, card.centroid, atol=1e-2)  # fp16 roundtrip
    run = store.latest_run(view)
    assert run is not None and run["id"] == "run-9"
    assert run["stats"]["view_id"] == view
    assert store.latest_run("no-such-view") is None
    assert store.view_member_chunk_ids(view) == {sid1}


def test_distill_views_upsert_get(store):
    assert store.get_view("notes") is None
    store.upsert_view("notes", '{"under":["/n"]}', '{"threshold":null,"min_members":3}')
    view = store.get_view("notes")
    assert view.scope_json == '{"under":["/n"]}'
    store.upsert_view("notes", '{"under":[]}', '{"threshold":0.8,"min_members":5}')
    again = store.get_view("notes")
    assert again.params_json == '{"threshold":0.8,"min_members":5}'
    assert again.created_at >= view.created_at


# ----------------------------------------- VectorTable cache + factory (03 F2/F6)


def test_load_vectors_cache_returns_identical_table(store):
    fid = store.upsert_file(FileRec(path="/a/v.txt", kind="doc", size=1, state="pending"))
    store.replace_segments(
        fid,
        [
            SegmentRec(kind="chunk", modality="text", text_for_fts="one", vec=_orthogonal_vec(0)),
            SegmentRec(kind="chunk", modality="image", text_for_fts="two", vec=_orthogonal_vec(1)),
        ],
    )
    cold = store.load_vectors()
    warm = store.load_vectors()
    assert warm is cold  # same instance: no rebuild between searches
    fresh = Store(store.path)
    try:
        reopened = fresh.load_vectors()
        assert reopened.ids == cold.ids
        assert reopened.file_ids == cold.file_ids
        assert reopened.modalities == cold.modalities
        assert np.array_equal(reopened.matrix, cold.matrix)
    finally:
        fresh.close()


def test_load_vectors_cache_invalidates_on_every_segments_write(store):
    a = store.upsert_file(FileRec(path="/a.txt", kind="doc", size=1, state="pending"))
    b = store.upsert_file(FileRec(path="/b.txt", kind="doc", size=1, state="pending"))
    store.replace_segments(
        a, [SegmentRec(kind="chunk", modality="text", text_for_fts="a", vec=_orthogonal_vec(0))]
    )
    t1 = store.load_vectors()
    assert len(t1) == 1

    # insert via fts_text (the pipeline's per-segment path)
    store.fts_text(
        b, "chunk", 1, "b", vec_ok=True, vec=_orthogonal_vec(1), modality="text"
    )
    t2 = store.load_vectors()
    assert len(t2) == 2 and t2.file_ids == [a, b]

    # delete the HIGHEST vector segment and insert a fresh one for another
    # file: count and max id both return to t1's values, yet the data differs.
    # The generation counter (not count/max-id) is what invalidates here.
    store.delete_content_segments(b)
    t3 = store.load_vectors()
    assert len(t3) == 1 and t3.ids == t1.ids
    store.fts_text(
        b, "chunk", 2, "b2", vec_ok=True, vec=_orthogonal_vec(2), modality="text"
    )
    t4 = store.load_vectors()
    assert len(t4) == 2 and t4.matrix[1][2] == pytest.approx(1.0, abs=1e-3)

    # whole-file deletion invalidates too (delete_file -> _delete_segments_and_fts)
    store.delete_file(b)
    assert len(store.load_vectors()) == 1


def test_search_results_identical_cached_vs_fresh_store(tmp_path):
    """03 F2 equivalence contract: a Store whose vector cache is warm must
    return byte-identical hits to a fresh Store on the same database."""
    from dataclasses import asdict

    from goldmine.backends.mock import MockBackend
    from goldmine.config import load as load_cfg
    from goldmine.ingest import IngestPipeline
    from goldmine.search import Searcher

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "k8s.txt").write_text(
        "The scheduler evicts a pod when the node is under pressure.\n"
        "Tolerations keep the workload running through the drain.\n",
        encoding="utf-8",
    )
    (root / "sourdough.md").write_text(
        "# Sourdough\n\nFeed the starter daily, then bulk ferment overnight.\n",
        encoding="utf-8",
    )
    backend = MockBackend(dim=768)
    cfg = load_cfg()
    db = tmp_path / "eq.sqlite"
    store = Store(db)
    try:
        IngestPipeline(cfg, store, backend).run([str(root)])
    finally:
        store.close()

    def run_all(s):
        searcher = Searcher(s, backend)
        return [
            [asdict(h) for h in searcher.search(q)]
            for q in ("pod evict pressure", "sourdough starter ferment", "scheduler")
        ]

    warm = Store(db)
    try:
        warm.load_vectors()  # prime the cache
        warm_hits = run_all(warm)
        # second pass on the SAME store: every search now rides the cache
        assert run_all(warm) == warm_hits
    finally:
        warm.close()
    fresh = Store(db)
    try:
        assert run_all(fresh) == warm_hits
    finally:
        fresh.close()


def test_connect_index_factory_pragmas(tmp_path):
    db = tmp_path / "p.sqlite"
    con = connect_index(db)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 60000
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        # derived-index durability pairing (03 F6): WAL + NORMAL
        assert con.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    finally:
        con.close()
    # Store rides the same factory
    s = Store(db)
    try:
        assert s.db.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        s.close()


def test_connect_index_ro_never_creates_or_writes(tmp_path):
    missing = tmp_path / "nope.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        connect_index(missing, ro=True)  # mode=ro: no file created
    assert not missing.exists()
    s = Store(tmp_path / "real.sqlite")
    s.close()
    con = connect_index(tmp_path / "real.sqlite", ro=True)
    try:
        assert con.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO meta(key, value) VALUES('x', 'y')")
    finally:
        con.close()


def test_store_v3_ddl_carries_the_partial_indexes(store):
    indexes = {
        r[0]
        for r in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    assert {"segments_vec_file", "files_kind_cap"} <= indexes
    # and the vec-file status subquery actually uses it
    plan = store.db.execute(
        "EXPLAIN QUERY PLAN SELECT DISTINCT file FROM segments WHERE vec IS NOT NULL"
    ).fetchall()
    assert any("segments_vec_file" in r[3] for r in plan)

def test_upsert_file_updates_sha256_on_conflict(tmp_home):
    """Debt fix: ON CONFLICT previously dropped sha256, so changed files
    re-committed on EVERY plain ingest (stale-sha loop)."""
    store = Store(tmp_home / "i.sqlite")
    f1 = tmp_home / "doc.txt"
    f1.write_text("v1", encoding="utf-8")
    rec = FileRec(path=str(f1), source="file", kind="doc", size=2,
                  mtime=1.0, sha256="aaa", state="pending")
    fid = store.upsert_file(rec)
    rec2 = FileRec(path=str(f1), source="file", kind="doc", size=6,
                   mtime=2.0, sha256="bbb", state="done",
                   capability="content_indexed")
    fid2 = store.upsert_file(rec2)
    assert fid2 == fid
    row = store.db.execute("SELECT sha256 FROM files WHERE id=?", (fid,)).fetchone()
    assert row[0] == "bbb"
