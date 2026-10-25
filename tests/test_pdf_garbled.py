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
    """OCR unavailable: the honest fallback is detection + info marker —
    the text layer keeps its words (DigUp takes the replace path only when
    it can render+OCR; goldmine's B2 adds the replace, this pins the floor).
    states mark the page 'unavailable' so a later OCR-enabled run re-does
    the file once (_needs_ocr_redo upgrade path)."""
    pipeline = _pipeline(tmp_path)
    _fake_render(monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline, "_ocr_probe", lambda: False)
    pages = [(1, (_LOST + "more real prose follows here ") * 8)]
    _fake_pages(monkeypatch, pages)
    outcome = pipeline._extract_pdf(Path("/docs/x.pdf"), 100)
    assert outcome.info["pdf_text_problem"] == "lostLetters@1"
    assert outcome.info["ocr_page_states"] == {"1": "unavailable"}
    # no diversion: the text layer keeps its words
    assert any("\u0627" in p.chunk.text for p in outcome.chunks)
    assert not outcome.media_segs
    assert outcome.extractor_version.startswith("pdftotext@")


# ------------------------------------------------- B2: lostLetters -> OCR replace

_VALID_AR = ("الكتاب المدرسي في المكتبة والسلام عليكم ورحمة الله. " * 6).strip()


def _ar_pipeline(tmp_path, monkeypatch, ocr_pages):
    """Probe on + a canned _ocr_pages: {page: (text, ms)} — the fake render
    keeps pdftoppm available so the pipeline's own gates pass."""
    pipeline = _pipeline(tmp_path)
    _fake_render(monkeypatch, tmp_path)

    def fake_ocr(_path, pages, _tmp, *, states=None):
        if states is not None:
            states.update({p: "complete" for p in pages})
        return {p: ocr_pages[p] for p in pages if p in ocr_pages}

    monkeypatch.setattr(pipeline, "_ocr_probe", lambda: True)
    monkeypatch.setattr(pipeline, "_ocr_pages", fake_ocr)
    return pipeline


def _fake_pages(monkeypatch, pages, total=None):
    """extract_pages monkeypatch with the REAL versioned tool string, so
    composed extractor versions match the fingerprints registry."""
    monkeypatch.setattr(
        extract_pdf, "extract_pages",
        lambda *a, **kw: PdfResult(
            pages=pages, total=total if total is not None else len(pages),
            tool_version=f"pdftotext@{extract_pdf.pdftotext_version() or 'unknown'}"),
    )


def test_lost_letters_replaced_when_ocr_valid(tmp_path, monkeypatch):
    pipeline = _ar_pipeline(tmp_path, monkeypatch, {1: (_VALID_AR, 5.0)})
    pages = [(1, (_LOST + "prose padding to pass thin gate " * 10))]
    _fake_pages(monkeypatch, pages)
    outcome = pipeline._extract_pdf(Path("/docs/x.pdf"), 100)
    # the broken layer never lands; the OCR text does
    assert not any("?\u0633" in (p.chunk.text or "") for p in outcome.chunks)
    assert any(p.chunk.text.startswith("الكتاب") for p in outcome.chunks)
    assert outcome.info["arocr_pages"] == [1]
    assert "pdf_text_problem" not in outcome.info
    assert outcome.extractor_version.startswith("pdftotext+arocr@")
    assert "ar-ocr: 1 lost-letter page(s) replaced" in (outcome.detail or "")
    # normal chunking: full-text path, no page-image stacking
    assert not outcome.media_segs


def test_lost_letters_kept_when_ocr_not_arabic(tmp_path, monkeypatch):
    latin = "The quick brown fox jumps over the lazy dog. " * 12
    pipeline = _ar_pipeline(tmp_path, monkeypatch, {1: (latin, 5.0)})
    pages = [(1, (_LOST + "prose padding to pass thin gate " * 10))]
    _fake_pages(monkeypatch, pages)
    outcome = pipeline._extract_pdf(Path("/docs/x.pdf"), 100)
    assert outcome.info["pdf_text_problem"] == "lostLetters@1"
    assert "arocr_pages" not in outcome.info
    assert any("\u0627" in p.chunk.text for p in outcome.chunks)  # original kept
    assert outcome.extractor_version.startswith("pdftotext@")     # no arocr tag
    assert outcome.info["ocr_page_states"] == {"1": "kept"}


def test_lost_letters_kept_when_ocr_too_short(tmp_path, monkeypatch):
    pipeline = _ar_pipeline(tmp_path, monkeypatch, {1: ("كتاب قصير", 5.0)})
    pages = [(1, (_LOST + "prose padding to pass thin gate " * 10))]
    _fake_pages(monkeypatch, pages)
    outcome = pipeline._extract_pdf(Path("/docs/x.pdf"), 100)
    assert outcome.info["pdf_text_problem"] == "lostLetters@1"
    assert "arocr_pages" not in outcome.info


def test_lost_letters_kept_when_ocr_itself_pathological(tmp_path, monkeypatch):
    bad_ar = (_LOST + "وحتى مع نص عربي أطول من مئتي حرف لاجتياز بوابة الطول. ") * 6
    assert extract_pdf.page_text_problem(bad_ar) == "lostLetters"
    pipeline = _ar_pipeline(tmp_path, monkeypatch, {1: (bad_ar, 5.0)})
    pages = [(1, (_LOST + "prose padding to pass thin gate " * 10))]
    _fake_pages(monkeypatch, pages)
    outcome = pipeline._extract_pdf(Path("/docs/x.pdf"), 100)
    assert outcome.info["pdf_text_problem"] == "lostLetters@1"
    assert "arocr_pages" not in outcome.info


def test_arocr_combines_with_thin_visionocr_version(tmp_path, monkeypatch):
    """Page 1 thin (OCR-upgraded -> visionocr), page 2 lostLetters (ar
    replace): one file, both tags, the composed string must be exactly what
    fingerprints.current_extractors registers."""
    long_latin = "The lighthouse keeper logged the harbor chart depth. " * 12
    pipeline = _ar_pipeline(tmp_path, monkeypatch,
                            {1: (long_latin, 5.0), 2: (_VALID_AR, 5.0)})
    pages = [(1, "short"), (2, (_LOST + "prose padding to pass thin gate " * 10))]
    _fake_pages(monkeypatch, pages)
    outcome = pipeline._extract_pdf(Path("/docs/x.pdf"), 100)
    assert outcome.extractor_version.startswith("pdftotext+arocr+visionocr@")
    assert outcome.info["arocr_pages"] == [2]
    assert outcome.info["ocr_pages"] == [1]
    from goldmine.fingerprints import current_extractors

    assert outcome.extractor_version in current_extractors()


def test_all_pdftotext_variants_registered_current():
    from goldmine.fingerprints import current_extractors

    versions = current_extractors()
    for stem in ("pdftotext", "pdftotext+visionocr", "pdftotext+arocr",
                 "pdftotext+arocr+visionocr"):
        assert any(v.startswith(stem + "@") for v in versions), stem


def test_media_version_registry_matches_the_constant():
    """The registry literal and extraction's shared constant must be equal —
    drift means out-of-registry rows re-extracting forever (review D1)."""
    from goldmine.fingerprints import EXTRACTOR_VERSIONS
    from goldmine.ingest.media import MEDIA_EXTRACTOR_VERSION

    assert EXTRACTOR_VERSIONS["media"] == MEDIA_EXTRACTOR_VERSION
    assert EXTRACTOR_VERSIONS["asr+media"].endswith(MEDIA_EXTRACTOR_VERSION)


def test_media_preprocessing_v5_tail_tag():
    from goldmine.ingest.media import MEDIA_PREPROCESSING, TAIL_MIN_NEW_S

    assert MEDIA_PREPROCESSING.startswith("v5:")
    assert ",tail-min2s;" in MEDIA_PREPROCESSING
    assert TAIL_MIN_NEW_S == 2.0


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
