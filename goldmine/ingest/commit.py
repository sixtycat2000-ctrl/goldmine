"""Commit paths: an _Outcome -> store rows, one atomic transaction per file.
The capability-verdict / name-segment / replace_segments / mark_file / report
tally tail is written ONCE here (name_segment / embed_batched / _finish_file /
_commit_segments / _commit_chunks) instead of three divergent copies: text
file chunks (_commit), media segments (_commit_media) and crawled pages
(urls.UrlIngestMixin shares _commit_chunks).

The mixin expects the host pipeline to provide: store, backend, lexical_only,
cfg, _chunker_version, _embed_identity, the token-budget helpers behind the
ctx guard (_token_budget/_tokenize_fn) and the failure-row recorder
(_record_failed)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from ..backends.base import EmbeddingBackend, EmbeddingError, document_prompt
from ..chunker import (
    PROMPT_FRAMING_TOKENS,
    TOKEN_SLACK,
    Chunk,
    split_text_by_tokens,
)
from ..store import FileRec, SegmentRec, _folder_words
from ..evidence import file_evidence_types
from ..util import excerpt
from . import walk
from .extraction import _Outcome, _Piece


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
    metrics: dict | None = None

    def note(self, code: str, message: str) -> None:
        self.notices.append({"code": code, "message": message})

    def to_json(self) -> dict:
        result = {
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
        if self.metrics is not None:
            result["metrics"] = self.metrics
        return result


def name_segment(excerpt_text: str, fts_words: str) -> SegmentRec:
    """The name row every committed file carries: file name (or URL) excerpt
    plus folder-path words for FTS. modality none — never vectorized."""
    return SegmentRec(
        kind="name", modality="none", excerpt=excerpt_text,
        text_for_fts=fts_words,
    )


def embed_batched(
    embed: EmbeddingBackend, prompts: list[str], batch: int,
    *, metrics=None,
) -> dict[int, np.ndarray]:
    """Embed prompted texts in `batch`-sized slices -> {position: vector}.
    Raises EmbeddingError; the caller owns the per-file failure policy."""
    vectors: dict[int, np.ndarray] = {}
    for i in range(0, len(prompts), batch):
        inputs = prompts[i:i + batch]
        if metrics is None:
            part = embed.embed_texts(inputs)
        else:
            metrics.call("embed_calls")
            metrics.call("embed_inputs", len(inputs))
            with metrics.stage("embed"):
                part = embed.embed_texts(inputs)
        for j, vec in enumerate(part):
            vectors[i + j] = vec
    return vectors


def _merge_reasons(reasons: list[dict], reason: str) -> list[dict]:
    for entry in reasons:
        if entry["reason"] == reason:
            entry["count"] += 1
            return reasons
    reasons.append({"reason": reason, "count": 1})
    return reasons

class CommitMixin:
    """Commit-side primitives shared by the file, media and URL paths."""

    def _tally(self, report: IngestReport, kind: str, capability: str) -> None:
        setattr(report, capability, getattr(report, capability) + 1)
        report.by_kind.setdefault(kind, {})
        report.by_kind[kind][capability] = report.by_kind[kind].get(capability, 0) + 1

    def _finish_file(
        self,
        fid: int,
        *,
        capability: str,
        detail: str | None,
        info: dict | None,
        extractor_version: str | None,
        report: IngestReport,
        kind: str,
        embedded: bool = False,
        content_date: float | None = None,
    ) -> None:
        """The shared verdict tail: mark the row done + tally the report.
        `embedded` selects the live embed identity (None when no vector
        landed). Image capture metadata replaces/clears the previous epoch;
        other kinds keep the existing None/coalesce convention."""
        info = dict(info or {})
        if kind in ("image", "audio", "video", "pdf"):
            info["evidence_types"] = file_evidence_types(
                self.store.db, fid, kind=kind, extractor_version=extractor_version, info=info,
            )
        self.store.mark_file(
            fid, state="done", capability=capability,
            detail=detail, info=info or None,
            extractor_version=extractor_version,
            chunker_version=self._chunker_version,
            embed_id=self._embed_identity if embedded else None,
            content_date=content_date, replace_content_date=kind == "image",
        )
        self._tally(report, kind, capability)

    def _commit_segments(
        self,
        fid: int,
        segs: list[SegmentRec],
        *,
        capability: str,
        detail: str | None,
        info: dict | None,
        extractor_version: str | None,
        report: IngestReport,
        kind: str,
        content_date: float | None = None,
    ) -> None:
        """Atomic segment replacement + the verdict tail (one transaction
        per file: the old segments die with the new ones or not at all)."""
        with self._metrics.stage("commit"):
            with self.store.write_transaction():
                self.store.replace_segments(fid, segs)
                self._finish_file(
                    fid, capability=capability, detail=detail, info=info,
                    extractor_version=extractor_version, report=report, kind=kind,
                    embedded=any(s.vec is not None for s in segs),
                    content_date=content_date,
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
        if cand.kind == "image" and outcome.info.get("exif_status") in ("error", "unavailable"):
            # Extraction workers have no store access. Preserve prior proven
            # capture metadata here only when the immutable source SHA agrees;
            # never bless legacy sips dates or carry dates onto changed bytes.
            from .media import EXIF_METADATA_VERSION

            previous = store.find_by_path(str(cand.path))
            prior = previous.info or {} if previous else {}
            if (previous and previous.sha256 == sha and prior.get("exif_version") == EXIF_METADATA_VERSION
                    and prior.get("taken_source") == "Exif.DateTimeOriginal"
                    and isinstance(prior.get("taken"), str) and previous.content_date is not None):
                outcome.info.update({"taken": prior["taken"], "taken_source": prior["taken_source"]})
                outcome.content_date = previous.content_date
        fid = store.upsert_file(
            FileRec(
                path=str(cand.path), source="file", kind=cand.kind,
                size=cand.size, mtime=cand.mtime, sha256=sha, state="pending",
            )
        )
        if outcome.capability == "failed":
            self._record_failed(store, str(cand.path), outcome.detail, report, fid=fid,
                                kind=cand.kind, info=outcome.info or None)
            return
        if outcome.capability == "unsupported":
            # keep the extractor's info (screenshot flag etc.) beside the
            # machine-stable reason — coverage/KPIs bucket on it
            info = dict(outcome.info or {})
            if outcome.detail:
                info["reason"] = outcome.detail
            with store.write_transaction():
                store.delete_content_segments(fid)
                self._finish_file(
                    fid, capability="unsupported", detail=outcome.detail,
                    info=info, extractor_version=outcome.extractor_version,
                    report=report, kind=cand.kind,
                    content_date=outcome.content_date,
                )
            _merge_reasons(report.unsupported_reasons, outcome.detail or "unknown")
            return

        if outcome.media_segs:
            self._commit_media(cand, fid, outcome, report, embed=embed)
            return

        try:
            self._commit_chunks(
                fid,
                name=Path(cand.path).name,
                fts_words=_folder_words(str(cand.path), "file"),
                pieces=outcome.chunks,
                title=outcome.title or Path(cand.path).stem,
                embed=embed, batch=batch, report=report, kind=cand.kind,
                detail=outcome.detail, info=dict(outcome.info),
                extractor_version=outcome.extractor_version,
                content_date=outcome.content_date,
                partial=outcome.partial,
            )
        except EmbeddingError as exc:
            # per-file failure, not a run abort: an oversized/rejected input
            # (or a backend that dies mid-run) fails THIS file and the report
            # carries it; other files still get indexed.
            self._record_failed(
                store, str(cand.path), f"embedding failed: {exc}",
                report, fid=fid, kind=cand.kind,
            )

    def _commit_chunks(
        self,
        fid: int,
        *,
        name: str,
        fts_words: str,
        pieces: list[_Piece],
        title: str,
        embed: EmbeddingBackend | None,
        batch: int,
        report: IngestReport,
        kind: str,
        detail: str | None,
        info: dict,
        extractor_version: str | None,
        content_date: float | None = None,
        partial: bool = False,
        with_loc: bool = True,
    ) -> None:
        """Shared TEXT commit (file chunks + crawled pages): ctx guard, batched
        prompted embedding, name row + content segments, atomic replace and the
        verdict/tally tail. Empty `pieces` commits a metadata_only row.

        Raises EmbeddingError after embedding failed — the CALLER owns failure
        bookkeeping (file rows: _record_failed; pages: drop the previous
        extract's segments and count a failure, urls.py).

        with_loc=False commits segments with loc=None regardless of chunk locs
        (the URL path's url-stdlib behavior — pages have no line numbers)."""
        if not pieces:
            self._commit_segments(
                fid, [name_segment(name, fts_words)], capability="metadata_only",
                detail=detail or "no content", info=info, extractor_version=extractor_version,
                report=report, kind=kind, content_date=content_date,
            )
            return

        # Paranoid ctx guard (D3, falsifiable claim 2): any chunk whose PROMPTED
        # token count still exceeds the ceiling is split at token boundaries —
        # SPLIT-not-fail. Token-true budgeting makes this structurally rare
        # (the 2670-token code / 82929-token single-line JSON 400 classes die
        # at the source); per-input 400 isolation below stays as the backstop.
        if embed is not None:
            pieces = self._ctx_guard_split(pieces, title)

        if partial:
            info["partial"] = True
            report.partial += 1

        segs: list[SegmentRec] = [name_segment(name, fts_words)]
        vec_chunks = [p for p in pieces if p.vec_ok]
        vectors: dict[int, np.ndarray] = {}
        if embed is not None and vec_chunks:
            prompts = [document_prompt(title, p.chunk.text) for p in vec_chunks]
            vectors = embed_batched(embed, prompts, batch, metrics=self._metrics)
        # map (chunk, ok) list position -> vector position
        vec_pos = 0
        vec_index_of: dict[int, int] = {}
        for n, piece in enumerate(pieces):
            if piece.vec_ok:
                vec_index_of[n] = vec_pos
                vec_pos += 1

        for n, piece in enumerate(pieces):
            vec = vectors.get(vec_index_of.get(n, -1))
            loc = float(piece.chunk.loc) if piece.chunk.loc is not None else None
            loc_end = (
                float(piece.chunk.loc_end)
                if piece.chunk.loc_end is not None else None
            )
            segs.append(
                SegmentRec(
                    kind=piece.kind,
                    modality="text" if (piece.vec_ok and vec is not None) else "none",
                    loc=loc if with_loc else None,
                    loc_end=loc_end if with_loc else None,
                    excerpt=(
                        piece.excerpt_override
                        if piece.excerpt_override is not None
                        else excerpt(piece.chunk.text)
                    ),
                    text_for_fts=piece.chunk.text,
                    vec=vec,
                )
            )
        self._commit_segments(
            fid, segs, capability="content_indexed", detail=detail, info=info,
            extractor_version=extractor_version, report=report, kind=kind,
            content_date=content_date,
        )

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
                    if piece.location_unit in ("page", "second"):
                        sub = replace(sub, loc=piece.chunk.loc, loc_end=piece.chunk.loc_end)
                    elif piece.chunk.loc is not None:
                        sub = replace(sub, loc=piece.chunk.loc + (sub.loc or 1) - 1,
                                      loc_end=piece.chunk.loc + (sub.loc_end or 1) - 1)
                    out.append(_Piece(
                        sub, piece.vec_ok, kind=piece.kind,
                        excerpt_override=piece.excerpt_override,
                        location_unit=piece.location_unit,
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
            name_segment(Path(cand.path).name,
                         _folder_words(str(cand.path), "file"))
        ]
        media_segs = []
        for m in outcome.media_segs:
            if m.kind != "ocr" or not m.text_for_fts:
                media_segs.append(m)
                continue
            limit = int(self.cfg.ingest.max_chunks)
            chunks = self._chunk_text(m.text_for_fts, "image", limit + 1)
            if len(chunks) > limit:
                info["partial"] = True
                if not outcome.partial:
                    report.partial += 1
                chunks = chunks[:limit]
            pieces = self._ctx_guard_split([_Piece(chunk, m.embed_text, kind="ocr") for chunk in chunks], Path(cand.path).stem)
            for piece in pieces:
                chunk = piece.chunk
                media_segs.append(replace(m, text_for_fts=chunk.text, excerpt=excerpt(chunk.text),
                                          loc=chunk.loc, loc_end=chunk.loc_end))
        for m in media_segs:
            vec: np.ndarray | None = None
            if embed is not None:
                try:
                    if m.kind == "ocr":
                        if m.embed_text and m.text_for_fts:
                            title = Path(cand.path).stem
                            prompted = document_prompt(title, m.text_for_fts)
                            self._metrics.call("embed_calls")
                            self._metrics.call("embed_inputs")
                            with self._metrics.stage("embed"):
                                vec = embed.embed_texts([prompted])[0]
                    elif m.kind == "image":
                        self._metrics.call("embed_calls")
                        self._metrics.call("embed_inputs")
                        with self._metrics.stage("embed"):
                            vec = embed.embed_image(m.path)
                    else:
                        self._metrics.call("embed_calls")
                        self._metrics.call("embed_inputs")
                        with self._metrics.stage("embed"):
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
        has_fts_text = any(s.kind != "name" and s.text_for_fts for s in segs)
        indexed = any(s.vec is not None for s in segs) or has_fts_text
        self._commit_segments(
            fid, segs,
            capability="content_indexed" if indexed else "metadata_only",
            detail=None if indexed else "no media vectors embedded",
            info=info, extractor_version=outcome.extractor_version,
            report=report, kind=cand.kind, content_date=outcome.content_date,
        )
