"""v2 item 19 (F3): output polish — ingest ticker, per-root media pre-flight,
`--format fzf` serialization, ANSI/NO_COLOR policy, and the zsh widget file."""

from __future__ import annotations

import io
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from goldmine.config import Config
from goldmine.output import (
    ASR_REALTIME_FACTOR,
    IngestTicker,
    RootMedia,
    asr_estimate_enabled,
    fzf_lines,
    fmt_clock,
    fmt_hours,
    render_root_media,
    scan_root_media,
)
from goldmine.util import ansi_enabled


# ---------------------------------------------------------------- formatting


def test_fmt_clock():
    assert fmt_clock(0) == "0:00"
    assert fmt_clock(44) == "0:44"
    assert fmt_clock(2661) == "44:21"
    assert fmt_clock(3661) == "1:01:01"


def test_fmt_hours():
    assert fmt_hours(11520.0) == "3.2h"
    assert fmt_hours(1620.0) == "27m"
    assert fmt_hours(40.0) == "40s"
    assert fmt_hours(0) == "0s"


# ---------------------------------------------------------------- ANSI policy


class _TTY(io.StringIO):
    def isatty(self):
        return True


class _NotTTY(io.StringIO):
    def isatty(self):
        return False


def test_ansi_enabled_matrix(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert ansi_enabled(stream=_TTY()) is True          # TTY, no env, no flag
    assert ansi_enabled(stream=_NotTTY()) is False      # piped: off
    assert ansi_enabled(no_color_flag=True, stream=_TTY()) is False
    monkeypatch.setenv("NO_COLOR", "")                  # ANY value (no-color.org)
    assert ansi_enabled(stream=_TTY()) is False
    monkeypatch.setenv("NO_COLOR", "1")
    assert ansi_enabled(stream=_TTY()) is False


# ------------------------------------------------------------------- ticker


def test_ticker_draws_throttled_single_line():
    stream = io.StringIO()
    t = IngestTicker(120, stream=stream, ansi=True, interval=2.0, clock=lambda: 1000.0)
    now = [1000.0]

    def tick(done, at):
        t.clock = lambda: at
        t.update(f"ingest: {done}/120 files, 0 failed")

    tick(20, 1001.0)               # first draw always happens
    t.clock = lambda: 1001.5
    t.update("ingest: 30/120 files, 0 failed")   # inside the 2 s window: skipped
    tick(42, 1044.0)               # 44 s in: draws, eta from file rate
    tick(120, 1088.0)              # final: always drawn
    t.finish()
    out = stream.getvalue()
    assert out.count("\x1b[K") == 3                # three draws, carriage-returned
    assert out.endswith("\n")
    assert "[ingest 42/120 files" in out
    assert "eta 1:22" in out                       # 44 s / 42 * 78 remaining
    assert "[ingest 120/120 files" in out
    assert "\r\x1b[K[ingest 120" in out            # redraw replaces, no newline spam
    assert out.count("\n") == 1                    # single line until finish


def test_ticker_non_ansi_pads_instead_of_escape():
    stream = io.StringIO()
    t = IngestTicker(10, stream=stream, ansi=False, interval=0.0, clock=lambda: 0.0)
    t.clock = lambda: 5.0
    t.update("ingest: 3/10 files, 0 failed")
    t.clock = lambda: 9.0
    t.update("ingest: 10/10 files, 1 failed")
    t.finish()
    out = stream.getvalue()
    assert "\x1b" not in out                       # NO ALL ANSI when disabled
    assert out.endswith("\n")
    assert "1 failed" in out


def test_ticker_reports_live_vectors():
    class FakeStore:
        def counts(self):
            class C:
                vectors = 1203

            return C()

    stream = io.StringIO()
    t = IngestTicker(100, store=FakeStore(), stream=stream, ansi=True,
                     interval=0.0, clock=lambda: 0.0)
    t.update("ingest: 10/100 files, 0 failed")
    assert "vectors 1,203" in stream.getvalue()


def test_ticker_ignores_unparseable_progress():
    stream = io.StringIO()
    t = IngestTicker(10, stream=stream, ansi=True, interval=0.0, clock=lambda: 0.0)
    t.update("something else entirely")
    assert stream.getvalue() == "" and not t.drawn


# ------------------------------------------------- per-root media pre-flight


@pytest.fixture
def media_tree(tmp_path):
    root = tmp_path / "media-root"
    root.mkdir()
    for i in range(3):
        (root / f"note{i}.txt").write_text("words\n", encoding="utf-8")
    for name in ("a.png", "b.jpg"):
        (root / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "song.mp3").write_bytes(b"ID3")
    (root / "clip.mov").write_bytes(b"\x00" * 64)
    (root / "big.log").write_text("x" * 300, encoding="utf-8")
    return root


def test_scan_root_media_counts(media_tree):
    durations = {  # injectable ffprobe
        media_tree / "song.mp3": 3600.0,
        media_tree / "clip.mov": 1800.0,
    }
    medias = scan_root_media(
        [str(media_tree)],
        max_file_mb=200,
        duration_fn=lambda p: durations.get(p),
    )
    (rm,) = medias
    assert rm.files == 7                # 3 txt + 2 images + mp3 + mov (log skipped)
    assert rm.images == 2
    assert rm.audio == 1 and rm.video == 1
    assert rm.media_seconds == 5400.0   # 1 h + 30 min
    assert not rm.probe_capped


def test_scan_root_media_cap_and_cap_s(media_tree):
    durations = {media_tree / "song.mp3": 1e9, media_tree / "clip.mov": 10.0}
    medias = scan_root_media(
        [str(media_tree)],
        duration_fn=lambda p: durations.get(p),
        max_probes=2,          # cap: both media files probed here
        audio_cap_s=120.0,     # per-file ASR cap mirrored in the estimate
    )
    (rm,) = medias
    # walk order is sorted, so clip.mov probes first — the cap only shows
    # past the probed count; the clamp is what this asserts (1e9 -> 120 s)
    assert rm.media_seconds == 130.0
    medias = scan_root_media(
        [str(media_tree)],
        duration_fn=lambda p: durations.get(p),
        max_probes=1,          # now the cap bites: only clip.mov is probed
        audio_cap_s=120.0,
    )
    (rm,) = medias
    assert rm.probe_capped and rm.probed == 1
    assert rm.media_seconds == 10.0


def test_scan_root_media_no_probe_leaves_seconds_none(media_tree):
    medias = scan_root_media(
        [str(media_tree)], probe_durations=False, duration_fn=lambda p: 1.0
    )
    (rm,) = medias
    assert rm.media_seconds is None
    assert rm.audio == 1 and rm.video == 1


def test_scan_root_media_skips_urls():
    assert scan_root_media(["https://example.com"], duration_fn=lambda p: None) == []


def test_render_root_media_c5_line():
    rm = RootMedia(root="~/X", files=412, audio=18, images=26, media_seconds=11520.0)
    line = render_root_media([rm], asr=False)
    assert line == "root ~/X: 412 files (18 audio ≈ 3.2h, 26 images)…"
    line = render_root_media([rm], asr=True)
    # 11520 s / 7.2 (measured ASR factor) = 1600 s ≈ 27m
    assert "asr ≈ 27m" in line


def test_render_root_media_video_only_and_probe_cap_note():
    rm = RootMedia(root="~/Y", files=9, video=3, media_seconds=1500.0,
                   probe_capped=True, probed=500)
    line = render_root_media([rm], asr=True)
    assert "root ~/Y: 9 files (3 video ≈ 25m → asr ≈ 3m)" in line
    assert "duration probed for 500 media files" in line
    assert line.endswith("…")


def test_render_root_media_no_media_counts():
    rm = RootMedia(root="~/Z", files=5)
    assert render_root_media([rm]) == "root ~/Z: 5 files…"


def test_asr_estimate_disabled_when_off():
    cfg = Config()
    cfg.ingest.asr = "off"
    assert asr_estimate_enabled(cfg) is False


def test_asr_estimate_disabled_when_whisper_missing(monkeypatch):
    import goldmine.ingest.asr as asr_mod

    monkeypatch.setattr(asr_mod, "resolve_tools", lambda cfg: None)
    assert asr_estimate_enabled(Config()) is False


def test_asr_factor_is_the_measured_constant():
    assert ASR_REALTIME_FACTOR == 7.2  # 02 §2: 21.6 min in 3:01 wall


# ------------------------------------------------------------- fzf serializer


class Hit:
    def __init__(self, path, kind, loc, loc_end, excerpt, score):
        self.path = path
        self.kind = kind
        self.loc = loc
        self.loc_end = loc_end
        self.excerpt = excerpt
        self.score = score


def test_fzf_lines_columns_and_escaping():
    hits = [
        Hit("/x/a\tb.png", "image", None, None, "line1\nline2\ttab", 0.9142),
        Hit("/x/keep.png", "image", 1.0, 2.0, "  spaced  ", 0.5),
    ]
    rows = fzf_lines(hits, loc_fn=lambda k, l, e: "L1" if l is not None else "")
    assert len(rows) == 2
    f0 = rows[0].split("\t")
    assert f0 == ["/x/a b.png", "image", "", "line1 line2 tab", "0.9142"]
    f1 = rows[1].split("\t")
    assert f1[2] == "L1" and f1[4] == "0.5000"


def test_fzf_lines_empty_excerpt_and_missing_loc_fn():
    rows = fzf_lines([Hit("/x/a.md", "doc", 3, None, None, 0.1)])
    assert rows[0].split("\t")[:3] == ["/x/a.md", "doc", ""]


# ------------------------------------------------------------- shell widget


def test_fzf_widget_file_exists_and_parses():
    widget = Path(__file__).resolve().parents[1] / "scripts" / "shell" / "goldmine.fzf.zsh"
    assert widget.is_file()
    src = widget.read_text()
    assert "goldmine search" in src and "--format fzf" in src
    assert "goldmine open" in src
    assert "brew install fzf" in src  # graceful message when fzf is absent
    if shutil.which("zsh") is None:
        pytest.skip("zsh not installed")
    proc = subprocess.run(["zsh", "-n", str(widget)], capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()
