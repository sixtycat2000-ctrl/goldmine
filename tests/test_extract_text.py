"""Tier-1/2: extract_text per format, incl. textutil-missing degradation."""

from __future__ import annotations

import pytest

from goldmine.ingest import extract_text
from goldmine.ingest.extract_text import (
    extract_docx,
    extract_markdown,
    extract_txt,
    extract_via_textutil,
)
from tests.conftest import make_docx


def test_extract_txt_utf8(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello ünïcode\nsecond line\n", encoding="utf-8")
    ex = extract_txt(p)
    assert ex.text.startswith("hello ünïcode")
    assert ex.detail is None
    assert ex.tool_version == "txt-stdlib.v1"


def test_extract_txt_utf16_bom(tmp_path):
    p = tmp_path / "bom.txt"
    p.write_text("utf-16 payload", encoding="utf-16")
    ex = extract_txt(p)
    assert ex.text == "utf-16 payload"
    assert ex.detail is None


def test_extract_txt_latin1_lossy(tmp_path):
    p = tmp_path / "latin1.txt"
    p.write_text("café — brève", encoding="windows-1252")
    ex = extract_txt(p)
    assert ex.detail == "lossy-decode: windows-1252"
    assert ex.text.startswith("caf")


def test_extract_txt_encoding_override(tmp_path):
    p = tmp_path / "latin1.txt"
    p.write_text("café — brève", encoding="windows-1252")
    ex = extract_txt(p, encoding_override="windows-1252")
    assert ex.text == "café — brève"
    assert ex.detail is None


def test_extract_markdown_title(tmp_path):
    p = tmp_path / "guide.md"
    p.write_text("# The Title\n\nbody text\n", encoding="utf-8")
    ex = extract_markdown(p)
    assert ex.title == "The Title"
    assert "body text" in ex.text
    # title must precede body: a later heading does not win
    p2 = tmp_path / "late.md"
    p2.write_text("intro first\n\n# Late Title\n", encoding="utf-8")
    assert extract_markdown(p2).title is None


def test_extract_docx(tmp_path):
    p = tmp_path / "report.docx"
    make_docx(p, ["First paragraph.", "Second paragraph with ünïcode."], title="The Report")
    ex = extract_docx(p)
    assert "First paragraph." in ex.text
    assert "Second paragraph with ünïcode." in ex.text
    assert "\n" in ex.text  # paragraphs became newlines
    assert ex.title == "The Report"
    assert ex.tool_version == "docx-stdlib.v1"


def test_extract_docx_corrupt_fails(tmp_path):
    p = tmp_path / "bad.docx"
    p.write_bytes(b"this is not a zip file")
    with pytest.raises(ValueError):
        extract_docx(p)


def test_extract_docx_over_declared_size_refused(tmp_path, monkeypatch):
    """P2-8(a) zip-bomb guard: word/document.xml declaring more than the cap is
    refused BEFORE zf.read. The cap is monkeypatched small so the fixture stays
    tiny (the declared size is what is checked, real inflation irrelevant)."""
    import zipfile

    from goldmine.util import ToolError

    p = tmp_path / "bomb.docx"
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        # highly compressible payload: a real bomb inflates huge from tiny bytes
        zf.writestr("word/document.xml", b"<w:document><w:body/></w:document>" + b"0" * 4096)
    monkeypatch.setattr(extract_text, "DOCX_MAX_XML_BYTES", 1024)
    with pytest.raises(ToolError, match="docx xml too large"):
        extract_docx(p)


def test_extract_docx_declaring_entities_refused(tmp_path):
    """P2-8(b): DOCTYPE/ENTITY declarations (XXE / entity-expansion vector)
    are refused up front."""
    import zipfile

    from goldmine.util import ToolError

    p = tmp_path / "entity.docx"
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<!DOCTYPE w:document [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&xxe;</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("word/document.xml", document)
    with pytest.raises(ToolError, match="entities"):
        extract_docx(p)


def test_textutil_missing_returns_none(tmp_path, monkeypatch):
    """Degradation contract: no textutil -> None -> caller records metadata_only."""
    monkeypatch.setattr(extract_text.shutil, "which", lambda name: None)
    p = tmp_path / "doc.rtf"
    p.write_text("{\\rtf1 hello}", encoding="utf-8")
    assert extract_via_textutil(p, "rtf") is None


def test_textutil_success(tmp_path, monkeypatch):
    class FakeResult:
        rc = 0
        stdout = "converted text body\n"
        stderr = ""
        timed_out = False

    monkeypatch.setattr(extract_text.shutil, "which", lambda name: "/usr/bin/textutil")
    monkeypatch.setattr(
        extract_text._util, "run_tool", lambda argv, **kw: FakeResult()
    )
    p = tmp_path / "doc.rtf"
    p.write_text("{\\rtf1 hello}", encoding="utf-8")
    ex = extract_via_textutil(p, "rtf")
    assert ex is not None
    assert ex.text == "converted text body\n"
    assert ex.tool_version and ex.tool_version.startswith("textutil@")


def test_textutil_failure_raises(tmp_path, monkeypatch):
    class FakeResult:
        rc = 1
        stdout = ""
        stderr = "boom"
        timed_out = False

    monkeypatch.setattr(extract_text.shutil, "which", lambda name: "/usr/bin/textutil")
    monkeypatch.setattr(
        extract_text._util, "run_tool", lambda argv, **kw: FakeResult()
    )
    with pytest.raises(RuntimeError, match="rc=1"):
        extract_via_textutil(tmp_path / "doc.rtf", "rtf")


def test_textutil_timeout_raises(tmp_path, monkeypatch):
    class FakeResult:
        rc = None
        stdout = ""
        stderr = ""
        timed_out = True

    monkeypatch.setattr(extract_text.shutil, "which", lambda name: "/usr/bin/textutil")
    monkeypatch.setattr(
        extract_text._util, "run_tool", lambda argv, **kw: FakeResult()
    )
    with pytest.raises(RuntimeError, match="timeout"):
        extract_via_textutil(tmp_path / "doc.rtf", "rtf")
