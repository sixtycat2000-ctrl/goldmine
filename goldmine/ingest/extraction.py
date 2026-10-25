"""Extraction: one walk candidate -> an _Outcome (chunks / media segments /
capability verdict), plus the run-shared OCR batch (02 F7: same-wave
screenshots and thin PDF pages drain through ONE goldmine-ocr helper spawn).

Pure policy, no store access: the host pipeline (goldmine.ingest.pipeline.
IngestPipeline) supplies the run state these methods read through `self` —
backend, lexical_only, cfg, the media scratch dir, the OCR batch queues —
and hands results to the commit helpers in goldmine.ingest.commit."""

from __future__ import annotations

import logging
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import ocr as ocr_mod
from ..chunker import Chunk, chunk_code_tokens
from ..util import ToolError, excerpt
from . import extract_pdf, media, rules, walk
from .extract_html import extract_html_file
from .extract_mail import extract_eml
from .extract_text import (
    TEXTUTIL_FORMATS,
    decode_bytes,
    extract_docx,
    extract_epub,
    extract_markdown,
    extract_txt,
    extract_via_textutil,
)

logger = logging.getLogger("goldmine.ingest")


CODE_HARD_CAP_BYTES = 1 << 20  # code files 1 MB hard cap (SPEC §2, D5c)

# v2 (02 §5.4): v1's 20 MB raw-image-byte cap is GONE — every image goes
# through a sips thumbnail first (media.prepare_image), which bounds what the
# backend ever sees. The residual caps live in media.py: minSide refusal and
# the ~8 MB post-thumbnail byte cap.


@dataclass
class _Piece:
    """One chunk-ready piece: vector eligibility + segment kind + excerpt."""

    chunk: Chunk
    vec_ok: bool
    kind: str = "chunk"                 # chunk | page (thin PDF page)
    excerpt_override: str | None = None  # None -> util.excerpt(chunk.text)
    location_unit: str = "line"          # page/second survive ctx-guard resplits


@dataclass
class _MediaSeg:
    """One media segment awaiting embedding (extracted in the worker pool,
    embedded on the main thread in _commit — ARCHITECTURE §7.1 keeps all
    vector production off the extraction threads)."""

    kind: str                        # 'image' | 'audio' | 'ocr' | 'page' (segment kind)
    path: Path | None = None         # image/page: the bitmap to embed (sips thumb / rendered page)
    wav: bytes | None = None         # audio: one 16 kHz mono PCM16 window
    loc: float | None = None         # audio: window start/end seconds; page: page number; image: NULL
    loc_end: float | None = None
    excerpt: str | None = None       # image: filename; audio: short window label
    text_for_fts: str | None = None  # ocr: recognized text (always FTS-indexed)
    embed_text: bool = False         # ocr under a text-only backend: text vector (A5)
    budget: int | None = None        # digup-plan-002: vision token budget (frozen constants)
    modality: str | None = None      # vector modality; page/garbled map away from `kind`
    garbled: str | None = None       # glyph-order page text: meaning-only TEXT vector (§3.5/A1)


@dataclass
class _Outcome:
    """What extraction decided about one candidate (pre-store)."""

    capability: str                 # content_indexed | metadata_only | unsupported | failed
    detail: str | None = None
    info: dict = field(default_factory=dict)
    extractor_version: str | None = None
    title: str | None = None
    chunks: list[_Piece] = field(default_factory=list)
    media_segs: list[_MediaSeg] = field(default_factory=list)
    partial: bool = False
    kind: str | None = None         # override the walk kind for unsupported rows
    content_date: float | None = None  # EXIF taken epoch (05 §4.1c/C10)


@dataclass
class _OcrRequest:
    """One screenshot queued for the shared OCR batch (02 F7)."""

    path: Path
    done: threading.Event = field(default_factory=threading.Event)
    page: "ocr_mod.OcrPage | None" = None   # helper answer (may carry .error)
    failed: str | None = None               # batch-level unavailability/timeout
    failed_status: str = "off"


#: Screenshot batch gather (02 F7): the drain window closes after this much
#: QUIESCENCE (no new arrivals), capped absolutely. Fixed-width windows race
#: stragglers (a worker scheduled ~1 wave-length late lands in a second
#: spawn); quiescence folds them in as long as arrivals keep coming. The
#: helper spawn itself is one subprocess per drain — ~0.15 s overhead and a
#: ~1.1 s Vision cold start each (was: per screenshot), so collapsing a wave
#: into one spawn also caps concurrent ~100 MB Vision hosts at one.
_OCR_BATCH_GATHER_S = 0.10
_OCR_BATCH_GATHER_MAX_S = 2.0
_OCR_BATCH_POLL_S = 0.01


def _has_words(text: str) -> bool:
    """Digits-only text loses its vector (DigUp SearchText.hasWords port):
    it competes lexically only, or a number dump would sink unrelated matches."""
    return any(ch.isalpha() for ch in text)

class ExtractMixin:
    """Extraction dispatch (moved verbatim out of pipeline.py): walk kinds ->
    _Outcome, plus the OCR probe/batch machinery shared by screenshots and
    thin PDF pages. Host supplies backend/cfg/lexical_only and the run-scoped
    media scratch dir + OCR batch state (see IngestPipeline.__init__)."""

    # ------------------------------------------------------------ extraction

    def _extract_notebook(self, text: str, detail: str | None, max_chunks: int):
        """P2 (DigUp CodeExtractor.swift:150-163): a Jupyter notebook's CELLS
        (code and markdown alike) as their text; outputs are never read —
        they are results, not what anyone wrote. nbformat 4 (cells[].source,
        str or list) and nbformat 3 (worksheets[0].cells[].input) only."""
        import json as _json

        try:
            doc = _json.loads(text)
        except ValueError as exc:
            return _Outcome(capability="failed", detail=f"not a valid notebook: {exc}")
        sources: list[str] = []
        if isinstance(doc.get("cells"), list):  # nbformat 4
            for cell in doc["cells"]:
                if not isinstance(cell, dict):
                    continue
                src = cell.get("source")
                if isinstance(src, list):
                    src = "".join(str(part) for part in src)
                if isinstance(src, str) and src.strip():
                    sources.append(src)
        elif isinstance(doc.get("worksheets"), list) and doc["worksheets"]:  # nbformat 3
            for cell in doc["worksheets"][0].get("cells", []):
                src = cell.get("input") if isinstance(cell, dict) else None
                if isinstance(src, list):
                    src = "".join(str(part) for part in src)
                if isinstance(src, str) and src.strip():
                    sources.append(src)
        else:
            return _Outcome(capability="failed", detail="not a valid notebook")
        if not sources:
            return _Outcome(
                capability="unsupported", detail="notebook without readable cells"
            )
        joined = "\n\n".join(sources)
        chunks = chunk_code_tokens(
            joined,
            budget_tokens=self._token_budget(),
            tokenize=self._tokenize_fn(),
            max_chunks=max_chunks,
        )
        return _Outcome(
            capability="content_indexed",
            detail=detail,
            extractor_version="ipynb-stdlib.v1",
            chunks=[_Piece(c, _has_words(c.text)) for c in chunks],
            info={"cells": len(sources)},
        )

    def _extract(
        self,
        cand: walk.Candidate,
        *,
        encoding_override: str | None,
        max_chunks: int,
    ) -> _Outcome:
        path = cand.path
        ext = path.suffix.lstrip(".").lower()
        try:
            if cand.kind == "doc":
                return self._extract_doc(path, ext, encoding_override, max_chunks, kind="doc")
            if cand.kind == "code":
                if cand.size > CODE_HARD_CAP_BYTES:
                    return _Outcome(capability="unsupported", detail="code size cap (>1 MB)")
                data = path.read_bytes()
                text, detail = decode_bytes(data, override=encoding_override)
                if ext == "ipynb":
                    # P2: notebooks are read as CELLS; outputs (base64
                    # blobs, tables, logs) are results, not what anyone
                    # wrote (DigUp CodeExtractor.swift:150-163) — they were
                    # previously chunked as code text.
                    return self._extract_notebook(text, detail, max_chunks)
                refusal = rules.code_content_refusal(text)
                if refusal:
                    return _Outcome(capability="unsupported", detail=refusal)
                chunks = chunk_code_tokens(
                    text,
                    budget_tokens=self._token_budget(),
                    tokenize=self._tokenize_fn(),
                    max_chunks=max_chunks,
                )
                return _Outcome(
                    capability="content_indexed",
                    detail=detail,
                    extractor_version="code-stdlib.v1",
                    chunks=[_Piece(c, _has_words(c.text)) for c in chunks],
                )
            if cand.kind in ("image", "audio", "video"):
                return self._extract_media(cand)
            if cand.kind == "pdf":
                return self._extract_pdf(path, max_chunks)
        except Exception as exc:
            return _Outcome(capability="failed", detail=f"{type(exc).__name__}: {exc}")
        return _Outcome(capability="failed", detail=f"unhandled kind {cand.kind}")

    def _extract_media(self, cand: walk.Candidate) -> _Outcome:
        """v2 media extraction (02 §5.2/§5.4). Images: sips thumbnail (photos
        1024 JPEG q90 / screenshots 1536 PNG) before embed_image — no raw-byte
        cap, minSide refusal, EXIF taken -> content_date. Screenshots ALSO get
        Vision OCR text as a `kind="ocr"` FTS segment: vector-less where an
        image vector exists (DigUp Indexer.swift:510-513), TEXT-vector under a
        text-only backend (A5: no image vector exists there to carry meaning).
        Audio/video: 30 s WAV windows (media.audio_windows); text-only
        backends stay metadata-only with an honest reason (SPEC §9)."""
        path = cand.path
        kind = cand.kind
        cap = self.backend.capability() if self.backend is not None else None

        if kind == "image":
            return self._extract_image(cand, cap)

        if self.lexical_only:
            # --lexical-only: no embeddings, and non-image media has no text
            return _Outcome(
                capability="metadata_only",
                detail="lexical-only ingest (media has no text)",
                info={"audio_routing": "none:lexical_only"},
            )

        # audio / video: ASR runs FIRST (02 §5.1; zero change here): a
        # successful transcript becomes TEXT — FTS + prompted vectors under
        # ANY text-capable backend. Raw-audio windows remain only for
        # non-speech or when whisper is missing/failing. Videos additionally
        # get keyframes (video-frames-001): every insertion below is gated on
        # kind == "video"; the audio path itself is untouched.
        asr_outcome = self._try_asr(cand, cap)
        asr_info = dict(asr_outcome.info or {}) if asr_outcome is not None else {}
        if asr_outcome is not None:
            mode = str(getattr(self.cfg.ingest, "asr", "auto") or "auto")
            if asr_outcome.capability != "metadata_only" or mode == "always":
                if kind == "video" and asr_outcome.capability == "content_indexed":
                    # transcript + frames coexist (preregistration §3)
                    return self._video_with_frames(cand, cap, asr_outcome, asr_info)
                return asr_outcome
            if cap is None or not cap.audio:
                asr_outcome.detail = "backend lacks audio embedding; " + (asr_outcome.detail or "ASR unavailable")
                asr_outcome.info = {**asr_info, "audio_routing": "none:no_capability"}
                return asr_outcome

        if cap is None or not cap.audio:
            return _Outcome(
                capability="metadata_only", detail="backend lacks audio embedding",
                info={**asr_info, "audio_routing": "none:no_capability"},
            )
        if media.ffprobe_path() is None:
            return _Outcome(
                capability="metadata_only", detail="ffprobe not found",
                info={**asr_info, "audio_routing": "none:ffprobe_missing"},
            )
        duration = media.ffprobe_duration(path)
        if duration is None:
            return _Outcome(
                capability="metadata_only",
                detail="ffprobe could not read duration",
                info={**asr_info, "audio_routing": "none:ffprobe_unreadable"},
            )
        if kind == "video" and media.has_audio_stream(path) is False:
            # soundtrack-less video (video-frames-001): skip the audio path,
            # frames carry the file. ffmpeg missing is still honest metadata.
            if media.ffmpeg_path() is None:
                return _Outcome(
                    capability="metadata_only",
                    detail="no audio stream; ffmpeg not found",
                    info={**asr_info, "duration": round(duration, 3),
                          "audio_routing": "none:no_audio_stream",
                          "frames": "none:ffmpeg_missing"},
                )
            return self._video_frames_outcome(
                cand, cap, asr_info, duration,
                audio_segs=[], audio_routing="none:no_audio_stream",
                audio_partial=False,
            )

        # MM-D3 (preregistered): raw-audio vectors exist ONLY for files with
        # no transcript — we only reach this line when ASR produced none.
        routing = "raw:no-transcript({})".format(
            asr_info.get("asr_status")
            or ("off" if str(getattr(self.cfg.ingest, "asr", "auto") or "auto") == "off"
                else "unavailable")
        )
        max_minutes = float(self.cfg.ingest.audio_max_minutes)
        try:
            with tempfile.TemporaryDirectory(prefix="goldmine-media-") as td:
                pieces = media.audio_windows(
                    path, Path(td), max_minutes=max_minutes,
                )
                segs = [
                    _MediaSeg(
                        kind="audio",
                        wav=piece.path.read_bytes(),
                        loc=piece.start,
                        loc_end=piece.end,
                        excerpt=f"audio {piece.start:g}–{piece.end:g}s",
                        modality="audio",
                    )
                    for piece in pieces
                ]
        except ToolError:
            return _Outcome(
                capability="metadata_only", detail="ffmpeg not found",
                info={**asr_info, "audio_routing": "none:ffmpeg_missing"},
            )
        # per-window ffmpeg failures/timeout are extraction errors => failed row
        # (let RuntimeError propagate into _extract's handler above)

        if kind == "video":
            # windows + frames (an all-silent soundtrack contributes nothing;
            # frames decide the verdict) — audio files keep the tail verbatim
            return self._video_frames_outcome(
                cand, cap, asr_info, duration,
                audio_segs=segs, audio_routing=routing,
                audio_partial=duration > max_minutes * 60.0,
            )

        info: dict = {
            **asr_info, "duration": round(duration, 3), "windows": len(segs),
            "audio_routing": routing,
        }
        partial = duration > max_minutes * 60.0
        if partial:
            info["read_end"] = max_minutes * 60.0
        if not segs:
            return _Outcome(
                capability="metadata_only",
                detail="all audio windows silent (RMS gate)",
                info={**info, "audio_routing": "none:all_windows_silent"},
            )
        return _Outcome(
            capability="content_indexed",
            info=info,
            extractor_version=media.MEDIA_EXTRACTOR_VERSION,
            media_segs=segs,
            partial=partial,
        )

    # ------------------------------------------------ video keyframes (v4) --

    def _video_frame_segs(self, cand: walk.Candidate, cap, info: dict) -> list[_MediaSeg]:
        """Keyframe segs for one video (video-frames-001). Records the audit
        markers into `info` — frames=<kept count> or "none:<reason>", plus
        frame_interval=<step> whenever extraction ran.

        Raises RuntimeError on extraction errors (decode failure / timeout /
        pairing mismatch); the CALLER owns the failure policy (degrade when
        audio content exists, propagate when nothing else landed)."""
        if cap is None or not cap.image:
            info["frames"] = "none:no_image_capability"
            return []
        if media.has_video_stream(cand.path) is False:
            info["frames"] = "none:no_video_stream"
            return []
        try:
            kf = media.video_keyframes(cand.path, self._media_tmp_dir())
        except ToolError:
            info["frames"] = "none:ffmpeg_missing"
            return []
        info["frames"] = len(kf.frames)
        info["frame_interval"] = round(kf.step, 3)
        info["frame_timestamps"] = media.FRAME_TIMESTAMPS_VERSION
        return [
            _MediaSeg(
                kind="frame",
                path=frame.path,
                loc=frame.loc,
                loc_end=frame.loc_end,
                excerpt=f"frame {frame.loc:g}s",
                budget=media.MEDIA_BUDGETS["video_frame"],
                modality="image",
            )
            for frame in kf.frames
        ]

    def _video_with_frames(self, cand, cap, outcome: _Outcome, asr_info: dict) -> _Outcome:
        """A transcribed video gains frame segs on top of its transcript
        (audio段+帧段并存, preregistration §3). Frame extraction failure
        DEGRADES — a transcript that already stands is never destroyed."""
        info = dict(outcome.info or asr_info)
        try:
            frame_segs = self._video_frame_segs(cand, cap, info)
        except RuntimeError as exc:
            reason = str(exc)[:120]
            info["frames"] = f"error:{reason}"
            outcome.info = info
            outcome.detail = (outcome.detail + "; " if outcome.detail else "") + \
                f"video frames failed: {reason}"
            return outcome
        outcome.media_segs = [*outcome.media_segs, *frame_segs]
        outcome.info = info
        if frame_segs:
            # composed extractor version (registry key "asr+media"): both the
            # transcript and the frame dimensions stay re-doable; a bump on
            # either side leaves the registry -> the row goes stale.
            outcome.extractor_version = (
                f"{outcome.extractor_version}+{media.MEDIA_EXTRACTOR_VERSION}"
            )
        return outcome

    def _video_frames_outcome(
        self, cand, cap, asr_info: dict, duration: float, *,
        audio_segs: list[_MediaSeg], audio_routing: str, audio_partial: bool,
    ) -> _Outcome:
        """The no-transcript video composition: raw audio windows (possibly
        none) + keyframes. All-silent soundtracks reroute honestly; frames
        decide the verdict when audio contributed nothing."""
        if not audio_segs and audio_routing.startswith("raw:"):
            audio_routing = "none:all_windows_silent"
        info: dict = {
            **asr_info, "duration": round(duration, 3),
            "audio_routing": audio_routing,
        }
        if audio_segs:
            info["windows"] = len(audio_segs)
        detail: str | None = None
        try:
            frame_segs = self._video_frame_segs(cand, cap, info)
        except RuntimeError as exc:
            if not audio_segs:
                raise  # nothing else landed: honest failed row
            reason = str(exc)[:120]
            info["frames"] = f"error:{reason}"
            detail = f"video frames failed: {reason}"
            frame_segs = []
        segs = [*audio_segs, *frame_segs]
        partial = audio_partial
        if partial:
            info["read_end"] = float(self.cfg.ingest.audio_max_minutes) * 60.0
        if not segs:
            reason = {
                "none:no_audio_stream": "no audio stream; no frames extracted",
                "none:all_windows_silent": "all audio windows silent (RMS gate); no frames extracted",
            }.get(audio_routing, "no media segments extracted")
            if isinstance(info.get("frames"), str) and info["frames"] == "none:no_video_stream":
                reason = "no decodable video stream"
            return _Outcome(
                capability="metadata_only", detail=detail or reason,
                info=info, partial=partial,
            )
        return _Outcome(
            capability="content_indexed",
            detail=detail,
            info=info,
            extractor_version=media.MEDIA_EXTRACTOR_VERSION,
            media_segs=segs,
            partial=partial,
        )

    # ------------------------------------------------------ ASR (v2 item 6) ---

    def _try_asr(self, cand: walk.Candidate, cap) -> "_Outcome | None":
        """whisper-cli transcript attempt for audio/video (02 §5.1, B1 guard).

        Returns None when ASR does not apply or is unavailable (`ingest.asr`
        = auto) — the caller falls back to the v1 raw-window/metadata path.
        `always` converts every gap into an honest outcome instead. A success
        returns transcript chunks: kind='audio' segments, modality text, FTS
        body = verbatim transcript, vector = the usual document prompt with
        title = file stem (handled by _commit)."""
        if self.lexical_only:
            return None
        mode = str(getattr(self.cfg.ingest, "asr", "auto") or "auto")
        if mode == "off":
            return None
        if cap is None or not cap.text:
            return None  # no text capability: a transcript could not vector here
        if cand.kind == "video" and media.has_audio_stream(cand.path) is False:
            return None  # let the v1 path record the honest "no audio stream"
        from . import asr as asr_mod

        if asr_mod.resolve_tools(self.cfg) is None:
            return _Outcome(
                capability="metadata_only", info={"asr_status": "unavailable"},
                detail=("ASR unavailable (whisper-cli or models missing) — "
                        "`goldmine model install whisper-large-v3-turbo-q5_0 silero-vad-v5.1.2`"),
            )
        try:
            self._metrics.call("asr_calls")
            result = asr_mod.transcribe(cand.path, self.cfg)
        except asr_mod.AsrError as exc:
            return _Outcome(capability="failed" if mode == "always" else "metadata_only",
                            detail=f"asr failed: {exc}", info={"asr_status": "error"})

        info: dict = {}
        if result.duration_s is not None:
            info["duration"] = round(result.duration_s, 3)
        if result.language:
            info["language"] = result.language
        if result.dropped:
            info["asr_dropped"] = result.dropped
        if result.read_end is not None:
            info["read_end"] = result.read_end

        if not result.segments:
            info["asr_status"] = "no_speech"
            return _Outcome(capability="metadata_only", detail="no speech detected (ASR)", info=info)

        pieces = [
            _Piece(
                Chunk(seg.text, seg.start_s, seg.end_s),
                _has_words(seg.text),
                kind="audio",
                location_unit="second",
            )
            for seg in result.segments
        ]
        # 02 §5.1 file-level yield gate: < 20 chars/min stays content_indexed
        # only if at least one vector can land (>= 1 piece with words)
        if result.duration_s and result.duration_s > 0:
            total_chars = sum(len(s.text) for s in result.segments)
            yield_cpm = total_chars / (result.duration_s / 60.0)
            if yield_cpm < asr_mod.MIN_CHARS_PER_MINUTE and not any(
                p.vec_ok for p in pieces
            ):
                return _Outcome(
                    capability="metadata_only",
                    detail="no speech detected (yield < 20 chars/min)",
                    info={**info, "asr_status": "low_yield"},
                )
        info["asr_segments"] = len(result.segments)
        info["asr_status"] = "complete"
        info["audio_routing"] = "text:transcript"
        return _Outcome(
            capability="content_indexed",
            info=info,
            extractor_version=asr_mod.ASR_EXTRACTOR_VERSION,
            chunks=pieces,
            partial=result.partial,
        )

    # -------------------------------------------------- image (v2 B4/B5) ------

    def _extract_image(self, cand: walk.Candidate, cap) -> _Outcome:
        """One image through the v2 policy (02 §5.2 Scope A + §5.4):
        screenshot detection (xattr-first) -> OCR (screenshots only) ->
        sips thumbnail when an image vector is wanted -> EXIF taken date in
        every case. Order of outcomes:
          - minSide / oversized-thumbnail refusal: unsupported (machine reason)
          - decode failure: metadata_only "sips could not decode"
          - no image capability AND no OCR text: metadata_only (honest reason)
          - else: content_indexed with image seg (thumbnail vector) and/or
            `ocr` seg (FTS text; text-vector only under a text-only backend)."""
        path = cand.path
        is_shot = rules.is_screenshot(path)
        metadata = media.exif_capture(path)
        taken = metadata.taken
        info: dict = {"exif_version": media.EXIF_METADATA_VERSION, "exif_reader": metadata.reader,
                      "exif_status": metadata.status,
                      "taken": taken[0] if taken else None}
        # Bounded camera/GPS live next to the date so text-only backends
        # (which never reach prepare_image) still store them (D4 revision).
        from .exif import camera_and_position

        camera = camera_and_position(path)
        if camera.make is not None:
            info["camera_make"] = camera.make
        if camera.gps is not None:
            info["gps"] = camera.gps
        if is_shot:
            info["screenshot"] = True
        if taken:
            info["taken_source"] = "Exif.DateTimeOriginal"
        content_date: float | None = taken[1] if taken else None
        detail: str | None = None

        ocr_details: dict = {}
        ocr_text, ocr_ms, ocr_status = (
            self._ocr_screenshot(path, details=ocr_details) if is_shot else (None, None, "skipped")
        )
        if ocr_text is not None:
            info["ocr_chars"] = len(ocr_text)
            if ocr_ms is not None:
                info["ocr_ms"] = round(ocr_ms, 1)
        elif ocr_status == "no-text":
            info["ocr"] = "no text"
        elif ocr_status == "off":
            # marker: a later ingest with OCR available re-does this row once
            # (_needs_ocr_redo); "no text"/ocr_chars never re-do.
            info["ocr"] = "unavailable"
        if is_shot:
            info["ocr_status"] = {
                "ok": "complete", "no-text": "no_text", "off": "unavailable",
                "low-confidence": "low_confidence", "error": "error",
            }[ocr_status]
            if ocr_status in ("error", "low-confidence"):
                info["ocr"] = "error" if ocr_status == "error" else "low confidence"
            if ocr_status == "off":
                info["ocr_reason"] = ocr_details.get("reason") or self._ocr_unavailable_reason or "helper unavailable"
            elif "reason" in ocr_details:
                info["ocr_reason"] = ocr_details["reason"]

        want_image_vec = cap is not None and cap.image and not self.lexical_only
        segs: list[_MediaSeg] = []
        extractor = "image-mm.v1"

        if want_image_vec:
            # full v2 prep: EXIF + thumbnail + minSide/cap refusals
            try:
                prep = media.prepare_image(
                    path, self._media_tmp_dir(), screenshot=is_shot, metadata=metadata,
                )
            except ToolError:
                return _Outcome(
                    capability="metadata_only",
                    detail="sips not found (image thumbnails)",
                    info=info,
                    content_date=content_date,
                )
            except RuntimeError as exc:
                # sips cannot decode (rc != 0, e.g. "Error 13"): honest
                # metadata_only unless the OCR text carries the file.
                if ocr_text:
                    segs.append(self._ocr_seg(path, ocr_text, cap))
                    return _Outcome(
                        capability="content_indexed",
                        detail=f"image undecodable; ocr text only ({str(exc)[:80]})",
                        info=info,
                        extractor_version="image-mm-ocr.v1",
                        media_segs=segs,
                        content_date=content_date,
                    )
                return _Outcome(
                    capability="metadata_only",
                    detail=f"sips could not decode: {str(exc)[:120]}",
                    info=info,
                    content_date=content_date,
                )
            if prep.taken_epoch is not None:
                content_date = prep.taken_epoch
                info["taken"] = prep.taken
                info["taken_source"] = "Exif.DateTimeOriginal"
                info["exif_status"] = "complete"
            if prep.camera_make is not None:
                info["camera_make"] = prep.camera_make
            if prep.gps is not None:
                info["gps"] = prep.gps
            if prep.refused is not None:
                if ocr_text:
                    # the pixels are un-embeddable (minSide / pathological
                    # thumbnail) but the OCR text carries the file honestly
                    segs.append(self._ocr_seg(path, ocr_text, cap))
                    return _Outcome(
                        capability="content_indexed",
                        detail=f"{prep.refused}; ocr text only",
                        info=info,
                        extractor_version="image-mm-ocr.v1",
                        media_segs=segs,
                        content_date=content_date,
                    )
                # minSide / pathological thumbnail: unsupported, machine-stable
                return _Outcome(
                    capability="unsupported",
                    detail=prep.refused,
                    info=info,
                    content_date=content_date,
                )
            segs.append(_MediaSeg(
                kind="image", path=prep.path, excerpt=path.name,
                budget=media.MEDIA_BUDGETS["screenshot" if is_shot else "photo"],
                modality="image",
            ))
            info["thumb"] = f"{prep.width}x{prep.height}"
        if ocr_text:
            segs.append(self._ocr_seg(path, ocr_text, cap))
            extractor = "image-mm-ocr.v1"

        if not segs:
            reason = (
                "lexical-only ingest (image has no text)"
                if self.lexical_only else "backend lacks image embedding"
            )
            return _Outcome(
                capability="metadata_only",
                detail=reason,
                info=info,
                content_date=content_date,
            )
        return _Outcome(
            capability="content_indexed",
            detail=detail,
            info=info,
            extractor_version=extractor,
            media_segs=segs,
            content_date=content_date,
        )

    def _ocr_seg(self, path: Path, ocr_text: str, cap) -> _MediaSeg:
        """The screenshot OCR segment policy (02 §5.2/A5): FTS text always;
        a TEXT vector only when the backend cannot embed the image itself
        (llama) and ingest.ocr_text_vector is on. Where an image vector
        exists (multimodal backends) the words stay keyword-only — DigUp
        measured a text vector making screenshots outrank better matches."""
        embed_text = (
            (cap is None or not cap.image)
            and bool(self.cfg.ingest.ocr_text_vector)
            and not self.lexical_only
        )
        return _MediaSeg(
            kind="ocr",
            loc=1,
            loc_end=max(1, len(ocr_text.splitlines())),
            excerpt=excerpt(ocr_text),
            text_for_fts=ocr_text,
            embed_text=embed_text,
        )

    def _ocr_probe(self) -> bool:
        """Lazy one-time OCR capability probe per run (thread-safe): helper
        present? else compile-on-demand once (never silent)."""
        if self._ocr_ready is None:
            with self._ocr_lock:
                if self._ocr_ready is None:
                    if not bool(getattr(self.cfg, "ocr", None) and self.cfg.ocr.enabled):
                        self._ocr_ready = False
                        self._ocr_unavailable_reason = "disabled (ocr.enabled=false)"
                    else:
                        _path, note = ocr_mod.ensure_helper(self.cfg)
                        self._ocr_ready = _path is not None
                        self._ocr_unavailable_reason = note
                    logger.info("ocr: %s", "ready" if self._ocr_ready
                                else f"unavailable ({self._ocr_unavailable_reason})")
        return self._ocr_ready

    def _ocr_screenshot(self, path: Path, *, details=None) -> tuple[str | None, float | None, str]:
        """OCR one screenshot -> (text, ms, status). status: "ok" (text may
        still be None), "no-text" (ran, nothing readable), "off" (helper
        unavailable/disabled), "error" (per-image failure — never a failed
        row, the image path stands on its own).

        02 F7: requests JOIN a shared batch drained by one leader thread into
        ONE helper spawn, instead of one spawn per screenshot (~0.15 s spawn
        overhead and a Vision cold start each, up to `workers` concurrent
        ~100 MB Vision hosts). Per-image errors stay isolated: the helper
        answers per image even in a batch, and a batch-level failure marks
        every member "off" (which the redo marker machinery can recover)."""
        details = {} if details is None else details
        if not self._ocr_probe():
            details["reason"] = self._ocr_unavailable_reason or "helper unavailable"
            return None, None, "off"
        req = _OcrRequest(path)
        with self._ocr_batch_lock:
            self._ocr_batch.append(req)
            leader = not self._ocr_batch_leader
            self._ocr_batch_leader = True
        logger.debug("ocr: queued %s (leader=%s)", path.name, leader)
        if leader:
            self._drain_ocr_batch()
        # Wait while SOME leader is alive — a batch that keeps growing runs
        # longer than any per-request bound the waiter could guess (each drain
        # is bounded by recognize()'s own subprocess timeout). Bail "off" only
        # when the request is ORPHANED (no leader, no answer — possible only
        # if a leader died between pulls); the honest off-marker stays
        # re-doable via _needs_ocr_redo.
        while not req.done.wait(_OCR_BATCH_POLL_S * 20):
            with self._ocr_batch_lock:
                orphaned = not self._ocr_batch_leader
            if orphaned:
                break
        if not req.done.is_set():
            logger.warning("ocr: batch leader lost before %s was answered",
                           path.name)
            details["reason"] = "OCR batch leader lost before answer"
            return None, None, "error"
        if req.failed is not None:
            details["reason"] = req.failed[:512]
            return None, None, req.failed_status
        page = req.page
        if page is None:
            details["reason"] = "OCR helper produced no answer for this image"
            return None, None, "error"  # helper produced no answer for this id
        if page.error is not None:
            logger.debug("ocr: %s on %s", page.error, path.name)
            details["reason"] = str(page.error)[:512]
            return None, page.ms, "error"
        text = page.text()
        if text.strip():
            return text, page.ms, "ok"
        if any(line.text.strip() for line in page.lines):
            return None, page.ms, "low-confidence"
        return None, page.ms, "no-text"

    def _drain_ocr_batch(self) -> None:
        """Leader loop: gather same-wave arrivals, then one `recognize` spawn
        per drain until the queue is empty. Runs on the leader's own
        extraction thread. Leadership is released under the queue lock exactly
        when an empty queue is observed, so an entry appended concurrently
        either joins this drain (appended before the check) or becomes the
        next leader (after)."""
        try:
            self._gather_ocr_batch()
            while True:
                with self._ocr_batch_lock:
                    batch, self._ocr_batch = self._ocr_batch, []
                    if not batch:
                        self._ocr_batch_leader = False
                        return
                logger.debug("ocr: draining batch of %d", len(batch))
                try:
                    self._metrics.call("ocr_calls")
                    self._metrics.call("ocr_inputs", len(batch))
                    results = ocr_mod.recognize(
                        [r.path for r in batch], self.cfg,
                    )
                except (ocr_mod.OcrUnavailable, ocr_mod.OcrTimeout) as exc:
                    logger.warning("ocr: batch of %d unavailable: %s",
                                   len(batch), exc)
                    for r in batch:
                        r.failed = str(exc)
                        r.failed_status = "error" if isinstance(exc, ocr_mod.OcrTimeout) else "off"
                        r.done.set()
                    continue  # later arrivals still deserve their own try
                for i, r in enumerate(batch):
                    r.page = results.get(str(i))
                    r.done.set()
        finally:
            # exception safety only (the empty-queue exit clears it above)
            with self._ocr_batch_lock:
                self._ocr_batch_leader = False

    def _gather_ocr_batch(self) -> None:
        """Wait for same-wave screenshots to join the queue: the window closes
        after `_OCR_BATCH_GATHER_S` of NO GROWTH (quiescence), or at the
        absolute cap — a fixed-width window races stragglers."""
        deadline = time.monotonic() + _OCR_BATCH_GATHER_MAX_S
        seen = len(self._ocr_batch)
        quiet_since = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(_OCR_BATCH_POLL_S)
            with self._ocr_batch_lock:
                n = len(self._ocr_batch)
            if n != seen:
                seen = n
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= _OCR_BATCH_GATHER_S:
                return

    def _ocr_pages(self, path: Path, pages: list[int], tmp: Path, *, states=None) -> dict[int, tuple[str, float | None]]:
        """Thin-page OCR batch (02 §5.2 Scope B): render each page via
        pdftoppm (150 dpi, 1400 px — DigUp renderPixels), OCR all renders in
        ONE helper spawn, return {page: (text, ms)}. Empty on any
        unavailability (never a failed file)."""
        states = {} if states is None else states
        states.update({page: "unavailable" for page in pages})
        if not pages or not self._ocr_probe():
            return {}
        if extract_pdf.pdftoppm_path() is None:
            return {}
        renders: dict[int, Path] = {}
        for page_no in pages:
            try:
                renders[page_no] = extract_pdf.render_page_png(
                    path, page_no, tmp / f"page-{page_no}",
                )
            except (ToolError, RuntimeError) as exc:
                states[page_no] = "error"
                logger.warning("ocr: page %s render failed for %s: %s", page_no, path.name, exc)
        if not renders:
            return {}
        try:
            ordered = sorted(renders)
            self._metrics.call("ocr_calls")
            self._metrics.call("ocr_inputs", len(ordered))
            results = ocr_mod.recognize(
                [renders[p] for p in ordered], self.cfg,
            )
        except (ocr_mod.OcrUnavailable, ocr_mod.OcrTimeout) as exc:
            logger.warning("ocr: %s on %s", exc, path.name)
            return {}
        out: dict[int, tuple[str, float | None]] = {}
        for idx, page_no in enumerate(ordered):
            page = results.get(str(idx))
            if page is None or page.error:
                states[page_no] = "error"
                continue
            text = page.text()
            if text.strip():
                states[page_no] = "complete"
                out[page_no] = (text, page.ms)
            else:
                states[page_no] = "low_confidence" if any(line.text.strip() for line in page.lines) else "no_text"
        return out

    def _media_tmp_dir(self) -> Path:
        """Scratch dir owned by the run: thumbnails must survive from worker
        extraction into main-thread commit (one wave later)."""
        with self._media_tmp_lock:
            if self._media_tmp is None:
                self._media_tmp = tempfile.TemporaryDirectory(
                    prefix="goldmine-media-tmp-"
                )
        return Path(self._media_tmp.name)

    def _close_media_tmp(self) -> None:
        if self._media_tmp is not None:
            self._media_tmp.cleanup()
            self._media_tmp = None

    def _extract_doc(self, path, ext, override, max_chunks, kind: str = "doc") -> _Outcome:
        if ext in ("txt", "text", "rst"):
            ex = extract_txt(path, override)
        elif ext in ("md", "markdown"):
            ex = extract_markdown(path, override)
        elif ext in ("html", "htm"):
            ex = extract_html_file(path, override)
        elif ext == "docx":
            ex = extract_docx(path)
        elif ext == "eml":
            ex = extract_eml(path)
        elif ext == "epub":
            ex = extract_epub(path)
        elif ext in TEXTUTIL_FORMATS:
            ex = extract_via_textutil(path, TEXTUTIL_FORMATS[ext])
            if ex is None:
                return _Outcome(capability="metadata_only", detail="textutil not found")
        else:  # pragma: no cover - walk only dispatches known doc exts
            return _Outcome(capability="metadata_only", detail=f"unknown doc type .{ext}")
        if not ex.text.strip():
            return _Outcome(
                capability="metadata_only",
                detail="no text extracted",
                info={**dict(ex.info or {}), **({"decode": ex.detail} if ex.detail else {})},
                extractor_version=ex.tool_version,
                title=ex.title,
            )
        chunks = self._chunk_text(ex.text, kind, max_chunks)
        partial = len(chunks) >= max_chunks
        info: dict = dict(ex.info or {})
        if ex.detail:
            info["decode"] = ex.detail
        if partial:
            info["partial"] = True
        return _Outcome(
            capability="content_indexed",
            detail=ex.detail,
            info=info,
            extractor_version=ex.tool_version,
            title=ex.title,
            chunks=[_Piece(c, _has_words(c.text)) for c in chunks],
            partial=partial,
        )

    def _extract_pdf(self, path, max_chunks) -> _Outcome:
        from .pdf_evidence import extract_pdf_outcome

        return extract_pdf_outcome(self, path, max_chunks)
