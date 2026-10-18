"""Tier-2: PDF extraction — pdftotext round-trip on hand-built PDF fixtures.

Uses the real poppler tools when installed (they are, per the machine snapshot)
and skips with the doctor-style hint when not. "Tool missing" behavior is tested
via monkeypatched probes, no tool needed.
"""

from __future__ import annotations

import pytest

from goldmine.backends.mock import MockBackend
from goldmine.config import Config
from goldmine.ingest import IngestPipeline, extract_pdf
from goldmine.store import Store

from conftest import make_pdf

pytestmark = pytest.mark.skipif(
    extract_pdf.pdftotext_path() is None,
    reason="pdftotext not installed (brew install poppler)",
)


def test_extract_pages_roundtrip(tmp_path):
    path = make_pdf(
        tmp_path / "two.pdf",
        ["Hello PDF page one with a known sentence K7Q2LM", "Second page text here."],
        title="Test Doc",
    )
    result = extract_pdf.extract_pages(path)
    assert [n for n, _ in result.pages] == [1, 2]
    assert "K7Q2LM" in result.pages[0][1]
    assert "Second page text here." in result.pages[1][1]
    assert result.total == 2
    assert result.truncated is False
    assert result.read_to == 2
    assert result.tool_version.startswith("pdftotext@")
    assert result.title == "Test Doc"


def test_pdf_info(tmp_path):
    path = make_pdf(tmp_path / "info.pdf", ["one line"], title="Info Title")
    info = extract_pdf.pdf_info(path)
    assert info.get("pages") == 1
    assert info.get("title") == "Info Title"


def test_max_pages_cap_marks_partial(tmp_path):
    path = make_pdf(
        tmp_path / "five.pdf", [f"Page {n} body text for the cap test." for n in range(1, 6)]
    )
    result = extract_pdf.extract_pages(path, max_pages=3)
    assert result.total == 5
    assert [n for n, _ in result.pages] == [1, 2, 3]
    assert result.truncated is True
    assert result.read_to == 3


def test_first_pass_then_steps_reads_all(tmp_path):
    path = make_pdf(
        tmp_path / "forty.pdf", [f"Page {n} of the batching corpus." for n in range(1, 41)]
    )
    result = extract_pdf.extract_pages(path)  # 30 first pass + 10 in step 2
    assert [n for n, _ in result.pages] == list(range(1, 41))
    assert result.truncated is False


def _pipeline(tmp_path):
    cfg = Config()
    cfg.home = tmp_path / "home"
    store = Store(cfg.home / "index.sqlite")
    return cfg, store


def test_pipeline_thin_page_is_fts_only_no_vector(tmp_path):
    cfg, store = _pipeline(tmp_path)
    # page 1 rich text, page 2 thin (<200 chars), page 3 rich again; the rich
    # page uses many short lines (a long single line clips at the page edge)
    rich = "The scheduler evicts a pod under pressure.\n" * 10
    path = make_pdf(tmp_path / "mixed.pdf", [rich, "thin page", rich], title="Mixed")
    pipeline = IngestPipeline(cfg, store, MockBackend(dim=768))
    report = pipeline.run([str(path.parent)])
    assert report.content_indexed == 1
    rows = store.db.execute(
        """SELECT s.kind, s.modality, s.loc, s.excerpt, s.vec IS NOT NULL,
                  ft.body
           FROM segments s
           JOIN files f ON f.id = s.file
           JOIN fts ft ON ft.rowid = s.id
           WHERE f.kind = 'pdf' AND s.kind != 'name' ORDER BY s.id"""
    ).fetchall()
    thin = [r for r in rows if r[0] == "page"]
    assert len(thin) == 1
    assert thin[0][1] == "none"        # modality none
    assert thin[0][2] == 2.0           # loc = page number
    assert thin[0][3] == ""            # empty excerpt (honest: nothing to show)
    assert thin[0][4] == 0            # no vector
    assert "thin page" in thin[0][5]   # but the FTS text is kept
    assert any(r[0] == "chunk" and r[4] for r in rows)  # rich pages carry vectors
    rec = store.find_by_path(str(path))
    assert rec.info["thin_text"] is True and rec.info["thin_pages"] == [2]
    assert "OCR" in rec.detail
    assert rec.extractor_version.startswith("pdftotext@")
    store.close()


def test_pipeline_scanned_pdf_is_metadata_only(tmp_path):
    cfg, store = _pipeline(tmp_path)
    path = make_pdf(tmp_path / "scan.pdf", ["", ""], title="Scanned")  # no text layer
    pipeline = IngestPipeline(cfg, store, MockBackend(dim=768))
    report = pipeline.run([str(path.parent)])
    assert report.metadata_only == 1
    rec = store.find_by_path(str(path))
    assert rec.capability == "metadata_only"
    assert rec.detail == "page needs OCR (scanned)"
    store.close()


def test_pipeline_pdftotext_missing_is_metadata_only(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_pdf, "pdftotext_path", lambda: None)
    monkeypatch.setattr(extract_pdf, "pdfinfo_path", lambda: None)
    cfg, store = _pipeline(tmp_path)
    path = make_pdf(tmp_path / "x.pdf", ["some words"])
    pipeline = IngestPipeline(cfg, store, MockBackend(dim=768))
    report = pipeline.run([str(path.parent)])
    assert report.metadata_only == 1
    rec = store.find_by_path(str(path))
    assert rec.capability == "metadata_only"
    assert rec.detail == "pdftotext not found"
    store.close()


def test_corrupt_pdf_is_failed_row(tmp_path):
    cfg, store = _pipeline(tmp_path)
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4 this is not a pdf at all\n")
    pipeline = IngestPipeline(cfg, store, MockBackend(dim=768))
    report = pipeline.run([str(path.parent)])
    assert report.failed == 1
    rec = store.find_by_path(str(path))
    assert rec.capability == "failed"
    store.close()
