"""Tier-2: media probes — ffprobe duration, ffmpeg windowing, silence skip.

Uses the real ffmpeg/ffprobe when installed (they are, via brew) and skips with
the doctor-style hint when not. WAV fixtures are generated with the stdlib
`wave` module — no binary blobs.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from goldmine.ingest import media

pytestmark = pytest.mark.skipif(
    media.ffmpeg_path() is None or media.ffprobe_path() is None,
    reason="ffmpeg/ffprobe not installed (brew install ffmpeg)",
)

RATE = 16000


def write_wav(path: Path, seconds: float, *, loud_first: float = 0.0) -> Path:
    """16 kHz mono PCM16 WAV; `loud_first` seconds of sine at the head, then
    digital silence (exercises the RMS gate)."""
    total = int(RATE * seconds)
    loud = int(RATE * loud_first)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        frames = bytearray()
        for i in range(loud):
            frames += struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / RATE)))
        frames += b"\x00\x00" * (total - loud)
        wav.writeframes(bytes(frames))
    return path


def test_sniff_media_no_tools_needed(tmp_path):
    assert media.sniff_media(tmp_path / "x.mp3") == "audio"
    assert media.sniff_media(tmp_path / "x.mov") == "video"
    assert media.sniff_media(tmp_path / "x.png") == "image"
    assert media.sniff_media(tmp_path / "x.txt") is None
    assert media.sniff_media(tmp_path / "raw", head=b"RIFF....WAVE") == "audio"


def test_ffprobe_duration(tmp_path):
    path = write_wav(tmp_path / "one.wav", 1.0, loud_first=1.0)
    duration = media.ffprobe_duration(path)
    assert duration == pytest.approx(1.0, abs=0.05)


def test_ffprobe_duration_garbage_is_none(tmp_path):
    path = tmp_path / "garbage.wav"
    path.write_bytes(b"not audio at all")
    assert media.ffprobe_duration(path) is None


def test_sub_window_file_gated_as_a_whole(tmp_path):
    """B1 (digup-p2-closeout-001): a file shorter than one window is gated as
    a whole (DigUp MediaExtractors.swift:76 offset==0 -> alreadyCovered 0) —
    a 1.0 s clip carries < 2 s of audio, so it yields NO window. The v2-era
    behavior (a lone 1 s window) is exactly what the tail gate removed."""
    path = write_wav(tmp_path / "short.wav", 1.0, loud_first=1.0)
    pieces = media.audio_windows(path, tmp_path / "windows", max_minutes=120)
    assert pieces == []
    # 2.5 s carries enough signal for exactly one window (format kept honest)
    kept = write_wav(tmp_path / "kept.wav", 2.5, loud_first=2.5)
    pieces = media.audio_windows(kept, tmp_path / "windows2", max_minutes=120)
    assert [(p.start, p.end) for p in pieces] == [(0.0, pytest.approx(2.5, abs=0.05))]
    with wave.open(str(pieces[0].path), "rb") as wav:
        assert wav.getframerate() == RATE
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2


def test_tail_gate_boundary_math(tmp_path):
    """The final partial window survives only with >= 2.0 s of NEW audio
    past the previous window's coverage (overlap = window - hop = 5 s):
      56.9 s -> tail [50, 56.9] has 1.9 s new -> dropped
      57.0 s -> exactly 2.0 s new -> kept (inclusive)
      62.0 s -> 7 s new -> kept (the anti-literal-example pin: that window is
      12 s long — a length-based '< 2 s' rule would never touch it)
    """
    for seconds, expects_tail in ((56.9, False), (57.0, True), (62.0, True)):
        path = write_wav(tmp_path / f"a{seconds}.wav", seconds, loud_first=seconds)
        pieces = media.audio_windows(path, tmp_path / f"w{seconds}")
        spans = [(p.start, round(p.end, 1)) for p in pieces]
        if expects_tail:
            assert spans[-1][0] == 50.0, (seconds, spans)
        else:
            assert spans == [(0.0, 30.0), (25.0, 55.0)], (seconds, spans)


def test_silent_tail_window_does_not_pop_the_previous_one(tmp_path):
    """The gate touches the final window only when THAT window survived the
    RMS gate — a silent 56.9 s tail must not pop the earlier full windows
    (digup-p2-closeout-001 review D5 counterexample)."""
    path = write_wav(tmp_path / "sil_tail.wav", 56.9, loud_first=50.0)
    pieces = media.audio_windows(path, tmp_path / "windows")
    assert [(p.start, p.end) for p in pieces] == [(0.0, 30.0), (25.0, 55.0)]


def test_windows_hop_and_silence_skip(tmp_path):
    # 62 s: loud only for the first 2 s -> windows [0,30] [25,55] [50,62],
    # only the first carries signal and survives the RMS gate
    path = write_wav(tmp_path / "long.wav", 62.0, loud_first=2.0)
    pieces = media.audio_windows(path, tmp_path / "windows")
    spans = [(p.start, p.end) for p in pieces]
    assert spans == [(0.0, 30.0)]


def test_all_silent_yields_no_windows(tmp_path):
    path = write_wav(tmp_path / "silent.wav", 31.0)
    pieces = media.audio_windows(path, tmp_path / "windows")
    assert pieces == []


def test_duration_cap_bounds_windows(tmp_path):
    path = write_wav(tmp_path / "long.wav", 62.0, loud_first=62.0)
    pieces = media.audio_windows(path, tmp_path / "windows", max_minutes=0.5)  # 30 s
    assert [(p.start, p.end) for p in pieces] == [(0.0, 30.0)]


def test_no_silence_skip_keeps_all_windows(tmp_path):
    path = write_wav(tmp_path / "long.wav", 62.0, loud_first=0.0)
    pieces = media.audio_windows(
        path, tmp_path / "windows", skip_silent=False, max_minutes=120
    )
    assert [(p.start, p.end) for p in pieces] == [(0.0, 30.0), (25.0, 55.0), (50.0, 62.0)]


def test_video_soundtrack_still_uses_audio_path(tmp_path):
    """v4: the soundtrack path is UNCHANGED by keyframes — a scored video
    still yields the same raw windows (video-frames-001 froze this). The
    keyframe engine itself is covered by tests/test_video_frames.py."""
    video = tmp_path / "clip.mp4"
    from goldmine.util import run_tool

    result = run_tool(
        [media.ffmpeg_path(), "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:s=64x64:d=2.5",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2.5",
         "-shortest", "-y", str(video)],
        timeout_s=60.0,
    )
    if result.rc != 0:
        pytest.skip(f"lavfi source unavailable: {result.stderr.strip()[:120]}")
    duration = media.ffprobe_duration(video)
    assert duration == pytest.approx(2.5, abs=0.2)
    pieces = media.audio_windows(video, tmp_path / "windows")
    assert len(pieces) == 1


def test_has_audio_stream(tmp_path):
    """Soundtrack probe used by the pipeline: video without audio -> False
    (honest metadata-only, never a failed row); wav and scored video -> True."""
    from goldmine.util import run_tool

    wav = write_wav(tmp_path / "tone.wav", 0.5, loud_first=0.5)
    assert media.has_audio_stream(wav) is True

    def make_video(name, *inputs):
        video = tmp_path / name
        result = run_tool(
            [media.ffmpeg_path(), "-hide_banner", "-loglevel", "error",
             *inputs, "-shortest", "-y", str(video)],
            timeout_s=60.0,
        )
        if result.rc != 0:
            pytest.skip(f"lavfi source unavailable: {result.stderr.strip()[:120]}")
        return video

    color = ["-f", "lavfi", "-i", "color=c=black:s=64x64:d=1"]
    assert media.has_audio_stream(make_video("mute.mp4", *color)) is False
    scored = make_video(
        "scored.mp4", *color,
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
    )
    assert media.has_audio_stream(scored) is True
