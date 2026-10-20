"""Text -> located chunks. DigUp DocExtractor.chunks semantics + the V3 fix:
prefer breaking at a paragraph boundary inside the window; fall back to the last
whitespace at least size/2 in; next chunk starts on a word boundary. Code chunks
align to whole lines (DigUp codeStretch=1800 won its eval) with line numbers.

v2 (chunk.v2-tok, REDESIGN D3 / 01 §5.5): the window is measured in TOKENS via
the backend's `/tokenize` pieces when available (`tokenize` callable), with the
kind-aware chars/token estimator as the deterministic offline fallback
(`est_tokens`). CJK text needs no whitespace: cuts land on sentence punctuation
(。！？…, 02 §5.5's CJK re-split rule folded per REDESIGN C5) or token
boundaries, never mid-ideograph. A single over-budget stretch is hard-cut at a
token boundary (split-not-fail) instead of becoming its own oversized chunk.
"""

from __future__ import annotations

import bisect
import codecs
import re
from dataclasses import dataclass
from typing import Callable

__all__ = [
    "Chunk", "CHUNKER_VERSION", "chunk_text", "chunk_code", "est_tokens",
    "effective_chunk_size", "CODE_CHARS_PER_TOKEN",
    "DEFAULT_TOKEN_BUDGET", "DEFAULT_TOKEN_OVERLAP",
    "effective_token_budget", "chunk_text_tokens", "chunk_code_tokens",
    "split_text_by_tokens", "piece_char_offsets", "Tokenize",
]

DEFAULT_SIZE = 1800   # ~450 tokens (EmbeddingGemma context ~512 text-only)
DEFAULT_OVERLAP = 200
DEFAULT_MAX_CHUNKS = 5000

#: v2 token-budget chunking targets (01 §5.5): ~450 tokens per chunk with
#: ~48 tokens of overlap on non-paragraph cuts (~11%; "where cheap").
DEFAULT_TOKEN_BUDGET = 450
DEFAULT_TOKEN_OVERLAP = 48

#: `title: … | text: …` prompt framing (~40 tokens measured) plus safety slack,
#: reserved when converting a backend's token ceiling into a chunk budget.
PROMPT_FRAMING_TOKENS = 40
TOKEN_SLACK = 24

#: Measured chars/token densities (04 §3.2/§3.3): prose ~4.9 c/t, code
#: ~0.67 c/t — an 1800-char code stretch measured 2670 tokens against the
#: 2048 `-ub` ceiling and 400-failed its file. The DEFAULT estimator stays
#: len//4 (4.0 c/t) so est_tokens' v1 behavior is unchanged for prose; only
#: kind='code' switches to the measured dense-punctuation density, shrinking
#: code stretches pre-send (04 §6.2 kind-aware token guard). Density change
#: is NOT a chunker-semantics bump: same cut logic, smaller budget for code.
CODE_CHARS_PER_TOKEN = 0.67
_KIND_CHARS_PER_TOKEN = {"code": CODE_CHARS_PER_TOKEN}

#: Identity of the CURRENT chunking semantics (v2 05 §4.2, dimension 2). This is
#: bookkeeping ONLY — adding it changes no chunking behavior. Bump it only when
#: chunk semantics actually change; a bump marks existing rows stale for the
#: next plain ingest run, never a refusal (A2/B11: refusal is reserved for an
#: incompatible embed identity).
CHUNKER_VERSION = "chunk.v2-tok"

#: Callable returning exact token PIECES (as utf-8 bytes, each piece one token,
#: concatenated ~= the utf-8 text) or None when unavailable -> estimator path.
Tokenize = Callable[[str], "list[bytes] | None"]

_WS_RUN = re.compile(r"[ \t\u00A0]+")
_BLANK_RUN = re.compile(r"\n\s*\n\s*\n+")

#: sentence-end characters for the tier-2 cut preference (CJK re-split rule,
#: 02 §5.5 / REDESIGN C5: deepest whitespace or 。！？…, never mid-ideograph).
#: CJK sentence ends need no following space; ASCII .!? must be followed by
#: whitespace/end so "3.14" or "e.g" never reads as a sentence boundary.
_CJK_SENTENCE_END = "。！？…"
_ASCII_SENTENCE_END = ".!?"


@dataclass
class Chunk:
    text: str
    loc: int | None = None
    loc_end: int | None = None
    #: upper bound on the chunk's real token count, set ONLY by the token-true
    #: paths (pieces aligned). None = token count unknown (estimator/char-legacy
    #: or hand-built chunk) — the pipeline's ctx guard must count those; a set
    #: count makes the guard redundant (06-perf F2: effective_token_budget
    #: already reserves framing+slack below the backend ceiling).
    tokens: int | None = None


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _clean(text: str) -> str:
    return _BLANK_RUN.sub("\n\n", _WS_RUN.sub(" ", text)).strip()


# --------------------------------------------------------------- token pieces

def piece_char_offsets(text: str, pieces: list[bytes]) -> list[int] | None:
    """Char offsets into `text` after each consumed piece (offsets[0] == 0);
    None when the pieces do not align exactly with the text.

    `/tokenize` pieces are exact substrings (verified live against the vendored
    b11461 server: SPM whitespace is unescaped, so pieces concatenate back to
    the text). The one tolerated divergence is a tokenizer dummy-prefix space
    glued to the first piece ("hello" -> [" hello", " world"] in classic SPM).
    Any other mismatch -> None -> the caller falls back to the estimator.

    The char offset is tracked INCREMENTALLY (running count + a streaming
    utf-8 decoder): the naive `len(data[:ptr].decode())` per piece is
    O(bytes x pieces) — 5 MB of code spent 6.5 CPU-minutes there (06-perf F4).
    Byte-for-byte identical output, O(n) total — including the surrogateescape
    corner where a byte-fallback piece boundary lands INSIDE a multi-byte
    sequence: the prefix decode counted one replacement char per dangling
    byte, so the boundary value adds the decoder's pending bytes.
    """
    data = text.encode("utf-8")
    n = len(data)
    ptr = 0
    chars = 0
    dec = codecs.getincrementaldecoder("utf-8")("surrogateescape")

    def boundary() -> int:
        # chars fully decoded so far, + one per dangling byte of an incomplete
        # trailing sequence — exactly `len(data[:ptr].decode("surrogateescape"))`
        pending, _ = dec.getstate()
        return chars + len(pending)

    offsets = [0]
    for i, piece in enumerate(pieces):
        if not piece:
            offsets.append(boundary())  # zero-width special token
            continue
        if (
            ptr == 0
            and i == 0
            and piece[:1] == b" "
            and data[:1] != b" "
            and len(piece) > 1
            and data.startswith(piece[1:])
        ):
            piece = piece[1:]  # SPM dummy prefix on the first piece
        end = ptr + len(piece)
        if end > n or data[ptr:end] != piece:
            return None
        ptr = end
        chars += len(dec.decode(piece))
        offsets.append(boundary())
    if ptr != n:
        return None
    return offsets


def ptr_char(data: bytes, ptr: int) -> int:
    """Char count of `data[:ptr]` (ptr is always on a piece boundary, hence a
    char boundary for valid utf-8 text). Kept for callers/tests; the hot path
    no longer decodes the prefix per piece (see piece_char_offsets)."""
    return len(data[:ptr].decode("utf-8", "surrogateescape"))


class _TokenIndex:
    """Piece boundaries of one cleaned text: char offset -> token count.

    `tok_at(char_off)` = number of tokens fully before that char position;
    `char_at(tok)` = first char offset whose token count >= tok (the boundary
    AFTER token `tok`). Hard cuts and budget walks only ever use these, so a
    cut never lands inside a token (never mid-ideograph for CJK).
    """

    def __init__(self, clean: str, pieces: list[bytes]):
        self.clean = clean
        self.offsets = piece_char_offsets(clean, pieces)
        self.total = len(pieces)

    @property
    def ok(self) -> bool:
        return self.offsets is not None

    def tok_at(self, char_off: int) -> int:
        """Token count consumed at (or just before) a char position; a position
        mid-token counts that token as consumed (conservative for budgets)."""
        assert self.offsets is not None
        return bisect.bisect_right(self.offsets, char_off) - 1

    def char_at(self, tok: int) -> int:
        """Char offset of the boundary after `tok` tokens (clamped)."""
        assert self.offsets is not None
        if tok <= 0:
            return 0
        if tok >= self.total:
            return len(self.clean)
        return self.offsets[tok]


def _is_paragraph_cut(clean: str, off: int) -> bool:
    return off >= 2 and clean[off - 2:off] == "\n\n"


def _is_sentence_cut(clean: str, off: int) -> bool:
    if off < 1 or off > len(clean):
        return False
    prev = clean[off - 1]
    if prev.isspace() and off >= 2:
        prev = clean[off - 2]
    if prev in _CJK_SENTENCE_END:
        return True  # CJK sentence end: the next sentence starts immediately
    if prev in _ASCII_SENTENCE_END:
        nxt = clean[off] if off < len(clean) else ""
        return not nxt or nxt.isspace()
    return False


def _is_space_cut(clean: str, off: int) -> bool:
    return 0 < off <= len(clean) and clean[off - 1].isspace()


# ------------------------------------------------------------- v2 chunkers

def _chars_per_token(kind: str | None) -> float:
    return _KIND_CHARS_PER_TOKEN.get(kind or "", 4.0)


def chunk_text_tokens(
    text: str,
    *,
    budget_tokens: int = DEFAULT_TOKEN_BUDGET,
    overlap_tokens: int = DEFAULT_TOKEN_OVERLAP,
    tokenize: Tokenize | None = None,
    kind: str | None = None,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> list[Chunk]:
    """~`budget_tokens` chunks with paragraph/sentence/whitespace cut
    preference measured in real tokens when `tokenize` is available.

    Estimator path (tokenize None or unusable): the budget converts to chars
    with the kind-aware density (default 4.0 -> the v1 1800-char behavior) and
    the v1 char-window walk runs unchanged — deterministic, offline-safe.

    Piece path: cuts prefer the deepest paragraph boundary, then sentence end
    (CJK-aware), then whitespace, all >= half the budget into the window;
    otherwise a hard cut at the token boundary. Paragraph cuts carry no
    overlap; other cuts back up ~`overlap_tokens` tokens. loc/loc_end are
    1-based line numbers of the chunk in the cleaned text.
    """
    clean = _clean(text)
    if not clean:
        return []
    pieces = tokenize(clean) if tokenize is not None else None
    index = _TokenIndex(clean, pieces) if pieces else None
    if index is None or not index.ok:
        cpt = _chars_per_token(kind)
        return chunk_text(
            clean,
            size=max(1, int(budget_tokens * cpt)),
            overlap=max(1, int(overlap_tokens * cpt)),
            max_chunks=max_chunks,
        )
    return _walk_tokens(clean, index, budget_tokens, overlap_tokens, max_chunks)


def _walk_tokens(
    clean: str, index: _TokenIndex, budget: int, overlap: int, max_chunks: int,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    n = len(clean)
    start_tok = 0
    while start_tok < index.total and len(chunks) < max_chunks:
        max_tok = min(start_tok + budget, index.total)
        floor_tok = start_tok + max(1, budget // 2)
        # candidate boundaries: token counts in (start_tok, max_tok], deep
        # enough into the window; tiers paragraph > sentence > whitespace
        cut_tok: int | None = None
        paragraph = False
        for tier in ("paragraph", "sentence", "space"):
            best: int | None = None
            for tok in range(max_tok, floor_tok - 1, -1):
                off = index.char_at(tok)
                if off <= index.char_at(start_tok):
                    continue
                if tier == "paragraph" and _is_paragraph_cut(clean, off):
                    best = tok
                elif tier == "sentence" and _is_sentence_cut(clean, off):
                    best = tok
                elif tier == "space" and _is_space_cut(clean, off):
                    best = tok
                if best is not None:
                    break
            if best is not None:
                cut_tok = best
                paragraph = tier == "paragraph"
                break
        if cut_tok is None:
            cut_tok = max_tok  # hard cut at the budget's token boundary
            paragraph = False
        cut_char = index.char_at(cut_tok)
        piece = clean[index.char_at(start_tok):cut_char].strip()
        if piece:
            chunks.append(
                Chunk(piece, _line_of(clean, index.char_at(start_tok)),
                      _line_of(clean, cut_char),
                      tokens=cut_tok - start_tok)
            )
        if cut_tok >= index.total:
            break
        if paragraph:
            nxt = cut_char
            while nxt < n and clean[nxt] == "\n":
                nxt += 1
            start_tok = index.tok_at(nxt) if nxt < n else index.total
            if start_tok <= cut_tok:  # paranoia: always advance
                start_tok = cut_tok + 1 if cut_tok < index.total else index.total
        else:
            back = max(cut_tok - max(0, overlap), start_tok + 1)
            start_tok = back
    return chunks


def chunk_code_tokens(
    text: str,
    *,
    budget_tokens: int = DEFAULT_TOKEN_BUDGET,
    tokenize: Tokenize | None = None,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> list[Chunk]:
    """Whole-line stretches of ~`budget_tokens` real tokens; a single
    over-budget line is HARD-CUT at token boundaries (01 §5.5 — the v1
    whole-line-own-chunk rule is what shipped an 82929-token single-line JSON
    blob into a 2048 ceiling). loc/loc_end are 1-based line numbers.

    Estimator path: budget converts with the measured code density.
    """
    pieces = tokenize(text) if tokenize is not None else None
    index = _TokenIndex(text, pieces) if pieces else None
    if index is None or not index.ok:
        stretch = max(1, int(budget_tokens * CODE_CHARS_PER_TOKEN))
        return chunk_code(text, stretch=stretch)
    if not text:
        return []
    return _walk_code_lines(text, index, budget_tokens, max_chunks)


def _walk_code_lines(
    text: str, index: _TokenIndex, budget: int, max_chunks: int,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    lines = text.split("\n")
    # char offset of each line start in `text`
    starts: list[int] = []
    pos = 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1

    def emit(start_line: int, end_line: int, a: int, b: int) -> None:
        piece = text[a:b].strip("\n")
        if piece.strip():
            chunks.append(Chunk(piece, start_line, end_line,
                                tokens=index.tok_at(b) - index.tok_at(a)))

    cur_a = 0
    cur_start_line = 1
    cur_tok = 0
    for i, line in enumerate(lines):
        ls = starts[i]
        le = ls + len(line)
        line_tok = index.tok_at(le) - index.tok_at(ls)
        if line_tok > budget:
            # over-budget single line: flush, then hard-cut at token boundaries
            if le > cur_a:
                emit(cur_start_line, i, cur_a, ls)  # accumulated head
            sub_a = ls
            sub_tok = index.tok_at(sub_a)
            line_end_tok = index.tok_at(le)
            while sub_tok < line_end_tok and len(chunks) < max_chunks:
                nxt_tok = min(sub_tok + budget, line_end_tok)
                nxt_char = index.char_at(nxt_tok)
                piece = text[sub_a:nxt_char]
                if piece.strip():
                    chunks.append(Chunk(piece, i + 1, i + 1))
                sub_a, sub_tok = nxt_char, index.tok_at(nxt_char) or nxt_tok
            cur_a = le + 1 if le + 1 <= len(text) else len(text)
            cur_start_line = i + 2
            cur_tok = index.tok_at(cur_a)
            continue
        if cur_tok + line_tok > budget and le > cur_a:
            emit(cur_start_line, i, cur_a, ls)
            cur_a, cur_start_line = ls, i + 1
            cur_tok = 0
        cur_tok += line_tok
    if len(text) > cur_a and len(chunks) < max_chunks:
        emit(cur_start_line, len(lines), cur_a, len(text))
    return chunks


def split_text_by_tokens(
    text: str,
    max_tokens: int,
    *,
    tokenize: Tokenize | None = None,
) -> list[Chunk]:
    """Paranoid ctx-guard helper: hard-split `text` into pieces of at most
    `max_tokens` tokens each (token boundaries when a tokenizer is available,
    proportional char cuts otherwise). No cut preference, no overlap — the
    SPLIT-not-fail backstop for anything that still exceeds the ceiling."""
    max_tokens = max(1, max_tokens)
    pieces = tokenize(text) if tokenize is not None else None
    index = _TokenIndex(text, pieces) if pieces else None
    out: list[Chunk] = []
    if index is None or not index.ok:
        # estimator: proportional char slices (kind-less len//4 density)
        size = max(1, max_tokens * 4)
        for a in range(0, len(text), size):
            piece = text[a:a + size]
            if piece.strip():
                out.append(Chunk(piece, _line_of(text, a),
                                 _line_of(text, min(a + size, len(text)))))
        return out
    tok = 0
    while tok < index.total:
        nxt = min(tok + max_tokens, index.total)
        a, b = index.char_at(tok), index.char_at(nxt)
        piece = text[a:b]
        if piece.strip():
            out.append(Chunk(piece, _line_of(text, a), _line_of(text, b)))
        tok = nxt
    return out


def effective_token_budget(max_input_tokens: int | None) -> int:
    """Token budget for a backend's per-input ceiling (01 §5.5):
    min(450, ceiling − framing(~40) − slack(24)); floors at 64 so a tiny
    context still yields usable chunks. Real tokens when the backend counts
    them; the estimator converts this to chars kind-awarely."""
    if max_input_tokens is None:
        return DEFAULT_TOKEN_BUDGET
    return max(64, min(DEFAULT_TOKEN_BUDGET,
                       int(max_input_tokens) - PROMPT_FRAMING_TOKENS - TOKEN_SLACK))


# ------------------------------------------------------------- v1 chunkers

def chunk_text(
    text: str, size: int = DEFAULT_SIZE, overlap: int = DEFAULT_OVERLAP,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> list[Chunk]:
    """~`size`-character chunks with a little overlap, paragraph-aware.

    - whitespace cleaned like DigUp (runs of spaces/tabs/nbsp -> one; 3+ newlines -> 2)
    - preferred cut: last paragraph boundary (blank line) deeper than size/2 into
      the window; the next chunk starts AFTER the blank line (no overlap needed —
      the boundary exists to keep paragraphs whole)
    - fallback cut: last whitespace deeper than size/2 in; the next chunk starts
      `overlap` back, advanced to a word boundary
    - loc/loc_end are 1-based line numbers of the chunk in the cleaned text
    """
    clean = _BLANK_RUN.sub("\n\n", _WS_RUN.sub(" ", text)).strip()
    chunks: list[Chunk] = []
    n = len(clean)
    start = 0
    while start < n and len(chunks) < max_chunks:
        end = min(start + size, n)
        cut: int | None = None
        paragraph = False
        if end < n:
            window = clean[start:end]
            idx = window.rfind("\n\n")
            while idx != -1:
                if idx > size // 2:
                    cut = start + idx
                    paragraph = True
                    break
                idx = window.rfind("\n\n", 0, idx)
            if cut is None:
                space = _last_deep_whitespace(window, size)
                if space is not None:
                    cut = start + space
        if cut is not None:
            piece = clean[start:cut].strip()
            if piece:
                chunks.append(Chunk(piece, _line_of(clean, start), _line_of(clean, cut)))
            if paragraph:
                # cut is > size/2 past start, so this always advances
                start = min(cut + 2, n)
                continue
            # whitespace fallback: overlap back, then to a word boundary
            nxt = max(cut - overlap, start)
            if nxt <= start:
                nxt = cut
            while nxt < cut and not clean[nxt].isspace():
                nxt += 1
            start = nxt if nxt < cut else cut
        else:
            piece = clean[start:end].strip()
            if piece:
                chunks.append(Chunk(piece, _line_of(clean, start), _line_of(clean, end)))
            break
    return chunks


def _last_deep_whitespace(window: str, size: int) -> int | None:
    """Index of the last whitespace in `window` positioned > size/2 into it."""
    best: int | None = None
    for i, ch in enumerate(window):
        if ch.isspace() and i > size // 2:
            best = i
    return best


def chunk_code(text: str, stretch: int = DEFAULT_SIZE) -> list[Chunk]:
    """Whole-line stretches of ~`stretch` chars; a longer line is its own chunk.
    loc/loc_end are 1-based line numbers into the source text."""
    chunks: list[Chunk] = []
    lines = text.split("\n")
    current: list[str] = []
    current_len = 0
    start_line = 1

    def emit(start: int, end: int, body: list[str]) -> None:
        piece = "\n".join(body).strip("\n")
        if piece.strip():
            chunks.append(Chunk(piece, start, end))

    for i, line in enumerate(lines):
        lineno = i + 1
        if len(line) + 1 > stretch:
            # a longer line is its own chunk: flush, then emit this line alone
            if current:
                emit(start_line, lineno - 1, current)
                current, current_len = [], 0
            emit(lineno, lineno, [line])
            start_line = lineno + 1
            continue
        if current and current_len + len(line) + 1 > stretch:
            emit(start_line, lineno - 1, current)
            current, current_len = [], 0
            start_line = lineno
        current.append(line)
        current_len += len(line) + 1
    if current:
        emit(start_line, len(lines), current)
    return chunks


def est_tokens(text: str, kind: str | None = None) -> int:
    """len//4 heuristic; warn-only guard vs the ~512-token model limit.
    kind='code' divides by the measured code chars/token density instead —
    code tokenizes ~7x denser than prose (04 §3.2/§3.3)."""
    if kind in _KIND_CHARS_PER_TOKEN:
        return int(len(text) / _KIND_CHARS_PER_TOKEN[kind])
    return len(text) // 4


def effective_chunk_size(max_input_tokens: int | None, kind: str | None = None) -> int:
    """Chunk-size cap for a backend's per-input token ceiling.

    1 char ~= 1 token in the worst case (CJK), so reserve room for the
    `title: ... | text: ...` framing (~32 tokens) plus tokenization slack
    (96 total). Never grows chunks beyond DEFAULT_SIZE; floors at 256 so a
    tiny context still yields usable chunks.

    kind='code' converts the token budget with the measured code density
    (04 §6.2: 2048-token ceiling -> ~1307-char stretches, not 1800) so the
    measured 2670-token 400 class cannot recur from accumulated stretches.

    v2: kept for the estimator path and existing callers; the token-true path
    uses `effective_token_budget` instead.
    """
    if max_input_tokens is None:
        return DEFAULT_SIZE
    budget = int(max_input_tokens) - 96
    if kind in _KIND_CHARS_PER_TOKEN:
        budget = int(budget * _KIND_CHARS_PER_TOKEN[kind])
    return max(256, min(DEFAULT_SIZE, budget))
