"""Text-layer pathology + page-image wiring (digup-plan-002 A1/T9).

page_text_problem is the DigUp PageTextProblem.check port (SearchText.swift:
165-215); the diversion tests pin the §3.5 behavior: glyph-order pages trade
their text for a page-IMAGE vector + a meaning-only garbled vector; thin
pages gain the page image; lost-letter pages are detected and MARKED only.
"""

from __future__ import annotations

from pathlib import Path

from goldmine.backends.mock import MockBackend
from goldmine.config import Config
from goldmine.ingest import IngestPipeline, extract_pdf
from goldmine.ingest.extract_pdf import PdfResult, page_text_problem

# Bengali: vowel sign U+09BF after a NON-consonant (Latin 'a') = misplaced
_GARBLED = "a \u09bf" * 12
# real Bengali word shape: consonant U+0995 carries the vowel sign
_CLEAN_BENGALI = "\u0995\u09bf" * 12
# Arabic lam-ligature loss: bare alef + '?' + arabic letter, repeated
_LOST = "\u0627?\u0633 " * 3


def test_page_text_problem_detection():
    assert page_text_problem(_GARBLED) == "glyphOrder"
    assert page_text_problem(_CLEAN_BENGALI) is None
    assert page_text_problem(_LOST) == "lostLetters"
    assert page_text_problem("plain english text") is None
    assert page_text_problem("") is None


def _pipeline(tmp_path, *, image_capable: bool = True):
    backend = MockBackend(multimodal=image_capable)
    return IngestPipeline(Config(), None, backend, lexical_only=False)


def _fake_render(monkeypatch, tmp_path):
    rendered = tmp_path / "page-1.png"
    rendered.write_bytes(b"\x89PNG-fake")
    monkeypatch.setattr(extract_pdf, "pdftoppm_path", lambda: "/usr/bin/pdftoppm")
    monkeypatch.setattr(
        extract_pdf, "render_page_png",
        lambda path, page, prefix, **kw: rendered,
    )
    return rendered


def test_glyph_order_page_diverts_to_image_plus_meaning_vector(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path)
    _fake_render(monkeypatch, tmp_path)
    pages = [(1, "plain readable page with real sentences " * 10),
             (2, _GARBLED + " tail words")]
    monkeypatch.setattr(extract_pdf, "extract_pages",
                        lambda *a, **kw: PdfResult(pages=pages, total=2))
    outcome = pipeline._extract_pdf(Path("/docs/x.pdf"), 100)
    image_segs = [m for m in outcome.media_segs if m.path is not None]
    garbled_segs = [m for m in outcome.media_segs if m.garbled is not None]
    assert len(image_segs) == 1 and image_segs[0].budget == 280
    assert image_segs[0].modality == "image" and image_segs[0].loc == 2.0
    assert len(garbled_segs) == 1 and garbled_segs[0].modality == "text"
    assert garbled_segs[0].garbled.startswith("a \u09bf")
    # the garbled words never reach the text pieces (FTS stays clean)
    assert all("\u09bf" not in (p.chunk.text or "") for p in outcome.chunks)
    assert outcome.info["garbled_pages"] == [2]
    assert outcome.info["page_images"] == 1


def test_glyph_order_without_image_capability_keeps_text(tmp_path, monkeypatch):
    """Text-only backend: the honest fallback is today's behavior — the text
    stays as chunks (DigUp only takes the garbled path when it can render)."""
    pipeline = _pipeline(tmp_path, image_capable=False)
    _fake_render(monkeypatch, tmp_path)
    pages = [(2, _GARBLED + " readable tail words for the chunk")]
    monkeypatch.setattr(extract_pdf, "extract_pages",
                        lambda *a, **kw: PdfResult(pages=pages, total=2))
    outcome = pipeline._extract_pdf(Path("/docs/x.pdf"), 100)
    assert outcome.media_segs == []
    assert any("\u09bf" in p.chunk.text for p in outcome.chunks)


def test_lost_letters_detected_and_marked_only(tmp_path, monkeypatch):
    """digup-plan-002 A1 scope: lostLetters is DETECTION + info marker; the
    Arabic OCR-replace content change is a deferred follow-up."""
    pipeline = _pipeline(tmp_path)
    _fake_render(monkeypatch, tmp_path)
    pages = [(1, (_LOST + "more real prose follows here ") * 8)]
    monkeypatch.setattr(extract_pdf, "extract_pages",
                        lambda *a, **kw: PdfResult(pages=pages, total=1))
    outcome = pipeline._extract_pdf(Path("/docs/x.pdf"), 100)
    assert outcome.info["pdf_text_problem"] == "lostLetters@1"
    # no diversion: the text layer keeps its words
    assert any("\u0627" in p.chunk.text for p in outcome.chunks)
    assert not outcome.media_segs


def test_thin_pages_gain_page_image_segments(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path)
    _fake_render(monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline, "_ocr_probe", lambda: False)
    pages = [(1, "short"), (2, "real prose page with plenty of words " * 20)]
    monkeypatch.setattr(extract_pdf, "extract_pages",
                        lambda *a, **kw: PdfResult(pages=pages, total=2))
    outcome = pipeline._extract_pdf(Path("/docs/x.pdf"), 100)
    page_segs = [m for m in outcome.media_segs if m.path is not None]
    assert len(page_segs) == 1  # only the thin page renders
    assert page_segs[0].loc == 1.0 and page_segs[0].budget == 280
    assert page_segs[0].modality == "image"
    # the thin FTS text still lands as a page piece (vectorless)
    assert {p.chunk.loc for p in outcome.chunks} == {1, 2}
