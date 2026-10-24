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
    "Chunk", "CHUNKER_VERSION", "ChunkTokenBudgetError", "chunk_text", "chunk_code", "est_tokens",
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
CHUNKER_VERSION = "chunk.v7-original-text"

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
    #: The verbatim source slice this chunk was cut from, when the chunker
    #: cleaned whitespace before cutting (chunk_text_tokens). `text` stays the
    #: cleaned embedding input; `original` is what the store keeps as the
    #: as-written segment text, and loc/loc_end then count ORIGINAL lines.
    #: None when text already is the verbatim slice (code, hand-built, resplit).
    original: str | None = None


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _clean(text: str) -> str:
    return _BLANK_RUN.sub("\n\n", _WS_RUN.sub(" ", text)).strip()


def _clean_mapped(text: str) -> tuple[str, list[int]]:
    """`_clean(text)` plus, for every cleaned character, the offset of the
    original character it stands for; one trailing sentinel offset marks where
    the stripped tail begins (len(text) when nothing was stripped). A collapsed
    run maps to the run's first characters, so slicing the original between two
    cleaned offsets yields the verbatim source including the run."""
    pieces: list[str] = []
    offsets: list[int] = []
    pos = 0
    for match in _WS_RUN.finditer(text):
        pieces.append(text[pos:match.start()])
        offsets.extend(range(pos, match.start()))
        pieces.append(" ")
        offsets.append(match.start())
        pos = match.end()
    pieces.append(text[pos:])
    offsets.extend(range(pos, len(text)))
    spaced = "".join(pieces)
    pieces, mapped, pos = [], [], 0
    for match in _BLANK_RUN.finditer(spaced):
        pieces.append(spaced[pos:match.start()])
        mapped.extend(offsets[pos:match.start()])
        pieces.append("\n\n")
        mapped.append(offsets[match.start()])
        mapped.append(offsets[match.start() + 1])
        pos = match.end()
    pieces.append(spaced[pos:])
    mapped.extend(offsets[pos:])
    collapsed = "".join(pieces)
    lead = len(collapsed) - len(collapsed.lstrip())
    trail = len(collapsed) - len(collapsed.rstrip())
    end = len(collapsed) - trail
    clean = collapsed[lead:end]
    result = mapped[lead:end]
    result.append(mapped[end] if trail else len(text))
    return clean, result


def _attach_original(chunks: list[Chunk], spans: list[tuple[int, int]], text: str, offsets: list[int]) -> list[Chunk]:
    """Rebind cleaned-text chunks to their verbatim source slices and lines."""
    out = []
    for chunk, (a, b) in zip(chunks, spans):
        start, stop = offsets[a], offsets[b]
        out.append(Chunk(chunk.text, _line_of(text, start), _line_of(text, max(start, stop - 1)),
                         tokens=chunk.tokens, original=text[start:stop]))
    return out


# --------------------------------------------------------------- token pieces

def _piece_boundaries(text: str, pieces: list[bytes]):
    """Linear exact alignment plus complete-codepoint boundary flags.

    Partial UTF8 prefixes contribute no character. Their raw token positions
    remain in the offsets array but are not valid starts or cuts.
    """
    data = text.encode("utf-8")
    ptr = chars = 0
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    offsets, safe = [0], [True]
    for i, piece in enumerate(pieces):
        if (ptr == 0 and i == 0 and piece[:1] == b" "
                and data[:1] != b" " and len(piece) > 1
                and data.startswith(piece[1:])):
            piece = piece[1:]  # The documented SPM dummy prefix only.
        end = ptr + len(piece)
        if end > len(data) or data[ptr:end] != piece:
            return None
        ptr = end
        chars += len(decoder.decode(piece))
        pending, _ = decoder.getstate()
        offsets.append(chars)
        safe.append(not pending)
    if ptr != len(data) or not safe[-1]:
        return None
    return offsets, safe


def piece_char_offsets(text: str, pieces: list[bytes]) -> list[int] | None:
    """Complete source characters after each exact token piece, or None.

    Offsets are monotonic. A byte-fallback token ending inside a UTF8 point
    keeps the preceding character offset; that position is not a legal cut.
    _TokenIndex retains the independent boundary flags, so repeated offsets
    cannot make the remaining byte of a point look like its complete cost.
    """
    result = _piece_boundaries(text, pieces)
    return result[0] if result is not None else None


def ptr_char(data: bytes, ptr: int) -> int:
    """Char count of `data[:ptr]` (ptr is always on a piece boundary, hence a
    char boundary for valid utf-8 text). Kept for callers/tests; the hot path
    no longer decodes the prefix per piece (see piece_char_offsets)."""
    return len(data[:ptr].decode("utf-8", "surrogateescape"))


class ChunkTokenBudgetError(ValueError):
    """A visited character-aligned raw-token group cannot fit the budget."""


class _TokenIndex:
    """Raw token indices with an independent complete-codepoint boundary map."""

    def __init__(self, clean: str, pieces: list[bytes]):
        self.clean = clean
        result = _piece_boundaries(clean, pieces)
        self.offsets, self.safe = result if result is not None else (None, None)
        self.total = len(pieces)
        self.boundaries = [i for i, valid in enumerate(self.safe or ()) if valid]
        self.boundary_chars = [self.offsets[i] for i in self.boundaries] if result is not None else []

    @property
    def ok(self) -> bool:
        return self.offsets is not None

    def tok_at(self, char_off: int) -> int:
        """Last safe token boundary at/before a character position."""
        assert self.offsets is not None
        pos = max(0, bisect.bisect_right(self.boundary_chars, char_off) - 1)
        return self.boundaries[pos]

    def char_at(self, tok: int) -> int:
        assert self.offsets is not None
        return self.offsets[max(0, min(tok, self.total))]

    def covering_tokens(self, a: int, b: int) -> int:
        """Conservatively include pieces crossing either whole-line seam."""
        if b <= a:
            return 0
        first = self.tok_at(a)
        last = self.boundaries[bisect.bisect_left(self.boundary_chars, b)]
        return last - first

    def cut(self, start: int, budget: int, end: int | None = None) -> int:
        """Farthest safe cut within budget; never consume a partial point."""
        if budget < 1:
            raise ChunkTokenBudgetError("token budget must fit a complete character-aligned token group")
        assert self.safe[start]
        stop = min(self.total if end is None else end, start + budget)
        pos = bisect.bisect_right(self.boundaries, stop) - 1
        cut = self.boundaries[pos]
        if self.char_at(cut) <= self.char_at(start):
            if self.char_at(start) >= len(self.clean):
                return self.total  # Trailing zero-width pieces contain no text.
            raise ChunkTokenBudgetError("token budget cannot fit the next complete character-aligned token group")
        return cut

    def overlap_start(self, old: int, cut: int, overlap: int) -> int:
        target = max(cut - max(0, overlap), old + 1)
        pos = bisect.bisect_left(self.boundaries, target)
        # Empty pieces can be safe without consuming any source character.
        pos = max(pos, bisect.bisect_right(self.boundary_chars, self.char_at(old)))
        return min(cut, self.boundaries[pos])


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
    1-based line numbers of the chunk in the ORIGINAL text and each chunk
    carries its verbatim `original` slice (chunk.v7); `text` is the cleaned
    cut that budgets and embeddings are measured on.
    """
    clean, offsets = _clean_mapped(text)
    if not clean:
        return []
    pieces = tokenize(clean) if tokenize is not None else None
    index = _TokenIndex(clean, pieces) if pieces else None
    spans: list[tuple[int, int]] = []
    if index is None or not index.ok:
        cpt = _chars_per_token(kind)
        chunks = chunk_text(
            clean,
            size=max(1, int(budget_tokens * cpt)),
            overlap=max(1, int(overlap_tokens * cpt)),
            max_chunks=max_chunks,
            _spans=spans,
        )
    else:
        chunks = _walk_tokens(clean, index, budget_tokens, overlap_tokens, max_chunks, spans)
    return _attach_original(chunks, spans, text, offsets)


def _stripped_span(clean: str, start: int, end: int) -> tuple[int, int]:
    """[start, end) narrowed to the `.strip()`ped piece inside it."""
    piece = clean[start:end]
    return start + len(piece) - len(piece.lstrip()), end - (len(piece) - len(piece.rstrip()))


def _walk_tokens(
    clean: str, index: _TokenIndex, budget: int, overlap: int, max_chunks: int,
    spans: list[tuple[int, int]] | None = None,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    n = len(clean)
    start_tok = 0
    while start_tok < index.total and len(chunks) < max_chunks:
        max_tok = index.cut(start_tok, budget)
        floor_tok = start_tok + max(1, budget // 2)
        # candidate boundaries: token counts in (start_tok, max_tok], deep
        # enough into the window; tiers paragraph > sentence > whitespace
        cut_tok: int | None = None
        paragraph = False
        for tier in ("paragraph", "sentence", "space"):
            best: int | None = None
            for tok in range(max_tok, floor_tok - 1, -1):
                if not index.safe[tok]:
                    continue
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
            if spans is not None:
                spans.append(_stripped_span(clean, index.char_at(start_tok), cut_char))
        if cut_tok >= index.total:
            break
        if paragraph:
            nxt = cut_char
            while nxt < n and clean[nxt] == "\n":
                nxt += 1
            # cut_tok has already advanced beyond the OLD start. Equality
            # here is the normal no-overlap boundary, not a stalled walk.
            # Skip only newline/zero-width pieces proven consumed at nxt;
            # if nxt lies inside a piece, retaining that piece is safe.
            start_tok = max(cut_tok, index.tok_at(nxt)) if nxt < n else index.total
        else:
            start_tok = index.overlap_start(start_tok, cut_tok, overlap)
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
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1

    def emit(start_line: int, end_line: int, a: int, b: int) -> None:
        while a < b and text[a] == "\n":
            a += 1
        while b > a and text[b - 1] == "\n":
            b -= 1
        piece = text[a:b]
        if piece.strip() and len(chunks) < max_chunks:
            cost = index.covering_tokens(a, b)
            if cost > budget:
                raise ChunkTokenBudgetError("whole-line source interval exceeds token budget")
            chunks.append(Chunk(piece, start_line, end_line, tokens=cost))

    cur_a, cur_start_line = 0, 1
    for i, line in enumerate(lines):
        if len(chunks) >= max_chunks:
            break
        ls, le = starts[i], starts[i] + len(line)
        if index.covering_tokens(ls, le) > budget:
            if ls > cur_a:
                emit(cur_start_line, i, cur_a, ls)
            sub_a = ls
            while sub_a < le and len(chunks) < max_chunks:
                start = index.tok_at(sub_a)
                cut = index.cut(start, budget)
                sub_b = min(index.char_at(cut), le)
                if sub_b <= sub_a:
                    raise ChunkTokenBudgetError("token budget cannot advance the source line")
                emit(i + 1, i + 1, sub_a, sub_b)
                sub_a = sub_b
            cur_a = min(le + 1, len(text))
            cur_start_line = i + 2
            continue
        if index.covering_tokens(cur_a, le) > budget and ls > cur_a:
            emit(cur_start_line, i, cur_a, ls)
            cur_a, cur_start_line = ls, i + 1
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
        nxt = index.cut(tok, max_tokens)
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
    max_chunks: int = DEFAULT_MAX_CHUNKS, _spans: list[tuple[int, int]] | None = None,
) -> list[Chunk]:
    """~`size`-character chunks with a little overlap, paragraph-aware.

    - whitespace cleaned like DigUp (runs of spaces/tabs/nbsp -> one; 3+ newlines -> 2)
    - preferred cut: last paragraph boundary (blank line) deeper than size/2 into
      the window; the next chunk starts AFTER the blank line (no overlap needed —
      the boundary exists to keep paragraphs whole)
    - fallback cut: last whitespace deeper than size/2 in; the next chunk starts
      `overlap` back, advanced to a word boundary
    - loc/loc_end are 1-based line numbers of the chunk in the cleaned text
    - `_spans` (internal) collects each emitted piece's [start, end) in the
      cleaned text so chunk_text_tokens can rebind it to the original
    """
    if size < 1:
        raise ValueError("character chunk size must be positive")
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
                if _spans is not None:
                    _spans.append(_stripped_span(clean, start, cut))
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
                if _spans is not None:
                    _spans.append(_stripped_span(clean, start, end))
            if end >= n:
                break
            start = end  # A hard window has no preferred boundary; keep its tail.
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
