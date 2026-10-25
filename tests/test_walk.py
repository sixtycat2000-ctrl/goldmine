"""Tier-2: walker — determinism, exclusions, symlink policy, hidden-file policy."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from goldmine.ingest import walk as walk_mod
from goldmine.ingest.walk import (
    TCC_REASON,
    SkipNotice,
    compute_deletions,
    detect_moves,
    walk_files,
)
from goldmine.store import FileRec, Store

walk = walk_files  # the walker entry point (renamed from `walk`, 02 F12)


def test_walk_module_not_shadowed_by_the_function():
    """02 F12: `from goldmine.ingest import walk` used to yield the FUNCTION
    (the package re-export shadowed the module) — any module-attribute use
    crashed with 'function' object has no attribute. The function is now
    `walk_files`, and the package attribute is the module again."""
    import types

    import goldmine.ingest as pkg

    assert isinstance(walk_mod, types.ModuleType)
    assert pkg.walk is walk_mod
    assert callable(walk_mod.walk_files)
    assert "walk" not in pkg.__all__ and "walk_files" in pkg.__all__


def test_walk_sorted_deterministic(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    (root / "b").mkdir(parents=True)
    (root / "a").mkdir()
    for name in ("z.txt", "a.txt", "m.txt"):
        (root / "b" / name).write_text("x", encoding="utf-8")
    (root / "a" / "top.txt").write_text("x", encoding="utf-8")

    def run():
        return [
            (i.path.name if hasattr(i, "path") else i.path)
            for i in walk([root], code=False, exclude=[], max_file_bytes=1 << 20)
            if hasattr(i, "kind")
        ]

    first = run()
    for _ in range(3):
        assert run() == first
    assert first == ["top.txt", "a.txt", "m.txt", "z.txt"]


def test_walk_skips_folders_with_reason(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "x.js").write_text("1", encoding="utf-8")
    (root / "keep.txt").write_text("x", encoding="utf-8")
    items = list(walk([root], code=False, exclude=[], max_file_bytes=1 << 20))
    dirs = [i for i in items if isinstance(i, SkipNotice) and i.is_dir]
    files = [i for i in items if hasattr(i, "kind")]
    assert len(dirs) == 1 and dirs[0].reason == "developer folder"
    assert [f.path.name for f in files] == ["keep.txt"]  # never descended


def test_walk_skips_build_artifact_folders(tmp_path):
    """A5 (digup-p2-closeout-001, DigUp Rules.swift:100-104): build/target/
    dist/out are visible dir names skipped with a reason; .next is a hidden
    dir, so the hidden-dir branch preempts folder_skip — skipped either way
    (belt-and-braces membership documented in rules.SKIP_FOLDERS)."""
    root = tmp_path / "w"
    root.mkdir()
    for name in ("build", "target", "dist", "out"):
        (root / name).mkdir()
        (root / name / "artifact.txt").write_text("x", encoding="utf-8")
    hidden = root / ".next"
    hidden.mkdir()
    (hidden / "cache.txt").write_text("x", encoding="utf-8")
    (root / "keep.txt").write_text("x", encoding="utf-8")
    items = list(walk([root], code=False, exclude=[], max_file_bytes=1 << 20))
    dir_notices = {i.path: i.reason for i in items
                   if isinstance(i, SkipNotice) and i.is_dir}
    files = [i.path.name for i in items if hasattr(i, "kind")]
    for name in ("build", "target", "dist", "out"):
        assert dir_notices[str(root / name)] == "developer folder", name
    assert str(hidden) not in dir_notices  # hidden branch: silent, same outcome
    assert files == ["keep.txt"]


def test_walk_hidden_files_classified_hidden_dirs_skipped(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    (root / ".hiddendir").mkdir()
    (root / ".hiddendir" / "x.txt").write_text("x", encoding="utf-8")
    items = list(walk([root], code=False, exclude=[], max_file_bytes=1 << 20))
    notices = [i for i in items if isinstance(i, SkipNotice)]
    assert any(n.path.endswith(".env") and n.reason == "key or password file" for n in notices)
    assert not any("x.txt" in getattr(i, "path", "") for i in items)


def test_walk_size_cap(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    big = root / "big.txt"
    big.write_bytes(b"x" * 100)
    (root / "small.txt").write_text("ok", encoding="utf-8")
    items = list(walk([root], code=False, exclude=[], max_file_bytes=50))
    notices = [i for i in items if isinstance(i, SkipNotice)]
    assert any("size cap" in n.reason and n.path.endswith("big.txt") for n in notices)
    assert any(getattr(i, "kind", None) == "doc" for i in items)


def test_walk_symlink_policy(tmp_path):
    root = tmp_path / "w"
    (root / "real").mkdir(parents=True)
    (root / "real" / "note.txt").write_text("real", encoding="utf-8")
    os.symlink(root / "real", root / "loop")  # dir symlink: never followed
    os.symlink(root / "real" / "note.txt", root / "alias.txt")  # file symlink follows
    items = list(walk([root], code=False, exclude=[], max_file_bytes=1 << 20))
    files = [i for i in items if hasattr(i, "kind")]
    names = sorted(f.path.name for f in files)
    assert names == ["alias.txt", "note.txt"]  # alias.txt indexed once, no descent loop


def test_walk_exclude_globs(tmp_path):
    root = tmp_path / "w"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "skip-me.txt").write_text("x", encoding="utf-8")
    (root / "sub" / "keep.txt").write_text("x", encoding="utf-8")
    (root / "skip-me.txt").write_text("x", encoding="utf-8")
    items = list(walk([root], code=False, exclude=["skip-me.txt", "*/skip-me.txt"], max_file_bytes=1 << 20))
    files = [i for i in items if hasattr(i, "kind")]
    assert [f.path.name for f in files] == ["keep.txt"]
    notices = [i for i in items if isinstance(i, SkipNotice) and not i.is_dir]
    assert len(notices) == 2  # both matched paths reported as excluded


def test_compute_deletions(tmp_path):
    """P1-2 contract: only files under the CURRENT roots that vanished from
    disk are pruned. Files under other roots (and URL rows) are never touched
    by a run that was not given their roots."""
    kept = tmp_path / "r"
    kept.mkdir()
    (kept / "a.txt").write_text("x", encoding="utf-8")
    store = Store(tmp_path / "s.sqlite")
    store.upsert_file(FileRec(path=str(kept / "a.txt"), kind="doc", size=1, state="pending"))
    store.upsert_file(FileRec(path=str(kept / "gone.txt"), kind="doc", size=1,
                              state="pending"))  # under root but gone from disk
    other_root = tmp_path / "other-root"
    other_root.mkdir()
    store.upsert_file(FileRec(path=str(other_root / "elsewhere.txt"), kind="doc",
                              size=1, state="pending"))  # outside current roots: kept
    store.upsert_file(FileRec(path="https://x.dev/p", source="url", kind="webpage",
                              size=1, state="pending"))
    store.upsert_file(FileRec(path="https://other.dev/p", source="url", kind="webpage",
                              size=1, state="pending"))
    gone = compute_deletions(store, [str(kept), "https://x.dev"]).gone
    assert [store.get_file(fid).path for fid in gone] == [
        str(kept / "gone.txt")
    ]
    store.close()


def test_compute_deletions_keeps_rows_under_vanished_root(tmp_path):
    """A1-18 (DigUp Indexer.swift:118-120): a root that vanished between the
    run's start-of-run stat and the deletion computation is UNAVAILABLE, not
    empty — its rows are kept and the root is reported."""
    import shutil

    r = tmp_path / "root"
    r.mkdir()
    (r / "a.txt").write_text("x", encoding="utf-8")
    store = Store(tmp_path / "s.sqlite")
    store.upsert_file(FileRec(path=str(r / "a.txt"), kind="doc", size=1, state="pending"))
    vanished = tmp_path / "gone-root"
    vanished.mkdir()  # passed F10 at run start ...
    (vanished / "b.txt").write_text("x", encoding="utf-8")
    store.upsert_file(FileRec(path=str(vanished / "b.txt"), kind="doc",
                              size=1, state="pending"))
    shutil.rmtree(vanished)  # ... then the drive left mid-run
    result = compute_deletions(store, [str(r), str(vanished)])
    assert result.gone == []  # without the guard, b.txt read as deleted
    assert result.kept_unreachable == 1
    assert [root for root, _ in result.unavailable_roots] == [str(vanished)]
    store.close()


def test_compute_deletions_keeps_rows_under_unreadable_subtree(tmp_path):
    """A1-18: a subtree the walk could not read is an unreachable prefix —
    rows under it are kept while sibling deletions still run. The prefix
    match is separator-true (/sub never covers /subway)."""
    r = tmp_path / "root"
    (r / "sub").mkdir(parents=True)
    (r / "sub" / "keep.txt").write_text("x", encoding="utf-8")
    store = Store(tmp_path / "s.sqlite")
    store.upsert_file(FileRec(path=str(r / "sub" / "keep.txt"), kind="doc",
                              size=1, state="pending"))
    store.upsert_file(FileRec(path=str(r / "deleted.txt"), kind="doc",
                              size=1, state="pending"))
    store.upsert_file(FileRec(path=str(r / "subway.txt"), kind="doc",
                              size=1, state="pending"))  # not under /sub
    result = compute_deletions(store, [str(r)], unreachable=[str(r / "sub")])
    assert sorted(store.get_file(fid).path for fid in result.gone) == [
        str(r / "deleted.txt"), str(r / "subway.txt"),
    ]
    assert result.kept_unreachable == 1
    store.close()


# ------------------------------- v2 item 21 (F8): TCC / unreadable-root detection


def test_walk_root_stat_permission_error_is_tcc_notice(tmp_path, monkeypatch):
    """A root the OS refuses to stat is macOS privacy (TCC), never 'empty':
    the walk yields a folder-level SkipNotice with the F8 reason."""
    import errno

    root = tmp_path / "locked"
    root.mkdir()
    (root / "a.txt").write_text("x", encoding="utf-8")

    real_stat = Path.stat

    def denied_stat(self, *args, **kwargs):
        if self.name == "locked":
            raise PermissionError(errno.EACCES, "not allowed", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    items = list(walk([root], code=False, exclude=[], max_file_bytes=1 << 20))
    notices = [i for i in items if isinstance(i, SkipNotice)]
    assert len(notices) == 1
    assert notices[0].is_dir is True
    assert notices[0].reason == TCC_REASON
    assert not any(hasattr(i, "kind") for i in items)  # nothing indexed silently


def test_walk_subdir_scandir_permission_error_is_tcc_notice(tmp_path, monkeypatch):
    """An unreadable SUBdirectory is surfaced with the same reason; readable
    siblings are still indexed (DigUp main.swift:122-126 lesson)."""
    import errno
    import os as os_mod

    root = tmp_path / "w"
    locked = root / "locked"
    locked.mkdir(parents=True)
    (locked / "x.txt").write_text("x", encoding="utf-8")
    (root / "ok.txt").write_text("x", encoding="utf-8")

    real_scandir = os_mod.scandir

    def fake_scandir(path):
        if str(path).endswith("locked"):
            raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os_mod, "scandir", fake_scandir)
    items = list(walk([root], code=False, exclude=[], max_file_bytes=1 << 20))
    tcc = [i for i in items if isinstance(i, SkipNotice) and i.reason == TCC_REASON]
    assert len(tcc) == 1 and tcc[0].path.endswith("locked") and tcc[0].is_dir
    assert tcc[0].unreachable is True  # A1-18: rows under it stay deletable-exempt
    files = [i.path.name for i in items if hasattr(i, "kind")]
    assert files == ["ok.txt"]


def test_walk_subdir_scandir_oserror_is_unreachable_dir_notice(tmp_path, monkeypatch):
    """A1-18: a directory that cannot be LISTED for a non-permission reason
    (drive ejected mid-walk, IO error) yields an unreachable dir notice —
    previously it was a silent log line and the subtree read as empty."""
    import os as os_mod

    root = tmp_path / "w"
    flaky = root / "flaky"
    flaky.mkdir(parents=True)
    (flaky / "x.txt").write_text("x", encoding="utf-8")
    (root / "ok.txt").write_text("x", encoding="utf-8")

    real_scandir = os_mod.scandir

    def fake_scandir(path):
        if str(path).endswith("flaky"):
            raise OSError("Input/output error")
        return real_scandir(path)

    monkeypatch.setattr(os_mod, "scandir", fake_scandir)
    items = list(walk([root], code=False, exclude=[], max_file_bytes=1 << 20))
    notices = [i for i in items if isinstance(i, SkipNotice) and i.is_dir]
    assert len(notices) == 1
    assert notices[0].path.endswith("flaky")
    assert "cannot list" in notices[0].reason
    assert notices[0].unreachable is True
    assert [i.path.name for i in items if hasattr(i, "kind")] == ["ok.txt"]


# Original FIFO regressions required by the acceptance inventory.

def test_walk_fifo_with_text_ext_is_skipped_not_read(tmp_path):
    """A named pipe named *.txt classifies as a doc; the old walker yielded a
    Candidate and extract_txt's read_bytes() BLOCKED FOREVER on the writerless
    pipe — one hung worker stalled the whole ingest. The walker now gates on
    S_ISREG and skips specials with a reason (02-ingestion F2)."""
    if os.name != "posix":
        pytest.skip("os.mkfifo is POSIX-only")
    root = tmp_path / "w"
    root.mkdir()
    os.mkfifo(root / "pipe_notes.txt")
    (root / "real.txt").write_text("readable", encoding="utf-8")
    items = list(walk([root], code=False, exclude=[], max_file_bytes=1 << 20))
    files = [i for i in items if hasattr(i, "kind")]
    notices = [i for i in items if isinstance(i, SkipNotice) and not i.is_dir]
    assert [f.path.name for f in files] == ["real.txt"]
    assert len(notices) == 1
    assert notices[0].path.endswith("pipe_notes.txt")
    assert notices[0].reason == "not a regular file"


def test_walk_fifo_as_root_is_skipped(tmp_path):
    """Same gate for a root that is itself a FIFO (`_handle_file`)."""
    if os.name != "posix":
        pytest.skip("os.mkfifo is POSIX-only")
    fifo = tmp_path / "top.txt"
    os.mkfifo(fifo)
    items = list(walk([fifo], code=False, exclude=[], max_file_bytes=1 << 20))
    notices = [i for i in items if isinstance(i, SkipNotice) and not i.is_dir]
    assert len(notices) == 1 and notices[0].reason == "not a regular file"
    assert not any(hasattr(i, "kind") for i in items)


def test_walk_end_to_end_ingest_survives_a_fifo(tmp_path):
    """Full pipeline: the FIFO is recorded unsupported with the machine reason
    and the run COMPLETES (the regression was a whole-run hang, confirmed by
    the critique's >5s subprocess probe)."""
    from goldmine.backends.mock import MockBackend
    from goldmine.config import load
    from goldmine.ingest import IngestPipeline
    from goldmine.store import Store as _Store

    if os.name != "posix":
        pytest.skip("os.mkfifo is POSIX-only")
    root = tmp_path / "w"
    root.mkdir()
    os.mkfifo(root / "pipe_notes.txt")
    (root / "note.txt").write_text("normal words here", encoding="utf-8")
    cfg = load()
    cfg.home = tmp_path / "home"
    store = _Store(cfg.home / "idx.sqlite")
    try:
        with MockBackend(dim=768) as backend:
            report = IngestPipeline(cfg, store, backend).run([str(root)])
        assert report.failed == 0
        assert report.content_indexed == 1
        assert report.unsupported == 1
        row = store.db.execute(
            "SELECT capability, detail FROM files WHERE path LIKE '%pipe_notes.txt'"
        ).fetchone()
        assert row == ("unsupported", "not a regular file")
    finally:
        store.close()

