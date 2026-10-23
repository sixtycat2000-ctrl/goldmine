"""Source-span query obligations, without models, storage or inferred facts."""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from .deadline import check_deadline
from .lexical import _is_cjk

ANCHOR_VERSION = "anchor.v1-lexical"
MAX_ANCHORS = 32
MAX_SOURCE_ROWS = 2000
_CODE = re.compile(r"[A-Z]{2,}-?[0-9]+\Z")
_NUMBER = re.compile(r"[+\-−]?(?:\d+(?:[.,]\d+)*|\.\d+)", re.UNICODE)
_COMPARE = (
    re.compile(r"^\s*compare\s+(?P<a>.+?)\s+and\s+(?P<b>.+?)\s+on\s+(?P<c>.+?)\s*$", re.I),
    re.compile(r"^\s*比较\s*(?P<a>.+?)\s*与\s*(?P<b>.+?)\s*在\s*(?P<c>.+?)\s*$"),
)


@dataclass(frozen=True)
class Anchor:
    id: int
    component: int                 # 0=query; 1..n=already parsed quoted phrase
    start: int
    end: int
    raw: str
    kind: str = "identity"


@dataclass(frozen=True)
class Clause:
    id: int
    anchor_ids: tuple[int, ...]
    remainder: str
    phrases: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueryPlan:
    anchors: tuple[Anchor, ...]
    clauses: tuple[Clause, ...]
    phrases: tuple[str, ...]
    pure_text: str | None
    reason: str | None = None


def _word(char):
    return char.isalnum() or char == "_" or unicodedata.category(char).startswith("M")


def lexemes(text):
    """Raw offsets; operators/whitespace/path separators delimit components.

    Dots/colons/hyphens remain inside an identity. A numeric sign is retained.
    NFKC expansion never changes the returned offsets in the supplied string.
    """
    i = 0
    while i < len(text):
        if i % 256 == 0:
            check_deadline()
        char = text[i]
        following = i + 1 < len(text) and _word(text[i + 1])
        digit = i + 1 < len(text) and text[i + 1].isdecimal()
        decimal = (i + 2 < len(text) and text[i + 1] in ".．"
                   and text[i + 2].isdecimal())
        surface = unicodedata.normalize("NFKC", char)
        leading = (surface == "." and following) or (surface in "+-−" and (digit or decimal))
        if not _word(char) and not leading:
            i += 1
            continue
        start = i
        i += 1
        while i < len(text):
            comma = (text[i] == "," and i > start and text[i - 1].isdecimal()
                     and i + 1 < len(text) and text[i + 1].isdecimal())
            if not (_word(text[i]) or text[i] in "-.:%－．：％" or comma):
                break
            i += 1
        end = i
        # A sentence's final period is not part of an inline symbol. Whole
        # filename/path identity is verified separately before this lexer.
        if end > start + 1 and text[end - 1] in ".．":
            end -= 1
        if end > start:
            yield start, end, text[start:end]


def _quantity(surface):
    found = _NUMBER.match(surface)
    return bool(found and any(c.isalpha() for c in surface[found.end():]))


def typed_normalize(value):
    """Casefold ordinary text, retaining complete numeric-suffix spelling.

    Whole paths/titles/phrases use this same gate: no prelookup may accept a
    casefolded key without checking the real original components afterwards.
    """
    surface = unicodedata.normalize("NFKC", value)
    output, previous = [], 0
    for start, end, word in lexemes(surface):
        output.append(surface[previous:start].casefold())
        output.append(word if _quantity(word) else word.casefold())
        previous = end
    output.append(surface[previous:].casefold())
    return "".join(output)


def compact_alias(left, right):
    """One explicit two-surface code grammar; never generic separator erase."""
    left, right = (unicodedata.normalize("NFKC", v) for v in (left, right))
    return (bool(_CODE.fullmatch(left)) and bool(_CODE.fullmatch(right))
            and ("-" in left or "-" in right)
            and left.replace("-", "") == right.replace("-", ""))


def literal_equal(left, right):
    return typed_normalize(left) == typed_normalize(right)


def identity_shape(raw):
    value = unicodedata.normalize("NFKC", raw)
    letters = any(c.isalpha() for c in value)
    digits = any(c.isdecimal() for c in value)
    camel = any(a.islower() and b.isupper() and c.islower()
                for a, b, c in zip(value, value[1:], value[2:]))
    if any(c in value for c in "_.:/\\") or camel or (letters and digits):
        return True
    return len(value) >= 4 and all(c.isdecimal() for c in value)


def _anchors(text, component, first_id):
    out = []
    protected = []
    if component and any(c in text for c in "/\\"):
        return [Anchor(first_id, component, 0, len(text), text, "path")]
    for found in re.finditer(r"`([^`]+)`|\S+", text):
        raw = found.group(1) if found.group(1) is not None else found.group()
        if found.group(1) is None and not any(c in raw for c in "/\\"):
            continue
        start, end = found.span(1) if found.group(1) is not None else found.span()
        kind = "path" if any(c in raw for c in "/\\") else "literal"
        protected.append((start, end))
        out.append(Anchor(first_id + len(out), component, start, end, raw, kind))
    for start, end, raw in lexemes(text):
        if any(a <= start and end <= b for a, b in protected):
            continue
        # Preserve a real Latin code next to a CJK label as its own raw span.
        fragments = [(start, end, raw)]
        if any(_is_cjk(c) for c in raw):
            fragments = []
            begin = None
            for pos, char in enumerate(raw + "中"):
                if not _is_cjk(char) and begin is None:
                    begin = pos
                elif _is_cjk(char) and begin is not None:
                    part = raw[begin:pos]
                    for a, b, actual in lexemes(part):
                        if identity_shape(actual):
                            fragments.append((start + begin + a, start + begin + b, actual))
                    begin = None
            if not fragments:
                fragments = [(start, end, raw)]
        for a, b, part in fragments:
            if identity_shape(part):
                out.append(Anchor(first_id + len(out), component, a, b, part))
    out.sort(key=lambda a: (a.start, a.end))
    return [Anchor(first_id + i, a.component, a.start, a.end, a.raw, a.kind)
            for i, a in enumerate(out)]


def erase_spans(text, spans):
    output, previous = [], 0
    for start, end in sorted(spans):
        if start < previous:
            continue
        output.extend((text[previous:start], " "))
        previous = end
    output.append(text[previous:])
    return "".join(output)


def _semicolon(text):
    protected = False
    cuts = []
    for i, char in enumerate(text):
        if char == "`":
            protected = not protected
        if char in ";；" and not protected:
            start = max(text.rfind(" ", 0, i), text.rfind("\t", 0, i)) + 1
            end = text.find(" ", i)
            token = text[start:end if end >= 0 else len(text)]
            if any(c in token for c in "/\\:"):
                return None, False
            cuts.append(i)
    return (cuts[0] if len(cuts) == 1 else None), bool(cuts or protected)


def plan_query(query, phrases=()):
    """Only explicit scopes. No generated queries, generic conjunction split."""
    phrases = tuple(phrases)
    anchors = _anchors(query, 0, 0)
    for component, phrase in enumerate(phrases, 1):
        anchors.extend(_anchors(phrase, component, len(anchors)))
    pure = query if not phrases else phrases[0] if not query.strip() and len(phrases) == 1 else None
    if len({typed_normalize(a.raw) for a in anchors}) > MAX_ANCHORS:
        return QueryPlan(tuple(anchors), (), phrases, pure, "anchor_verification_bound")
    qanchors = [a for a in anchors if a.component == 0]

    def clause(number, start, end, shared=""):
        owned = tuple(a.id for a in qanchors if start <= a.start and a.end <= end)
        spans = [(a.start - start, a.end - start) for a in qanchors if a.id in owned]
        remainder = erase_spans(query[start:end], spans) + " " + shared
        return Clause(number, owned, remainder, phrases)

    cut, malformed = _semicolon(query)
    if cut is not None:
        clauses = (clause(0, 0, cut), clause(1, cut + 1, len(query)))
        reason = "anchor_scope_ambiguous" if phrases or any(not c.anchor_ids for c in clauses) else None
        return QueryPlan(tuple(anchors), clauses, phrases, pure, reason)
    for regex in _COMPARE:
        found = regex.fullmatch(query)
        if found:
            a, b = (clause(i, *found.span(name), found["c"]) for i, name in enumerate(("a", "b")))
            exactly_operand = all(len(c.anchor_ids) == 1 and
                                  literal_equal(query[slice(*found.span(name))].strip(), anchors[c.anchor_ids[0]].raw)
                                  for c, name in ((a, "a"), (b, "b")))
            reason = None if exactly_operand and not phrases else "anchor_scope_ambiguous"
            cstart, cend = found.span("c")
            shared_ids = tuple(x.id for x in qanchors if cstart <= x.start and x.end <= cend)
            shared = erase_spans(found["c"], [(x.start - cstart, x.end - cstart)
                                             for x in qanchors if x.id in shared_ids])
            a = Clause(a.id, (*a.anchor_ids, *shared_ids), shared, phrases)
            b = Clause(b.id, (*b.anchor_ids, *shared_ids), shared, phrases)
            return QueryPlan(tuple(anchors), (a, b), phrases, pure, reason)
    one = clause(0, 0, len(query))
    one = Clause(0, tuple(a.id for a in anchors), one.remainder, phrases)
    return QueryPlan(tuple(anchors), (one,), phrases, pure,
                     "anchor_scope_ambiguous" if malformed and anchors else None)
