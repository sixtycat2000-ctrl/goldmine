"""chunk.v7-original-text: prose chunks keep the verbatim source slice and true lines.

Preregistered in reports/execution/P0-05/chunker-original-text-001. The
cleaned `text` (embedding input) and `tokens` must stay byte-identical to the
pre-change chunker; only the stored text and line locations change.
"""
from __future__ import annotations

import hashlib
import random
import re
from contextlib import closing

import pytest

from goldmine.chunker import (
    CHUNKER_VERSION, _TokenIndex, _clean, _clean_mapped, _walk_tokens, chunk_text, chunk_text_tokens,
)

INDENTED = (
    "Timeouts\n--------\n\n\n\n"
    "Most requests to external servers should have a timeout attached.\n"
    "\n"
    "    r = requests.get('https://github.com', timeout=(3.05, 27))\n"
    "\n"
    ".. note:: The connect timeout applies to each connection attempt.\n"
    "          If multiple addresses exist for a domain name, the underlying\n"
    "          ``urllib3`` will try each address\tsequentially.\n"
    "\n\n\n"
    "完整的\u00a0事实   保留在\n    缩进行里。\n\n"
    "Final paragraph with trailing spaces.   \n\n\n"
)


def pieces_for(text):
    """Deterministic pseudo-tokenizer: one piece per word/whitespace run."""
    return [part.encode("utf-8") for part in re.findall(r"\s+|\S+", text)]


def random_text(seed):
    rng = random.Random(seed)
    atoms = ["word", "Word", "甲乙", "丙", "x_y", "3.5", " ", "  ", "\t", "\u00a0", "\n", "\n\n", "\n\n\n", "\n \n\t\n", "。", "."]
    return "".join(rng.choice(atoms) for _ in range(rng.randint(0, 120)))


@pytest.mark.parametrize("seed", range(60))
def test_clean_mapped_reproduces_clean_and_offsets_point_at_source(seed):
    text = random_text(seed) if seed else INDENTED
    clean, offsets = _clean_mapped(text)
    assert clean == _clean(text)
    assert len(offsets) == len(clean) + 1
    assert offsets == sorted(offsets) and all(0 <= o <= len(text) for o in offsets)
    for i, ch in enumerate(clean):
        if not ch.isspace():
            assert text[offsets[i]] == ch
    assert _clean(clean) == clean  # idempotent: the estimator path re-cleans


def _old_chunks(text, budget, overlap, tokenize):
    """The pre-change chunker: cleaned text in, cleaned-text locations out."""
    clean = _clean(text)
    if not clean:
        return []
    pieces = tokenize(clean) if tokenize is not None else None
    index = _TokenIndex(clean, pieces) if pieces else None
    if index is None or not index.ok:
        return chunk_text(clean, size=max(1, budget * 4), overlap=max(1, overlap * 4))
    return _walk_tokens(clean, index, budget, overlap, 5000)


@pytest.mark.parametrize("tokenize", [None, pieces_for], ids=["estimator", "pieces"])
@pytest.mark.parametrize("budget,overlap", [(8, 2), (20, 4), (450, 48)])
@pytest.mark.parametrize("seed", [0, 1, 2, 3, 7, 11])
def test_text_and_tokens_are_byte_identical_to_the_previous_chunker(tokenize, budget, overlap, seed):
    text = INDENTED if seed == 0 else random_text(seed) + INDENTED
    new = chunk_text_tokens(text, budget_tokens=budget, overlap_tokens=overlap, tokenize=tokenize)
    old = _old_chunks(text, budget, overlap, tokenize)
    assert [c.text for c in new] == [c.text for c in old]
    assert [c.tokens for c in new] == [c.tokens for c in old]


@pytest.mark.parametrize("tokenize", [None, pieces_for], ids=["estimator", "pieces"])
@pytest.mark.parametrize("budget", [6, 12, 450])
@pytest.mark.parametrize("seed", [0, 1, 2, 5, 8, 13, 21])
def test_original_is_the_verbatim_slice_within_its_true_lines(tokenize, budget, seed):
    text = INDENTED if seed == 0 else random_text(seed) + INDENTED
    chunks = chunk_text_tokens(text, budget_tokens=budget, overlap_tokens=2, tokenize=tokenize)
    assert chunks
    lines = text.split("\n")
    for chunk in chunks:
        assert chunk.original is not None and chunk.original in text
        assert not chunk.original[0].isspace()
        assert _clean(chunk.original) == chunk.text
        assert 1 <= chunk.loc <= chunk.loc_end <= len(lines)
        window = "\n".join(lines[chunk.loc - 1:chunk.loc_end])
        assert chunk.original in window
        # the slice starts on line `loc` and ends on line `loc_end` exactly
        start = text.index(chunk.original)
        assert text.count("\n", 0, start) + 1 == chunk.loc
        assert text.count("\n", 0, start + len(chunk.original) - 1) + 1 == chunk.loc_end


def test_indented_fact_survives_in_one_stored_slice():
    chunks = chunk_text_tokens(INDENTED, budget_tokens=450, overlap_tokens=48, tokenize=pieces_for)
    fact = ("The connect timeout applies to each connection attempt.\n"
            "          If multiple addresses exist for a domain name, the underlying\n"
            "          ``urllib3`` will try each address\tsequentially.")
    assert any(fact in c.original for c in chunks)
    assert not any(fact in c.text for c in chunks)  # the cleaned text never held it
    note = next(c for c in chunks if fact in c.original)
    assert note.loc == 1 and INDENTED.split("\n")[note.loc_end - 1].startswith("Final paragraph")
    assert "完整的\u00a0事实   保留在\n    缩进行里。" in note.original


def test_code_and_resplit_chunks_keep_no_original():
    from goldmine.chunker import chunk_code_tokens, split_text_by_tokens

    assert all(c.original is None for c in chunk_code_tokens("def f():\n    return 1\n", budget_tokens=8))
    assert all(c.original is None for c in split_text_by_tokens("a b c d e f", 2))


def test_store_keeps_verbatim_original_and_true_lines(tmp_path, monkeypatch):
    from goldmine.backends.mock import MockBackend
    from goldmine.config import Config
    from goldmine.ingest import IngestPipeline
    from goldmine.ingest import pipeline as pipeline_module
    from goldmine.lexical import normalize
    from goldmine.store import Store

    assert CHUNKER_VERSION == "chunk.v7-original-text"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "advanced.rst"
    source.write_text(INDENTED, encoding="utf-8")
    cfg = Config()
    cfg.home = tmp_path / "home"
    cfg.ingest.workers = 1
    monkeypatch.setattr(pipeline_module.fingerprints, "current_extractors", lambda: {"text.v1"})
    with closing(Store(cfg.home / "index.sqlite")) as store:
        with MockBackend(dim=768) as backend:
            report = IngestPipeline(cfg, store, backend).run([str(source)])
        assert report.content_indexed == 1 and report.failed == 0
        fid = store.find_by_path(str(source)).id
        rows = store.db.execute(
            "SELECT s.id, s.loc, s.loc_end, f.body, f.original FROM segments s JOIN fts f ON f.rowid=s.id "
            "WHERE s.file=? AND s.kind!='name' ORDER BY s.id", (fid,)).fetchall()
        assert rows
        lines = INDENTED.split("\n")
        for sid, loc, loc_end, body, original in rows:
            stored = store.segment_text(sid)
            assert stored == (original if original is not None else body)
            assert stored in INDENTED
            assert body == normalize(stored)
            assert stored in "\n".join(lines[int(loc) - 1:int(loc_end)])
        joined = "".join(r[4] or r[3] for r in rows)
        assert "          If multiple addresses exist" in joined
        # vectors were computed from the cleaned text: the mock is content-addressed
        vec = store.db.execute("SELECT vec FROM segments WHERE id=?", (rows[0][0],)).fetchone()[0]
        assert vec is not None and len(vec) == 768 * 2
    digest = hashlib.sha256(INDENTED.encode()).hexdigest()
    assert digest  # fixture identity for the preregistration record
