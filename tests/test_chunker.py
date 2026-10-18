"""Tier-1: chunker — paragraph preference, overlap, caps, code line alignment."""

from __future__ import annotations

from goldmine.chunker import chunk_code, chunk_text, est_tokens


def test_paragraph_boundary_preferred():
    paras = ["word " * 200, "word " * 200, "word " * 200]  # ~1000 chars each
    text = "\n\n".join(paras)
    chunks = chunk_text(text, size=1200, overlap=200)
    assert len(chunks) >= 2
    # no chunk ends mid-word: every chunk ends at a paragraph or whitespace edge
    for chunk in chunks:
        assert chunk.text == chunk.text.strip()
        assert not chunk.text.endswith("wor")


def test_short_text_single_chunk():
    text = "just one small paragraph"
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].loc == 1 and chunks[0].loc_end == 1


def test_whitespace_fallback_when_no_paragraphs():
    text = " ".join(f"w{i}" for i in range(1000))  # no \n\n anywhere
    chunks = chunk_text(text, size=500, overlap=100)
    assert len(chunks) > 1
    # chunks break at whitespace, never mid-token
    for chunk in chunks:
        assert not chunk.text.startswith(" ") and not chunk.text.endswith(" ")
    # overlap: consecutive chunks share content
    assert chunks[1].text in text


def test_overlap_present_in_fallback():
    text = " ".join(f"tok{i}" for i in range(400))
    chunks = chunk_text(text, size=400, overlap=100)
    assert len(chunks) >= 2
    tail_of_first = chunks[0].text[-40:]
    assert tail_of_first.strip()
    # the token sequence of chunk0's tail appears in chunk1 (they overlap)
    first_tokens = chunks[0].text.split()
    second_tokens = chunks[1].text.split()
    shared = set(first_tokens) & set(second_tokens)
    assert shared, "expected overlap between consecutive chunks"


def test_max_chunks_cap():
    text = "\n\n".join(f"paragraph {i} " + "x " * 100 for i in range(100))
    chunks = chunk_text(text, size=200, overlap=50, max_chunks=3)
    assert len(chunks) == 3


def test_whitespace_cleaning_matches_digup():
    text = "a  b\t\tc\u00a0d\n\n\n\n\ne"
    chunks = chunk_text(text)
    assert chunks[0].text == "a b c d\n\ne"


def test_loc_are_line_numbers():
    text = "\n".join(f"line {i} with words to fill" for i in range(50))
    text += "\n\n" + text  # two paragraphs
    chunks = chunk_text(text, size=200, overlap=40)
    assert all(c.loc is not None and c.loc_end is not None for c in chunks)
    assert chunks[0].loc == 1
    later = chunks[-1]
    assert later.loc_end > later.loc


def test_chunk_code_line_alignment():
    lines = [f"line_{i} = {i}  # padding padding padding" for i in range(60)]
    text = "\n".join(lines)
    chunks = chunk_code(text, stretch=400)
    assert len(chunks) > 1
    joined = "\n".join(c.text for c in chunks)
    # whole lines preserved, never cut mid-line
    for chunk in chunks:
        for line in chunk.text.split("\n"):
            assert line in lines
    assert "line_0" in joined and "line_59" in joined
    assert chunks[0].loc == 1
    assert chunks[0].loc_end == chunks[0].text.count("\n") + 1


def test_chunk_code_long_line_own_chunk():
    long_line = "x" * 3000
    text = "short line\n" + long_line + "\nanother short\n"
    chunks = chunk_code(text, stretch=1800)
    assert any(c.text == long_line and c.loc == 2 and c.loc_end == 2 for c in chunks)
    assert any(c.text == "short line" and c.loc == 1 for c in chunks)
    assert any(c.text == "another short" and c.loc == 3 for c in chunks)


def test_est_tokens():
    assert est_tokens("") == 0
    assert est_tokens("x" * 400) == 100


# ------------------------------------------------- kind-aware token guard


def test_est_tokens_kind_aware():
    """kind='code' divides by the measured code density (04 §3.2/§3.3:
    ~0.67 chars/token — an 1800-char stretch hit 2670 tokens); every other
    kind keeps the v1 len//4 heuristic (pinned by test_est_tokens)."""
    from goldmine.chunker import CODE_CHARS_PER_TOKEN

    assert est_tokens("x" * 400) == 100                    # default unchanged
    assert est_tokens("x" * 400, kind="doc") == 100
    assert est_tokens("x" * 400, kind="code") == int(400 / CODE_CHARS_PER_TOKEN)
    # ~7x denser than prose: 670 code chars estimate ~1000 tokens
    assert est_tokens("x" * 670, kind="code") == int(670 / CODE_CHARS_PER_TOKEN)


def test_effective_chunk_size_kind_aware():
    from goldmine.chunker import CODE_CHARS_PER_TOKEN, effective_chunk_size

    # v1 behavior pinned for the default kind (existing guard tests rely on it)
    assert effective_chunk_size(2048) == effective_chunk_size(2048, "doc")
    assert effective_chunk_size(512, "code") == int((512 - 96) * CODE_CHARS_PER_TOKEN)
    # code shrinks below the prose budget at llama's 2048-token ceiling
    code_size = effective_chunk_size(2048, "code")
    assert code_size == int((2048 - 96) * CODE_CHARS_PER_TOKEN)  # ~1307
    assert code_size < effective_chunk_size(2048)
    assert 256 < code_size < 1800
    # the floor still applies to code on tiny contexts
    assert effective_chunk_size(200, "code") == 256
    assert effective_chunk_size(None, "code") == 1800


def test_code_stretch_shrinks_under_kind_aware_guard():
    """End-to-end guard: at a 2048-token ceiling, code chunks must respect
    the code budget (a stretch larger than the budget must split)."""
    from goldmine.chunker import chunk_code, effective_chunk_size

    stretch = effective_chunk_size(2048, "code")
    lines = [f"fn_{i:04d}(x) = (x * {i}) // {i}  # dense punctuation, symbols" for i in range(400)]
    chunks = chunk_code("\n".join(lines), stretch=stretch)
    assert max(len(c.text) for c in chunks) <= stretch + 1  # whole-line slack only
