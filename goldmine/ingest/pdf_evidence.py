"""Page-preserving PDF extraction; missing OCR pages remain explicit metadata."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from ..chunker import Chunk
from ..util import ToolError
from . import extract_pdf


def extract_pdf_outcome(pipeline, path, max_chunks):
    # Imported here to keep the extraction dispatch's result types in one place.
    from .extraction import _Outcome, _Piece, _has_words

    try:
        result = extract_pdf.extract_pages(path, max_pages=pipeline.cfg.ingest.max_pages)
    except ToolError:
        return _Outcome(capability="metadata_only", detail="pdftotext not found")
    except RuntimeError as exc:
        return _Outcome(capability="failed", detail=str(exc))
    candidates = [number for number, text in result.pages if len(text.strip()) < extract_pdf.THIN_PAGE_CHARS]
    states = {number: "unavailable" for number in candidates}
    recovered = {}
    if candidates and pipeline._ocr_probe():
        with tempfile.TemporaryDirectory(prefix="goldmine-pdfocr-") as td:
            recovered = pipeline._ocr_pages(path, candidates, Path(td), states=states)
    pieces = []
    thin_pages, ocr_pages = [], []
    partial = result.truncated
    read_to = None
    for number, source in result.pages:
        stripped = source.strip()
        upgraded = recovered.get(number, ("", None))[0].strip()
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
    version = result.tool_version
    if ocr_pages:
        version = version.replace("pdftotext@", "pdftotext+visionocr@", 1)
        detail = f"ocr: {len(ocr_pages)} page(s)"
        if thin_pages:
            detail += f"; thin-text: {len(thin_pages)} page(s) need OCR (scanned)"
    elif thin_pages:
        detail = f"thin-text: {len(thin_pages)} page(s) need OCR (scanned)"
    else:
        detail = None
    if not pieces:
        return _Outcome(capability="metadata_only", detail="page needs OCR (scanned)" if result.pages else "no text extracted",
                        info=info, extractor_version=version, title=result.title, partial=partial)
    return _Outcome(capability="content_indexed", detail=detail, info=info,
                    extractor_version=version, title=result.title, chunks=pieces, partial=partial)
