"""A2 (digup-p2-closeout-001): query-time deleted-file filter, root-scoped
(DigUp Engine.swift:749-760). A row whose file vanished between ingest runs
must not answer a query; URL rows are exempt; the filter runs BEFORE limit
truncation so the pack budget is spent on live files only. Stores without a
stored roots_json (the offline synthetic fixtures) keep today's behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from goldmine.search import Searcher
from goldmine.store import FileRec, SegmentRec, Store

from test_search_offline import FixedBackend, add_file, evec


@pytest.fixture
def rooted_store(tmp_path):
    """A REAL tree: roots_json points at tmp_path, files exist on disk."""
    tree = tmp_path / "tree"
    tree.mkdir()
    store = Store(tmp_path / "s.sqlite")
    files: list[Path] = []
    for i in range(6):
        p = tree / f"doc{i}.txt"
        p.write_text(f"needle unique sentence {i}", encoding="utf-8")
        files.append(p)
    return store, tree, files


def _index(store: Store, paths: list[Path], vecs: list[np.ndarray]):
    for p, v in zip(paths, vecs):
        add_file(store, str(p), [f"needle unique sentence {p.name}"], [v])
    store.set_meta("roots_json", json.dumps([str(paths[0].parent.parent)]))


def _add_url_file(store: Store, url: str, text: str, vec: np.ndarray) -> None:
    fid = store.upsert_file(FileRec(path=url, kind="webpage", size=10, state="pending",
                                    source="url"))
    store.mark_file(fid, state="done", capability="content_indexed", detail=None,
                    info=None, extractor_version="url-http.v1")
    segs = [SegmentRec(kind="name", modality="none", excerpt=url.split("/")[-1],
                       text_for_fts=f"page words {url}"),
            SegmentRec(kind="chunk", modality="text", loc=1, loc_end=1, excerpt=text,
                       text_for_fts=text, vec=vec)]
    store.replace_segments(fid, segs)


def test_deleted_file_hit_filtered_and_counted(rooted_store, tmp_path):
    store, tree, files = rooted_store
    # doc0 is the STRONGEST match (e0); the rest are fillers (e1)
    _index(store, files, [evec(0)] + [evec(1)] * (len(files) - 1))
    searcher = Searcher(store, FixedBackend(evec(0)))

    files[0].unlink()  # the strongest match just vanished from disk

    stats: dict = {}
    hits = searcher.search("needle", limit=5, stats=stats)
    assert [h.path for h in hits] == [str(f) for f in files[1:6]][:5]
    fid = store.db.execute("SELECT id FROM files WHERE path = ?",
                           (str(files[0]),)).fetchone()[0]
    assert stats["_deleted_ids"] == {fid}


def test_filter_before_limit_keeps_budget_whole(rooted_store):
    """limit=1 with the TOP match deleted must still return one LIVE hit —
    the deleted row cannot consume a budget slot."""
    store, tree, files = rooted_store
    _index(store, files, [evec(0)] + [evec(1)] * (len(files) - 1))
    searcher = Searcher(store, FixedBackend(evec(0)))
    files[0].unlink()
    hits = searcher.search("needle", limit=1)
    assert len(hits) == 1
    assert hits[0].path != str(files[0])


def test_url_rows_exempt_from_the_filter(rooted_store):
    """URL rows never touch the disk check (same semantics as
    compute_deletions) — a fetched page always answers."""
    store, tree, files = rooted_store
    _index(store, files, [evec(0)] + [evec(1)] * (len(files) - 1))
    _add_url_file(store, "https://example.com/gone", "needle unique url page", evec(0))
    searcher = Searcher(store, FixedBackend(evec(0)))
    files[0].unlink()  # the url row's path does not exist on disk anyway
    hits = searcher.search("needle", limit=10)
    assert "https://example.com/gone" in [h.path for h in hits]
    assert str(files[0]) not in [h.path for h in hits]


def test_only_match_deleted_is_an_honest_zero(rooted_store):
    store, tree, files = rooted_store
    _index(store, files, [evec(0)] + [evec(1)] * (len(files) - 1))
    searcher = Searcher(store, FixedBackend(evec(0)))
    for f in files:
        f.unlink()
    assert searcher.search("needle", limit=5) == []


def test_store_without_roots_json_is_untouched(tmp_path):
    """Synthetic offline fixtures (no roots_json, paths never on disk) keep
    today's behavior — the reviewer's blocking case (A2 amendment)."""
    store = Store(tmp_path / "s.sqlite")
    add_file(store, "/nonexistent/neutral-name.txt", ["needle target"], [evec(0)])
    for i in range(25):
        add_file(store, f"/fill/f{i}.txt", ["filler"], [evec(1)])
    searcher = Searcher(store, FixedBackend(evec(0)))
    hits = searcher.search("needle", limit=5)
    assert [h.path for h in hits] == ["/nonexistent/neutral-name.txt"]


def test_path_outside_stored_roots_is_untouched(rooted_store, tmp_path):
    """A row under a DIFFERENT prefix than the stored roots: not this index's
    walk territory — the existence filter does not apply."""
    store, tree, files = rooted_store
    _index(store, files, [evec(0)] + [evec(1)] * (len(files) - 1))
    add_file(store, "/elsewhere/other-root.txt", ["needle elsewhere"], [evec(0)])
    searcher = Searcher(store, FixedBackend(evec(0)))
    files[0].unlink()
    hits = searcher.search("needle", limit=10)
    assert "/elsewhere/other-root.txt" in [h.path for h in hits]
