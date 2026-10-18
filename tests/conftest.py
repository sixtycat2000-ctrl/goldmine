"""Shared fixtures: redirected GOLDMINE_HOME/CONFIG, mock backend, fixture builders.

Everything here is offline: no network, no llama-server, no model files. The only
optionally-probed tool (textutil) is monkeypatched per-test.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goldmine.backends.mock import MockBackend  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_ocr_autocompile(monkeypatch):
    """Suite-wide: never swiftc-compile the real goldmine-ocr inside a test.
    OCR tests point GOLDMINE_OCR_HELPER at the conftest stub explicitly; the
    live suite opts in with GOLDMINE_OCR_AUTOCOMPILE=1."""
    monkeypatch.setenv("GOLDMINE_OCR_AUTOCOMPILE", "0")


@pytest.fixture
def tmp_home(monkeypatch, tmp_path):
    """Redirect every persistent location into tmp so tests never touch user data."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOLDMINE_HOME", str(tmp_path / "gmhome"))
    monkeypatch.delenv("GOLDMINE_CONFIG", raising=False)
    monkeypatch.delenv("GOLDMINE_BACKEND", raising=False)
    monkeypatch.delenv("GOLDMINE_LOG_LEVEL", raising=False)
    monkeypatch.delenv("GOLDMINE_MODEL", raising=False)
    monkeypatch.delenv("GOLDMINE_LLAMA_SERVER", raising=False)
    monkeypatch.delenv("GOLDMINE_FORESIGHT_SOCKET", raising=False)
    # v2 OCR: no stray helper override, and NEVER autocompile the real Swift
    # helper inside offline tests (ocr tests point GOLDMINE_OCR_HELPER at a
    # stub explicitly).
    monkeypatch.delenv("GOLDMINE_OCR_HELPER", raising=False)
    monkeypatch.setenv("GOLDMINE_OCR_AUTOCOMPILE", "0")
    return tmp_path


@pytest.fixture
def mock_backend():
    return MockBackend(dim=768)


# ------------------------------------------------------------------ builders


def make_docx(path: Path, paragraphs: list[str], title: str | None = None) -> None:
    """Minimal docx: [Content_Types].xml + word/document.xml (+ optional core.xml)."""
    w_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(
        f"<w:p><w:r><w:t>{escape(p)}</w:t></w:r></w:p>" for p in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{w_ns}"><w:body>{body}</w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("word/document.xml", document)
        if title is not None:
            core = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                f"<dc:title>{escape(title)}</dc:title></cp:coreProperties>"
            )
            zf.writestr("docProps/core.xml", core)


HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="windows-1252">
  <title>Brève histoire du café</title>
  <style>body { color: red; }</style>
  <script>var tracking = 1;</script>
</head>
<body>
  <h1>Caf&eacute; Notes</h1>
  <p>First paragraph about roasting beans &#233;lightly.</p>
  <div>Second block with <b>bold</b> and <a href="/x">a link</a>.</div>
  <!-- a comment that must vanish -->
  <p>Third paragraph.</p>
</body>
</html>
"""


@pytest.fixture
def fixture_tree(tmp_path):
    """The standard fixture corpus: docs in several formats + trap files."""
    root = tmp_path / "corpus"
    docs = root / "docs"
    docs.mkdir(parents=True)

    (docs / "notes.txt").write_text(
        "The scheduler evicts a pod when a node is under pressure.\n"
        "Tolerations keep workloads running.\n",
        encoding="utf-8",
    )
    (docs / "guide.md").write_text(
        "# Sourdough Guide\n\nFeed the starter daily.\n\nBulk ferment for four hours.\n",
        encoding="utf-8",
    )
    (docs / "page.html").write_text(HTML_PAGE, "windows-1252")
    make_docx(docs / "report.docx", ["Quarterly report body.", "Second paragraph."],
              title="Report")
    (docs / "latin1.txt").write_text("café crème — brève", encoding="windows-1252")
    (docs / "bom.txt").write_text("BOM-prefixed utf-16 text", encoding="utf-16")
    (docs / "numbertext.txt").write_text("4815162342 9988776655\n", encoding="utf-8")

    # trap files
    (docs / "secret.pem").write_text("-----BEGIN PRIVATE KEY-----\nnope\n", encoding="utf-8")
    (docs / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (docs / "app.min.js").write_text("var a=1;var b=2;", encoding="utf-8")
    (docs / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    # v2: a REAL decodable PNG (>= minSide 256) — sips thumbnails it and the
    # multimodal tests exercise the v2 image path; the name keeps the
    # screenshot prefix for detection.
    (docs / "Screenshot 2026-01-01 at 10.00.00.png").write_bytes(tiny_png(600, 400))

    # skipped folders
    (root / "node_modules" / "left-pad").mkdir(parents=True)
    (root / "node_modules" / "left-pad" / "index.js").write_text("module.exports=1;", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\x00" * 16)

    # a code project: off by default, indexed with --code
    proj = root / "proj"
    (proj / ".git").mkdir(parents=True)
    (proj / "main.py").write_text("def main():\n    print('hello')\n", encoding="utf-8")
    (proj / "utils.py").write_text("def util(x):\n    return x\n", encoding="utf-8")

    return root


# ------------------------------------------------------------------ PDF builder


def make_pdf(path: Path, pages: list[str], title: str | None = None) -> Path:
    """Minimal valid multi-page PDF built from raw bytes (no deps, no blobs).

    One content stream per page; each non-empty line of `pages[i]` becomes a
    Tj text run that pdftotext recovers verbatim. Parentheses/backslashes in
    the text are escaped per the PDF string syntax.
    """
    objects: list[bytes] = []  # 1-based object numbers assigned below

    def obj(number: int, body: bytes) -> bytes:
        return b"%d 0 obj\n%s\nendobj\n" % (number, body)

    def text_stream(lines: list[str]) -> bytes:
        parts = [b"BT /F1 12 Tf 72 720 Td 14 TL"]
        for line in lines:
            escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            parts.append(b"(%s) Tj T*" % escaped.encode("utf-8"))
        parts.append(b"ET")
        content = b"\n".join(parts)
        return b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content)

    page_ids: list[int] = []
    content_ids: list[int] = []
    # object layout: 1 catalog, 2 pages, then per page (page_obj, content_obj),
    # last object: font
    n_pages = len(pages)
    first_page_obj = 3
    font_id = 3 + 2 * n_pages
    for i, text in enumerate(pages):
        page_id = first_page_obj + 2 * i
        content_id = page_id + 1
        page_ids.append(page_id)
        content_ids.append(content_id)

    objects.append(obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    objects.append(
        obj(2, b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, n_pages))
    )
    for i, text in enumerate(pages):
        page_id = first_page_obj + 2 * i
        content_id = page_id + 1
        objects.append(obj(
            page_id,
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents %d 0 R /Resources << /Font << /F1 %d 0 R >> >> >>"
            % (content_id, font_id),
        ))
        objects.append(obj(content_id, text_stream(text.splitlines() or [""])))
    objects.append(obj(
        font_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    ))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (len(objects) + 1)
    for number, body in enumerate(objects, start=1):
        offsets[number] = len(out)
        out.extend(body)
    xref_pos = len(out)
    out.extend(b"xref\n0 %d\n" % (len(objects) + 1))
    out.extend(b"0000000000 65535 f \n")
    for number in range(1, len(objects) + 1):
        out.extend(b"%010d 00000 n \n" % offsets[number])
    info_line = b""
    if title:
        escaped = title.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        info_line = b"/Info << /Title (%s) >>" % escaped.encode("utf-8")
    out.extend(
        b"trailer\n<< /Size %d /Root 1 0 R %s >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, info_line, xref_pos)
    )
    path.write_bytes(bytes(out))
    return path


@pytest.fixture
def make_pdf_fixture():
    return make_pdf


# ------------------------------------------------- media bytes for embed tests


def tiny_png(width: int = 8, height: int = 8) -> bytes:
    """Hand-built 8x8 RGB PNG via zlib+struct — no PIL."""
    import struct as _struct
    import zlib as _zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            _struct.pack(">I", len(data)) + tag + data
            + _struct.pack(">I", _zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = _struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes((0x80, 0x40, 0x20) * width)  # filter 0 + RGB pixels
    raw = row * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", _zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def tiny_wav_bytes(seconds: float = 0.5, rate: int = 16000) -> bytes:
    """0.5 s 16 kHz mono PCM16 sine WAV in memory (stdlib wave)."""
    import io
    import math
    import struct
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * seconds)):
            frames += struct.pack(
                "<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))
            )
        wav.writeframes(bytes(frames))
    return buf.getvalue()


# ------------------------------------------------------- fake goldmine-ocr ----
#
# A Python stub honoring the helper contract (02 §5.2): --version handshake,
# stdin batch JSON-lines {"id","path"} -> one JSON line per input with canned
# recognition lines (mixed confidences so client-side filtering is exercised).
# Tests set GOLDMINE_OCR_HELPER to the written script.

FAKE_OCR_TEXT = "externally managed environment"
FAKE_OCR_LONG_TEXT = (
    "The lighthouse keeper logged the harbor chart depth at 42 meters "
    "during the morning survey. " * 8
)  # > 200 chars: upgrades a thin PDF page

FAKE_OCR_HELPER = '''#!/usr/bin/env python3
import json
import os
import sys

if sys.argv[1:] == ["--version"]:
    print("goldmine-ocr 1")
    sys.exit(0)

canned = os.environ.get("FAKE_OCR_TEXT", "externally managed environment")
lines = [
    {"text": canned, "confidence": 0.97, "box": [0.0, 0.9, 0.5, 0.05]},
    {"text": "low confidence junk", "confidence": 0.20, "box": [0.0, 0.8, 0.5, 0.05]},
]

if sys.argv[1:]:
    for path in sys.argv[1:]:
        print(json.dumps({"id": path, "path": path, "texts": lines, "ms": 5.0}))
    sys.exit(0)

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    req = json.loads(raw)
    if os.environ.get("FAKE_OCR_SLEEP"):
        import time
        time.sleep(float(os.environ["FAKE_OCR_SLEEP"]))
    print(json.dumps(
        {"id": req["id"], "path": req["path"], "texts": lines, "ms": 5.0}))
'''


def write_fake_ocr_helper(tmp_path, *, text: str | None = None) -> Path:
    """Materialize the stub, chmod +x, return its path. `text` overrides the
    canned recognition line (injected AFTER the shebang so the script stays
    executable)."""
    script = tmp_path / "fake-goldmine-ocr"
    body = FAKE_OCR_HELPER
    if text is not None:
        lines = body.splitlines(True)
        lines.insert(1, f'import os as _os; _os.environ["FAKE_OCR_TEXT"] = {text!r}\n')
        body = "".join(lines)
    script.write_text(body)
    script.chmod(0o755)
    return script
