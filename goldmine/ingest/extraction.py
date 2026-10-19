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


@dataclass
class _MediaSeg:
    """One media segment awaiting embedding (extracted in the worker pool,
    embedded on the main thread in _commit — ARCHITECTURE §7.1 keeps all
    vector production off the extraction threads)."""

    kind: str                        # 'image' | 'audio' | 'ocr' (segment kind)
    path: Path | None = None         # image: the thumbnail to embed (v2 sips prep)
    wav: bytes | None = None         # audio: one 16 kHz mono PCM16 window
    loc: float | None = None         # audio: window start/end seconds; image: NULL
    loc_end: float | None = None
    excerpt: str | None = None       # image: filename; audio: short window label
    text_for_fts: str | None = None  # ocr: recognized text (always FTS-indexed)
    embed_text: bool = False         # ocr under a text-only backend: text vector (A5)


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
            )

        # audio / video: the soundtrack is the only embeddable content.
        # v2 ASR (02 §5.1) runs FIRST: a successful transcript becomes TEXT —
        # FTS + prompted vectors under ANY text-capable backend (llama
        # included). Raw-audio windows remain only for non-speech or when
        # whisper is missing/failing (ingest.asr = auto|always|off).
        asr_outcome = self._try_asr(cand, cap)
        if asr_outcome is not None:
            return asr_outcome

        if cap is None or not cap.audio:
            return _Outcome(capability="metadata_only", detail="backend lacks audio embedding")
        if media.ffprobe_path() is None:
            return _Outcome(capability="metadata_only", detail="ffprobe not found")
        duration = media.ffprobe_duration(path)
        if duration is None:
            return _Outcome(
                capability="metadata_only",
                detail="ffprobe could not read duration",
            )
        if kind == "video" and media.has_audio_stream(path) is False:
            return _Outcome(capability="metadata_only", detail="no audio stream")

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
                    )
                    for piece in pieces
                ]
        except ToolError:
            return _Outcome(capability="metadata_only", detail="ffmpeg not found")
        # per-window ffmpeg failures/timeout are extraction errors => failed row
        # (let RuntimeError propagate into _extract's handler above)

        info: dict = {"duration": round(duration, 3), "windows": len(segs)}
        partial = duration > max_minutes * 60.0
        if partial:
            info["read_end"] = max_minutes * 60.0
        if not segs:
            return _Outcome(
                capability="metadata_only",
                detail="all audio windows silent (RMS gate)",
                info=info,
            )
        return _Outcome(
            capability="content_indexed",
            info=info,
            extractor_version="media-ffmpeg.v1",
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
            if mode == "always":
                return _Outcome(
                    capability="metadata_only",
                    detail=(
                        "ASR unavailable (whisper-cli or models missing) — "
                        "`goldmine model install whisper-large-v3-turbo-q5_0 "
                        "silero-vad-v5.1.2`"
                    ),
                )
            return None  # auto: raw windows (multimodal) / metadata (text-only)
        try:
            result = asr_mod.transcribe(cand.path, self.cfg)
        except asr_mod.AsrError as exc:
            if mode == "always":
                return _Outcome(capability="failed", detail=f"asr failed: {exc}")
            return None  # auto: honest fallback; raw windows still index it

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
            if mode == "always":
                return _Outcome(
                    capability="metadata_only", detail="no speech detected (ASR)",
                    info=info or None,
                )
            return None  # auto: probably music -> raw-audio embedding path

        pieces = [
            _Piece(
                Chunk(seg.text, seg.start_s, seg.end_s),
                _has_words(seg.text),
                kind="audio",
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
                    info=info or None,
                )
        info["asr_segments"] = len(result.segments)
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
        info: dict = {"screenshot": True} if is_shot else {}
        detail: str | None = None

        ocr_text, ocr_ms, ocr_status = (
            self._ocr_screenshot(path) if is_shot else (None, None, "skipped")
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

        want_image_vec = cap is not None and cap.image and not self.lexical_only
        segs: list[_MediaSeg] = []
        content_date: float | None = None
        extractor = "image-sips.v2"

        if want_image_vec:
            # full v2 prep: EXIF + thumbnail + minSide/cap refusals
            try:
                prep = media.prepare_image(
                    path, self._media_tmp_dir(), screenshot=is_shot,
                )
            except ToolError:
                return _Outcome(
                    capability="metadata_only",
                    detail="sips not found (image thumbnails)",
                    info=info,
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
                        extractor_version="image-sips-ocr.v2",
                        media_segs=segs,
                    )
                return _Outcome(
                    capability="metadata_only",
                    detail=f"sips could not decode: {str(exc)[:120]}",
                    info=info,
                )
            if prep.taken_epoch is not None:
                content_date = prep.taken_epoch
                info["taken"] = prep.taken
            if prep.refused is not None:
                if ocr_text:
                    # the pixels are un-embeddable (minSide / pathological
                    # thumbnail) but the OCR text carries the file honestly
                    segs.append(self._ocr_seg(path, ocr_text, cap))
                    return _Outcome(
                        capability="content_indexed",
                        detail=f"{prep.refused}; ocr text only",
                        info=info,
                        extractor_version="image-sips-ocr.v2",
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
            segs.append(_MediaSeg(kind="image", path=prep.path, excerpt=path.name))
            info["thumb"] = f"{prep.width}x{prep.height}"
        elif media.sips_path() is not None:
            # no image vector wanted, but EXIF is backend-independent metadata
            taken = media.exif_taken(path)
            if taken is not None:
                content_date = taken[1]
                info["taken"] = taken[0]

        if ocr_text:
            segs.append(self._ocr_seg(path, ocr_text, cap))
            extractor = "image-sips-ocr.v2"

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
        exists (foresight/multimodal) the words stay keyword-only — DigUp
        measured a text vector making screenshots outrank better matches."""
        embed_text = (
            (cap is None or not cap.image)
            and bool(self.cfg.ingest.ocr_text_vector)
            and not self.lexical_only
        )
        return _MediaSeg(
            kind="ocr",
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

    def _ocr_screenshot(self, path: Path) -> tuple[str | None, float | None, str]:
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
        if not self._ocr_probe():
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
            return None, None, "off"
        if req.failed is not None:
            return None, None, "off"
        page = req.page
        if page is None:
            return None, None, "error"  # helper produced no answer for this id
        if page.error is not None:
            logger.debug("ocr: %s on %s", page.error, path.name)
            return None, page.ms, "error"
        text = page.text()
        if text.strip():
            return text, page.ms, "ok"
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
                    results = ocr_mod.recognize(
                        [r.path for r in batch], self.cfg,
                    )
                except (ocr_mod.OcrUnavailable, ocr_mod.OcrTimeout) as exc:
                    logger.warning("ocr: batch of %d unavailable: %s",
                                   len(batch), exc)
                    for r in batch:
                        r.failed = str(exc)
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

    def _ocr_pages(self, path: Path, pages: list[int], tmp: Path) -> dict[int, tuple[str, float | None]]:
        """Thin-page OCR batch (02 §5.2 Scope B): render each page via
        pdftoppm (150 dpi, 1400 px — DigUp renderPixels), OCR all renders in
        ONE helper spawn, return {page: (text, ms)}. Empty on any
        unavailability (never a failed file)."""
        if not pages or not self._ocr_probe():
            return {}
        if extract_pdf.pdftoppm_path() is None:
            return {}
        renders: dict[int, Path] = {}
        try:
            for page_no in pages:
                renders[page_no] = extract_pdf.render_page_png(
                    path, page_no, tmp / f"page-{page_no}",
                )
        except (ToolError, RuntimeError) as exc:
            logger.warning("ocr: page render failed for %s: %s", path.name, exc)
            return {}
        try:
            ordered = sorted(renders)
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
                continue
            text = page.text()
            if text.strip():
                out[page_no] = (text, page.ms)
        return out

    def _media_tmp_dir(self) -> Path:
        """Scratch dir owned by the run: thumbnails must survive from worker
        extraction into main-thread commit (one wave later)."""
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
        if ext in ("txt", "text"):
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
        """pdftotext extraction (agent B, build pass 5) + the v2 thin-page OCR
        upgrade (02 §5.2 Scope B): pages with < 200 chars (including empty
        scanned pages) are rendered via pdftoppm and OCR'd when the helper is
        available; upgraded pages become normal text chunks (FTS + vector
        under BOTH backends — the vector is the page text's). Pages that stay
        thin keep the honest FTS-only no-vector row; a PDF with no text layer
        at all becomes content_indexed once every page OCRs."""
        try:
            result = extract_pdf.extract_pages(path, max_pages=self.cfg.ingest.max_pages)
        except ToolError:
            return _Outcome(
                capability="metadata_only",
                detail="pdftotext not found",
            )
        except RuntimeError as exc:
            return _Outcome(capability="failed", detail=str(exc))

        # candidates for OCR: every page below the thin threshold, INCLUDING
        # empty ones (a scanned page extracts as ""), capped like text reads.
        ocr_candidates = [
            number for number, text in result.pages
            if len(text.strip()) < extract_pdf.THIN_PAGE_CHARS
        ]
        ocr_texts: dict[int, str] = {}
        if ocr_candidates and self._ocr_probe():
            with tempfile.TemporaryDirectory(prefix="goldmine-pdfocr-") as td:
                ocr_texts = {
                    page: text
                    for page, (text, _ms) in self._ocr_pages(
                        path, ocr_candidates, Path(td)
                    ).items()
                }

        pieces: list[_Piece] = []
        thin_pages: list[int] = []
        ocr_pages: list[int] = []
        for number, text in result.pages:
            stripped = text.strip()
            if len(stripped) < extract_pdf.THIN_PAGE_CHARS:
                upgraded = ocr_texts.get(number, "").strip()
                if len(upgraded) >= extract_pdf.THIN_PAGE_CHARS:
                    # OCR recovered a full page: normal chunks, vector-eligible
                    ocr_pages.append(number)
                    for chunk in self._chunk_text(upgraded, "pdf", max_chunks):
                        pieces.append(_Piece(chunk, _has_words(chunk.text)))
                    continue
                if not stripped and not upgraded:
                    continue  # nothing at all on this page (v1 behavior)
                # honest thin page: whatever text exists, FTS-only, no vector
                thin_pages.append(number)
                pieces.append(_Piece(
                    Chunk("\n".join(t for t in (stripped, upgraded) if t),
                          number, number), False,
                    kind="page", excerpt_override="",
                ))
                continue
            for chunk in self._chunk_text(stripped, "pdf", max_chunks):
                pieces.append(_Piece(chunk, _has_words(chunk.text)))

        info: dict = {"pages": result.total}
        if result.truncated:
            info["partial"] = True
            info["read_to"] = result.read_to
            info["read_end"] = result.read_end
        if ocr_pages:
            info["ocr_pages"] = ocr_pages
        if thin_pages:
            info["thin_text"] = True
            info["thin_pages"] = thin_pages

        tool_version = result.tool_version
        if ocr_pages:
            # records the OCR pass so the G10 staleness / --refresh-thin
            # machinery can find and re-do these rows (02 §5.2)
            tool_version = tool_version.replace(
                "pdftotext@", "pdftotext+visionocr@", 1
            )
            detail = f"ocr: {len(ocr_pages)} page(s)"
            if thin_pages:
                detail += f"; thin-text: {len(thin_pages)} page(s) need OCR (scanned)"
        elif thin_pages:
            detail = f"thin-text: {len(thin_pages)} page(s) need OCR (scanned)"
        else:
            detail = None

        if not pieces:
            return _Outcome(
                capability="metadata_only",
                detail=(
                    "page needs OCR (scanned)" if result.pages else "no text extracted"
                ),
                info=info,
                extractor_version=tool_version,
                title=result.title,
            )
        return _Outcome(
            capability="content_indexed",
            detail=detail,
            info=info,
            extractor_version=tool_version,
            title=result.title,
            chunks=pieces,
            partial=result.truncated,
        )
