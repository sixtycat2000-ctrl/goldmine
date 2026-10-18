"""Tier-1/2: .eml and .epub extraction (v2 B6, 02 §5.5) + pipeline dispatch.

eml: stdlib email — Subject (RFC2047-decoded) -> title, From/Date -> info,
first text/plain body (html-only mail goes through the shared stripper),
attachments NEVER extracted, only counted. epub: zip of xhtml in spine
order, dc:title -> title, DOCX-pattern size cap + entity refusal.
"""

from __future__ import annotations

import base64
import zipfile
from pathlib import Path

import pytest

from goldmine.ingest.extract_mail import extract_eml
from goldmine.ingest.extract_text import EPUB_MAX_BYTES, extract_epub


def write_eml(path: Path, raw: str) -> Path:
    path.write_bytes(raw.replace("\n", "\r\n").encode("utf-8"))
    return path


PLAIN_EML = """\
From: Dana Wong <dana@example.org>
To: team@example.org
Subject: Aquarium filter maintenance schedule
Date: Mon, 12 Oct 2026 09:30:00 +0800
Message-ID: <m1@example.org>
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

The canister filter needs a rinse every two weeks.
Order replacement sponges part number FX-8841 before month end.
"""

RFC2047_EML = """\
From: =?utf-8?B?UGllcnJlIE1hcnRpbg==?=<pierre@example.fr>
Subject: =?utf-8?Q?R=C3=A9union_chantier_pont_levis?=
Date: Tue, 13 Oct 2026 14:00:00 +0200
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

Le chantier du pont levevis reprend lundi.
"""

HTML_ONLY_EML = """\
From: bot@example.com
Subject: Build notification 5571
Date: Wed, 14 Oct 2026 02:11:00 +0000
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8

<html><head><title>ignored</title><style>a{}</style></head>
<body><h1>Build 5571 green</h1><p>All tests passed on the staging cluster.</p>
</body></html>
"""

ATTACHMENT_EML = f"""\
From: alice@example.net
Subject: Invoice attachment quarter three
Date: Thu, 15 Oct 2026 11:00:00 -0700
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BOUND"

--BOUND
Content-Type: text/plain; charset=utf-8

Invoice quarter three attached; total 4820 EUR for the leviathan pump.
--BOUND
Content-Type: application/pdf; name="invoice-q3.pdf"
Content-Disposition: attachment; filename="invoice-q3.pdf"
Content-Transfer-Encoding: base64

{base64.b64encode(b"%PDF-1.4 fake pdf bytes").decode()}
--BOUND--
"""


def test_eml_plain_body_headers_into_info(tmp_path):
    eml = write_eml(tmp_path / "maint.eml", PLAIN_EML)
    ex = extract_eml(eml)
    assert "canister filter" in ex.text
    assert "FX-8841" in ex.text
    assert ex.title == "Aquarium filter maintenance schedule"
    assert ex.tool_version == "eml-stdlib.v2"
    assert ex.info["format"] == "eml"
    assert ex.info["from"] == "dana@example.org"
    assert ex.info["date"] == "2026-10-12T09:30:00+08:00"


def test_eml_rfc2047_subject_decoded(tmp_path):
    eml = write_eml(tmp_path / "reunion.eml", RFC2047_EML)
    ex = extract_eml(eml)
    assert ex.title == "Réunion chantier pont levis"
    assert "pont levevis" in ex.text


def test_eml_html_body_stripped(tmp_path):
    eml = write_eml(tmp_path / "build.eml", HTML_ONLY_EML)
    ex = extract_eml(eml)
    assert "Build 5571 green" in ex.text
    assert "All tests passed" in ex.text
    assert "<h1>" not in ex.text and "ignored" not in ex.text  # style/head gone
    assert ex.info["from"] == "bot@example.com"


def test_eml_attachments_never_extracted_only_counted(tmp_path):
    eml = write_eml(tmp_path / "invoice.eml", ATTACHMENT_EML)
    ex = extract_eml(eml)
    assert "leviathan pump" in ex.text
    assert "%PDF" not in ex.text and "fake pdf bytes" not in ex.text
    assert ex.info["attachments_not_indexed"] == 1


def test_eml_empty_body_is_empty_text_not_error(tmp_path):
    eml = write_eml(
        tmp_path / "empty.eml",
        "From: x@example.com\nSubject: nothing\n"
        "Content-Type: text/plain\n\n",
    )
    ex = extract_eml(eml)
    assert ex.text == ""
    assert ex.title == "nothing"


# ------------------------------------------------------------------- epub


def write_epub(path: Path, chapters: list[tuple[str, str]], title: str) -> Path:
    container = (
        '<?xml version="1.0"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    manifest = "".join(
        f'<item id="c{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(len(chapters))
    )
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
    opf = (
        '<?xml version="1.0"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-id="uid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:title>{title}</dc:title><dc:identifier id="uid">test-book</dc:identifier>'
        "</metadata>"
        f"<manifest>{manifest}</manifest><spine>{spine}</spine></package>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        for i, (_name, body) in enumerate(chapters):
            zf.writestr(
                f"OEBPS/ch{i}.xhtml",
                f'<html><body><h1>{_name}</h1><p>{body}</p></body></html>',
            )
    return path


def test_epub_spine_order_and_title(tmp_path):
    epub = write_epub(
        tmp_path / "book.epub",
        [
            ("Lighthouse manual", "The Fresnel lens rotates twice per minute."),
            ("Harbor log", "The tide gauge recorded 4.2 meters at dawn."),
        ],
        title="Coastal Almanac",
    )
    ex = extract_epub(epub)
    assert ex.title == "Coastal Almanac"
    assert ex.tool_version == "epub-stdlib.v2"
    assert ex.text.index("Fresnel lens") < ex.text.index("tide gauge")  # spine order
    assert "The tide gauge recorded 4.2 meters" in ex.text


def test_epub_corrupt_zip_raises_value_error(tmp_path):
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"not a zip at all")
    with pytest.raises(ValueError):
        extract_epub(bad)


def test_epub_missing_container_raises(tmp_path):
    epub = write_epub(tmp_path / "ok.epub", [("a", "b")], "T")
    stripped = tmp_path / "nocontainer.epub"
    with zipfile.ZipFile(stripped, "w") as zf:  # valid zip, wrong layout
        zf.writestr("OEBPS/content.opf", "<package/>")
    with pytest.raises(ValueError):
        extract_epub(stripped)


def test_epub_entity_refusal_and_size_cap(tmp_path, monkeypatch):
    epub = write_epub(tmp_path / "book.epub", [("a", "b")], "T")
    evil = tmp_path / "evil.epub"
    container = (
        '<?xml version="1.0"?>'
        "<!DOCTYPE container [<!ENTITY xxe SYSTEM \"file:///etc/passwd\">]>"
        "<container/>"
    )
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", container)
    from goldmine.ingest import extract_text
    from goldmine.util import ToolError

    with pytest.raises(ToolError):
        extract_epub(evil)

    monkeypatch.setattr(extract_text, "EPUB_MAX_BYTES", 8)
    with pytest.raises(ToolError):
        extract_epub(epub)
    assert EPUB_MAX_BYTES > 8  # sanity: the real cap was never mutated


# --------------------------------------------------------------- pipeline


def _run_pipeline(tmp_path, files: dict[str, str], mock_backend):
    from goldmine.config import Config
    from goldmine.ingest import IngestPipeline
    from goldmine.store import Store

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for name, raw in files.items():
        target = corpus / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(raw, bytes):
            target.write_bytes(raw)
        elif name.endswith(".epub"):
            write_epub(target, *raw)
        else:
            target.write_bytes(raw.replace("\n", "\r\n").encode("utf-8"))

    cfg = Config(home=str(tmp_path / "gmhome"))
    store = Store(tmp_path / "index.sqlite")
    pipeline = IngestPipeline(cfg, store, mock_backend)
    report = pipeline.run([str(corpus)])
    store.close()
    return report


def test_eml_epub_ingest_end_to_end(tmp_path, mock_backend):
    files = {
        "maint.eml": PLAIN_EML,
        "invoice.eml": ATTACHMENT_EML,
    }
    report = _run_pipeline(tmp_path, files, mock_backend)
    assert report.failed == 0, report.failures
    assert report.content_indexed == 2
    eml_kind = report.by_kind.get("doc", {})
    assert eml_kind.get("content_indexed", 0) == 2


def test_epub_ingest_end_to_end(tmp_path, mock_backend, tmp_path_factory):
    corpus = tmp_path / "corpus2"
    corpus.mkdir()
    write_epub(
        corpus / "almanac.epub",
        [("Lighthouse manual", "The Fresnel lens rotates twice per minute."),
         ("Harbor log", "The tide gauge recorded 4.2 meters at dawn.")],
        title="Coastal Almanac",
    )
    from goldmine.config import Config
    from goldmine.ingest import IngestPipeline
    from goldmine.store import Store

    cfg = Config(home=str(tmp_path / "gmhome2"))
    store = Store(tmp_path / "index2.sqlite")
    pipeline = IngestPipeline(cfg, store, mock_backend)
    report = pipeline.run([str(corpus)])
    store.close()
    assert report.failed == 0, report.failures
    assert report.by_kind.get("doc", {}).get("content_indexed") == 1


def test_eml_info_lands_in_file_row(tmp_path, mock_backend):
    from goldmine.config import Config
    from goldmine.ingest import IngestPipeline
    from goldmine.store import Store

    corpus = tmp_path / "corpus3"
    corpus.mkdir()
    write_eml(corpus / "maint.eml", PLAIN_EML)
    cfg = Config(home=str(tmp_path / "gmhome3"))
    store = Store(tmp_path / "index3.sqlite")
    IngestPipeline(cfg, store, mock_backend).run([str(corpus)])
    rec = store.find_by_path(str(corpus / "maint.eml"))
    assert rec is not None and rec.capability == "content_indexed"
    assert rec.info["format"] == "eml"
    assert rec.info["from"] == "dana@example.org"
    assert rec.extractor_version == "eml-stdlib.v2"
    store.close()
