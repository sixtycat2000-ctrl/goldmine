"""Shared primitives: hashing, bounded subprocess runner, fp16 packing, text helpers.

Owns (MODULE_MAP): hashing, subprocess runner, fp16 packing, text normalization,
magic sniffing, excerpt. Nothing here touches the DB or spawns long-lived processes.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

__all__ = [
    "ToolResult",
    "sha256_file",
    "sha256_bytes",
    "run_tool",
    "pack_fp16",
    "unpack_fp16",
    "normalize_text",
    "excerpt",
    "fold_text",
    "term_matches",
    "snippet",
    "sniff_image",
    "is_mpeg_ts",
    "human_size",
    "ansi_enabled",
    "ToolError",
]

VEC_DIM = 768
_FP16_ITEMSIZE = 2  # float16 bytes; 768 x 2 = 1536-byte BLOB contract (D7)


class ToolError(RuntimeError):
    """An external tool could not be run at all (not found / not executable)."""


@dataclass
class ToolResult:
    rc: int | None
    stdout: str
    stderr: str
    timed_out: bool


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    from .deadline import check_deadline

    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            check_deadline()
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    check_deadline()
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_tool(
    argv: list[str],
    timeout_s: float,
    max_output: int = 1 << 20,
    cwd: Path | None = None,
) -> ToolResult:
    """Run an external tool with a hard deadline and bounded captured output.

    The child is killed on timeout (SIGKILL after SIGTERM grace inside subprocess).
    Captured streams are truncated to `max_output` bytes after collection; the
    subprocess pipe itself is drained by communicate(), so the child never blocks
    on a full pipe.
    """
    from .deadline import check_deadline, remaining_timeout

    check_deadline()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=remaining_timeout(timeout_s),
            cwd=None if cwd is None else str(cwd),
        )
    except subprocess.TimeoutExpired as exc:
        check_deadline()
        # communicate() after the kill returns whatever was produced in time.
        out = exc.stdout or b""
        err = exc.stderr or b""
        return ToolResult(
            rc=None,
            stdout=out[:max_output].decode("utf-8", "replace"),
            stderr=err[:max_output].decode("utf-8", "replace"),
            timed_out=True,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"{argv[0]}: not found") from exc
    except PermissionError as exc:
        raise ToolError(f"{argv[0]}: not executable") from exc
    check_deadline()
    return ToolResult(
        rc=proc.returncode,
        stdout=proc.stdout[:max_output].decode("utf-8", "replace"),
        stderr=proc.stderr[:max_output].decode("utf-8", "replace"),
        timed_out=False,
    )


def pack_fp16(vec: np.ndarray) -> bytes:
    """768 floats -> 1536-byte little-endian float16 BLOB (store contract)."""
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    if arr.shape[0] != VEC_DIM:
        raise ValueError(f"expected {VEC_DIM}-dim vector, got {arr.shape[0]}")
    return arr.astype("<f2").tobytes()


def unpack_fp16(blob: bytes) -> np.ndarray:
    """1536-byte float16 BLOB -> float32 vector."""
    if len(blob) != VEC_DIM * _FP16_ITEMSIZE:
        raise ValueError(f"expected {VEC_DIM * _FP16_ITEMSIZE}-byte blob, got {len(blob)}")
    return np.frombuffer(blob, dtype="<f2").astype(np.float32)


def normalize_text(text: str) -> tuple[str, bool]:
    """NFC-normalize; returns (normalized, changed)."""
    import unicodedata

    normalized = unicodedata.normalize("NFC", text)
    return normalized, normalized != text


_WS = re.compile(r"\s+")


def excerpt(text: str, limit: int = 240) -> str:
    """One line, at most `limit` characters — DigUp excerpt(): for result lists."""
    line = _WS.sub(" ", text).strip()
    if len(line) > limit:
        return line[:limit] + "…"
    return line


# ------------------------------------------------------- snippet machinery
# Port of DigUp SearchText (§5.6, docs/v2/01-search-relevance.md): match query
# terms in the text AS WRITTEN (case/accent-insensitive), mark them «» in a
# term-density window. Char-based throughout — no whitespace assumption, so
# CJK text works the same as prose.

#: Han + kana + hangul + compatibility ranges: scripts written without
#: spaces. Word-boundary checks are relaxed next to these characters (there
#: is no boundary to check); fullwidth forms included (used inside CJK runs).
def _cjk_ish(ch: str) -> bool:
    o = ord(ch)
    return (
        0x2E80 <= o <= 0x9FFF        # CJK radicals..Han
        or 0x3040 <= o <= 0x30FF     # hiragana + katakana
        or 0xAC00 <= o <= 0xD7AF     # hangul syllables
        or 0xF900 <= o <= 0xFAFF     # CJK compatibility ideographs
        or 0xFF00 <= o <= 0xFF60     # fullwidth forms
        or 0x20000 <= o <= 0x3FFFF   # extension planes
    )


def _fold_char(ch: str) -> str:
    """One character as keyword search sees it: NFD, accent marks dropped,
    lowercased. ('É'/'é'/'e'+U+0301 all fold to 'e' — parity with the FTS
    tokenizer's remove_diacritics.) May be empty (a lone combining mark)."""
    import unicodedata

    return "".join(
        c for c in unicodedata.normalize("NFD", ch) if not unicodedata.combining(c)
    ).lower()


def fold_text(text: str) -> tuple[str, list[tuple[int, int]]]:
    """(folded, spans): `folded` is the per-character fold of `text`;
    spans[k] = (i, j) the slice of `text` folded[k] came from (DigUp Folded).
    Folding is per character on purpose: origins stay mappable, so matches
    found in `folded` map back to the text as written."""
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    for i, ch in enumerate(text):
        piece = _fold_char(ch)
        parts.append(piece)
        spans.extend([(i, i + 1)] * len(piece))
    return "".join(parts), spans


def fold_term(term: str) -> str:
    """A query term folded the same way fold_text folds text."""
    return "".join(_fold_char(ch) for ch in term)


def term_matches(term: str, text: str, folded: str | None = None,
                 spans: list[tuple[int, int]] | None = None,
                 limit: int = 500, prefix: bool = False) -> list[tuple[int, int]]:
    """[(start, end)] ranges of `text` (as written) where `term` occurs:
    folded substring scan with whole-word checks, offsets mapped back through
    the per-character origin array (DigUp SearchText.matches). `prefix=True`
    relaxes the RIGHT boundary the way a while-typing last term inflects: the
    match extends through the word it started (DigUp `inflects`); the left
    boundary stays strict."""
    needle = fold_term(term)
    if not needle:
        return []
    if folded is None or spans is None:
        folded, spans = fold_text(text)
    out: list[tuple[int, int]] = []
    pos = 0
    while len(out) < limit:
        idx = folded.find(needle, pos)
        if idx < 0:
            break
        pos = idx + 1
        origin = (spans[idx][0], spans[idx + len(needle) - 1][1])
        s0, e0 = origin
        if s0 > 0:
            ch = text[s0 - 1]
            if (ch.isalnum() or ch == "_") and not _cjk_ish(ch):
                continue  # inside a bigger word — not a word start
        if e0 < len(text):
            ch = text[e0]
            if (ch.isalnum() or ch == "_") and not _cjk_ish(ch):
                if not prefix:
                    continue
                end = e0
                while end < len(text):
                    ch2 = text[end]
                    if (ch2.isalnum() or ch2 == "_") and not _cjk_ish(ch2):
                        end += 1
                    else:
                        break
                origin = (s0, end)
        if out and origin[0] < out[-1][1]:  # overlapping the previous match
            continue
        out.append(origin)
    return out


def snippet(text: str, terms: list[str], width: int = 240,
            prefix_term: str | None = None) -> str | None:
    """Term-density window of `text` (~`width` chars) with every query term
    inside marked «» (DigUp-style marking; §5.6 improves DigUp's
    first-match anchor to a density window). Char-based, so CJK needs no
    spaces; window edges snap outward to whitespace (≤16 chars) so prose
    words are not cut. None when no term occurs (caller keeps the stored
    excerpt) — a pure-semantic hit shows the stored excerpt unchanged.
    `prefix_term` (the while-typing last query word) also marks the whole
    word it starts, mirroring FTS's prefix-last semantics. Existing «/» in
    the text pass through untouched (DigUp parity)."""
    if not text or not terms or width <= 0:
        return None
    folded, spans = fold_text(text)
    matches: list[tuple[int, int, int]] = []  # (start, end, term index)
    for ti, term in enumerate(terms):
        for s, e in term_matches(term, text, folded, spans,
                                 prefix=(term == prefix_term)):
            matches.append((s, e, ti))
    if not matches:
        return None
    matches.sort()

    if len(text) <= width:
        ws, we = 0, len(text)  # nothing to slide — the whole text is the window
    else:
        def window_score(ws: int, we: int) -> tuple[int, int, int, int]:
            inside = [m for m in matches if ws <= m[0] and m[1] <= we]
            # density first (distinct terms, then occurrences); ties prefer the
            # window CENTERED on the matches it contains (a flush-right candidate
            # of equal density would otherwise clip the cluster at the window
            # edge), then the earliest
            if not inside:
                return (0, 0, 0, -ws)
            centroid = sum((m[0] + m[1]) // 2 for m in inside) // len(inside)
            return (len({m[2] for m in inside}), len(inside),
                     -abs((ws + we) // 2 - centroid), -ws)

        best: tuple[tuple[int, int, int, int], int, int] | None = None
        for s, e, _ti in matches:
            mid = s + (e - s) // 2
            cws = max(0, min(mid - width // 2, len(text) - width))
            candidates = (
                (cws, min(len(text), cws + width)),  # centered on the match
                (s, min(len(text), s + width)),      # flush-left
                (max(0, e - width), e),              # flush-right
            )
            for cws2, cwe in candidates:
                if cws2 >= cwe:
                    continue
                score = window_score(cws2, cwe)
                if best is None or score > best[0]:
                    best = (score, cws2, cwe)
        assert best is not None  # matches is non-empty
        ws, we = best[1], best[2]

    # snap edges outward to a whitespace boundary (bounded) — prose words
    # stay whole; CJK just gains a few context characters
    lead = 0
    while ws > 0 and lead < 16 and not text[ws - 1].isspace():
        ws -= 1
        lead += 1
    tail = 0
    while we < len(text) and tail < 16 and not text[we].isspace():
        we += 1
        tail += 1

    parts: list[str] = ["…" if ws > 0 else ""]
    cursor = ws
    for s, e, _ti in matches:
        if s < ws or e > we or s < cursor:
            continue
        parts.append(text[cursor:s])
        parts.append("«")
        parts.append(text[s:e])
        parts.append("»")
        cursor = e
    parts.append(text[cursor:we])
    parts.append("…" if we < len(text) else "")
    return _WS.sub(" ", "".join(parts)).strip()


def sniff_image(head: bytes) -> str | None:
    """Image format from magic bytes: jpeg/png/gif/webp/heic-family, else None."""
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] in (b"GIF8",):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"heim", b"hevc", b"hevx", b"mif1", b"msf1"):
            return "heic"
        return "ftyp"
    return None


def is_mpeg_ts(head: bytes) -> bool:
    """MPEG-TS packets start with 0x47 every 188 bytes (or 192 with a 4-byte timecode).

    Port of DigUp Rules.isMPEGTransportStream; needs >= 389 bytes to decide.
    """
    if len(head) < 389:
        return False
    b = head
    return (b[0] == 0x47 and b[188] == 0x47 and b[376] == 0x47) or (
        b[4] == 0x47 and b[196] == 0x47 and b[388] == 0x47
    )


def human_size(n: int | float) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def ansi_enabled(no_color_flag: bool = False, stream=None) -> bool:
    """Whether ANSI escapes may be emitted (v2 item 19 / 03 §5.5).

    ALL of these disable ANSI: `NO_COLOR` present in the environment (any
    value, including empty — the no-color.org convention), an explicit
    `--no-color`, and a non-TTY `stream` (default stderr). """
    if os.environ.get("NO_COLOR") is not None:
        return False
    if no_color_flag:
        return False
    stream = sys.stderr if stream is None else stream
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False
