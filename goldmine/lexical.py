"""Deterministic Unicode query units and identifier normalization.

Grams are recall tokens. A whole CJK run remains one coverage unit and quoted
phrases need a contiguous original-text match, independent of the recall grams.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

LEXICAL_CJK_VERSION = "lex.v1-cjk2"
LEXICAL_VERSION = "lex.v2-exact"
_TOKEN = re.compile(r"[\w]+(?:[-.:/－．：／][\w]+)*", re.UNICODE)
_SEPARATORS = re.compile(r"[-_.:/]")


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _is_cjk(char: str) -> bool:
    n = ord(char)
    return (
        0x3400 <= n <= 0x9FFF or 0xF900 <= n <= 0xFAFF or
        0x20000 <= n <= 0x323AF or 0x3040 <= n <= 0x30FF or
        0x31F0 <= n <= 0x31FF or 0x1100 <= n <= 0x11FF or
        0x3130 <= n <= 0x318F or 0xAC00 <= n <= 0xD7AF or
        0xD7B0 <= n <= 0xD7FF
    )


def cjk_runs(text: str) -> list[str]:
    runs, current = [], []
    for char in normalize(text):
        if _is_cjk(char):
            current.append(char)
        elif current:
            runs.append("".join(current))
            current = []
    if current:
        runs.append("".join(current))
    return list(dict.fromkeys(runs))


def grams(run: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(run[i:i + 2] for i in range(len(run) - 1))) if len(run) > 1 else (run,)


def cjk_tokens(text: str) -> str:
    tokens = []
    for run in cjk_runs(text):
        tokens.extend(run)
        tokens.extend(grams(run))
    return " ".join(dict.fromkeys(tokens))


def fts_quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def identifier_tokens(text: str) -> tuple[str, ...]:
    """Keep punctuated identifiers whole; ordinary body words need no lookup row."""
    out = []
    for match in _TOKEN.finditer(text):
        raw = match.group(0)
        # Latin IDs are often adjacent to Chinese labels without whitespace.
        # Preserve each actual substring as well as the full raw token.
        fragments = [raw]
        if any(_is_cjk(ch) for ch in raw):
            fragments.extend("".join(" " if _is_cjk(ch) else ch for ch in raw).split())
        for fragment in fragments:
            normalized = normalize(fragment)
            digit = any(ch.isdigit() for ch in normalized)
            letter = any(ch.isalpha() for ch in normalized)
            if (digit and letter) or (normalized.isdigit() and len(normalized) >= 4) or _SEPARATORS.search(normalized):
                out.append(fragment)
    return tuple(dict.fromkeys(out))


def term_forms(raw: str) -> tuple[tuple[str, str], ...]:
    normalized = normalize(raw)
    alias = _SEPARATORS.sub("", normalized)
    return ((normalized, "raw"), (alias, "alias")) if alias and alias != normalized else ((normalized, "raw"),)


def path_identifier_tokens(path: str) -> tuple[str, ...]:
    """Keep a filename code attached to its metadata segment, never a body page."""
    candidates = list(identifier_tokens(path))
    for component in re.split(r"[/\\]", path):
        # Both LH-2209.md and invoice-K7Q2LM.pdf contain complete identifiers
        # that users may supply without the surrounding name/extension.
        stem = component.rsplit(".", 1)[0] if "." in component else component
        candidates.extend(identifier_tokens(stem))
        for part in re.split(r"[-_.:－＿．：]+", stem):
            candidates.extend(identifier_tokens(part))
    return tuple(dict.fromkeys(candidates))


@dataclass(frozen=True)
class QueryUnit:
    kind: str
    text: str
    patterns: tuple[str, ...]


def query_units(query: str, latin_terms: list[str], phrases: list[str]) -> list[QueryUnit]:
    units = [QueryUnit("latin", t, (fts_quote(t),)) for t in latin_terms]
    if units:
        last = units[-1]
        units[-1] = QueryUnit(last.kind, last.text, (last.patterns[0] + "*",))
    units.extend(QueryUnit("cjk", run, tuple(fts_quote(g) for g in grams(run))) for run in cjk_runs(query))
    units.extend(QueryUnit("phrase", normalize(p), tuple(fts_quote(g) for run in cjk_runs(p) for g in grams(run)))
                 for p in dict.fromkeys(phrases))
    return units


@dataclass(frozen=True)
class ExactMatch:
    level: int
    segment_id: int
    source: str
    raw_term: str
    match_type: str
    query_term: str


@dataclass
class LexicalResult:
    word_rank: dict[int, int] = field(default_factory=dict)
    keyword_segment: dict[int, int] = field(default_factory=dict)
    coverage: dict[int, float] = field(default_factory=dict)
    exact: dict[int, ExactMatch] = field(default_factory=dict)
    rejected_exact: set[int] = field(default_factory=set)
    # Actual body evidence, independent of the name column copied onto every
    # FTS row. Units retain identity so compound evidence can cover disjoint
    # facts without counting each CJK gram as a separate fact.
    segment_units: dict[int, frozenset[int]] = field(default_factory=dict)
    segment_files: dict[int, int] = field(default_factory=dict)
    unit_count: int = 0
    #: The query units behind segment_units, in index order, so evidence
    #: selection can locate them inside a candidate's stored text without
    #: re-deriving them from a different query string (search.v7).
    units: list["QueryUnit"] = field(default_factory=list)
