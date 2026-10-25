"""A1 (digup-p2-closeout-001): iCloud dataless placeholders are skipped BEFORE
anything opens them — an open IS the download (DigUp Crawler.swift:85-91).

A real SF_DATALESS file needs an iCloud-evicted file (Finder → Remove
Download); CI cannot create one, so these are mechanism tests over a
monkeypatched os.lstat (the walk's documented no-side-effect probe). The
real-path verification recipe lives in the execution SUMMARY and docs.
"""

from __future__ import annotations

import os
import stat as stat_mod
from pathlib import Path

from goldmine.ingest.walk import (
    DATALESS_REASON,
    SF_DATALESS,
    SkipNotice,
    _is_dataless,
    walk_files,
)


class _FakeStat:
    def __init__(self, flags: int):
        self.st_flags = flags


def _flagged(real_lstat, path: str, flags: int):
    """A real stat_result clone with the flags swapped — resolve()/Path
    internals read st_mode etc. off lstat results too."""
    from types import SimpleNamespace

    st = real_lstat(path)
    fields = {n: getattr(st, n) for n in dir(st) if n.startswith("st_")}
    fields["st_flags"] = flags
    return SimpleNamespace(**fields)


def test_sf_dataless_constant():
    # the fallback value is the Darwin sys/stat.h one (0x40000000)
    assert SF_DATALESS == 0x40000000


def test_is_dataless_by_stat_flags_and_lstat(monkeypatch, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    # followed-stat short-circuit
    assert _is_dataless(str(f), _FakeStat(0x40000000)) is True
    assert _is_dataless(str(f), _FakeStat(0)) is False
    # lstat probe path (the mechanism-test seam)
    real_lstat = os.lstat
    monkeypatch.setattr(
        os, "lstat", lambda p: _FakeStat(0x40000000) if p == str(f) else real_lstat(p)
    )
    assert _is_dataless(str(f), _FakeStat(0)) is True
    # missing st_flags attribute (non-macOS shape) is not dataless
    assert _is_dataless(str(f), None) is True  # falls through to lstat


def test_walk_dir_skips_dataless_before_any_open(monkeypatch, tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    # a TS file: _decide_file sniffs its head by OPENING it — the interception
    # must fire before that, or this test explodes
    evicted = root / "clip.ts"
    evicted.write_bytes(b"\x47" * 64)
    neighbor = root / "keep.txt"
    neighbor.write_text("x", encoding="utf-8")

    real_lstat = os.lstat
    monkeypatch.setattr(
        os, "lstat",
        lambda p: _flagged(real_lstat, p, 0x40000000) if p == str(evicted) else real_lstat(p),
    )
    # any attempt to sniff/open the evicted file must explode — the
    # interception point is before _decide_file (whose TS head-sniff opens)
    def _no_open(path, n=600):
        raise AssertionError(f"walk opened a dataless file: {path}")

    monkeypatch.setattr("goldmine.ingest.walk._read_head", _no_open)

    items = list(walk_files([root], code=False, exclude=[], max_file_bytes=1 << 20))
    notices = [i for i in items if isinstance(i, SkipNotice)]
    files = [i for i in items if hasattr(i, "kind")]
    assert [n.reason for n in notices] == [DATALESS_REASON]
    assert notices[0].path == str(evicted) and not notices[0].is_dir
    assert [f.path.name for f in files] == ["keep.txt"]


def test_walk_root_file_dataless(monkeypatch, tmp_path):
    """A root that IS a file: _handle_file must refuse it before the TS sniff
    (watch delta roots re-walk single files through this path)."""
    evicted = tmp_path / "clip.ts"
    evicted.write_bytes(b"\x47" * 64)
    real_lstat = os.lstat
    monkeypatch.setattr(
        os, "lstat",
        lambda p: _flagged(real_lstat, p, 0x40000000) if p == str(evicted) else real_lstat(p)
    )

    def _no_open(path, n=600):
        raise AssertionError(f"_handle_file opened a dataless root: {path}")

    import goldmine.ingest.walk as walk_mod

    monkeypatch.setattr(walk_mod, "_read_head", _no_open)

    items = list(walk_files([evicted], code=False, exclude=[], max_file_bytes=1 << 20))
    assert len(items) == 1 and isinstance(items[0], SkipNotice)
    assert items[0].reason == DATALESS_REASON
