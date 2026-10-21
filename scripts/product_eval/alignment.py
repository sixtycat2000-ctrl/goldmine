"""Conservative contiguous source/read-back proofs, independent of labels/ranks.

ASR alone may differ in letter case and enumerated sentence punctuation. All
other symbols, apostrophes/hyphens inside words and numeric spelling survive
tokenization. Every proof is one contiguous source run and one actual observed
substring; a missing word never gets stitched back in from another run.
"""

from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
import hashlib
import re

from answer_eval.protocol import EvaluationError, fields


_SENTENCE = frozenset(".,!?;:…，。！？；：、")
MAX_ALIGNMENT_CHARS = 131072
MAX_ALIGNMENT_TOKENS = 4096
MAX_ALIGNMENT_TOKEN_PRODUCT = 250000
MAX_ALIGNMENT_CANDIDATE_RUNS = 1024
# Signs, decimal/grouping separators, dates, ratios and clock times are part
# of a numeric token, never silently rewritten (2.5 != 25; -5 != 5).
_TOKEN = re.compile(r"(?<!\w)[+\-−]?(?:\d+(?:[.,:/\-−]\d+)*|[.,]\d+)"
                    r"\w*(?:[.,:/+\-−]\w+)*"
                    r"|\.?[^\W_]+(?:['’\-‐‑.][^\W_]+)*|[^\s]", re.UNICODE)
_NUMERIC_PREFIX = re.compile(r"^[+\-−]?(?:\d|[.,]\d)")
_FIELD = re.compile(r"\S+")


def _sentence_separator(text, offset):
    # A dot immediately before '_' belongs to an identifier, including a
    # leading ._private or the attribute separator in client._transport.
    return text[offset] in _SENTENCE and not (text[offset] == "." and text[offset + 1:offset + 2] == "_")


def word_tokens(text):
    """Return (comparison token, original start, original end), with symbols.

    Only the explicit sentence separators above may disappear. For example
    percent, comparison signs, plus, hash, underscore and quotes stay tokens.
    Curly and straight internal apostrophes are deliberately not conflated.
    Leading/internal dots on identifiers are retained (.NET is not NET).
    Preserve the raw next non-whitespace field after every numeric token,
    including its symbol-separated pieces. This is intentionally conservative:
    it also preserves an ordinary word in that position, without guessing units.
    """
    result, previous_numeric_end, preserve_until = [], None, 0
    for match in _TOKEN.finditer(text):
        raw, start, end = match.group(), match.start(), match.end()
        if len(raw) == 1 and _sentence_separator(text, start):
            continue
        if previous_numeric_end is not None and start >= preserve_until:
            gap = text[previous_numeric_end:start]
            if not gap or gap.isspace():
                preserve_until = _FIELD.match(text, start).end()
        numeric = _NUMERIC_PREFIX.match(raw) is not None
        value = raw if numeric or start < preserve_until else raw.casefold()
        result.append((value, start, end))
        previous_numeric_end = end if numeric else None
    return result


def _bounds(text, tokens, start, end):
    lower, upper = tokens[start][1], tokens[end - 1][2]
    while lower and (text[lower - 1].isspace() or _sentence_separator(text, lower - 1)):
        lower -= 1
    while upper < len(text) and (text[upper].isspace() or _sentence_separator(text, upper)):
        upper += 1
    return lower, upper


def _occurrences(values, needle):
    # KMP avoids repeatedly slicing/comparing long repeated-token patterns.
    # Two occurrences already prove ambiguity; no full count is needed.
    prefix = [0] * len(needle)
    matched = 0
    for i in range(1, len(needle)):
        while matched and needle[i] != needle[matched]:
            matched = prefix[matched - 1]
        if needle[i] == needle[matched]:
            matched += 1
        prefix[i] = matched
    matched = count = 0
    for token in values:
        while matched and token != needle[matched]:
            matched = prefix[matched - 1]
        if token == needle[matched]:
            matched += 1
        if matched == len(needle):
            count += 1
            if count == 2:
                return count
            matched = prefix[matched - 1]
    return count


def asr_word_spans(canonical, observed):
    """Maximal equal word runs of >=4 tokens, with no ambiguous placement.

    No edit distance, stemming, translation, numeric conversion or label input.
    Runs on different sides of a substitution/deletion remain separate. A run
    repeated in either input has no unambiguous character placement and is
    rejected. Call separately for each stored segment/emitted material record.
    """
    if max(len(canonical), len(observed)) > MAX_ALIGNMENT_CHARS:
        raise EvaluationError("ASR alignment input character bound exceeded; explicit segmentation required", status="MISSING")
    left, right = word_tokens(canonical), word_tokens(observed)
    if max(len(left), len(right)) > MAX_ALIGNMENT_TOKENS or len(left) * len(right) > MAX_ALIGNMENT_TOKEN_PRODUCT:
        raise EvaluationError("ASR alignment token/product bound exceeded; explicit segmentation required", status="MISSING")
    a, b = [token[0] for token in left], [token[0] for token in right]
    positions = defaultdict(list)
    for j, token in enumerate(b):
        positions[token].append(j)
    result, candidates = [], 0
    for i, token in enumerate(a):
        for j in positions[token]:
            if i and j and a[i - 1] == b[j - 1]:
                continue
            size = 0
            while i + size < len(a) and j + size < len(b) and a[i + size] == b[j + size]:
                size += 1
            if size < 4:
                continue
            candidates += 1
            if candidates > MAX_ALIGNMENT_CANDIDATE_RUNS:
                raise EvaluationError("ASR alignment candidate-run work bound exceeded; explicit segmentation required", status="MISSING")
            values = a[i:i + size]
            if _occurrences(a, values) != 1 or _occurrences(b, values) != 1:
                continue
            start, end = _bounds(canonical, left, i, i + size)
            raw_start, raw_end = _bounds(observed, right, j, j + size)
            actual = observed[raw_start:raw_end]
            result.append({"start_char": start, "end_char": end, "text": canonical[start:end],
                           "word_alignment": {"schema": 1, "mode": "asr-words-v1", "observed_text": actual,
                                              "observed_sha256": hashlib.sha256(actual.encode("utf-8")).hexdigest()}})
    return result


def validate_word_alignment(span):
    """Validate the optional witness; caller checks source/CIT/body membership."""
    if "word_alignment" not in span:
        return None
    if span.get("evidence_type") != "asr_text":
        raise EvaluationError("word alignment is restricted to actual ASR evidence")
    proof = span["word_alignment"]
    fields(proof, {"schema", "mode", "observed_text", "observed_sha256"}, "ASR word alignment")
    if (type(proof["schema"]) is not int or proof["schema"] != 1 or proof["mode"] != "asr-words-v1"
            or not isinstance(proof["observed_text"], str) or not proof["observed_text"]
            or not isinstance(proof["observed_sha256"], str)
            or hashlib.sha256(proof["observed_text"].encode("utf-8")).hexdigest() != proof["observed_sha256"]):
        raise EvaluationError("invalid ASR observed substring/schema/SHA proof")
    canonical = [token[0] for token in word_tokens(span["text"])]
    observed = [token[0] for token in word_tokens(proof["observed_text"])]
    if len(canonical) < 4 or canonical != observed:
        raise EvaluationError("ASR source and observed contiguous token sequences differ")
    return proof["observed_text"]


def exact_character_spans(canonical, observed):
    """Literal >=32-character blocks; never bridge a mismatch or add folding."""
    matcher = SequenceMatcher(None, canonical, observed, autojunk=False)
    return [{"start_char": match.a, "end_char": match.a + match.size,
             "text": canonical[match.a:match.a + match.size]}
            for match in matcher.get_matching_blocks() if match.size >= 32]
