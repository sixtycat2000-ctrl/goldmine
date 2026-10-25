"""Media probes: ffprobe metadata + ffmpeg audio windows + video keyframes.

Audio windows are the raw-soundtrack path (only via media-capable backends);
keyframes (video-frames-001) are the video-visual path.
- audio: 16 kHz mono PCM16 WAV windows, 30 s window / 25 s hop (5 s overlap —
  DigUp audioWindow: the model ignores anything longer), silent windows
  (RMS < 0.003 ≈ −50 dBFS) skipped, duration capped by cfg.ingest.audio_max_minutes.
- video: ffprobe metadata + the soundtrack via the same audio path + keyframes
  (ffmpeg select sampling every ~3 s, 8×8 aHash dedup ≤4 bits, JPEG ≤1024).
- sniff_media works with NO tools installed so capability reporting stays exact.

Tool absence raises ToolError; callers record metadata_only with the reason.
"""

from __future__ import annotations

import logging
import math
import shutil
import struct
import subprocess
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

from ..util import ToolError
from .exif import METADATA_VERSION as EXIF_METADATA_VERSION

logger = logging.getLogger("goldmine.media")

AUDIO_EXTS = {"mp3", "m4a", "aac", "wav", "flac", "ogg", "oga", "opus", "aiff", "aif", "wma", "caf"}
VIDEO_EXTS = {"mp4", "mov", "avi", "mkv", "webm", "m4v", "mpg", "mpeg", "wmv", "flv"}

WINDOW_S = 30.0
HOP_S = 25.0
SILENCE_RMS = 0.003        # ≈ −50 dBFS (DigUp)
FFMPEG_TIMEOUT_S = 120.0    # per window batch (ARCHITECTURE §8)

#: DigUp MediaExtractors.swift:76 `minimum`: the final partial audio window
#: survives only when it carries at least this much NEW audio beyond the
#: previous window's coverage (windows overlap by WINDOW_S - HOP_S = 5 s).
TAIL_MIN_NEW_S = 2.0

#: One name for the media extractor family (fingerprints registry, audio and
#: video outcome composition share it — an out-of-sync literal means rows go
#: stale forever, the digup-p2-closeout-001 D1 review item).
MEDIA_EXTRACTOR_VERSION = "media-ffmpeg-mm.v3"

#: Recorded in the media-capable backends' fingerprints (the mock's
#: multimodal flag keeps its own "bytes.v1"): these windowing/gate parameters
#: define the bytes the model actually sees, so changing any of them must force
#: a reindex. Media vectors take NO prompt (D7: media unprompted) — this field
#: is the media analogue of the prompt-scheme entry.
#: v2 bump (ONE bump for items B4+B5, 02 §5.2/§5.4): images are sips
#: thumbnails (no more raw bytes / 20 MB cap), minSide 256 refusal, EXIF
#: taken; screenshots additionally get Vision OCR text — FTS-only where an
#: image vector exists (DigUp Indexer.swift:510-513), TEXT-vector under
#: text-only backends (A5: there is no image vector to carry the meaning).
#: v3 bump (digup-plan-002, multimodal helper landing): goldmine-side resize
#: to the token budget (bit-exact mtmd/DigUp bicubic, 48px patch grid),
#: per-kind budget constants below, thin-PDF pages rendered to page images,
#: glyph-order pages keep a meaning-only garbled-text vector; OCR text vectors
#: now exist ONLY under text-only backends (the capability flip closes them).
#: v4 bump (video-frames-001, A2 unfreeze): video keyframes land — one sample
#: every ~3 s (fps1/3 shorthand; engine = ffmpeg select gte(prev+step)
#: snapping to input frames, DigUp-shaped), 8x8 aHash dedup within 4 bits,
#: JPEG <=1024 no upscale, maxFrames 1200 with step adaptation.
#: v5 bump (digup-p2-closeout-001 B1): the final audio window is kept only
#: with >= 2 s of NEW audio past the previous window's coverage (DigUp
#: MediaExtractors.swift:67-76 port — short lived tails carried almost
#: nothing but overlap); media extractor family moves v2 -> v3 so every
#: media row re-extracts and re-embeds once (user-authorized, no installed
#: base).
MEDIA_PREPROCESSING = (
    "v5:audio=wav-16k-mono-pcm16,window30s,hop25s,rms-gate0.003,tail-min2s; "
    "image=jpeg-thumb-1024-q90|png-1536(screenshot),minSide256,"
    "resize=bicubic-a-0.5-48px-fill-budget; "
    "page=pdftoppm150dpi-thin+glyphorder; "
    "ocr_text_vector=text-only-backends; "
    "frames=fps1/3,ahash4bit,jpeg1024,max1200"
)

#: Frozen token budgets per media kind (digup-plan-002 preregistration;
#: DigUp Model.swift:84 granularity — the whole index shares one embed
#: identity, so any budget change re-embeds everything).
MEDIA_BUDGETS = {
    "photo": 140,
    "screenshot": 280,
    "pdf_page": 280,
    "video_frame": 70,
}
#: The legal helper budget set (DigUp LlamaEmbedder.budgets, swift:16).
LEGAL_BUDGETS = (70, 140, 280, 560, 1120)

#: MM-D3 routing rule recorded verbatim in fingerprints + per-file info.
MEDIA_ROUTING = "raw-audio-only-when-no-transcript.v1"

#: image / audio / video magic sniffing (extension + content head)
_MAGIC = (
    (b"RIFF", "audio"),      # wav (webp handled by util.sniff_image)
    (b"fLaC", "audio"),
    (b"OggS", "audio"),
    (b"ID3", "audio"),
    (b"ftyp", "video"),      # mp4/mov/m4a family (refined by brand below)
)


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def sniff_media(path: Path, head: bytes | None = None) -> str | None:
    """'image' | 'audio' | 'video' | None — runs with zero tools installed."""
    ext = path.suffix.lstrip(".").lower()
    from . import rules

    if ext in rules.IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    if head is None:
        try:
            with open(path, "rb") as f:
                head = f.read(16)
        except OSError:
            return None
    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return "audio"
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return "audio" if magic == b"RIFF" and head[8:12] in (b"WAVE",) else kind
    return None


def ffprobe_duration(path: Path) -> float | None:
    """Seconds, or None when ffprobe is missing/unreadable (-> metadata_only)."""
    tool = ffprobe_path()
    if tool is None:
        return None
    try:
        proc = subprocess.run(
            [
                tool, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True, timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.decode().strip())
    except ValueError:
        return None


def has_audio_stream(path: Path) -> bool | None:
    """True/False via ffprobe; None when ffprobe is missing or cannot decide.

    Used for video kind: a soundtrack-less clip is honestly metadata-only
    ("no audio stream"), never a failed row.
    """
    tool = ffprobe_path()
    if tool is None:
        return None
    try:
        proc = subprocess.run(
            [
                tool, "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=index", "-of", "csv=p=0", str(path),
            ],
            capture_output=True, timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return bool(proc.stdout.strip())


def _rms_of_wav(path: Path) -> float:
    """RMS of a PCM16 WAV normalized to [0, 1] — numpy-free (stdlib struct)."""
    with wave.open(str(path), "rb") as wav:
        frames = wav.readframes(wav.getnframes())
        width = wav.getsampwidth()
        count = len(frames) // max(1, width)
        if count == 0:
            return 0.0
        fmt = {1: "<%db", 2: "<%dh", 4: "<%di"}.get(width)
        if fmt is None:
            return 1.0  # unknown width: never call it silent
        samples = struct.unpack(fmt % count, frames[: count * width])
        peak = float(1 << (8 * width - 1))
        sq = sum(float(s) * float(s) for s in samples) / count
        return math.sqrt(sq) / peak


@dataclass
class MediaPiece:
    path: Path        # tmp WAV file (16 kHz mono PCM16)
    start: float      # seconds into the source
    end: float


def audio_windows(
    path: Path,
    tmp: Path,
    *,
    window: float = WINDOW_S,
    hop: float = HOP_S,
    max_minutes: float = 120.0,
    skip_silent: bool = True,
) -> list[MediaPiece]:
    """Extract 16 kHz mono WAV windows: `ffmpeg -i X -ac 1 -ar 16000 -f wav`.

    Bounded by `max_minutes` of SOURCE audio (caps honored by stopping the
    extraction, never by silent truncation of the list). Silent windows are
    skipped (they carry no embedding signal). Raises ToolError when ffmpeg is
    missing; RuntimeError on per-window failure/timeout.
    """
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        from ..util import ToolError

        raise ToolError("ffmpeg: not found")
    duration = ffprobe_duration(path)
    if duration is None:
        raise RuntimeError(f"ffprobe could not read duration of {path}")
    cap_seconds = max_minutes * 60.0
    effective = min(duration, cap_seconds)

    from ..util import run_tool

    pieces: list[MediaPiece] = []
    tmp.mkdir(parents=True, exist_ok=True)
    start = 0.0
    index = 0
    tail_appended = False     # did the FINAL cut window survive the RMS gate?
    tail_span: tuple[float, float] | None = None
    while start < effective:
        end = min(start + window, effective)
        out = tmp / f"window-{index:04d}.wav"
        result = run_tool(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
                "-i", str(path),
                "-ac", "1", "-ar", "16000", "-f", "wav",
                "-y", str(out),
            ],
            timeout_s=FFMPEG_TIMEOUT_S,
        )
        if result.timed_out:
            raise RuntimeError(f"ffmpeg timeout after {FFMPEG_TIMEOUT_S:g}s")
        if result.rc != 0:
            raise RuntimeError(
                f"ffmpeg failed rc={result.rc}: {result.stderr.strip()[:200]}"
            )
        if not skip_silent or _rms_of_wav(out) >= SILENCE_RMS:
            pieces.append(MediaPiece(path=out, start=round(start, 3), end=round(end, 3)))
        else:
            logger.debug("media: window %.1f-%.1fs silent, skipped", start, end)
            out.unlink(missing_ok=True)
        if start + window >= effective:
            tail_appended = len(pieces) > 0 and pieces[-1].path == out
            tail_span = (start, end)
            break
        start += hop
        index += 1
    # B1 tail gate (DigUp MediaExtractors.swift:67-76): the final partial
    # window survives only with >= TAIL_MIN_NEW_S of NEW audio past the
    # previous window's coverage. Geometry uses the loop's RAW values (piece
    # fields are rounded); a full-window file has no partial tail, and a
    # sub-window file is gated as a whole (DigUp offset == 0 -> no overlap).
    if tail_appended and tail_span is not None:
        tail_len = tail_span[1] - tail_span[0]
        new_audio = tail_len - (window - hop) if effective >= window else tail_len
        if new_audio < TAIL_MIN_NEW_S:
            logger.debug("media: tail window %.1f-%.1fs dropped (%.2fs new audio)",
                         tail_span[0], tail_span[1], new_audio)
            pieces[-1].path.unlink(missing_ok=True)
            pieces.pop()
    return pieces


def has_video_stream(path: Path) -> bool | None:
    """True/False via ffprobe; None when ffprobe is missing or cannot decide."""
    tool = ffprobe_path()
    if tool is None:
        return None
    try:
        proc = subprocess.run(
            [
                tool, "-v", "error", "-select_streams", "v",
                "-show_entries", "stream=index", "-of", "csv=p=0", str(path),
            ],
            capture_output=True, timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return bool(proc.stdout.strip())


# -------------------------------------------------- video keyframes (v4) ---
#
# DigUp VideoExtractor.keyframes (MediaExtractors.swift:101-157) with ffmpeg
# as the engine (video-frames-001 preregistration): sample one frame every
# `step` seconds, keep one per shot (8×8 average hash within 4 bits only
# extends the previous kept frame), each kept frame stands for the stretch
# until the next one. One ffmpeg call decodes once and writes two outputs —
# the JPEG sequence to embed and an 8×8 gray rawvideo stream for the hash —
# both chains driven by the SAME select expression so the pairing invariant
# (n_jpeg == n_gray) holds by construction and is still verified per run.
#
# Engine notes (measured, preregistration appendix): the fps=1/step filter
# DROPS the tail sample frame (12 s / step 3.5 → 3 frames, not 4), so the
# sampler is `select='isnan(prev_selected_t)+gte(t,prev_selected_t+step)'`
# — the first frame after the seek, then the first frame at/after
# prev+step, snapping to real input frames exactly like DigUp's
# ±step/2-tolerance generator. image2/rawvideo outputs duplicate frames to
# CFR unless `-fps_mode vfr` (4 selected frames became 139 files without it).

#: DigUp Model.swift:65 frameInterval — the sampling floor.
FRAME_INTERVAL_S = 3.0
#: DigUp MediaExtractors.swift:105 maxFrames — step adapts beyond this.
FRAME_MAX_COUNT = 1200
FRAME_TIMESTAMPS_VERSION = "source-pts.v1"
#: Sampled-frame JPEG long-side cap (Indexer.swift:410 maxPixel 1024).
FRAME_JPEG_MAX_SIDE = 1024


@dataclass
class Keyframe:
    path: Path              # kept frame's JPEG (deduped frames' JPEGs are discarded)
    loc: float              # actual source presentation timestamp
    loc_end: float = 0.0    # next kept frame's time; last kept frame = duration


@dataclass
class VideoKeyframes:
    frames: list[Keyframe]
    step: float
    duration: float


def _frame_grid(duration: float, max_frames: int = FRAME_MAX_COUNT) -> tuple[float, float] | None:
    """(start, step) for the sampling grid, or None when duration is unusable.

    DigUp semantics: start = min(0.5, duration/2) (so any duration > 0 has at
    least one sample), step = max(3.0, duration/max_frames) with the sampling
    points strictly below duration (Swift stride is half-open). The step is
    rounded UP to 6 decimals — plain rounding could nudge the point count past
    max_frames by float arithmetic, so bump by 1e-6 until it fits."""
    if not math.isfinite(duration) or duration <= 0:
        return None
    start = min(0.5, duration / 2.0)
    step = max(FRAME_INTERVAL_S, duration / float(max_frames))
    step = math.ceil(step * 1e6) / 1e6
    while math.ceil((duration - start) / step) > max_frames:
        step = math.ceil((step + 1e-6) * 1e6) / 1e6
    return start, step


def _ahash(gray64: bytes) -> int:
    """8×8 grayscale → 64-bit average hash (DigUp averageHash, swift:140-156):
    integer mean over the 64 bytes, bit i set iff byte i is STRICTLY greater.
    Pure stdlib bit ops — no numpy/Pillow dependency."""
    if len(gray64) != 64:
        raise ValueError(f"aHash needs exactly 64 bytes, got {len(gray64)}")
    mean = sum(gray64) // 64
    hash_ = 0
    for i, pixel in enumerate(gray64):
        if pixel > mean:
            hash_ |= 1 << i
    return hash_


def _hamming64(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _dedup_keyframes(
    samples: list[tuple[float, Path, bytes]], duration: float
) -> list[Keyframe]:
    """One pass of aHash dedup + the fixup loop (DigUp swift:124-136).

    `samples` = (actual source PTS, JPEG path, 64 gray bytes) in sampling
    order. A frame within 4 bits of the LAST KEPT frame is a near-duplicate:
    its JPEG is deleted and the kept frame's interval implicitly extends.
    Kept frame i ends where kept frame i+1 starts; the last ends at duration
    (the fixup loop — swift:136)."""
    kept: list[Keyframe] = []
    last_hash: int | None = None
    for loc, jpeg, gray in samples:
        hash_ = _ahash(gray)
        if last_hash is not None and _hamming64(hash_, last_hash) <= 4:
            jpeg.unlink(missing_ok=True)
            continue
        last_hash = hash_
        kept.append(Keyframe(path=jpeg, loc=round(loc, 3)))
    for i in range(len(kept) - 1):
        kept[i].loc_end = kept[i + 1].loc
    if kept:
        kept[-1].loc_end = round(duration, 3)
    return kept


def video_keyframes(
    path: Path, tmp: Path, *, max_frames: int = FRAME_MAX_COUNT
) -> VideoKeyframes:
    """Sample + dedup keyframes: JPEGs (≤1024 long side, never upscaled,
    rotation honored via ffmpeg's default autorotate) + their grid times.

    Raises ToolError when ffmpeg is missing; RuntimeError on decode failure,
    timeout, or a broken pairing/grid (honest extraction failures). A video
    with a readable duration but no decodable frames returns EMPTY frames
    (the caller's honest metadata path, never a failure)."""
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise ToolError("ffmpeg: not found")
    duration = ffprobe_duration(path)
    if duration is None:
        raise RuntimeError(f"ffprobe could not read duration of {path}")
    grid = _frame_grid(duration, max_frames)
    if grid is None:
        raise RuntimeError(f"unusable video duration {duration!r} for {path}")
    start, step = grid
    predicted = math.ceil((duration - start) / step)

    tmp.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:10]
    jpeg_pattern = tmp / f"kf-{token}-%06d.jpg"
    gray_path = tmp / f"kf-{token}.gray"
    # filtergraph-level quoting: the expression is single-quoted and the
    # commas inside gte()/min() are backslash-escaped (probe-verified form).
    select_ = (
        "select='isnan(prev_selected_t)"
        f"+gte(t\\,prev_selected_t+{step:.6f})'"
    )
    vf_jpeg = (
        f"{select_},showinfo,scale=min(iw\\,{FRAME_JPEG_MAX_SIDE}):"
        f"min(ih\\,{FRAME_JPEG_MAX_SIDE}):force_original_aspect_ratio=decrease"
    )
    vf_gray = f"{select_},scale=8:8,format=gray"

    from ..util import run_tool

    result = run_tool(
        [
            ffmpeg, "-hide_banner", "-loglevel", "info", "-nostats", "-y",
            "-ss", f"{start:.6f}", "-i", str(path),
            "-map", "0:v:0", "-vf", vf_jpeg,
            "-fps_mode", "vfr", "-q:v", "2", str(jpeg_pattern),
            "-map", "0:v:0", "-vf", vf_gray,
            "-fps_mode", "vfr", "-f", "rawvideo", str(gray_path),
        ],
        timeout_s=FFMPEG_TIMEOUT_S,
    )
    gray_bytes = gray_path.read_bytes() if gray_path.is_file() else b""
    jpegs = sorted(tmp.glob(f"kf-{token}-*.jpg"))
    if result.timed_out:
        for j in jpegs:
            j.unlink(missing_ok=True)
        gray_path.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg timeout after {FFMPEG_TIMEOUT_S:g}s")
    if result.rc != 0:
        for j in jpegs:
            j.unlink(missing_ok=True)
        gray_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"ffmpeg failed rc={result.rc}: {result.stderr.strip()[:200]}"
        )
    if len(jpegs) * 64 != len(gray_bytes):
        for j in jpegs:
            j.unlink(missing_ok=True)
        gray_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"keyframe stream mismatch: {len(jpegs)} JPEGs vs "
            f"{len(gray_bytes) // 64} gray frames for {path.name}"
        )
    n = len(jpegs)
    import re

    timestamps = [start + float(value) for value in re.findall(
        r"\[Parsed_showinfo_\d+[^\]]*\].*?\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:([-+\d.eE]+)",
        result.stderr,
    )]
    if (len(timestamps) != n or any(not math.isfinite(t) for t in timestamps)
            or any(a >= b for a, b in zip(timestamps, timestamps[1:]))):
        for jpeg in jpegs:
            jpeg.unlink(missing_ok=True)
        gray_path.unlink(missing_ok=True)
        raise RuntimeError("keyframe presentation timestamps missing or inconsistent")
    if n > predicted + 1:
        # the select engine follows the declared grid; exceeding it by more
        # than the one exact-boundary frame means the grid was violated
        for j in jpegs:
            j.unlink(missing_ok=True)
        gray_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"keyframe grid violation: {n} frames > {predicted}+1 predicted"
        )
    samples: list[tuple[float, Path, bytes]] = []
    for i, jpeg in enumerate(jpegs):
        loc = timestamps[i]
        if loc >= duration:
            break  # half-open grid: a boundary frame at exactly duration drops
        samples.append((loc, jpeg, gray_bytes[i * 64:(i + 1) * 64]))
    for jpeg in jpegs[len(samples):]:  # dropped tail JPEGs
        jpeg.unlink(missing_ok=True)
    gray_path.unlink(missing_ok=True)
    return VideoKeyframes(
        frames=_dedup_keyframes(samples, duration),
        step=step,
        duration=duration,
    )


# ----------------------------------------------- image preprocessing (v2 B5) --
#
# sips is the PIL replacement [measured, 02 §5.4]: /usr/bin/sips reads
# HEIC/HEIF, WebP, AVIF, JXL, TIFF, JPEG-2000, and RAW (DNG/CR2/CR3/NEF/ARW/
# ORF/RW2/SRW/PEF/RAF/3FR...). Every image goes through a bounded thumbnail
# before embed_image — killing v1's 20 MB raw-byte cap — with DigUp's budgets
# (Indexer.swift:371): photos 1024 px JPEG q90, screenshots 1536 px PNG (UI
# text stays crisp). EXIF creation date survives the round-trip [measured].

PHOTO_THUMB_MAX_SIDE = 1024       # px, DigUp photo budget
SCREENSHOT_THUMB_MAX_SIDE = 1536  # px, PNG keeps UI text crisp
IMAGE_THUMB_MIN_SIDE = 256        # DigUp minImageSide (Extractors.swift:43)
#: Byte cap AFTER thumbnail: a sane 1024 px JPEG is <= ~1.5 MB; this only
#: refuses pathological encodes (v1's 20 MB raw cap is gone with thumbnails).
IMAGE_THUMB_MAX_BYTES = 8 << 20
IMAGE_SIPS_TIMEOUT_S = 60.0

def sips_path() -> str | None:
    return shutil.which("sips")


@dataclass
class ImagePrep:
    """Outcome of preparing one image for embedding / metadata."""

    path: Path | None = None        # thumbnail file (None on refusal/no-op)
    width: int = 0                  # thumbnail pixel size
    height: int = 0
    taken: str | None = None        # EXIF creation date, ISO "YYYY-MM-DD HH:MM:SS"
    taken_epoch: float | None = None
    camera_make: str | None = None  # bounded IFD0 Make (2026-10-23 D4 revision)
    gps: str | None = None          # bounded GPS IFD, "dd.dddd H, dd.dddd H"
    refused: str | None = None      # machine-stable refusal reason, or None


def _run_sips(argv: list[str]):
    from ..util import run_tool

    tool = sips_path()
    if tool is None:
        raise ToolError("sips: not found")
    return run_tool([tool, *argv], timeout_s=IMAGE_SIPS_TIMEOUT_S)


def _sips_properties(path: Path, props: list[str]) -> dict[str, str]:
    """`sips -g k1 -g k2` -> {key: value}; '<nil>' values kept verbatim."""
    argv: list[str] = []
    for prop in props:
        argv += ["-g", prop]
    result = _run_sips([*argv, str(path)])
    if result.rc != 0 or result.timed_out:
        raise RuntimeError(
            f"sips failed rc={result.rc}: {result.stderr.strip()[:200]}"
        )
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key in props:
            out[key] = value.strip()
    return out


def image_dimensions(path: Path) -> tuple[int, int] | None:
    """(width, height) via sips -g pixelWidth/pixelHeight, None on failure."""
    try:
        props = _sips_properties(
            path, ["pixelWidth", "pixelHeight"]
        )
    except (ToolError, RuntimeError):
        return None
    try:
        return int(props["pixelWidth"]), int(props["pixelHeight"])
    except (KeyError, ValueError):
        return None


def parse_sips_creation(value: str | None) -> tuple[str, float] | None:
    """Legacy string parser only; sips creation does not establish capture time."""
    from .exif import parse_capture_time

    return parse_capture_time(value.strip() if isinstance(value, str) else value)


def exif_taken(path: Path) -> tuple[str, float] | None:
    """Actual DateTimeOriginal; JPEG uses bounded TIFF parsing, others ImageIO.

    IFD0 modified DateTime and sips creation never substitute for capture time.
    Explicit offsets affect epoch only; the ISO string stays camera wall clock.
    Missing zone retains the documented host-local epoch convention.
    """
    from .exif import taken

    return taken(path)


def exif_reader(path: Path) -> str | None:
    from .exif import reader

    return reader(path)


def exif_capture(path: Path):
    from .exif import capture

    return capture(path)


def thumbnail_image(
    path: Path, out: Path, *, max_side: int, png: bool = False
) -> Path:
    """sips thumbnail: `sips -s format jpeg -s formatOptions 90% -Z N in
    --out out` (PNG variant for screenshots — DigUp Indexer.swift:371-373).
    sips -Z also UPSCALES, so small images are converted at their own size
    (never resampled up); Raises ToolError (no sips) / RuntimeError (decode
    failure, rc != 0 — e.g. 'Error 13: Cannot extract image from file')."""
    argv = ["-s", "format", "png" if png else "jpeg"]
    if not png:
        argv += ["-s", "formatOptions", "90%"]
    resize = max_side
    source_dims = image_dimensions(path)
    if source_dims is not None and max(source_dims) <= max_side:
        resize = max(source_dims)  # at/below budget: format-convert only
    argv += ["-Z", str(resize), str(path), "--out", str(out)]
    result = _run_sips(argv)
    if result.timed_out:
        raise RuntimeError(f"sips timeout after {IMAGE_SIPS_TIMEOUT_S:g}s")
    if result.rc != 0 or not out.is_file():
        raise RuntimeError(
            f"sips could not decode {path.name}: "
            f"{result.stderr.strip()[:200] or result.stdout.strip()[:200]}"
        )
    return out


def prepare_image(path: Path, tmp: Path, *, screenshot: bool = False, metadata=None) -> ImagePrep:
    """One image through the v2 pipeline (02 §5.4): EXIF date first (recorded
    even on refusal), then the thumbnail, then minSide + post-thumb byte cap.

    Refusals are ImagePrep.refused strings (caller decides the capability):
      "small image WxH (DigUp minImageSide)"  — thumbnail min side < 256
      "thumbnail exceeds 8 MB after resize"   — pathological encode
    Raises ToolError when sips is absent (caller: honest metadata_only) and
    RuntimeError when the file cannot be decoded at all."""
    taken = metadata.taken if metadata is not None else exif_taken(path)
    prep = ImagePrep(taken=taken[0] if taken else None,
                     taken_epoch=taken[1] if taken else None)
    # The retry object only caches the DATE attempt; camera/GPS are separate
    # bounded reads and always attempted for JPEGs.
    from .exif import camera_and_position

    camera = camera_and_position(path)
    prep.camera_make, prep.gps = camera.make, camera.gps
    token = uuid.uuid4().hex[:10]
    tmp.mkdir(parents=True, exist_ok=True)
    out = tmp / (f"shot-{token}.png" if screenshot else f"thumb-{token}.jpg")
    thumbnail_image(
        path, out,
        max_side=SCREENSHOT_THUMB_MAX_SIDE if screenshot else PHOTO_THUMB_MAX_SIDE,
        png=screenshot,
    )
    dims = image_dimensions(out)
    if dims is None:
        raise RuntimeError(f"sips produced an unreadable thumbnail for {path.name}")
    prep.width, prep.height = dims
    if min(dims) < IMAGE_THUMB_MIN_SIDE:
        out.unlink(missing_ok=True)
        prep.refused = (
            f"small image {dims[0]}x{dims[1]} (DigUp minImageSide)"
        )
        return prep
    if out.stat().st_size > IMAGE_THUMB_MAX_BYTES:
        out.unlink(missing_ok=True)
        prep.refused = "thumbnail exceeds 8 MB after resize"
        return prep
    prep.path = out
    return prep
