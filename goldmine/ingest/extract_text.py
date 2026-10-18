"""Text extraction: txt/md decoding, docx via stdlib zip+xml, rtf/doc/odt via textutil.

BOM-aware decoding ladder (V5b, D5b):
  --encoding override -> BOM (utf-8-sig / utf-16 / utf-32) -> utf-8 strict ->
  declared charset -> windows-1252 lossy (with a `lossy-decode:` detail note).

textutil/pdftotext are runtime-optional: probed via shutil.which at call time and
degraded to honestly-reported capabilities when absent.
"""

from __future__ import annotations

import codecs
import logging
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
import xml.etree.ElementTree as ET

from .. import util as _util

logger = logging.getLogger("goldmine.extract_text")

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DC_NS = "{http://purl.org/dc/elements/1.1/}"

#: Decompressed-size cap for word/document.xml, checked against the zip's
#: declared file_size BEFORE zf.read (P2-8 zip-bomb guard: a tiny .docx can
#: declare/inflate gigabytes of XML).
DOCX_MAX_XML_BYTES = 64 << 20

#: DOCTYPE/ENTITY in document.xml => XXE/entity-expansion risk; stdlib ET won't
#: resolve external entities, but refusing up front is cheaper than trusting it.
_DOCX_ENTITY_SCAN_BYTES = 64 * 1024


@dataclass
class Extracted:
    text: str
    title: str | None = None
    detail: str | None = None
    tool_version: str | None = None   # recorded per file as extractor_version (V5d)
    #: structured metadata merged into files.info by the pipeline (v2: eml
    #: headers From/Date/attachment count; epub has title only, so None).
    info: dict | None = None


def decode_bytes(
    data: bytes, declared_charset: str | None = None, override: str | None = None
) -> tuple[str, str | None]:
    """Bytes -> text through the decode ladder. Returns (text, detail | None)."""
    if override:
        try:
            codecs.lookup(override)
        except LookupError:
            logger.warning("decode: unknown --encoding %r — ignoring override", override)
        else:
            try:
                return data.decode(override), None
            except UnicodeDecodeError:
                return (
                    data.decode(override, errors="replace"),
                    f"lossy-decode: {override}",
                )
    # 4-byte BOMs before 2-byte ones: FF FE 00 00 starts with the utf-16 LE BOM.
    for bom, encoding in (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if data.startswith(bom):
            return data.decode(encoding), None
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        pass
    if declared_charset:
        try:
            return data.decode(declared_charset), None
        except (UnicodeDecodeError, LookupError):
            logger.warning(
                "decode: declared charset %r failed — falling back lossy", declared_charset
            )
    return data.decode("windows-1252", errors="replace"), "lossy-decode: windows-1252"


def _read_bytes(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def extract_txt(path: Path, encoding_override: str | None = None) -> Extracted:
    text, detail = decode_bytes(_read_bytes(path), override=encoding_override)
    return Extracted(text=text, title=None, detail=detail, tool_version="txt-stdlib.v1")


def extract_markdown(path: Path, encoding_override: str | None = None) -> Extracted:
    """txt path + title from the first `# ` heading."""
    text, detail = decode_bytes(_read_bytes(path), override=encoding_override)
    title = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and stripped[2:].strip():
            title = stripped[2:].strip()
            break
        if stripped:  # title must precede body text
            break
    return Extracted(text=text, title=title, detail=detail, tool_version="md-stdlib.v1")


def extract_docx(path: Path) -> Extracted:
    """docx = zip of XML: w:p paragraphs -> newlines, w:t runs -> text. stdlib only
    (D1 deviation note: removes the Cocoa/textutil dependency DigUp had).

    Hardening (P2-8): the declared decompressed size of word/document.xml is
    capped (DOCX_MAX_XML_BYTES), and DOCTYPE/ENTITY declarations are refused.
    Both raise ToolError -> the pipeline records a failed row with the reason.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            info = zf.getinfo("word/document.xml")
            if info.file_size > DOCX_MAX_XML_BYTES:
                raise _util.ToolError(
                    f"docx xml too large: word/document.xml declares "
                    f"{_util.human_size(info.file_size)} "
                    f"(cap {_util.human_size(DOCX_MAX_XML_BYTES)})"
                )
            xml_data = zf.read("word/document.xml")
            head = xml_data[:_DOCX_ENTITY_SCAN_BYTES]
            if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
                raise _util.ToolError(
                    "docx declares entities (DOCTYPE/ENTITY) — refused"
                )
            title = None
            try:
                core = ET.fromstring(zf.read("docProps/core.xml"))
                node = core.find(f"{_DC_NS}title")
                if node is not None and node.text and node.text.strip():
                    title = node.text.strip()
            except (KeyError, ET.ParseError):
                pass
    except zipfile.BadZipFile as exc:
        raise ValueError(f"corrupt docx: {exc}") from exc
    root = ET.fromstring(xml_data)
    paragraphs: list[str] = []
    for para in root.iter(f"{_W_NS}p"):
        runs = [node.text or "" for node in para.iter(f"{_W_NS}t")]
        paragraphs.append("".join(runs))
    text = "\n".join(paragraphs)
    return Extracted(text=text, title=title, detail=None, tool_version="docx-stdlib.v1")


def textutil_path() -> str | None:
    """Runtime probe — monkeypatch-friendly for 'textutil missing' tests."""
    return shutil.which("textutil")


def textutil_version() -> str:
    import platform

    return f"textutil@{platform.mac_ver()[0] or 'unknown'}"


def extract_via_textutil(path: Path, fmt_hint: str) -> Extracted | None:
    """rtf/doc/odt through macOS `textutil -convert txt -stdout`.

    Returns None when textutil is missing — the caller records capability
    metadata_only with detail "textutil not found" (honest degradation).
    Raises RuntimeError when textutil runs but fails.
    """
    binary = textutil_path()
    if binary is None:
        return None
    result = _util.run_tool(
        [binary, "-convert", "txt", "-stdout", str(path)], timeout_s=60.0
    )
    if result.timed_out:
        raise RuntimeError(f"textutil timeout after 60s ({fmt_hint})")
    if result.rc != 0:
        raise RuntimeError(
            f"textutil failed rc={result.rc}: {result.stderr.strip()[:200]}"
        )
    text = result.stdout
    return Extracted(
        text=text, title=None, detail="lossy-conversion: textutil" if not text.strip() else None,
        tool_version=textutil_version(),
    )


#: Formats extract_text handles for the pipeline dispatch (html/htm live in
#: extract_html; pdf in extract_pdf; eml in extract_mail; webpages are
#: fetched, not read).
TEXTUTIL_FORMATS = {"rtf": "rtf", "doc": "doc", "odt": "odt"}


# ---------------------------------------------------------------------- epub

#: Total uncompressed size cap for the epub's spine documents (02 §5.5: the
#: DOCX zip-bomb guard pattern, DOCX_MAX_XML_BYTES above, at 256 MB).
EPUB_MAX_BYTES = 256 << 20

_CONTAINER_NS = "{urn:oasis:names:tc:opendocument:xmlns:container}"
_OPF_NS = "{http://www.idpf.org/2007/opf}"
_DC_NS = "{http://purl.org/dc/elements/1.1/}"
EPUB_TOOL_VERSION = "epub-stdlib.v2"


def _refuse_entities(data: bytes, label: str) -> None:
    """DOCTYPE/ENTITY refusal, the docx guard copied verbatim (XXE-safe)."""
    if b"<!DOCTYPE" in data[:_DOCX_ENTITY_SCAN_BYTES] or b"<!ENTITY" in data[
        :_DOCX_ENTITY_SCAN_BYTES
    ]:
        raise _util.ToolError(f"epub {label} declares entities (DOCTYPE/ENTITY) — refused")


def extract_epub(path: Path) -> Extracted:
    """epub = zip of xhtml: container.xml -> OPF -> spine order -> html_to_text
    per document, joined with blank lines. dc:title -> title. stdlib only
    (02 §5.5). Raises ToolError on the size cap / entity refusal, ValueError
    on structural corruption — the pipeline maps both to honest rows."""
    from .extract_html import html_to_text

    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "META-INF/container.xml" not in names:
                raise ValueError("corrupt epub: no META-INF/container.xml")
            container = zf.read("META-INF/container.xml")
            _refuse_entities(container, "container.xml")
            opf_path = None
            root = ET.fromstring(container)
            for node in root.iter(f"{_CONTAINER_NS}rootfile"):
                candidate = node.get("full-path")
                if candidate and candidate in names:
                    opf_path = candidate
                    break
            if opf_path is None:
                raise ValueError("corrupt epub: container.xml names no readable OPF")

            opf = zf.read(opf_path)
            _refuse_entities(opf, opf_path)
            opf_root = ET.fromstring(opf)
            title = None
            node = opf_root.find(f".//{_DC_NS}title")
            if node is not None and node.text and node.text.strip():
                title = node.text.strip()

            opf_dir = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
            hrefs: dict[str, str] = {}
            for item in opf_root.iter(f"{_OPF_NS}item"):
                item_id, href = item.get("id"), item.get("href")
                if item_id and href:
                    hrefs[item_id] = href
            spine: list[str] = []
            for itemref in opf_root.iter(f"{_OPF_NS}itemref"):
                idref = itemref.get("idref")
                if idref and idref in hrefs:
                    spine.append(hrefs[idref])

            spine_names = {opf_dir + href for href in spine}
            total = sum(
                info.file_size for info in zf.infolist()
                if info.filename in spine_names
            )
            if total > EPUB_MAX_BYTES:
                raise _util.ToolError(
                    f"epub too large: spine declares {_util.human_size(total)} "
                    f"(cap {_util.human_size(EPUB_MAX_BYTES)})"
                )

            parts: list[str] = []
            for href in spine:
                full = opf_dir + href
                if full not in names:
                    continue
                data = zf.read(full)
                _refuse_entities(data, href)
                text, _ = html_to_text(decode_bytes(data)[0])
                if text.strip():
                    parts.append(text)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"corrupt epub: {exc}") from exc

    return Extracted(
        text="\n\n".join(parts),
        title=title,
        detail=None,
        tool_version=EPUB_TOOL_VERSION,
    )
