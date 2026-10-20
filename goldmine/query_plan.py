"""Conservative, deterministic compound queries made only from source spans."""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


@dataclass(frozen=True)
class Subquery:
    id: str
    text: str
    weight: int
    spans: tuple[tuple[int, int], ...]


_COMPARE = (
    re.compile(r"^\s*比较\s*(?P<a>.+?)\s*与\s*(?P<b>.+?)\s*在\s*(?P<c>.+?)\s*$"),
    re.compile(r"^\s*compare\s+(?P<a>.+?)\s+and\s+(?P<b>.+?)\s+on\s+(?P<c>.+?)\s*$", re.I),
)
_STOP = {"and", "the", "of", "on", "a", "an", "in", "to", "or", "与", "和", "在"}


def _meaningful(text):
    normalized = unicodedata.normalize("NFKC", text).casefold()
    words = re.findall(r"[\w-]+", normalized)
    count = 0
    for word in words:
        if word in _STOP or len(word) < 2:
            continue
        cjk = sum("\u3400" <= c <= "\u9fff" or "\u3040" <= c <= "\u30ff"
                  or "\uac00" <= c <= "\ud7a3" for c in word)
        count += max(1, cjk // 2) if cjk else 1
    return count >= 2


def _separator(query):
    quote = None
    cuts = []
    for index, char in enumerate(query):
        if char in ('"', "'", "“", "”"):
            if quote is None:
                quote = "”" if char == "“" else char
            elif char == quote:
                quote = None
        elif char in ";；" and quote is None:
            # Semicolons embedded in a path/URI are literal path characters.
            start = max(query.rfind(" ", 0, index), query.rfind("\t", 0, index)) + 1
            end = query.find(" ", index)
            token = query[start:end if end >= 0 else len(query)]
            if any(c in token for c in ("/", "\\", ":")):
                return None
            cuts.append(index)
    return cuts[0] if quote is None and len(cuts) == 1 else None


def plan_query(query: str) -> tuple[Subquery, ...]:
    """Always retain q0, and add exactly two safe clauses or no expansion."""
    original = Subquery("q0", query, 2, ((0, len(query)),))
    spans = None
    cut = _separator(query)
    if cut is not None:
        spans = [((0, cut),), ((cut + 1, len(query)),)]
    elif not any(c in query for c in ('"', "“", "”", ";", "；")):
        for pattern in _COMPARE:
            match = pattern.fullmatch(query)
            if match:
                if pattern is _COMPARE[1] and (query.casefold().count(" and ") != 1
                                              or query.casefold().count(" on ") != 1):
                    return (original,)
                spans = [(match.span("a"), match.span("c")),
                         (match.span("b"), match.span("c"))]
                break
    if spans is None:
        return (original,)
    texts = [" ".join(query[a:b].strip() for a, b in parts) for parts in spans]
    keys = [unicodedata.normalize("NFKC", text).casefold() for text in texts]
    if keys[0] == keys[1] or any(not _meaningful(text) for text in texts):
        return (original,)
    return (original,) + tuple(Subquery(f"q{i}", text, 1, tuple(parts))
                                for i, (text, parts) in enumerate(zip(texts, spans), 1))
