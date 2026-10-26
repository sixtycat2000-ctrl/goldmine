"""Page-preserving PDF extraction; missing OCR pages remain explicit metadata."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from ..chunker import Chunk
from ..util import ToolError
from . import extract_pdf, media


def extract_pdf_outcome(pipeline, path, max_chunks):
    # Imported here to keep the extraction dispatch's result types in one place.
    from .extraction import _Outcome, _Piece, _MediaSeg, _has_words

    try:
        result = extract_pdf.extract_pages(path, max_pages=pipeline.cfg.ingest.max_pages)
    except ToolError:
        return _Outcome(capability="metadata_only", detail="pdftotext not found")
    except RuntimeError as exc:
        return _Outcome(capability="failed", detail=str(exc))
    # digup-plan-002: page-IMAGE vectors for thin/scanned and glyph-order
    # pages — only when the backend can actually embed images and pdftoppm
    # can render (otherwise the current text-only behavior is the honest one).
    cap = pipeline.backend.capability() if pipeline.backend is not None else None
    want_page_images = (
        cap is not None and cap.image
        and not pipeline.lexical_only
        and extract_pdf.pdftoppm_path() is not None
    )
    candidates = [number for number, text in result.pages if len(text.strip()) < extract_pdf.THIN_PAGE_CHARS]
    states = {number: "unavailable" for number in candidates}
    recovered = {}
    if candidates and pipeline._ocr_probe():
        with tempfile.TemporaryDirectory(prefix="goldmine-pdfocr-") as td:
            recovered = pipeline._ocr_pages(path, candidates, Path(td), states=states)
    # B2 (digup-p2-closeout-001, DigUp Extractors.swift:96-160): lostLetters
    # pages upgrade from marker-only to render -> OCR -> three validity checks
    # -> REPLACE. Non-thin pages only (thin pages are the thin batch's job —
    # no double render/OCR); the ar batch carries its OWN states dict so the
    # thin batch's blanket 'unavailable' init cannot clobber real states.
    ar_recovered: dict[int, str] = {}
    lost_candidates = [
        number for number, text in result.pages
        if len(text.strip()) >= extract_pdf.THIN_PAGE_CHARS
        and extract_pdf.page_text_problem(text.strip()) == "lostLetters"
    ]
    if lost_candidates:
        if pipeline._ocr_probe():
            ar_states: dict[int, str] = {}
            with tempfile.TemporaryDirectory(prefix="goldmine-pdfarocr-") as td:
                ar_raw = pipeline._ocr_pages(path, lost_candidates, Path(td),
                                             states=ar_states)
            for number, (text, _ms) in ar_raw.items():
                text = text.strip()
                # DigUp's three checks: enough text, itself problem-free, and
                # actually Arabic (0x0600-0x06FF — the detector's own block).
                if (
                    len(text) >= extract_pdf.THIN_PAGE_CHARS
                    and extract_pdf.page_text_problem(text) is None
                    and any(0x0600 <= ord(c) <= 0x06FF for c in text)
                ):
                    ar_recovered[number] = text
                    ar_states[number] = "complete"
                else:
                    ar_states[number] = "kept"  # honest fallback: original kept
            states.update(ar_states)
        else:
            # 'unavailable' rides the _needs_ocr_redo upgrade path: one redo
            # when OCR arrives, terminal states after (no churn).
            states.update({number: "unavailable" for number in lost_candidates})
    pieces = []
    media_segs = []
    thin_pages, ocr_pages, garbled_pages, lost_pages = [], [], [], []
    partial = result.truncated
    read_to = None

    def render_page(number: int) -> Path | None:
        """One page to PNG in the run-scoped media scratch dir (never a
        TemporaryDirectory — media segs are consumed a wave later at commit).
        ToolError (no pdftoppm) is swallowed: the page falls back to text."""
        try:
            prefix = pipeline._media_tmp_dir() / f"page-{number}"
            return extract_pdf.render_page_png(path, number, prefix)
        except (ToolError, RuntimeError):
            return None

    for number, source in result.pages:
        stripped = source.strip()
        if number in ar_recovered:
            # ar-replaced page: the OCR text IS the page — normal chunking,
            # no thin treatment, no page-image stacking (existing rules apply
            # unchanged; is_thin falls out False since the check passed
            # >= THIN_PAGE_CHARS).
            stripped = ar_recovered[number]
        from .extraction import rehydrate_ocr
        upgraded = rehydrate_ocr(recovered.get(number, ("", None))[0]).strip()
        is_thin = len(stripped) < extract_pdf.THIN_PAGE_CHARS
        text = stripped
        if is_thin:
            if len(upgraded) >= extract_pdf.THIN_PAGE_CHARS:
                ocr_pages.append(number)
                text = upgraded
                states[number] = "complete"
            else:
                thin_pages.append(number)  # Includes empty/unreadable pages.
                text = "\n".join(t for t in (stripped, upgraded) if t)
                if upgraded:
                    states[number] = "low_text"
        # Text-layer pathology (DigUp PageTextProblem port, extract_pdf):
        # glyph-order pages divert to picture + meaning-only vector when the
        # image path is live; lost-letter pages are DETECTED ONLY this
        # milestone (the Arabic OCR-replace is a deferred content change).
        problem = extract_pdf.page_text_problem(stripped) if stripped else None
        if problem == "glyphOrder" and want_page_images:
            rendered = render_page(number)
            if rendered is not None:
                garbled_pages.append(number)
                media_segs.append(_MediaSeg(
                    kind="page", path=rendered, loc=float(number),
                    loc_end=float(number), excerpt="",
                    budget=media.MEDIA_BUDGETS["pdf_page"], modality="image",
                ))
                media_segs.append(_MediaSeg(
                    kind="page", loc=float(number), loc_end=float(number),
                    garbled=stripped, modality="text",
                ))
                read_to = number
                continue  # the garbled words never reach FTS/chunks (§3.5)
        elif problem == "lostLetters":
            lost_pages.append(number)
        if not text:
            read_to = number
            continue
        remaining = max(0, max_chunks - len(pieces))
        if not remaining:
            partial = True
            break
        if is_thin and number not in ocr_pages:
            chunks = [Chunk(text, number, number)]
        else:
            chunks = pipeline._chunk_text(text, "pdf", remaining + 1)
        if len(chunks) > remaining:
            partial = True
        for chunk in chunks[:remaining]:
            # A page may have many token chunks. None of their line offsets
            # are PDF page numbers; all share the actual 1-based page.
            page_chunk = replace(chunk, loc=number, loc_end=number)
            thin = is_thin and number not in ocr_pages
            pieces.append(_Piece(page_chunk, not thin and _has_words(chunk.text),
                                 kind="page" if thin else "chunk",
                                 excerpt_override="" if thin else None,
                                 location_unit="page"))
        read_to = number
        if partial and len(chunks) > remaining:
            break
    # Still-thin pages (scanned slides the OCR could not rescue) get the
    # page-IMAGE vector alongside their thin FTS text (DigUp Indexer.swift
    # pageImage rows; budget 280). Glyph-order diversions already rendered.
    if want_page_images:
        for number in thin_pages:
            if number in garbled_pages:
                continue
            rendered = render_page(number)
            if rendered is not None:
                media_segs.append(_MediaSeg(
                    kind="page", path=rendered, loc=float(number),
                    loc_end=float(number), excerpt="",
                    budget=media.MEDIA_BUDGETS["pdf_page"], modality="image",
                ))
    info = {"pages": result.total}
    if partial:
        info.update(partial=True, read_to=read_to, read_end=read_to)
    if states:
        info["ocr_page_states"] = {str(p): state for p, state in sorted(states.items())}
    if ocr_pages:
        info["ocr_pages"] = ocr_pages
    if recovered:
        info["ocr_text_pages"] = sorted(recovered)
    if thin_pages:
        info.update(thin_text=True, thin_pages=thin_pages)
    if media_segs:
        info["page_images"] = len([m for m in media_segs if m.path is not None])
    if garbled_pages:
        info["garbled_pages"] = garbled_pages
    if ar_recovered:
        info["arocr_pages"] = sorted(ar_recovered)
    if lost_pages:
        info["pdf_text_problem"] = "lostLetters@" + ",".join(map(str, lost_pages))
    upgrades: set[str] = set()
    if ocr_pages:
        upgrades.add("visionocr")
    if ar_recovered:
        upgrades.add("arocr")
    version = extract_pdf.upgraded_pdftotext_version(result.tool_version, upgrades)
    if ocr_pages:
        detail = f"ocr: {len(ocr_pages)} page(s)"
        if thin_pages:
            detail += f"; thin-text: {len(thin_pages)} page(s) need OCR (scanned)"
    elif thin_pages:
        detail = f"thin-text: {len(thin_pages)} page(s) need OCR (scanned)"
    else:
        detail = None
    if ar_recovered:
        detail = (detail + "; " if detail else "") + (
            f"ar-ocr: {len(ar_recovered)} lost-letter page(s) replaced")
    if garbled_pages:
        detail = (detail + "; " if detail else "") + f"garbled: {len(garbled_pages)} page(s) as image+meaning"
    if not pieces and not media_segs:
        return _Outcome(capability="metadata_only", detail="page needs OCR (scanned)" if result.pages else "no text extracted",
                        info=info, extractor_version=version, title=result.title, partial=partial)
    return _Outcome(capability="content_indexed", detail=detail, info=info,
                    extractor_version=version, title=result.title, chunks=pieces,
                    media_segs=media_segs, partial=partial)
