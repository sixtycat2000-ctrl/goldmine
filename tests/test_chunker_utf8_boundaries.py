"""Source conservation and real byte-piece budgets, independent of cut strategy."""
import pytest

from goldmine.chunker import (
    ChunkTokenBudgetError, chunk_code_tokens, chunk_text_tokens,
    piece_char_offsets, split_text_by_tokens,
)


def byte_pieces(text):
    return [bytes([value]) for value in text.encode("utf-8")]


def run(kind, text, budget, *, tokenize=byte_pieces, cap=5000, overlap=0):
    if kind == "prose":
        return chunk_text_tokens(text, budget_tokens=budget, overlap_tokens=overlap,
                                 tokenize=tokenize, max_chunks=cap)
    if kind == "code":
        return chunk_code_tokens(text, budget_tokens=budget, tokenize=tokenize, max_chunks=cap)
    return split_text_by_tokens(text, budget, tokenize=tokenize)


def test_offsets_count_only_complete_codepoints():
    assert piece_char_offsets("甲A", byte_pieces("甲A")) == [0, 0, 0, 1, 2]
    assert piece_char_offsets("é🙂", byte_pieces("é🙂")) == [0, 0, 1, 1, 1, 1, 2]


@pytest.mark.parametrize("kind", ["prose", "code", "guard"])
def test_complete_point_has_its_full_three_piece_cost(kind):
    chunks = run(kind, "甲A", 3)
    assert [chunk.text for chunk in chunks] == ["甲", "A"]
    assert all(len(chunk.text.encode("utf-8")) <= 3 for chunk in chunks)
    for chunk in chunks:
        if chunk.tokens is not None:
            assert len(chunk.text.encode("utf-8")) <= chunk.tokens <= 3
    assert [(chunk.loc, chunk.loc_end) for chunk in chunks] == [(1, 1), (1, 1)]


@pytest.mark.parametrize("kind", ["prose", "code", "guard"])
def test_unsplittable_point_raises_instead_of_duplicating_neighbor(kind):
    with pytest.raises(ChunkTokenBudgetError, match="complete character-aligned token group"):
        run(kind, "甲A", 1)


@pytest.mark.parametrize("kind", ["prose", "code", "guard"])
def test_original_64_piece_counterexample_conserves_source_and_budget(kind):
    text = "X" + "甲" * 50 + "Y"
    chunks = run(kind, text, 64)
    assert "".join(chunk.text for chunk in chunks) == text
    assert all(len(chunk.text.encode("utf-8")) <= 64 for chunk in chunks)
    for chunk in chunks:
        if chunk.tokens is not None:
            assert len(chunk.text.encode("utf-8")) <= chunk.tokens <= 64


@pytest.mark.parametrize("budget", [4, 5, 8, 12])
@pytest.mark.parametrize("overlap", [0, 1, 3, 20])
def test_overlap_progresses_on_complete_points_without_gaps(budget, overlap):
    text = "A甲B乙C🙂DéE"
    chunks = run("prose", text, budget, overlap=overlap)
    spans = [(text.index(chunk.text), text.index(chunk.text) + len(chunk.text)) for chunk in chunks]
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    assert len(spans) <= len(text)
    for previous, current in zip(spans, spans[1:]):
        assert previous[0] < current[0] <= previous[1]
    for chunk in chunks:
        assert len(chunk.text.encode("utf-8")) <= chunk.tokens <= budget
    if overlap == 0:
        assert "".join(chunk.text for chunk in chunks) == text


@pytest.mark.parametrize("kind", ["prose", "code", "guard"])
def test_empty_piece_inside_pending_point_cannot_make_it_safe(kind):
    parts = [b"\xe7", b"", b"\x94", b"\xb2", b"A"]
    tokenize = lambda text: parts
    with pytest.raises(ChunkTokenBudgetError):
        run(kind, "甲A", 3, tokenize=tokenize)
    chunks = run(kind, "甲A", 4, tokenize=tokenize)
    assert [chunk.text for chunk in chunks] == ["甲", "A"]
    if kind != "guard":
        assert [chunk.tokens for chunk in chunks] == [4, 1]


@pytest.mark.parametrize("kind", ["prose", "code"])
def test_cap_does_not_visit_unsplittable_tail(kind):
    assert [chunk.text for chunk in run(kind, "A甲", 1, cap=1)] == ["A"]
    with pytest.raises(ChunkTokenBudgetError):
        run(kind, "A甲", 1, cap=2)


def test_whole_line_flush_conservatively_counts_crossing_token():
    text = "AA\nBB\nCC"
    parts = [b"AA\nB", b"B\n", b"CC"]
    chunks = chunk_code_tokens(text, budget_tokens=2, tokenize=lambda value: parts)
    assert [chunk.text for chunk in chunks] == ["AA\nBB", "CC"]
    assert [chunk.tokens for chunk in chunks] == [2, 1]
    assert [(chunk.loc, chunk.loc_end) for chunk in chunks] == [(1, 2), (3, 3)]
    tiny = chunk_code_tokens(text, budget_tokens=1, tokenize=lambda value: parts)
    assert "".join(chunk.text for chunk in tiny) == "AABBCC"
    assert all(chunk.tokens == 1 for chunk in tiny)


def test_dummy_prefix_and_mismatch_fallback_remain_explicit():
    assert piece_char_offsets("hello world", [b" hello", b" world"]) == [0, 5, 11]
    assert piece_char_offsets("abc", [b"wrong"]) is None
    chunks = chunk_text_tokens("short source", budget_tokens=10, tokenize=lambda text: [b"wrong"])
    assert "".join(chunk.text for chunk in chunks) == "short source"
    assert all(chunk.tokens is None for chunk in chunks)


def test_paragraph_byte_pieces_preserve_body_and_locations():
    text = "甲乙\n\n丙丁"
    chunks = run("prose", text, 8)
    assert "".join(chunk.text for chunk in chunks) == "甲乙丙丁"
    assert [(chunk.loc, chunk.loc_end) for chunk in chunks] == [(1, 1), (3, 3)]
    assert all(len(chunk.text.encode("utf-8")) <= chunk.tokens <= 8 for chunk in chunks)


@pytest.mark.parametrize("kind", ["prose", "code", "guard"])
def test_atomic_raw_group_can_include_more_than_one_point(kind):
    pieces = [b"A\xe7", b"\x94\xb2"]
    with pytest.raises(ChunkTokenBudgetError):
        run(kind, "A甲", 1, tokenize=lambda text: pieces)
    chunks = run(kind, "A甲", 2, tokenize=lambda text: pieces)
    assert [chunk.text for chunk in chunks] == ["A甲"]
    assert all(chunk.tokens in (None, 2) for chunk in chunks)


@pytest.mark.parametrize("kind", ["prose", "code", "guard"])
def test_safe_raw_group_can_exceed_an_ordinary_64_token_budget(kind):
    text = "A" + "甲" * 70
    pieces = [b"A\xe7"] + [b"\x94\xb2\xe7"] * 69 + [b"\x94\xb2"]
    assert b"".join(pieces).decode("utf-8") == text
    with pytest.raises(ChunkTokenBudgetError):
        run(kind, text, 64, tokenize=lambda value: pieces)
    chunks = run(kind, text, len(pieces), tokenize=lambda value: pieces)
    assert [chunk.text for chunk in chunks] == [text]
    assert all(chunk.tokens in (None, len(pieces)) for chunk in chunks)
