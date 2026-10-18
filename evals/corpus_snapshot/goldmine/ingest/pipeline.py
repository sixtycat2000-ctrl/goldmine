"""Ingest orchestration: walk -> classify -> extract -> chunk -> (embed|lexical-only)
-> store -> FTS. One transaction per file (atomic segment replacement), capability
matrix assignment for every scanned file (V5a), content-hash skip, deletion pruning,
and an honest IngestReport matching SPEC §2.
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .. import fingerprints
from .. import ocr as ocr_mod
from ..backends.base import EmbeddingBackend, EmbeddingError, document_prompt
from ..chunker import (
    CHUNKER_VERSION,
    Chunk,
    PROMPT_FRAMING_TOKENS,
    TOKEN_SLACK,
    chunk_code_tokens,
    chunk_text_tokens,
    effective_token_budget,
    split_text_by_tokens,
)
from ..config import Config
from ..store import FileRec, SegmentRec, Store, _folder_words
from ..util import ToolError, excerpt, human_size, sha256_file
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
class IngestReport:
    roots: list[str] = field(default_factory=list)
    scanned: int = 0
    content_indexed: int = 0
    metadata_only: int = 0
    unsupported: int = 0
    failed: int = 0
    unchanged: int = 0
    deleted: int = 0
    partial: int = 0
    by_kind: dict[str, dict[str, int]] = field(default_factory=dict)
    failures: list[dict] = field(default_factory=list)          # [{"path","detail"}]
    metadata_reasons: list[dict] = field(default_factory=list)  # [{"reason","count"}]
    unsupported_reasons: list[dict] = field(default_factory=list)
    vectors: int = 0
    segments: int = 0
    fingerprint: dict | None = None
    notices: list[dict] = field(default_factory=list)
    dry_run: bool = False
    cancelled: bool = False

    def note(self, code: str, message: str) -> None:
        self.notices.append({"code": code, "message": message})

    def to_json(self) -> dict:
        return {
            "roots": self.roots,
            "files": {
                "scanned": self.scanned,
                "content_indexed": self.content_indexed,
                "metadata_only": self.metadata_only,
                "unsupported": self.unsupported,
                "failed": self.failed,
                "unchanged": self.unchanged,
                "deleted": self.deleted,
                "partial": self.partial,
            },
            "by_kind": self.by_kind,
            "failures": self.failures,
            "metadata_reasons": self.metadata_reasons,
            "unsupported_reasons": self.unsupported_reasons,
            "vectors": self.vectors,
            "segments": self.segments,
            "fingerprint": self.fingerprint,
            "notices": self.notices,
            "dry_run": self.dry_run,
        }


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


def _has_words(text: str) -> bool:
    """Digits-only text loses its vector (DigUp SearchText.hasWords port):
    it competes lexically only, or a number dump would sink unrelated matches."""
    return any(ch.isalpha() for ch in text)


def _thin_marker(rec) -> bool:
    """--refresh-thin selection (02 §5.2): a stored row that carries thin-page
    state — info.thin_text (pages that STAYED thin) or the scanned-PDF
    metadata_only detail. ocr_pages-only rows are current; a better future
    OCR is an extractor-version bump, handled by the G10 path instead."""
    if rec.detail and "page needs OCR" in rec.detail:
        return True
    return bool(rec.info and rec.info.get("thin_text"))


def _needs_ocr_redo(rec) -> bool:
    """A screenshot that was indexed metadata_only by a text-only backend
    before OCR was available (detail 'backend lacks image embedding',
    screenshot flag, OCR marker absent or 'unavailable'): plain ingest re-does
    it once OCR is ready so it gains its text (A5 upgrade path). Rows marked
    'no text' or with ocr_chars never re-do — no churn."""
    if rec.kind != "image" or rec.capability != "metadata_only":
        return False
    if rec.detail != "backend lacks image embedding":
        return False
    if not rec.info or not rec.info.get("screenshot"):
        return False
    marker = rec.info.get("ocr")
    if marker is None:
        return True   # v1-era row, OCR never attempted
    return marker == "unavailable"


class IngestPipeline:
    def __init__(
        self,
        cfg: Config,
        store: Store | None,
        backend: EmbeddingBackend | None,
        lexical_only: bool = False,
    ):
        if backend is None and not lexical_only:
            # SPEC §2: an unavailable backend is a refusal unless --lexical-only
            # was given explicitly — never a silent keyword-only index.
            from ..backends import BackendUnavailable

            raise BackendUnavailable(
                "no embedding backend available for ingest",
                fix_hint="configure a backend, or pass --lexical-only to build a "
                "keyword-only index explicitly",
            )
        self.cfg = cfg
        self.store = store
        self.backend = backend
        self.lexical_only = lexical_only or backend is None
        self.cancelled = False
        # v2 per-file identity (05 §4.2): the chunker constant is code state;
        # the live embed identity id is filled in run() once the backend has
        # started (its fingerprint fields are only fully known then).
        self._chunker_version = CHUNKER_VERSION
        self._embed_identity: str | None = None
        # F1: the backend-derived identity dict (live_embed_identity) for
        # _finalize's meta bookkeeping — re-deriving from the fingerprint dict
        # would parse the legacy prompts scheme, which foresight's has no
        # d-slot for.
        self._embed_identity_dict: dict | None = None
        # v2 OCR (B4): lazy one-time helper probe shared by screenshots and
        # thin PDF pages ("unknown" -> probed on first need, cached per run;
        # never silent — ensure_helper logs/notes every outcome).
        self._ocr_ready: bool | None = None
        self._ocr_unavailable_reason: str | None = None
        self._ocr_lock = threading.Lock()
        # v2 image prep (B5): thumbnails must outlive extraction (they embed
        # in _commit, one wave later), so the run owns one scratch dir.
        self._media_tmp: tempfile.TemporaryDirectory | None = None
        # G10: current extractor-version set, computed lazily once per run
        # (tool probing is not free).
        self._current_extractors: set[str] | None = None

    # ------------------------------------------------------------------ run

    def run(
        self,
        roots: list[str],
        *,
        code: bool = False,
        exclude: list[str] | None = None,
        dry_run: bool = False,
        reindex: bool = False,
        refresh_thin: bool = False,
        crawl_bounds: object | None = None,
        encoding_override: str | None = None,
        include_datasets: bool = False,
        progress=None,
    ) -> IngestReport:
        exclude = list(exclude or [])
        # P2-11: one absolute, CWD-independent form for everything stored or
        # compared (file rows, roots_json, prune comparisons). URLs stay verbatim.
        # F10 (phantom roots): each file root must stat — a root that does not
        # exist (or cannot be stat'ed) becomes a per-root FAILURE (exit-5
        # partial semantics via the standard failures channel) and is dropped
        # from report.roots, so it is NEVER persisted into meta roots_json
        # (a typo must not silently become index scope). URLs are exempt:
        # the crawler owns their error semantics.
        file_roots = []
        root_errors: list[tuple[str, str]] = []
        for r in roots:
            if r.startswith(("http://", "https://")):
                continue
            resolved = str(Path(r).expanduser().resolve())
            try:
                Path(resolved).stat()
            except OSError as exc:
                root_errors.append((resolved, str(exc)))
            else:
                file_roots.append(resolved)
        url_roots = [r for r in roots if r.startswith(("http://", "https://"))]
        resolved_roots = [*file_roots, *url_roots]
        report = IngestReport(roots=resolved_roots, dry_run=dry_run)
        for root_path, reason in root_errors:
            report.failed += 1
            report.failures.append({
                "path": root_path,
                "detail": f"root not accessible: {reason}",
            })
        if root_errors:
            report.note(
                "phantom_roots",
                f"{len(root_errors)} root(s) not accessible — not indexed, not "
                "recorded among the index roots",
            )

        if dry_run:
            return self._dry_run(
                resolved_roots, code=code, exclude=exclude, report=report
            )

        assert self.store is not None, "pipeline needs a Store unless dry_run"
        max_file_bytes = int(self.cfg.ingest.max_file_mb) * (1 << 20)
        max_chunks = self.cfg.ingest.max_chunks
        batch = max(1, int(self.cfg.llama.batch))

        live_fp = None
        try:
            if self.backend is not None and not self.lexical_only:
                # startup FIRST: the foresight fingerprint's identity slot is the
                # helper's Ping().version, which is only known after the ping (a
                # pre-startup build recorded "helper@unknown"). For llama startup
                # is a spawn; the fingerprint itself reads the model file.
                self.backend.startup()
                live_fp = fingerprints.build(self.backend)
                self._embed_identity = fingerprints.live_embed_id(self.backend)
                self._embed_identity_dict = fingerprints.live_embed_identity(
                    self.backend
                )
                # Refusal gate: never mix vectors across fingerprints (V2 CRITICAL).
                fingerprints.gate(self.store.meta, live_fp)
                if reindex:
                    # P1-3: clear only AFTER startup + the fingerprint gate
                    # passed — a failed startup must not leave a hollow index
                    # that the content-hash skip would never rebuild.
                    self._clear_vectors()
                # llama.cpp build string is provenance, NOT fingerprint (D2): stored
                # in meta; a change produces a build_changed notice, not a refusal.
                build_string = getattr(self.backend, "build_string", None)
                if build_string and self.store is not None:
                    previous = self.store.meta("llama_build")
                    if previous and previous != build_string:
                        report.note(
                            "build_changed",
                            f"llama.cpp build changed (recorded: {previous}, "
                            f"installed: {build_string}) — reindex recommended",
                        )
                    self.store.set_meta("llama_build", build_string)
            elif reindex:
                # lexical-only reindex: no backend, no gate — clear directly.
                self._clear_vectors()

            candidates: list[walk.Candidate] = []
            for item in walk.walk_files(
                [Path(r) for r in file_roots],
                code=code,
                exclude=exclude,
                max_file_bytes=max_file_bytes,
            ):
                if isinstance(item, walk.Candidate):
                    candidates.append(item)
                elif item.is_dir:
                    pass  # folder-level skip: counted only, never a file row
                else:
                    self._record_unsupported(self.store, item.path, item.reason, report)

            if not include_datasets:
                # P2-9: --include-datasets skips dataset-folder detection.
                self._drop_dataset_folders(candidates, file_roots, report)

            report.scanned += len(candidates)
            wave = max(int(self.cfg.ingest.workers) * 8, 16)
            done = 0
            pool = ThreadPoolExecutor(max_workers=max(1, int(self.cfg.ingest.workers)))
            try:
                for wave_start in range(0, len(candidates), wave):
                    if self.cancelled:
                        break
                    wave_items = candidates[wave_start:wave_start + wave]
                    futures = [
                        pool.submit(
                            self._extract, cand,
                            encoding_override=encoding_override, max_chunks=max_chunks,
                        )
                        for cand in wave_items
                    ]
                    hashes = [sha256_file(c.path) for c in wave_items]
                    for cand, future, sha in zip(wave_items, futures, hashes):
                        if self._skip_unchanged(cand, sha, reindex,
                                                 refresh_thin=refresh_thin):
                            report.unchanged += 1
                            done += 1
                            continue
                        try:
                            outcome = future.result()
                        except Exception as exc:  # extraction crash => failed row
                            outcome = _Outcome(
                                capability="failed",
                                detail=f"{type(exc).__name__}: {exc}",
                            )
                        try:
                            self._commit(
                                cand, sha, outcome, report,
                                batch=batch,
                                embed=None if self.lexical_only else self.backend,
                            )
                        except EmbeddingError as exc:
                            self._record_failed(
                                self.store, str(cand.path),
                                f"embedding failed: {exc}", report,
                            )
                            raise
                        done += 1
                        if progress is not None and (
                            done % 20 == 0 or done == len(candidates)
                        ):
                            progress(
                                f"ingest: {done}/{len(candidates)} files, "
                                f"{report.failed} failed"
                            )
            except KeyboardInterrupt:
                self.cancelled = True
                report.cancelled = True
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

            # URLs: crawl.py (build pass 5) drives link-following; until it lands
            # this build records URLs honestly instead of pretending.
            for url in url_roots:
                try:
                    self._ingest_urls([url], crawl_bounds, report)
                except ImportError:
                    self._record_url_unimplemented(url, report)

            # Deletions: files under a root that vanished since the last ingest.
            gone = walk.compute_deletions(self.store, resolved_roots)
            for fid in gone:
                self.store.delete_file(fid)
            report.deleted = len(gone)
        finally:
            if self.backend is not None:
                self.backend.close()
            self._close_media_tmp()

        self._finalize(report, live_fp)
        return report

    # ------------------------------------------------------------- dry run

    def _dry_run(self, roots, *, code, exclude, report) -> IngestReport:
        from ..util import human_size

        max_file_bytes = int(self.cfg.ingest.max_file_mb) * (1 << 20)
        kinds: dict[str, int] = {}
        file_skips: dict[str, int] = {}
        dir_skips: dict[str, int] = {}
        for item in walk.walk_files(
            [Path(r) for r in roots if not r.startswith(("http://", "https://"))],
            code=code, exclude=exclude, max_file_bytes=max_file_bytes,
        ):
            if isinstance(item, walk.Candidate):
                kinds[item.kind] = kinds.get(item.kind, 0) + 1
            elif item.is_dir:
                dir_skips[item.reason] = dir_skips.get(item.reason, 0) + 1
            else:
                file_skips[item.reason] = file_skips.get(item.reason, 0) + 1
        report.scanned = sum(kinds.values()) + sum(file_skips.values())
        # preview: text kinds would be content-indexed (with a backend); media
        # kinds follow the backend's declared image/audio capability — and
        # speech becomes text via ASR when whisper + models are present
        # (02 §5.1: transcript vectors under ANY text-capable backend)
        image_ok = audio_ok = False
        if self.backend is not None and not self.lexical_only:
            cap = self.backend.capability()
            image_ok, audio_ok = cap.image, cap.audio
            if not audio_ok and cap.text and str(
                getattr(self.cfg.ingest, "asr", "auto") or "auto"
            ) != "off":
                from . import asr as asr_mod

                audio_ok = asr_mod.resolve_tools(self.cfg) is not None
        for kind in ("doc", "pdf", "webpage", "code"):
            if kinds.get(kind):
                report.by_kind.setdefault(kind, {})["content_indexed"] = kinds[kind]
        for kind, ok in (("image", image_ok), ("audio", audio_ok), ("video", audio_ok)):
            if kinds.get(kind):
                report.by_kind.setdefault(kind, {})[
                    "content_indexed" if ok else "metadata_only"
                ] = kinds[kind]
        report.content_indexed = sum(kinds.get(k, 0) for k in ("doc", "pdf", "webpage", "code"))
        report.content_indexed += sum(
            kinds.get(k, 0) for k, ok in (("image", image_ok), ("audio", audio_ok), ("video", audio_ok)) if ok
        )
        report.metadata_only = sum(
            kinds.get(k, 0)
            for k, ok in (("image", image_ok), ("audio", audio_ok), ("video", audio_ok))
            if not ok
        )
        report.unsupported = sum(file_skips.values())
        for reason, n in sorted(file_skips.items(), key=lambda kv: (-kv[1], kv[0])):
            report.unsupported_reasons.append({"reason": reason, "count": n})
        report.note(
            "dry_run",
            "classify only — no DB writes, no backend, no tool spawns",
        )
        report.note(
            "cap_impact",
            f"per-file cap {human_size(max_file_bytes)}, "
            f"code cap {human_size(CODE_HARD_CAP_BYTES)}, "
            f"chunk cap {self.cfg.ingest.max_chunks}",
        )
        if dir_skips:
            tops = ", ".join(f"{r} {n}" for r, n in sorted(dir_skips.items(), key=lambda kv: -kv[1])[:5])
            report.note("folders_skipped", tops)
        return report

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
        row, the image path stands on its own)."""
        if not self._ocr_probe():
            return None, None, "off"
        try:
            page = ocr_mod.recognize_one(path, self.cfg)
        except (ocr_mod.OcrUnavailable, ocr_mod.OcrTimeout) as exc:
            logger.warning("ocr: %s on %s", exc, path.name)
            return None, None, "off"
        if page.error is not None:
            logger.debug("ocr: %s on %s", page.error, path.name)
            return None, page.ms, "error"
        text = page.text()
        if text.strip():
            return text, page.ms, "ok"
        return None, page.ms, "no-text"

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

    # ---------------------------------------------------------------- store

    def _skip_unchanged(self, cand: walk.Candidate, sha: str, reindex: bool,
                        refresh_thin: bool = False) -> bool:
        """(size, mtime) prefilter passed on the walk; sha256 confirms here.

        v2 skip-fix family — THE plain-ingest re-do path (02 G10 + D3/B11;
        one shared check for the chunker and extractor dimensions):
        - a done content file whose chunker_version is stale is RE-DONE (D3);
        - a done content file whose extractor_version is outside the current
          registry is RE-DONE (G10: landing OCR/sips upgrades old rows on the
          next ordinary ingest, no flags, never a refusal);
        - refresh_thin additionally re-does files carrying thin-page markers
          (info.thin_text / detail 'page needs OCR') even with sha unchanged —
          targeted repair without a full pass (02 §5.2)."""
        if reindex:
            return False
        rec = self.store.find_by_path(str(cand.path))
        if rec is None or rec.state != "done":
            return False
        if rec.size != cand.size or rec.sha256 is None:
            return False
        if rec.mtime is not None and cand.mtime is not None and rec.mtime != cand.mtime:
            return False
        if rec.sha256 != sha:
            return False
        if rec.capability == "content_indexed":
            if rec.chunker_version != self._chunker_version:
                return False
            if rec.extractor_version not in self._current_extractor_set():
                return False
        if refresh_thin and _thin_marker(rec):
            return False
        if _needs_ocr_redo(rec) and self._ocr_probe():
            # a screenshot indexed under a text-only backend BEFORE OCR was
            # available can now gain text. The re-done row always carries an
            # ocr marker (ocr_chars or info.ocr), so this cannot churn.
            return False
        return True

    def _current_extractor_set(self) -> set[str]:
        """Current extractor versions, computed once per run (tool probes)."""
        if self._current_extractors is None:
            self._current_extractors = fingerprints.current_extractors()
        return self._current_extractors

    def _token_budget(self) -> int:
        """Token budget for the active backend's ceiling (D3): ~450 tokens,
        shrunk when the context is tighter than framing+slack+450."""
        backend = self.backend
        ceiling = backend.max_input_tokens() if backend is not None else None
        return effective_token_budget(ceiling)

    def _tokenize_fn(self):
        """Exact token pieces when the (started) backend offers them; None ->
        the chunker's kind-aware estimator path (mock/offline, C6)."""
        backend = self.backend
        if backend is None or self.lexical_only:
            return None
        if not backend.capability().text:
            return None
        return backend.token_pieces

    def _chunk_text(self, text: str, kind: str | None, max_chunks: int) -> list[Chunk]:
        """Token-budget chunking for prose-like kinds (D3 / 01 §5.5)."""
        return chunk_text_tokens(
            text,
            budget_tokens=self._token_budget(),
            tokenize=self._tokenize_fn(),
            kind=kind,
            max_chunks=max_chunks,
        )

    def _commit(
        self,
        cand: walk.Candidate,
        sha: str,
        outcome: _Outcome,
        report: IngestReport,
        *,
        batch: int,
        embed: EmbeddingBackend | None,
    ) -> None:
        store = self.store
        fid = store.upsert_file(
            FileRec(
                path=str(cand.path), source="file", kind=cand.kind,
                size=cand.size, mtime=cand.mtime, sha256=sha, state="pending",
            )
        )
        if outcome.capability == "failed":
            self._record_failed(store, str(cand.path), outcome.detail, report, fid=fid,
                                kind=cand.kind)
            return
        if outcome.capability == "unsupported":
            # keep the extractor's info (screenshot flag etc.) beside the
            # machine-stable reason — coverage/KPIs bucket on it
            info = dict(outcome.info or {})
            if outcome.detail:
                info["reason"] = outcome.detail
            store.mark_file(
                fid, state="done", capability="unsupported",
                detail=outcome.detail, info=info or None,
                extractor_version=outcome.extractor_version,
                chunker_version=self._chunker_version, embed_id=None,
            )
            self._tally(report, cand.kind, "unsupported")
            _merge_reasons(report.unsupported_reasons, outcome.detail or "unknown")
            return

        if outcome.media_segs:
            self._commit_media(cand, fid, outcome, report, embed=embed)
            return

        info = dict(outcome.info)

        if not outcome.chunks:
            store.mark_file(
                fid, state="done", capability="metadata_only",
                detail=outcome.detail or "no content",
                info=info or None, extractor_version=outcome.extractor_version,
                chunker_version=self._chunker_version, embed_id=None,
            )
            self._tally(report, cand.kind, "metadata_only")
            return

        # Paranoid ctx guard (D3, falsifiable claim 2): any chunk whose PROMPTED
        # token count still exceeds the ceiling is split at token boundaries —
        # SPLIT-not-fail. Token-true budgeting makes this structurally rare
        # (the 2670-token code / 82929-token single-line JSON 400 classes die
        # at the source); per-input 400 isolation below stays as the backstop.
        if embed is not None:
            title = outcome.title or Path(cand.path).stem
            outcome.chunks = self._ctx_guard_split(outcome.chunks, title)

        if outcome.partial:
            info["partial"] = True
            report.partial += 1

        segs: list[SegmentRec] = [
            SegmentRec(
                kind="name", modality="none",
                excerpt=Path(cand.path).name,
                text_for_fts=_folder_words(str(cand.path), "file"),
            )
        ]
        vec_chunks = [p for p in outcome.chunks if p.vec_ok]
        vectors: dict[int, np.ndarray] = {}
        if embed is not None and vec_chunks:
            title = outcome.title or Path(cand.path).stem
            prompts = [document_prompt(title, p.chunk.text) for p in vec_chunks]
            try:
                for i in range(0, len(prompts), batch):
                    part = embed.embed_texts(prompts[i:i + batch])
                    for j, vec in enumerate(part):
                        vectors[i + j] = vec
            except EmbeddingError as exc:
                # per-file failure, not a run abort: an oversized/rejected
                # input (or a backend that dies mid-run) fails THIS file and
                # the report carries it; other files still get indexed.
                self._record_failed(
                    store, str(cand.path), f"embedding failed: {exc}",
                    report, fid=fid, kind=cand.kind,
                )
                return
        # map (chunk, ok) list position -> vector position
        vec_pos = 0
        vec_index_of: dict[int, int] = {}
        for n, piece in enumerate(outcome.chunks):
            if piece.vec_ok:
                vec_index_of[n] = vec_pos
                vec_pos += 1

        for n, piece in enumerate(outcome.chunks):
            vec = vectors.get(vec_index_of.get(n, -1))
            segs.append(
                SegmentRec(
                    kind=piece.kind,
                    modality="text" if (piece.vec_ok and vec is not None) else "none",
                    loc=float(piece.chunk.loc) if piece.chunk.loc is not None else None,
                    loc_end=float(piece.chunk.loc_end) if piece.chunk.loc_end is not None else None,
                    excerpt=(
                        piece.excerpt_override
                        if piece.excerpt_override is not None
                        else excerpt(piece.chunk.text)
                    ),
                    text_for_fts=piece.chunk.text,
                    vec=vec,
                )
            )
        store.replace_segments(fid, segs)
        embedded = any(s.vec is not None for s in segs)
        store.mark_file(
            fid, state="done", capability="content_indexed",
            detail=outcome.detail, info=info or None,
            extractor_version=outcome.extractor_version,
            chunker_version=self._chunker_version,
            embed_id=self._embed_identity if embedded else None,
            content_date=outcome.content_date,
        )
        self._tally(report, cand.kind, "content_indexed")

    def _ctx_guard_split(self, pieces: list[_Piece], title: str | None) -> list[_Piece]:
        """Split over-ceiling chunks at token boundaries before embedding
        (SPLIT-not-fail, D3). Only embed-eligible pieces are guarded (a
        vectorless FTS row has no server ceiling).

        Token-true chunks (Chunk.tokens set by the aligned-pieces chunkers)
        are NOT counted: every chunk is capped at the budget by construction
        and effective_token_budget reserves framing+slack below the ceiling,
        so a prompted token-true chunk cannot exceed `ceiling` — unless the
        budget was floored above the true allowance by a tiny ceiling
        (< 128 tokens), where the guard stays. Estimator/char-legacy chunks
        (Chunk.tokens None: mock/offline, /tokenize failure, ASR transcript
        merges, hand-built pieces) keep the per-chunk count — 06-perf F2:
        the guard was 250 of 300 /tokenize calls per 50-file ingest."""
        backend = self.backend
        if backend is None or self.lexical_only or not pieces:
            return pieces
        ceiling = backend.max_input_tokens()
        if ceiling is None:
            return pieces  # no server ceiling to guard (mock/foresight)
        guardable = [i for i, p in enumerate(pieces) if p.vec_ok]
        if not guardable:
            return pieces
        if (self._token_budget() + PROMPT_FRAMING_TOKENS + TOKEN_SLACK
                <= int(ceiling)):
            guardable = [i for i in guardable if pieces[i].chunk.tokens is None]
            if not guardable:
                return pieces  # all budgeted in token space: nothing to count
        counts = backend.count_tokens(
            [document_prompt(title, pieces[i].chunk.text) for i in guardable]
        )
        if counts is None or len(counts) != len(guardable):
            return pieces  # counting unavailable: the budgeted chunks stand
        limit = int(ceiling) - 1
        oversized = [k for k, n in enumerate(counts) if n > limit]
        if not oversized:
            return pieces
        # framing = prompted tokens - bare-text tokens; each split part may
        # spend the ceiling minus that overhead
        frame_of: dict[int, int] = {}
        text_counts = backend.count_tokens(
            [pieces[guardable[k]].chunk.text for k in oversized]
        )
        if text_counts is not None and len(text_counts) == len(oversized):
            for k, bare in zip(oversized, text_counts):
                frame_of[k] = max(0, counts[k] - bare)
        tokenize = self._tokenize_fn()
        split_map: dict[int, list[Chunk]] = {}
        for k in oversized:
            allowed = max(1, limit - frame_of.get(k, 8))
            split_map[guardable[k]] = split_text_by_tokens(
                pieces[guardable[k]].chunk.text, allowed, tokenize=tokenize
            )
        out: list[_Piece] = []
        for i, piece in enumerate(pieces):
            if i in split_map:
                for sub in split_map[i]:
                    out.append(_Piece(
                        sub, piece.vec_ok, kind=piece.kind,
                        excerpt_override=piece.excerpt_override,
                    ))
            else:
                out.append(piece)
        return out

    def _commit_media(
        self,
        cand: walk.Candidate,
        fid: int,
        outcome: _Outcome,
        report: IngestReport,
        *,
        embed: EmbeddingBackend | None,
    ) -> None:
        """Embed media segments on the MAIN thread (serial, one call per piece —
        ARCHITECTURE §7.1): images via embed_image(thumbnail), audio windows
        via embed_audio_wav(bytes). Image/audio vectors carry NO prompt (D7);
        `ocr` segments carry the recognized text for FTS always, plus a
        document-prompted TEXT vector only under the A5 llama policy. A file
        with any FTS-bearing segment is content_indexed even without vectors
        (SPEC's definition — OCR'd screenshots under llama count)."""
        store = self.store
        info = dict(outcome.info)
        if outcome.partial:
            info["partial"] = True
            report.partial += 1
        segs: list[SegmentRec] = [
            SegmentRec(
                kind="name", modality="none",
                excerpt=Path(cand.path).name,
                text_for_fts=_folder_words(str(cand.path), "file"),
            )
        ]
        for m in outcome.media_segs:
            vec: np.ndarray | None = None
            if embed is not None:
                try:
                    if m.kind == "ocr":
                        if m.embed_text and m.text_for_fts:
                            title = Path(cand.path).stem
                            prompted = document_prompt(title, m.text_for_fts)
                            vec = embed.embed_texts([prompted])[0]
                    elif m.kind == "image":
                        vec = embed.embed_image(m.path)
                    else:
                        vec = embed.embed_audio_wav(m.wav)
                except OSError as exc:
                    # file vanished/unreadable since the walk: per-file failure,
                    # not a backend crash
                    self._record_failed(
                        store, str(cand.path), f"media read failed: {exc}",
                        report, fid=fid, kind=cand.kind,
                    )
                    return
                except EmbeddingError as exc:
                    self._record_failed(
                        store, str(cand.path), f"embedding failed: {exc}",
                        report, fid=fid, kind=cand.kind,
                    )
                    return
            if m.kind == "ocr":
                modality = "text" if vec is not None else "none"
            else:
                modality = m.kind if vec is not None else "none"
            segs.append(
                SegmentRec(
                    kind=m.kind,
                    modality=modality,
                    loc=m.loc,
                    loc_end=m.loc_end,
                    excerpt=m.excerpt,
                    text_for_fts=m.text_for_fts,
                    vec=vec,
                )
            )
        store.replace_segments(fid, segs)
        embedded = any(s.vec is not None for s in segs)
        has_fts_text = any(s.text_for_fts for s in segs)
        indexed = embedded or has_fts_text
        store.mark_file(
            fid, state="done",
            capability="content_indexed" if indexed else "metadata_only",
            detail=None if indexed else "no media vectors embedded",
            info=info or None,
            extractor_version=outcome.extractor_version,
            chunker_version=self._chunker_version,
            embed_id=self._embed_identity if embedded else None,
            content_date=outcome.content_date,
        )
        self._tally(report, cand.kind, "content_indexed" if indexed else "metadata_only")

    def _record_unsupported(self, store: Store, path: str, reason: str, report,
                            kind_override: str | None = None,
                            source: str = "file") -> None:
        """A file the walker refused to read: recorded as capability=unsupported
        with a machine-stable reason (SPEC §9). Folder notices never reach here."""
        if kind_override is None:
            ext = Path(path).suffix.lstrip(".").lower()
            kind_override = (
                "code" if ext in rules.CODE_EXTS
                else "image" if ext in rules.IMAGE_EXTS
                else "audio" if ext in rules.AUDIO_EXTS
                else "video" if ext in rules.VIDEO_EXTS
                else "pdf" if ext in rules.PDF_EXTS
                else "doc"
            )
        kind = kind_override
        store.upsert_file(
            FileRec(
                path=path, source=source, kind=kind, size=0, mtime=None,
                sha256=None, state="done", capability="unsupported", detail=reason,
                info={"reason": reason},
                chunker_version=self._chunker_version,
            )
        )
        report.scanned += 1
        self._tally(report, kind, "unsupported")
        _merge_reasons(report.unsupported_reasons, reason)

    def _record_failed(self, store: Store, path: str, detail: str | None, report,
                       fid: int | None = None, kind: str = "doc") -> None:
        if fid is None:
            rec = store.find_by_path(path)
            fid = rec.id if rec and rec.id is not None else store.upsert_file(
                FileRec(path=path, source="file", kind=kind, size=0, state="pending")
            )
        store.mark_file(
            fid, state="failed", capability="failed", detail=detail,
            info=None, extractor_version=None,
            chunker_version=self._chunker_version, embed_id=None,
        )
        # P1-5: drop the previous extract's content segments (+ their FTS rows)
        # so search cannot serve text of a file that no longer extracts; the
        # name row stays. Search also excludes failed files (belt and braces).
        store.delete_content_segments(fid)
        self._tally(report, kind, "failed")
        report.failures.append({"path": path, "detail": detail or "failed"})
        del report.failures[:-10]

    def _drop_dataset_folders(
        self, candidates: list["walk.Candidate"], file_roots: list[str], report,
    ) -> int:
        """Dataset-folder detection (rules.dataset_folders, DigUp port): a
        folder stuffed with hundreds of machine-named media files is training
        data, not your stuff. Every candidate inside a detected folder becomes
        an unsupported row with a machine-stable reason; --include-datasets
        skips this gate entirely. Returns the dropped count."""
        media = [c for c in candidates if c.kind in ("image", "audio", "video")]
        if not media:
            return 0
        files = [(str(c.path.parent), c.path.stem, c.kind) for c in media]
        datasets = rules.dataset_folders(files, roots={str(Path(r)) for r in file_roots})
        if not datasets:
            return 0
        kept: list[walk.Candidate] = []
        dropped = 0
        for cand in candidates:
            if str(cand.path.parent) in datasets:
                self._record_unsupported(
                    self.store, str(cand.path), "dataset folder", report,
                )
                dropped += 1
            else:
                kept.append(cand)
        candidates[:] = kept
        return dropped

    def _record_url_unimplemented(self, url: str, report) -> None:
        self.store.upsert_file(
            FileRec(
                path=url, source="url", kind="webpage", size=0, mtime=None,
                sha256=None, state="done", capability="metadata_only",
                detail="crawler not implemented in this build",
                chunker_version=self._chunker_version,
            )
        )
        report.scanned += 1
        self._tally(report, "webpage", "metadata_only")

    def _ingest_urls(self, urls: list[str], bounds, report) -> None:
        """Driven by ingest/crawl.py (agent B, build pass 5): crawl() yields
        CrawledPage | SkipNotice; each page is stored as kind=webpage rows with
        path=<canonical URL>, source='url' (SPEC §2)."""
        from .crawl import CrawlBounds, crawl  # agent B's module

        bounds = bounds or CrawlBounds(politeness=self.cfg.ingest.politeness)
        for item in crawl(urls, bounds):
            if isinstance(item, walk.SkipNotice):
                self._record_unsupported(
                    self.store, item.path, item.reason, report,
                    kind_override="webpage", source="url",
                )
                continue
            from .extract_html import html_to_text

            plain, title = html_to_text(item.html)
            fid = self.store.upsert_file(
                FileRec(
                    path=item.url, source="url", kind="webpage",
                    size=len(item.html.encode("utf-8")), mtime=None,
                    sha256=item.sha256, state="pending",
                )
            )
            if not plain.strip():
                self.store.mark_file(
                    fid, state="done", capability="metadata_only",
                    detail="no text extracted", info={
                        "fetched_at": item.fetched_at, "final_url": item.final_url,
                    }, extractor_version="url-stdlib.v1",
                    chunker_version=self._chunker_version, embed_id=None,
                )
                self._tally(report, "webpage", "metadata_only")
                continue
            # token-budget chunking (D3) + the same paranoid ctx guard as the
            # file path: an over-ceiling page chunk is split, never failed
            pieces_in = self._chunk_text(plain, "webpage",
                                         self.cfg.ingest.max_chunks)
            if self.backend is not None and not self.lexical_only:
                pieces = self._ctx_guard_split(
                    [_Piece(c, _has_words(c.text)) for c in pieces_in],
                    title or "page",
                )
                chunks = [p.chunk for p in pieces]
            else:
                chunks = pieces_in
            vec_chunks = [c for c in chunks if _has_words(c.text)]
            vectors: dict[int, np.ndarray] = {}
            if self.backend is not None and not self.lexical_only and vec_chunks:
                prompts = [
                    document_prompt(title or "page", c.text) for c in vec_chunks
                ]
                batch = max(1, int(self.cfg.llama.batch))
                try:
                    for i in range(0, len(prompts), batch):
                        part = self.backend.embed_texts(prompts[i:i + batch])
                        for j, v in enumerate(part):
                            vectors[i + j] = v
                except EmbeddingError as exc:
                    # P1-5: the re-crawled page failed — its previous extract
                    # must not stay searchable.
                    self.store.delete_content_segments(fid)
                    self._tally(report, "webpage", "failed")
                    report.failures.append(
                        {"path": item.url, "detail": f"embedding failed: {exc}"}
                    )
                    report.failed += 1
                    continue
            segs: list[SegmentRec] = [
                SegmentRec(
                    kind="name", modality="none", excerpt=item.url,
                    text_for_fts=_folder_words(item.url, "url"),
                )
            ]
            vec_pos = 0
            for chunk in chunks:
                ok = _has_words(chunk.text)
                vec = vectors.get(vec_pos) if ok else None
                if ok:
                    vec_pos += 1
                segs.append(
                    SegmentRec(
                        kind="chunk",
                        modality="text" if (ok and vec is not None) else "none",
                        loc=None, loc_end=None,
                        excerpt=excerpt(chunk.text),
                        text_for_fts=chunk.text,
                        vec=vec,
                    )
                )
            self.store.replace_segments(fid, segs)
            embedded = any(s.vec is not None for s in segs)
            self.store.mark_file(
                fid, state="done", capability="content_indexed", detail=None,
                info={"fetched_at": item.fetched_at, "final_url": item.final_url},
                extractor_version="url-stdlib.v1",
                chunker_version=self._chunker_version,
                embed_id=self._embed_identity if embedded else None,
            )
            self._tally(report, "webpage", "content_indexed")

    # -------------------------------------------------------------- helpers

    def _tally(self, report: IngestReport, kind: str, capability: str) -> None:
        setattr(report, capability, getattr(report, capability) + 1)
        report.by_kind.setdefault(kind, {})
        report.by_kind[kind][capability] = report.by_kind[kind].get(capability, 0) + 1

    def _clear_vectors(self) -> None:
        """--reindex: clear segments + fingerprint; re-extract everything."""
        with self.store.db:
            self.store.db.execute("DELETE FROM fts")
            self.store.db.execute("DELETE FROM segments")
        self.store.set_meta("fingerprint_id", None)
        self.store.set_meta("fingerprint_json", None)

    def _finalize(self, report: IngestReport, live_fp: dict | None) -> None:
        store = self.store
        if store is None:
            return
        counts = store.counts()
        report.vectors = counts.vectors
        report.segments = counts.segments
        if live_fp is not None:
            store.set_meta("fingerprint_id", fingerprints.fingerprint_id(live_fp))
            store.set_meta("fingerprint_json", fingerprints.canonical(live_fp).decode())
            # v2 embed identity meta. F1: derived from the backend's OWN
            # structured fields (same derivation the per-file writes used);
            # the stored-fingerprint parse is only a safety net for paths
            # that never ran the live block.
            store.set_meta(
                "embed_id", self._embed_identity
                or fingerprints.embed_id_from_fingerprint(live_fp)
            )
            store.set_meta(
                "embed_identity_json",
                fingerprints.canonical(
                    self._embed_identity_dict
                    or fingerprints.embed_identity(live_fp)
                ).decode(),
            )
            report.fingerprint = {
                "id": fingerprints.fingerprint_id(live_fp),
                "backend": live_fp.get("backend"),
                "model_id": live_fp.get("model_id"),
                "dim": live_fp.get("dim"),
            }
        report.metadata_reasons = [
            {"reason": r, "count": n} for r, n in store.reasons("metadata_only")
        ]
        report.unsupported_reasons = [
            {"reason": r, "count": n} for r, n in store.reasons("unsupported")
        ]
        if self.lexical_only:
            report.note("lexical_only", "keyword-only index — no vectors were embedded")
        if report.cancelled:
            report.note(
                "interrupted",
                "interrupted — resume with the same command (ingest is idempotent)",
            )
        # roots bookkeeping: first ingest records, later runs merge (SPEC §2).
        stored = store.meta("roots_json")
        try:
            previous = json.loads(stored) if stored else []
        except json.JSONDecodeError:
            previous = []
        merged = list(dict.fromkeys([*previous, *report.roots]))
        store.set_meta("roots_json", json.dumps(merged))
        if store.meta("schema_version") is None:
            store.set_meta("schema_version", str(Store.SCHEMA_VERSION))
        if store.meta("app_version") is None:
            from .. import __version__

            store.set_meta("app_version", __version__)
        if store.meta("index_uuid") is None:
            import uuid

            store.set_meta("index_uuid", str(uuid.uuid4()))
        store.set_meta("config_snapshot_json", json.dumps({"ingest": True, "at": time.time()}))


def _merge_reasons(reasons: list[dict], reason: str) -> list[dict]:
    for entry in reasons:
        if entry["reason"] == reason:
            entry["count"] += 1
            return reasons
    reasons.append({"reason": reason, "count": 1})
    return reasons
