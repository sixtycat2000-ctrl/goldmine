"""Lossless visited character windows; estimator output has unknown tokens."""
import math

import pytest

from goldmine.chunker import chunk_text, chunk_text_tokens

SOURCES = ["ABCDEFGHIJKLMNOPQRSTUVWXYZ", "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥",
           "🙂🙃🦊🐈🌳🌼🌻🚲", "A甲B🙂CéD乙E"]


@pytest.mark.parametrize("text", SOURCES)
@pytest.mark.parametrize("size", [1, 2, 3, 7, 8, 26, 100])
def test_hard_windows_keep_entire_no_space_source(text, size):
    chunks = chunk_text(text, size=size, overlap=0)
    assert "".join(chunk.text for chunk in chunks) == text
    assert len(chunks) == math.ceil(len(text) / size)
    assert all(0 < len(chunk.text) <= size and chunk.tokens is None for chunk in chunks)
    assert all(chunk.loc == chunk.loc_end == 1 for chunk in chunks)


@pytest.mark.parametrize("text", SOURCES)
@pytest.mark.parametrize("mode", ["unavailable", "mismatched"])
def test_tokenizer_fallback_keeps_tail_without_claiming_token_count(text, mode):
    tokenize = None if mode == "unavailable" else lambda value: [b"wrong"]
    chunks = chunk_text_tokens(text, budget_tokens=1, overlap_tokens=0, tokenize=tokenize)
    assert "".join(chunk.text for chunk in chunks) == text
    assert all(len(chunk.text) <= 4 and chunk.tokens is None for chunk in chunks)


@pytest.mark.parametrize("text", SOURCES)
def test_hard_window_cap_keeps_explicit_prefix_only(text):
    limited = chunk_text(text, size=3, overlap=0, max_chunks=2)
    assert "".join(chunk.text for chunk in limited) == text[:6]
    assert len(limited) == 2
    assert chunk_text(text, size=3, max_chunks=0) == []


def test_hard_window_line_coordinates_remain_source_coordinates():
    source = "ABCDEFGHIJ\nKLMNOPQRST\nUVWXYZ"
    chunks = chunk_text(source, size=4, overlap=0)
    assert "".join("".join(chunk.text.split()) for chunk in chunks) == "".join(source.split())
    for chunk in chunks:
        start = source.index(chunk.text)
        end = start + len(chunk.text)
        assert chunk.loc == source.count("\n", 0, start) + 1
        assert chunk.loc_end == source.count("\n", 0, end) + 1


def test_empty_and_short_input_do_not_add_material():
    assert chunk_text("") == [] and chunk_text(" \n\t ") == []
    chunks = chunk_text("small", size=8, overlap=0)
    assert len(chunks) == 1 and chunks[0].text == "small"


@pytest.mark.parametrize("size", [0, -1])
@pytest.mark.parametrize("text", ["abc", ""])
def test_nonpositive_character_size_is_an_explicit_error(text, size):
    with pytest.raises(ValueError, match="positive"):
        chunk_text(text, size=size)
