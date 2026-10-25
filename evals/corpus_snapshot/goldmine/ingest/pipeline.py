"""Ingest orchestration: walk -> classify -> extract -> chunk -> (embed|lexical-only)
-> store -> FTS. One transaction per file (atomic segment replacement), capability
matrix assignment for every scanned file (V5a), content-hash skip, deletion pruning,
and an honest IngestReport matching SPEC §2.

The class is assembled from mixins that are pure moves out of this file:
goldmine.ingest.extraction (walk candidate -> _Outcome, incl. the shared OCR
batch) and goldmine.ingest.commit (outcomes -> store rows; the verdict/name/
atomic-commit/tally tail is shared with the URL path in goldmine.ingest.urls).
This module keeps the run itself: root resolution, extraction waves,
skip/staleness, record rows, deletions, URL hand-off and run finalization."""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import fingerprints
from ..backends.base import EmbeddingBackend, EmbeddingError
from ..chunker import CHUNKER_VERSION, Chunk, chunk_text_tokens, effective_token_budget
from ..config import Config
from ..store import FileRec, Store
from ..util import sha256_file
from . import media, rules, walk
from .commit import CommitMixin, IngestReport, _merge_reasons
from .extraction import CODE_HARD_CAP_BYTES, ExtractMixin, _OcrRequest, _Outcome
from .metrics import IngestMetrics
from .urls import UrlIngestMixin

logger = logging.getLogger("goldmine.ingest")


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
    info = rec.info or {}
    if rec.kind == "pdf":
        states = info.get("ocr_page_states", {})
        if states:
            return any(state in ("unavailable", "error") for state in states.values())
        return _thin_marker(rec)  # legacy thin pages, before per-page provenance
    if rec.kind != "image" or not info.get("screenshot"):
        return False
    if info.get("ocr_status") in ("unavailable", "error"):
        return True
    if info.get("ocr_status") in ("complete", "no_text", "low_confidence"):
        return False
    return info.get("ocr") == "unavailable" or ("ocr" not in info and "ocr_chars" not in info)


class IngestPipeline(CommitMixin, ExtractMixin, UrlIngestMixin):
    """The ingest run: roots -> waves -> outcomes -> commits -> report."""

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
        # would parse the legacy prompts scheme, which has no d-slot for some
        # backends.
        self._embed_identity_dict: dict | None = None
        # v2 OCR (B4): lazy one-time helper probe shared by screenshots and
        # thin PDF pages ("unknown" -> probed on first need, cached per run;
        # never silent — ensure_helper logs/notes every outcome).
        self._ocr_ready: bool | None = None
        self._ocr_unavailable_reason: str | None = None
        self._ocr_lock = threading.Lock()
        # 02 F7: screenshots batch into ONE helper spawn per drain (the
        # helper's stdin batch contract; per-image error isolation kept).
        self._ocr_batch: list["_OcrRequest"] = []
        self._ocr_batch_leader = False
        self._ocr_batch_lock = threading.Lock()
        # v2 image prep (B5): thumbnails must outlive extraction (they embed
        # in _commit, one wave later), so the run owns one scratch dir.
        self._media_tmp: tempfile.TemporaryDirectory | None = None
        self._media_tmp_lock = threading.Lock()
        # G10: current extractor-version set, computed lazily once per run
        # (tool probing is not free).
        self._current_extractors: set[str] | None = None
        self._asr_ready: bool | None = None
        self._metrics = IngestMetrics(enabled=False, backend=backend)
        self.last_metrics: dict | None = None
        self._backend_started = False
        self._startup_interrupted = False

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
        update_roots: bool = True,
        progress=None,
        instrument: bool = True,
    ) -> IngestReport:
        self._metrics = IngestMetrics(enabled=instrument, backend=self.backend)
        self._backend_started = False
        self._startup_interrupted = False
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
            with self._metrics.stage("scan"):
                result = self._dry_run(resolved_roots, code=code, exclude=exclude, report=report)
            result.metrics = self.last_metrics = self._metrics.snapshot(files_skipped=result.unchanged)
            return result

        assert self.store is not None, "pipeline needs a Store unless dry_run"
        max_file_bytes = int(self.cfg.ingest.max_file_mb) * (1 << 20)
        max_chunks = self.cfg.ingest.max_chunks
        batch = max(1, int(self.cfg.llama.batch))

        live_fp = None
        try:
            if self.backend is not None and not self.lexical_only:
                # Known static identities can be gated without loading a model.
                # Unknown/helper identities still require startup before Ping's
                # version is trusted. Explicit reindex must start successfully
                # before destructive clearing, even for an empty source tree.
                eager = reindex or bool(url_roots) or not self.backend.fingerprint_without_startup
                with self._metrics.stage("embed"):
                    live_fp = self._prepare_backend(report, startup=eager)
                if reindex:
                    # P1-3: clear only AFTER startup + the fingerprint gate
                    # passed — a failed startup must not leave a hollow index
                    # that the content-hash skip would never rebuild.
                    self._clear_vectors()
            elif reindex:
                # lexical-only reindex: no backend, no gate — clear directly.
                self._clear_vectors()

            candidates: list[walk.Candidate] = []
            unreachable_prefixes: list[str] = []
            with self._metrics.stage("scan"):
                for item in walk.walk_files(
                    [Path(r) for r in file_roots], code=code, exclude=exclude,
                    max_file_bytes=max_file_bytes,
                ):
                    if isinstance(item, walk.Candidate):
                        candidates.append(item)
                    elif item.is_dir:
                        # A1-18: an unreadable subtree is unavailable, not
                        # empty — its rows are exempt from deletion below.
                        if item.unreachable:
                            unreachable_prefixes.append(item.path)
                    else:
                        with self._metrics.stage("commit"):
                            self._record_unsupported(self.store, item.path, item.reason, report)
                if not include_datasets:
                    with self._metrics.stage("commit"):
                        self._drop_dataset_folders(candidates, file_roots, report)

            report.scanned += len(candidates)
            # P1-3: match vanished rows against this run's candidates by
            # POSIX identity BEFORE processing — a moved file re-points its
            # row (segments/vectors kept) and then hits the unchanged skip
            # at the new path, instead of delete + full re-embed.
            with self._metrics.stage("scan"):
                moves = walk.detect_moves(self.store, resolved_roots, candidates)
            for fid, old_path, new_path in moves:
                with self._metrics.stage("commit"):
                    self.store.move_file(fid, new_path)
                report.moves += 1
            if moves:
                report.note(
                    "moved",
                    f"{len(moves)} file(s) moved on disk — re-pointed without "
                    "re-embedding",
                )
            wave = max(int(self.cfg.ingest.workers) * 8, 16)
            done = 0
            pool = ThreadPoolExecutor(max_workers=max(1, int(self.cfg.ingest.workers)))
            try:
                for wave_start in range(0, len(candidates), wave):
                    if self.cancelled:
                        break
                    wave_items = candidates[wave_start:wave_start + wave]
                    with self._metrics.stage("hash"):
                        hashes = [sha256_file(c.path) for c in wave_items]
                    jobs = []
                    for cand, sha in zip(wave_items, hashes):
                        with self._metrics.stage("scan"):
                            unchanged = self._skip_unchanged(cand, sha, reindex, refresh_thin=refresh_thin)
                        if unchanged:
                            report.unchanged += 1
                            done += 1
                            continue
                        if not self.lexical_only and not self._backend_started:
                            with self._metrics.stage("embed"):
                                live_fp = self._prepare_backend(report, startup=True)
                        # extract ONLY what will be committed: submitting the
                        # future BEFORE the skip check ran a full extraction
                        # for every unchanged file as a discarded orphan —
                        # pool.shutdown(wait=False) cannot revoke a running
                        # future, so the work (and a stray OCR spawn) leaked
                        # past run() on every plain ingest.
                        jobs.append((
                            cand, sha,
                            pool.submit(
                                self._extract_measured, cand,
                                encoding_override=encoding_override,
                                max_chunks=max_chunks,
                            ),
                        ))
                    for cand, sha, future in jobs:
                        try:
                            outcome = future.result()
                        except Exception as exc:  # extraction crash => failed row
                            outcome = _Outcome(
                                capability="failed",
                                detail=f"{type(exc).__name__}: {exc}",
                            )
                        try:
                            with self._metrics.stage("commit"):
                                self._commit(
                                    cand, sha, outcome, report, batch=batch,
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
                if self._startup_interrupted:
                    raise
                self.cancelled = True
                report.cancelled = True
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

            # URLs: crawl.py (build pass 5) drives link-following; until it lands
            # this build records URLs honestly instead of pretending.
            for url in url_roots:
                try:
                    with self._metrics.stage("extract"):
                        self._ingest_urls([url], crawl_bounds, report)
                except ImportError:
                    self._record_url_unimplemented(url, report)

            # Deletions: files under a root that vanished since the last ingest.
            # A1-18 guard: roots/subtrees unavailable RIGHT NOW keep their rows
            # (an ejected drive or a TCC prompt must not read as "all deleted").
            with self._metrics.stage("scan"):
                deletions = walk.compute_deletions(
                    self.store, resolved_roots, unreachable=unreachable_prefixes
                )
            with self._metrics.stage("commit"):
                for fid in deletions.gone:
                    self.store.delete_file(fid)
            report.deleted = len(deletions.gone)
            report.kept_unreachable = deletions.kept_unreachable
            if deletions.kept_unreachable or deletions.unavailable_roots:
                roots_named = ", ".join(r for r, _ in deletions.unavailable_roots)
                report.note(
                    "kept_unreachable",
                    f"{deletions.kept_unreachable} file row(s) kept: root or "
                    "subtree unavailable (not read as deleted)"
                    + (f" — {roots_named}" if roots_named else ""),
                )
        finally:
            if self.backend is not None:
                self.backend.close()
            self._close_media_tmp()
            report.metrics = self.last_metrics = self._metrics.snapshot(files_skipped=report.unchanged)

        with self._metrics.stage("commit"):
            self._finalize(report, live_fp, update_roots=update_roots)
        report.metrics = self.last_metrics = self._metrics.snapshot(files_skipped=report.unchanged)
        return report

    def _prepare_backend(self, report, *, startup):
        if startup and not self._backend_started:
            try:
                self.backend.startup()
            except KeyboardInterrupt:
                self._startup_interrupted = True
                raise
            self._backend_started = True
        live_fp = fingerprints.build(self.backend)
        fingerprints.gate(self.store.meta, live_fp)
        self._embed_identity = fingerprints.live_embed_id(self.backend)
        self._embed_identity_dict = fingerprints.live_embed_identity(self.backend)
        build_string = getattr(self.backend, "build_string", None)
        if build_string and self._backend_started:
            previous = self.store.meta("llama_build")
            if previous and previous != build_string:
                report.note("build_changed", f"llama.cpp build changed (recorded: {previous}, installed: {build_string}) — reindex recommended")
            with self._metrics.stage("commit"):
                self.store.set_meta("llama_build", build_string)
        return live_fp

    def _extract_measured(self, cand, **kwargs):
        with self._metrics.stage("extract"):
            return self._extract(cand, **kwargs)

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
        # video: soundtrack (ASR/audio) OR keyframes make it indexable
        # (video-frames-001: a soundtrack-less clip gets frame vectors)
        media_kinds = (
            ("image", image_ok), ("audio", audio_ok),
            ("video", audio_ok or image_ok),
        )
        for kind, ok in media_kinds:
            if kinds.get(kind):
                report.by_kind.setdefault(kind, {})[
                    "content_indexed" if ok else "metadata_only"
                ] = kinds[kind]
        report.content_indexed = sum(kinds.get(k, 0) for k in ("doc", "pdf", "webpage", "code"))
        report.content_indexed += sum(kinds.get(k, 0) for k, ok in media_kinds if ok)
        report.metadata_only = sum(
            kinds.get(k, 0) for k, ok in media_kinds if not ok
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
        if rec.kind == "image" and (rec.info or {}).get("exif_version") != media.EXIF_METADATA_VERSION:
            # Repair legacy capture/edit-time confusion once, including
            # metadata-only photos. The marker also records a truthful absence.
            return False
        if rec.kind == "image" and (rec.info or {}).get("exif_status") not in {"complete", "absent", "unsupported", "unavailable"}:
            # Native timeout/process/JSON failures are not authoritative absence.
            # Rows written before the status distinction also need one refresh.
            return False
        if rec.kind == "image" and (rec.info or {}).get("exif_reader") is None and media.exif_reader(cand.path) is not None:
            return False  # A formerly missing native metadata reader arrived.
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
            if rec.kind != "pdf":
                return False
            from .extract_pdf import pdftoppm_path

            if pdftoppm_path() is not None:
                return False
        if (rec.kind in ("audio", "video")
                and (rec.info or {}).get("asr_status") in ("unavailable", "error")
                and not self.lexical_only and self.backend.capability().text
                and str(self.cfg.ingest.asr) != "off"):
            if self._asr_ready is None:
                from .asr import resolve_tools

                self._asr_ready = resolve_tools(self.cfg) is not None
            if self._asr_ready:
                return False
        # digup-plan-002 media-capability redo rules (amendments M5, normative
        # predicates — a re-done row always writes audio_routing or a media
        # vector or an honest none:* reason, so neither rule can churn):
        if self.backend is not None and not self.lexical_only:
            cap = self.backend.capability()
            if (rec.kind in ("audio", "video")
                    and cap.audio
                    and (rec.info or {}).get("audio_routing") is None
                    and (rec.info or {}).get("asr_segments") is None):
                # R-a: an audio/video row from before the audio capability
                # (or before routing markers existed) with NO transcript can
                # now gain raw windows — including when ASR tools are absent.
                return False
            if (rec.kind == "image" and cap.image
                    and rec.capability == "metadata_only"
                    and rec.detail == "backend lacks image embedding"):
                # R-b: photos that never got a thumbnail pass because the
                # image capability was off.
                return False
            if (rec.kind == "video" and cap.image
                    and (rec.info or {}).get("frames") in (None, "none:no_image_capability")):
                # R-c (video-frames-001): rows from before keyframes landed
                # (including asr-versioned transcript rows — the media bump
                # alone cannot reach them) get their frame pass; the re-done
                # row always writes a frames marker, so this cannot churn.
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
                       fid: int | None = None, kind: str = "doc", info: dict | None = None) -> None:
        if fid is None:
            rec = store.find_by_path(path)
            fid = rec.id if rec and rec.id is not None else store.upsert_file(
                FileRec(path=path, source="file", kind=kind, size=0, state="pending")
            )
        # P1-5: drop the previous extract's content segments (+ their FTS rows)
        # so search cannot serve text of a file that no longer extracts; the
        # name row stays. Search also excludes failed files (belt and braces).
        with store.write_transaction():
            store.delete_content_segments(fid)
            store.mark_file(
                fid, state="failed", capability="failed", detail=detail,
                info=info, extractor_version=None,
                chunker_version=self._chunker_version, embed_id=None,
            )
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

    # -------------------------------------------------------------- helpers

    def _clear_vectors(self) -> None:
        """--reindex: clear segments + fingerprint; re-extract everything."""
        with self.store.write_transaction():
            self.store.db.execute("DELETE FROM fts")
            self.store.db.execute("DELETE FROM segments")
        self.store.set_meta("fingerprint_id", None)
        self.store.set_meta("fingerprint_json", None)

    def _finalize(self, report: IngestReport, live_fp: dict | None, *,
                  update_roots: bool = True) -> None:
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
        # R9/D3: update_roots=False skips the merge — a watch delta pass's roots
        # are ephemeral changed-file paths that must never become index scope
        # (the F10 principle). Default True keeps every existing caller
        # byte-identical.
        if update_roots:
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
