"""PDF extraction via poppler (`pdftotext` / `pdfinfo`), page-capped and honest.

Caps (SPEC §2 / D5c): max_pages 5000; first pass reads 30 pages, then 50-page
steps (DigUp pdfStep). When the cap stops the read the file is marked partial
with read_to/read_end recorded. Pages with < 200 chars of text are "thin":
kept for FTS with NO vector (no OCR in v1 — never faked). A PDF with no text
layer at all is metadata-only with reason "page needs OCR (scanned)".

Both tools are runtime-optional: absence raises ToolError, which the pipeline
maps to capability=metadata_only with detail "pdftotext not found".
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .. import util

logger = logging.getLogger("goldmine.extract_pdf")

THIN_PAGE_CHARS = 200   # below this a page stays vectorless (ARCHITECTURE §8)
FIRST_PASS_PAGES = 30   # DigUp pdfFirst
STEP_PAGES = 50         # DigUp pdfStep
PDF_TIMEOUT_S = 60.0    # subprocess timeout per pdftotext/pdfinfo call (ARCH §8)

BREW_POPPLER_BIN = Path("/opt/homebrew/bin")


# ------------------------------------------------- text-layer pathology (§3.5)
#
# DigUp PageTextProblem.check (SearchText.swift:165-215) ported verbatim:
# a deterministic Unicode-scalar scan over the EXTRACTED page text. The
# disease lives in the PDF's text layer, so any extractor (DigUp's PDFKit,
# goldmine's poppler) sees the same symptoms.

#: dependent vowel signs of Bengali and Devanagari — in a real word each
#: follows a consonant (or its nukta)
_INDIC_VOWEL_SIGN = (
    set(range(0x09BE, 0x09C5)) | {0x09C7, 0x09C8, 0x09CB, 0x09CC, 0x09D7,
                                  0x09E2, 0x09E3}
    | {0x093A, 0x093B} | set(range(0x093E, 0x094D)) | {0x094E, 0x094F}
    | set(range(0x0955, 0x0958)) | {0x0962, 0x0963}
)
_INDIC_CONSONANT_OR_NUKTA = (
    set(range(0x0995, 0x09BA)) | {0x09BC, 0x09CE, 0x09DC, 0x09DD, 0x09DF,
                                  0x09F0, 0x09F1}
    | set(range(0x0915, 0x093A)) | {0x093C} | set(range(0x0958, 0x0960))
    | set(range(0x0979, 0x0980))
)
#: the Arabic block proper (DigUp Script.arabic, SearchText.swift:161 — the
#: whole 0x0600 block; the isalpha() guard narrows to letters like Swift's
#: scalar.properties.isAlphabetic)
_ARABIC = set(range(0x0600, 0x0700))
_LOST_LETTERS_MIDDLE = set("?.\ufffd")


def page_text_problem(text: str) -> str | None:
    """"glyphOrder" | "lostLetters" | None — the page's text-layer pathology.

    glyphOrder: Bengali/Hindi read in glyph order (vowel signs ahead of their
    consonants, conjuncts lost) — too garbled to show or match, enough words
    survive for a meaning vector beside the page's picture.
    lostLetters: Arabic lam-ligature loss (a bare alef before ?/. where the
    article's lam went with the ligature). Detection only in goldmine for now
    (digup-plan-002 A1): the OCR-replace path is a deferred content change.
    """
    import unicodedata

    text = unicodedata.normalize("NFC", text)
    vowel_signs = misplaced = lost_letters = 0
    previous = 0x20
    before_previous = 0x20
    for scalar in map(ord, text):
        if scalar in _INDIC_VOWEL_SIGN:
            vowel_signs += 1
            if previous not in _INDIC_CONSONANT_OR_NUKTA:
                misplaced += 1
        if (
            before_previous == 0x0627
            and chr(previous) in _LOST_LETTERS_MIDDLE
            and scalar in _ARABIC
            and chr(scalar).isalpha()
        ):
            lost_letters += 1
        before_previous = previous
        previous = scalar
    if vowel_signs >= 10 and misplaced * 20 >= vowel_signs:
        return "glyphOrder"
    if lost_letters >= 2:
        return "lostLetters"
    return None


def upgraded_pdftotext_version(tool_version: str, upgrades: set[str]) -> str:
    """Compose 'pdftotext@<ver>' + sorted upgrade tags -> 'pdftotext+a+b@<ver>'.
    Single-'visionocr' output is byte-identical to the legacy
    replace('pdftotext@', 'pdftotext+visionocr@') form; fingerprints'
    current_extractors registers every combination this can emit — the two
    must stay in lockstep (digup-p2-closeout-001 B2 review D8)."""
    tags = "".join(f"+{name}" for name in sorted(upgrades))
    if not tags:
        return tool_version
    base, sep, ver = tool_version.partition("@")
    return f"{base}{tags}{sep}{ver}"


def _which(tool: str) -> str | None:
    """shutil.which with the brew poppler dir as fallback (monkeypatch-friendly)."""
    found = shutil.which(tool)
    if found:
        return found
    fallback = BREW_POPPLER_BIN / tool
    return str(fallback) if fallback.is_file() else None


def pdftotext_path() -> str | None:
    return _which("pdftotext")


def pdfinfo_path() -> str | None:
    return _which("pdfinfo")


def pdftoppm_path() -> str | None:
    """Renderer for the v2 thin-page OCR upgrade (02 §5.2 Scope B): poppler,
    same dependency as pdftotext."""
    return _which("pdftoppm")


def pdftotext_version() -> str | None:
    """e.g. "24.08.0" — the banner goes to stderr (`pdftotext -v`)."""
    tool = pdftotext_path()
    if tool is None:
        return None
    try:
        result = util.run_tool([tool, "-v"], timeout_s=5.0)
    except util.ToolError:
        return None
    banner = (result.stderr or result.stdout).strip()
    match = re.search(r"pdftotext version ([0-9][^\s,]*)", banner)
    return match.group(1) if match else (banner.splitlines()[0] if banner else None)


@dataclass
class PdfResult:
    pages: list[tuple[int, str]] = field(default_factory=list)  # (1-based page, text)
    total: int = 0                    # page count per pdfinfo (0 = unknown)
    title: str | None = None
    truncated: bool = False           # a cap stopped the read early
    tool_version: str = "pdftotext"
    read_to: int | None = None        # last page actually read
    read_end: int | None = None       # == read_to (kept for info-JSON symmetry)


def pdf_info(path: Path) -> dict:
    """pdfinfo: {"pages": int, "title": str | None}. Missing pdfinfo => {}."""
    tool = pdfinfo_path()
    if tool is None:
        return {}
    try:
        result = util.run_tool([tool, str(path)], timeout_s=PDF_TIMEOUT_S)
    except util.ToolError:
        return {}
    if result.rc != 0 or result.timed_out:
        return {}
    info: dict = {}
    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            try:
                info["pages"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("Title:"):
            title = line.split(":", 1)[1].strip()
            if title:
                info["title"] = title
    return info


def _page_batches(max_page: int) -> list[tuple[int, int]]:
    """(first, last) 1-based inclusive batches: 30 pages first, then 50s."""
    batches: list[tuple[int, int]] = []
    start = 1
    while start <= max_page:
        width = FIRST_PASS_PAGES if start == 1 else STEP_PAGES
        end = min(start + width - 1, max_page)
        batches.append((start, end))
        start = end + 1
    return batches


def _run_pdftotext(path: Path, first: int, last: int, timeout_s: float) -> str:
    tool = pdftotext_path()
    if tool is None:
        raise util.ToolError("pdftotext: not found")
    result = util.run_tool(
        [tool, "-f", str(first), "-l", str(last), "-enc", "UTF-8", str(path), "-"],
        timeout_s=timeout_s,
    )
    if result.timed_out:
        raise RuntimeError(f"pdftotext timeout after {timeout_s:g}s")
    if result.rc != 0:
        raise RuntimeError(
            f"pdftotext failed rc={result.rc}: {result.stderr.strip()[:200]}"
        )
    return result.stdout


def _split_pages(raw: str, first: int) -> list[tuple[int, str]]:
    """pdftotext separates pages with \f (one after every page, incl. the last)."""
    parts = raw.split("\f")
    if parts and parts[-1] == "":
        parts.pop()
    out: list[tuple[int, str]] = []
    for offset, text in enumerate(parts):
        out.append((first + offset, text))
    return out


def extract_pages(
    path: Path,
    *,
    max_pages: int = 5000,
    timeout_s: float = PDF_TIMEOUT_S,
) -> PdfResult:
    """Read up to `max_pages` (30 first, then 50-page steps).

    Raises util.ToolError when pdftotext is missing (pipeline -> metadata_only)
    and RuntimeError on timeout/tool failure (pipeline -> capability=failed).
    """
    if pdftotext_path() is None:
        raise util.ToolError("pdftotext: not found")

    info = pdf_info(path)
    total = int(info.get("pages", 0))
    title = info.get("title")

    # Without pdfinfo, read page-by-page until a short/empty read ends the doc.
    if total <= 0:
        return _extract_unknown_length(path, max_pages=max_pages, timeout_s=timeout_s)

    limit = min(total, max_pages)
    pages: list[tuple[int, str]] = []
    for first, last in _page_batches(limit):
        raw = _run_pdftotext(path, first, last, timeout_s)
        got = _split_pages(raw, first)
        pages.extend(got)
        if len(got) < last - first + 1:
            break  # pdfinfo overstated the page count; stop at the real end
    read_end = pages[-1][0] if pages else 0
    return PdfResult(
        pages=pages,
        total=total,
        title=title,
        truncated=read_end < total,
        tool_version=f"pdftotext@{pdftotext_version() or 'unknown'}",
        read_to=read_end,
        read_end=read_end,
    )


def _extract_unknown_length(path: Path, *, max_pages: int, timeout_s: float) -> PdfResult:
    pages: list[tuple[int, str]] = []
    for first, last in _page_batches(max_pages):
        raw = _run_pdftotext(path, first, last, timeout_s)
        got = _split_pages(raw, first)
        pages.extend(got)
        if len(got) < last - first + 1:
            break
    read_end = pages[-1][0] if pages else 0
    return PdfResult(
        pages=pages,
        total=0,
        title=None,
        truncated=False,
        tool_version=f"pdftotext@{pdftotext_version() or 'unknown'}",
        read_to=read_end,
        read_end=read_end,
    )


# --------------------------------------------- thin-page OCR render (v2 B4) --
#
# A thin page (< THIN_PAGE_CHARS, including empty scanned pages) is rendered
# to PNG and OCR'd by the caller (02 §5.2 Scope B): `pdftoppm -f N -l N -r 150
# -scale-to-x 1400 -scale-to-y -1 -png` (1400 px matches DigUp's
# renderPixels, Indexer.swift:384). pdftoppm names outputs `<prefix>-N.png`
# with page-number padding that varies by total page count — glob it.


def render_page_png(
    path: Path, page: int, out_prefix: Path, *, timeout_s: float = PDF_TIMEOUT_S
) -> Path:
    """Render one 1-based page to `<prefix>-<N>.png`; returns its path.
    Raises util.ToolError when pdftoppm is missing and RuntimeError on
    failure/timeout."""
    tool = pdftoppm_path()
    if tool is None:
        raise util.ToolError("pdftoppm: not found")
    result = util.run_tool(
        [
            tool, "-f", str(page), "-l", str(page),
            "-r", "150", "-scale-to-x", "1400", "-scale-to-y", "-1",
            "-png", str(path), str(out_prefix),
        ],
        timeout_s=timeout_s,
    )
    if result.timed_out:
        raise RuntimeError(f"pdftoppm timeout after {timeout_s:g}s")
    if result.rc != 0:
        raise RuntimeError(
            f"pdftoppm failed rc={result.rc}: {result.stderr.strip()[:200]}"
        )
    rendered = sorted(out_prefix.parent.glob(out_prefix.name + "-*.png"))
    if not rendered:
        raise RuntimeError(f"pdftoppm wrote no output for page {page}")
    return rendered[0]
