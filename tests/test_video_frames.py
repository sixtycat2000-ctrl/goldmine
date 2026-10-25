"""Tier-2: video keyframes (video-frames-001).

Pure units (grid / aHash / dedup+fixup) run with ZERO tools installed; the
engine tests use the deterministic fixture video (scripts/gen_video_fixture.py,
sha256-pinned) behind the usual ffmpeg skip marker.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest

from goldmine.ingest.media import (
    Keyframe,
    _ahash,
    _dedup_keyframes,
    _frame_grid,
    video_keyframes,
)

FIXTURE = Path(__file__).with_name("fixtures_data") / "media" / "ref-video.mp4"
FIXTURE_SHA256 = "32baa650f7331c0ffe74e783eef1c4988be10f7c132c5893165609d2017c112c"

needs_ffmpeg = pytest.mark.skipif(
    __import__("shutil").which("ffmpeg") is None
    or __import__("shutil").which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed (brew install ffmpeg)",
)


# ------------------------------------------------------------- pure units ---

def test_ahash_flat_image_is_zero():
    """Every pixel equals the integer mean: none is STRICTLY greater — a
    uniform frame of ANY color hashes to 0 (mean uses integer division)."""
    assert _ahash(bytes([137] * 64)) == 0
    assert _ahash(bytes([0] * 64)) == 0
    assert _ahash(bytes([255] * 64)) == 0


def test_ahash_known_bit_pattern():
    # mean = (32*200 + 32*10)//64 = (6400+320)//64 = 105: the bright half is
    # above, the dark half at/below -> exactly the low 32 bits set
    assert _ahash(bytes([200] * 32 + [10] * 32)) == (1 << 32) - 1
    # one bright pixel among 64: mean = (63*10+200)//64 = 13 -> bit 0 set only
    h = _ahash(bytes([200] + [10] * 63))
    assert h == 1


def test_ahash_rejects_wrong_size():
    with pytest.raises(ValueError):
        _ahash(b"\x00" * 63)


def test_grid_basic_shapes():
    assert _frame_grid(2.0) == (0.5, 3.0)      # min(0.5, d/2) + 3 s floor
    assert _frame_grid(0.8) == (0.4, 3.0)      # sub-second: start = d/2
    assert _frame_grid(20.0) == (0.5, 3.0)


def test_grid_step_adapts_beyond_max_frames():
    # 5000 s: duration/1200 = 4.1667 > 3 -> step grows, count == max_frames
    start, step = _frame_grid(5000.0)
    assert step > 3.0
    assert math.ceil((5000.0 - start) / step) == 1200
    # exactly at the adaptation boundary the floor still binds
    start, step = _frame_grid(3600.0)
    assert step == 3.0
    assert math.ceil((3600.0 - start) / step) == 1200


def test_grid_rejects_unusable_durations():
    assert _frame_grid(0.0) is None
    assert _frame_grid(-3.0) is None
    assert _frame_grid(math.inf) is None
    assert _frame_grid(math.nan) is None


def test_grid_count_never_exceeds_max_frames():
    for duration in (0.5, 2.0, 20.0, 3599.9, 3600.0, 3601.0, 5000.0,
                     7200.0, 43200.0, 86400.0):
        start, step = _frame_grid(duration)
        assert start < duration
        assert step >= 3.0
        assert math.ceil((duration - start) / step) <= 1200, duration


def _sample(loc: float, tag: str, gray: bytes, tmp: Path):
    p = tmp / f"f-{tag}.jpg"
    p.write_bytes(b"jpeg")
    return (loc, p, gray)


FLAT_A = bytes([80] * 64)          # flat frames of any color hash to 0
TEXTURED_1 = bytes([200] * 32 + [10] * 32)   # low-32 bits
TEXTURED_2 = bytes([10] * 32 + [200] * 32)   # high-32 bits (distance 32)


def test_dedup_static_frames_merge_into_previous(tmp_path):
    samples = [
        _sample(0.5, "a0", FLAT_A, tmp_path),
        _sample(3.5, "a1", FLAT_A, tmp_path),     # duplicate -> JPEG deleted
        _sample(6.5, "a2", FLAT_A, tmp_path),     # duplicate
    ]
    kept = _dedup_keyframes(samples, 20.0)
    assert len(kept) == 1
    assert kept[0].loc == 0.5 and kept[0].loc_end == 20.0  # last = duration
    assert not (tmp_path / "f-a1.jpg").exists()            # deduped JPEG gone
    assert (tmp_path / "f-a0.jpg").exists()                # kept JPEG stays


def test_dedup_scene_cuts_kept_and_fixup_loop(tmp_path):
    samples = [
        _sample(0.5, "s0", TEXTURED_1, tmp_path),
        _sample(3.5, "s1", TEXTURED_1, tmp_path),   # dup within scene 1
        _sample(6.5, "s2", TEXTURED_2, tmp_path),   # cut -> kept
        _sample(9.5, "s3", TEXTURED_2, tmp_path),   # dup within scene 2
    ]
    kept = _dedup_keyframes(samples, 12.0)
    assert [k.loc for k in kept] == [0.5, 6.5]
    # the fixup loop: each kept frame ends where the next starts; last = 12.0
    assert [(k.loc, k.loc_end) for k in kept] == [(0.5, 6.5), (6.5, 12.0)]


def test_dedup_hamming_boundary_four_bits(tmp_path):
    # hamming 4 vs the last kept frame -> duplicate; 5 -> kept
    base = TEXTURED_1
    near = bytearray(base)
    for i in range(4):                    # flip 4 low bits: distance 4
        near[i] = 10 if near[i] == 200 else 200
    far = bytearray(base)
    for i in range(5):                    # flip 5: distance 5
        far[i] = 10 if far[i] == 200 else 200
    samples = [
        _sample(0.5, "b0", base, tmp_path),
        _sample(3.5, "b1", bytes(near), tmp_path),   # 4 bits -> merged
        _sample(6.5, "b2", bytes(far), tmp_path),    # 5 bits -> kept
    ]
    kept = _dedup_keyframes(samples, 10.0)
    assert [k.loc for k in kept] == [0.5, 6.5]


def test_dedup_compares_against_last_kept_not_last_sample(tmp_path):
    """DigUp semantics: the reference is the LAST KEPT frame (the hash only
    updates on keep) — a slow drift stays merged while each step is ≤4 bits
    from its predecessor, but once the accumulated distance from the KEPT
    anchor exceeds 4 the frame is kept. Comparing against the last SAMPLE
    would merge d2 as well (3 bits from d1); against the anchor it is 6."""
    drift0 = TEXTURED_1
    drift1 = bytearray(drift0)
    for i in range(3):
        drift1[i] = 10 if drift1[i] == 200 else 200   # 3 bits from drift0
    drift2 = bytearray(drift1)
    for i in range(3, 6):
        drift2[i] = 10 if drift2[i] == 200 else 200   # 3 from d1, 6 from drift0
    samples = [
        _sample(0.5, "d0", drift0, tmp_path),
        _sample(3.5, "d1", bytes(drift1), tmp_path),  # 3 vs kept -> merged
        _sample(6.5, "d2", bytes(drift2), tmp_path),  # 6 vs kept -> kept
    ]
    kept = _dedup_keyframes(samples, 10.0)
    assert [k.loc for k in kept] == [0.5, 6.5]
    assert kept[0].loc_end == 6.5 and kept[1].loc_end == 10.0


def test_dedup_empty_samples(tmp_path):
    assert _dedup_keyframes([], 10.0) == []


# ------------------------------------------------------------ fixture sha ---

def test_fixture_video_sha_pinned():
    """The checked-in fixture IS the ground truth: a drifted regeneration
    (different ffmpeg build/settings) can never slip in unnoticed."""
    assert FIXTURE.is_file(), "run scripts/gen_video_fixture.py --write"
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256


# ------------------------------------------------------- engine (ffmpeg) ---

@needs_ffmpeg
def test_keyframes_fixture_scenes(tmp_path):
    """The 4-scene fixture: one kept frame per scene, within-scene duplicates
    merged, spans follow the fixup loop, last span = duration."""
    kf = video_keyframes(FIXTURE, tmp_path)
    assert kf.step == 3.0
    assert 19.9 < kf.duration < 20.5
    kept = [(k.loc, k.loc_end) for k in kf.frames]
    assert len(kept) == 4
    assert kept[0][0] == 0.5 and kept[1][0] == 6.5
    assert kept[2][0] == 12.5 and kept[3][0] == 15.5
    assert kept[0][1] == kept[1][0] and kept[2][1] == kept[3][0]
    assert kept[3][1] == pytest.approx(kf.duration, abs=0.01)
    # only the KEPT frames' JPEGs remain in the scratch dir
    leftovers = sorted(p.name for p in tmp_path.glob("kf-*"))
    assert len(leftovers) == 4, leftovers


@needs_ffmpeg
def test_keyframes_jpeg_never_upscaled(tmp_path):
    """The 320x180 fixture is below the 1024 budget: frames keep their size
    (sips confirms 320x180 after extraction)."""
    import subprocess

    kf = video_keyframes(FIXTURE, tmp_path)
    out = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(kf.frames[0].path)],
        capture_output=True, timeout=30,
    )
    text = out.stdout.decode()
    assert "pixelWidth: 320" in text and "pixelHeight: 180" in text


@needs_ffmpeg
def test_keyframes_deterministic_across_runs(tmp_path):
    a = video_keyframes(FIXTURE, tmp_path / "a")
    b = video_keyframes(FIXTURE, tmp_path / "b")
    assert [(k.loc, k.loc_end) for k in a.frames] == \
           [(k.loc, k.loc_end) for k in b.frames]
    assert [k.path.read_bytes() for k in a.frames] == \
           [k.path.read_bytes() for k in b.frames]


@needs_ffmpeg
def test_keyframes_subsecond_video_one_frame(tmp_path):
    import subprocess

    video = tmp_path / "tiny.mp4"
    subprocess.run(
        [__import__("shutil").which("ffmpeg"), "-hide_banner", "-loglevel",
         "error", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=0.8",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-y", str(video)],
        capture_output=True, timeout=60, check=True,
    )
    kf = video_keyframes(video, tmp_path / "out")
    assert len(kf.frames) == 1
    assert kf.frames[0].loc == pytest.approx(0.4, abs=0.01)   # min(0.5, d/2)
    assert kf.frames[0].loc_end == pytest.approx(kf.duration, abs=0.05)


@needs_ffmpeg
def test_keyframes_static_video_collapses_to_one(tmp_path):
    import subprocess

    video = tmp_path / "static.mp4"
    subprocess.run(
        [__import__("shutil").which("ffmpeg"), "-hide_banner", "-loglevel",
         "error", "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=11",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-y", str(video)],
        capture_output=True, timeout=60, check=True,
    )
    kf = video_keyframes(video, tmp_path / "out")
    assert len(kf.frames) == 1
    assert kf.frames[0].loc_end == pytest.approx(kf.duration, abs=0.05)


@needs_ffmpeg
def test_keyframes_garbage_raises_honestly(tmp_path):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\xde\xad\xbe\xef" * 64)
    with pytest.raises(RuntimeError):
        video_keyframes(bad, tmp_path / "out")


@needs_ffmpeg
def test_keyframes_no_video_stream_raises(tmp_path):
    """Audio-only data in a video container: the map fails — the extraction
    layer pre-checks has_video_stream, the media contract is an honest error."""
    import subprocess

    video = tmp_path / "audio-only.mp4"
    subprocess.run(
        [__import__("shutil").which("ffmpeg"), "-hide_banner", "-loglevel",
         "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-c:a", "aac", "-y", str(video)],
        capture_output=True, timeout=60, check=True,
    )
    with pytest.raises(RuntimeError):
        video_keyframes(video, tmp_path / "out")
