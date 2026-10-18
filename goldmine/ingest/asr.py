"""whisper-cli ASR (v2 item 6 / 02 §5.1, B1-amended guard).

Speech in audio/video soundtracks becomes TEXT: transcript chunks go to FTS
AND get prompted text vectors under ANY text-capable backend (llama included)
— the raw-audio window path stays only for non-speech (or when whisper is
missing, `ingest.asr = auto`).

Pipeline (02 §5.1):
  resolve whisper-cli (env GOLDMINE_WHISPER > [ingest] asr_whisper >
  /opt/homebrew/bin/whisper-cli > PATH) + models via the model-manager
  registry (`whisper-large-v3-turbo-q5_0`, `silero-vad-v5.1.2`) ->
  ffmpeg -i X -ac 1 -ar 16000 -f wav (streamed to a tmp dir, capped at
  asr_max_minutes) -> whisper-cli -m <model> --vad -vm <vad> -l auto -ojf
  -of <tmp>/t -np -f <wav> -> parse segments.

Guard (B1, amended 2026-10-16): whisper-cli does NOT emit segment-level
no_speech_prob/avg_logprob; with -ojf the only confidence data is tokens[]
with per-token p. Per segment: mean token p < 0.50 -> drop; chars/minute < 20
-> drop (hallucination on music/silence). `info.asr_dropped` records the
count; the file-level < 20 chars/min yield gate lives in the pipeline
(02 §5.1 quality gate).

Merge: consecutive surviving segments combine into <= 40 s / <= ~450 est-token
transcript chunks (a 4 s whisper segment is too little context for a good
text vector). v2 amendment (02 F8, 2026-10-16): the size cap is measured in
TOKENS via the chunker's est_tokens estimator — the same ~450-token budget
text chunks use — replacing the old 900-char cap that under-filled the embed
budget ~2.5-3x; the 40 s span cap is kept. Extraction-semantics change is
marked by the asr-whisper.v2 version bump (G10 re-does old rows on the next
plain ingest). loc = start seconds (ms offsets -> float s), loc_end = end.
Subprocess timeout = max(120, 2 x duration_s) — a 2 h transcription is
legitimately long, a hung one is not.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path

from ..chunker import DEFAULT_TOKEN_BUDGET, est_tokens
from ..config import Config
from ..util import ToolError, run_tool
from . import media

logger = logging.getLogger("goldmine.ingest.asr")

__all__ = [
    "AsrError", "AsrSegment", "AsrResult", "Tools",
    "resolve_tools", "transcribe", "merge_segments",
    "MIN_SEGMENT_MEAN_P", "MIN_CHARS_PER_MINUTE",
    "MERGE_MAX_S", "MERGE_MAX_TOKENS",
]

#: manifest keys (goldmine/model_manifest.py; roles asr + vad)
WHISPER_MODEL_KEY = "whisper-large-v3-turbo-q5_0"
VAD_MODEL_KEY = "silero-vad-v5.1.2"

#: guard floors (02 §5.1: "start at 0.50; calibrate on the real-podcast
#: acceptance run" — the calibration knob lives here, one constant)
MIN_SEGMENT_MEAN_P = 0.50
MIN_CHARS_PER_MINUTE = 20.0

#: transcript merge bounds (02 §5.1, amended by 02 F8: the ~450-token text
#: chunk budget replaces the old 900-char cap so transcripts pack like text;
#: the ~40 s span cap is kept)
MERGE_MAX_S = 40.0
MERGE_MAX_TOKENS = DEFAULT_TOKEN_BUDGET

#: brew whisper.cpp install location (checked before PATH so a brew install
#: wins over whatever else might lurk there)
BREW_WHISPER = "/opt/homebrew/bin/whisper-cli"

#: reported in extractor identity/meta; bump when extraction semantics change
#: v2: merge cap switched to est-tokens (02 F8) -> v1 rows re-merge on the
#: next plain ingest (G10)
ASR_EXTRACTOR_VERSION = "asr-whisper.v2"

#: One whisper-cli at a time (measured on this machine, 2026-10-16: two
#: concurrent whisper loads beside a live llama-server raced on Metal and one
#: crashed mid-run — the auto fallback recorded metadata honestly, but the
#: transcript was lost). whisper holds a ~1.6 GB working set; serializing the
#: decode is the C10 journey-spec scheduling stance for v2.0.
_TRANSCRIBE_LOCK = threading.Lock()


class AsrError(RuntimeError):
    """whisper-cli ran and failed (missing model file is NOT this — check
    resolve_tools first). Carries a bounded stderr tail."""


@dataclass(frozen=True)
class Tools:
    whisper: str
    model: Path
    vad: Path


@dataclass
class AsrSegment:
    text: str
    start_s: float          # seconds into the SOURCE audio (float, ms/1000)
    end_s: float

    @property
    def chars_per_minute(self) -> float:
        minutes = (self.end_s - self.start_s) / 60.0
        if minutes <= 0:
            return 0.0
        return len(self.text.strip()) / minutes


@dataclass
class AsrResult:
    language: str | None
    segments: list[AsrSegment] = field(default_factory=list)  # merged, guarded
    dropped: int = 0          # guard-dropped whisper segments
    duration_s: float | None = None
    partial: bool = False     # source ran past asr_max_minutes
    read_end: float | None = None


def resolve_whisper_cli(cfg: Config) -> str | None:
    """GOLDMINE_WHISPER/[ingest] asr_whisper > /opt/homebrew/bin/whisper-cli >
    shutil.which. An explicitly configured path is honored even when missing
    (same philosophy as llama.server)."""
    explicit = (getattr(cfg.ingest, "asr_whisper", "") or "").strip()
    if explicit:
        return explicit
    if Path(BREW_WHISPER).is_file():
        return BREW_WHISPER
    return shutil.which("whisper-cli")


def resolve_tools(cfg: Config) -> Tools | None:
    """whisper-cli + both manifest models, or None (with a debug log naming
    the gap). Models resolve through the model manager's order: state >
    models-dir > repo models/ copy."""
    cli = resolve_whisper_cli(cfg)
    if cli is None or not Path(cli).is_file():
        logger.debug("asr: whisper-cli not found — unavailable")
        return None
    try:
        from ..model import ModelManager

        manager = ModelManager(cfg)
        model, model_src = manager.resolve(WHISPER_MODEL_KEY)
        vad, vad_src = manager.resolve(VAD_MODEL_KEY)
    except Exception as exc:  # manifest/manager problems never crash ingest
        logger.debug("asr: model resolution failed (%s)", exc)
        return None
    if model is None or vad is None:
        logger.debug(
            "asr: models missing (asr=%s via %s, vad=%s via %s) — unavailable",
            model, model_src, vad, vad_src,
        )
        return None
    return Tools(whisper=cli, model=model, vad=vad)


def _wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 16000)
    except (wave.Error, OSError, ZeroDivisionError):
        return None


def _needs_transcode(path: Path) -> bool:
    """16 kHz mono WAV goes to whisper as-is; anything else is transcoded."""
    if path.suffix.lower() != ".wav":
        return True
    probe = shutil.which("ffprobe")
    if probe is None:
        return False  # cannot verify; whisper will complain loudly if wrong
    try:
        proc = subprocess.run(
            [probe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate,channels",
             "-of", "csv=p=0", str(path)],
            capture_output=True, timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if proc.returncode != 0:
        return True
    parts = (proc.stdout.decode() or "").strip().split(",")
    if len(parts) != 2:
        return True
    try:
        return int(parts[0]) != 16000 or int(parts[1]) != 1
    except ValueError:
        return True


def _transcode(path: Path, out: Path, cap_s: float) -> None:
    ffmpeg = media.ffmpeg_path()
    if ffmpeg is None:
        raise AsrError("ffmpeg not found (needed to prepare audio for ASR)")
    argv = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-ac", "1", "-ar", "16000",
        "-t", f"{cap_s:.3f}",
        "-f", "wav", "-y", str(out),
    ]
    result = run_tool(argv, timeout_s=max(60.0, cap_s / 10.0))
    if result.timed_out:
        raise AsrError(f"ffmpeg transcode timed out for {path.name}")
    if result.rc != 0:
        raise AsrError(
            f"ffmpeg transcode failed rc={result.rc}: "
            f"{result.stderr.strip()[:200]}"
        )


def _mean_token_p(tokens: list) -> float | None:
    """Mean of per-token p values, or None when the build did not serialize
    them (no -ojf tokens[] => no confidence guard, B1)."""
    values = []
    for tok in tokens:
        if not isinstance(tok, dict):
            return None
        p = tok.get("p")
        if not isinstance(p, (int, float)):
            return None
        values.append(float(p))
    if not values:
        return None
    return sum(values) / len(values)


def _guard_segments(raw: list[dict]) -> tuple[list[AsrSegment], int]:
    """B1-amended guard: drop segments with mean token p < MIN_SEGMENT_MEAN_P
    or yield < MIN_CHARS_PER_MINUTE. Returns survivors + dropped count."""
    kept: list[AsrSegment] = []
    dropped = 0
    for entry in raw:
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        offsets = entry.get("offsets") or {}
        try:
            start = float(offsets.get("from", 0)) / 1000.0
            end = float(offsets.get("to", 0)) / 1000.0
        except (TypeError, ValueError):
            dropped += 1
            continue
        seg = AsrSegment(text=text, start_s=round(start, 3), end_s=round(end, 3))
        mean_p = _mean_token_p(entry.get("tokens") or [])
        if mean_p is not None and mean_p < MIN_SEGMENT_MEAN_P:
            dropped += 1
            logger.debug("asr: drop segment %.1f-%.1fs (mean p %.3f)",
                         start, end, mean_p)
            continue
        if seg.chars_per_minute < MIN_CHARS_PER_MINUTE and end > start:
            dropped += 1
            logger.debug("asr: drop segment %.1f-%.1fs (yield %.1f chars/min)",
                         start, end, seg.chars_per_minute)
            continue
        kept.append(seg)
    return kept, dropped


def merge_segments(
    segments: list[AsrSegment],
    *,
    max_s: float = MERGE_MAX_S,
    max_tokens: int = MERGE_MAX_TOKENS,
) -> list[AsrSegment]:
    """Greedy consecutive merge into <= max_s / <= max_tokens transcript
    chunks. The size cap is the chunker's est_tokens estimator on the CANDIDATE
    merged text (the same density the estimator chunker assumes), so a merged
    transcript chunk fits the same ~450-token budget as a text chunk (02 F8;
    was: a 900-char cap that under-filled the budget ~2.5-3x). The span cap
    still binds first for normal speech. loc = first segment's start, loc_end
    = last segment's end — the pipeline stores these as segment loc/loc_end."""
    out: list[AsrSegment] = []
    for seg in segments:
        if out:
            cur = out[-1]
            span = max(seg.end_s, cur.end_s) - cur.start_s
            merged_text = f"{cur.text} {seg.text}"
            if span <= max_s and est_tokens(merged_text) <= max_tokens:
                cur.text = merged_text
                cur.end_s = seg.end_s
                continue
        out.append(AsrSegment(text=seg.text, start_s=seg.start_s,
                              end_s=seg.end_s))
    return out


def transcribe(path: Path, cfg: Config, *, timeout_s: float | None = None) -> AsrResult:
    """Transcribe one audio/video file. Raises AsrError on tool failure or
    timeout; a successful run with no surviving segments returns an empty
    segment list (caller decides metadata/fallback)."""
    tools = resolve_tools(cfg)
    if tools is None:
        raise AsrError("whisper-cli or ASR models unavailable")

    duration = media.ffprobe_duration(path)
    if duration is None:
        duration = _wav_duration(path)
    cap_s = float(getattr(cfg.ingest, "asr_max_minutes", 120.0)) * 60.0
    effective = min(duration, cap_s) if duration is not None else cap_s
    partial = duration is not None and duration > cap_s

    with tempfile.TemporaryDirectory(prefix="goldmine-asr-") as td:
        tmp = Path(td)
        if _needs_transcode(path):
            wav = tmp / "audio-16k.wav"
            try:
                _transcode(path, wav, cap_s=effective)
            except ToolError as exc:
                raise AsrError(f"ffmpeg not found: {exc}") from exc
        else:
            wav = path
        out_base = tmp / "t"
        argv = [
            tools.whisper,
            "-m", str(tools.model),
            "--vad", "-vm", str(tools.vad),
            "-l", str(getattr(cfg.ingest, "asr_language", "auto") or "auto"),
            "-ojf", "-of", str(out_base),
            "-np", "-f", str(wav),
        ]
        deadline = timeout_s if timeout_s is not None else max(120.0, 2.0 * effective)
        try:
            with _TRANSCRIBE_LOCK:
                proc = subprocess.run(
                    argv, capture_output=True, timeout=deadline,
                    stdin=subprocess.DEVNULL,
                )
        except subprocess.TimeoutExpired as exc:
            raise AsrError(
                f"whisper-cli timeout after {deadline:g}s on {path.name}"
            ) from exc
        except OSError as exc:
            raise AsrError(f"whisper-cli failed to run: {exc}") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-500:]
            raise AsrError(
                f"whisper-cli rc={proc.returncode} on {path.name}: {tail}"
            )
        json_path = out_base.with_suffix(".json")
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AsrError(f"whisper-cli JSON unreadable: {exc}") from exc

    raw = payload.get("transcription") or []
    if not isinstance(raw, list):
        raw = []
    kept, dropped = _guard_segments(raw)
    merged = merge_segments(kept)
    language = None
    result = payload.get("result")
    if isinstance(result, dict):
        language = result.get("language")
    return AsrResult(
        language=language if isinstance(language, str) else None,
        segments=merged,
        dropped=dropped,
        duration_s=duration,
        partial=partial,
        read_end=cap_s if partial else None,
    )
