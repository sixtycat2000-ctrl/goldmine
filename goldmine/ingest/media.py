"""Media probes: ffprobe metadata + ffmpeg audio window extraction.

v1 scope (build pass 7): audio windows are the ONLY media embedding path, and
only via media-capable backends (llama /v1/embeddings is text-only).
- audio: 16 kHz mono PCM16 WAV windows, 30 s window / 25 s hop (5 s overlap —
  DigUp audioWindow: the model ignores anything longer), silent windows
  (RMS < 0.003 ≈ −50 dBFS) skipped, duration capped by cfg.ingest.audio_max_minutes.
- video: ffprobe metadata + the soundtrack via the same audio path; visual
  frames are a documented stretch (ARCHITECTURE §2.1) and are NOT built here.
- sniff_media works with NO tools installed so capability reporting stays exact.

Tool absence raises ToolError; callers record metadata_only with the reason.
"""

from __future__ import annotations

import logging
import math
import shutil
import struct
import subprocess
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
#: Video frames are DEFERRED: frame budget 70 stays frozen in the constants,
#: no frame vectors are produced yet (preregistration scope decision A2).
MEDIA_PREPROCESSING = (
    "v3:audio=wav-16k-mono-pcm16,window30s,hop25s,rms-gate0.003; "
    "image=jpeg-thumb-1024-q90|png-1536(screenshot),minSide256,"
    "resize=bicubic-a-0.5-48px-fill-budget; "
    "page=pdftoppm150dpi-thin+glyphorder; "
    "ocr_text_vector=text-only-backends; frames=deferred"
)

#: Frozen token budgets per media kind (digup-plan-002 preregistration;
#: DigUp Model.swift:84 granularity — the whole index shares one embed
#: identity, so any budget change re-embeds everything). `frame` is frozen
#: here for identity stability but produces no vectors yet (deferred).
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
            break
        start += hop
        index += 1
    return pieces


def video_soundtrack_windows(path: Path, tmp: Path, **kwargs) -> list[MediaPiece]:
    """Video v1 = metadata (ffprobe) + the soundtrack via audio_windows.

    Visual frames (ffmpeg fps=1/3 + aHash dedup) are a documented stretch —
    deliberately not built (ARCHITECTURE §2.1, DESIGN_DECISIONS §10).
    """
    return audio_windows(path, tmp, **kwargs)


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
    import uuid

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
