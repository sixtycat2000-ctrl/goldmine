"""Deterministic token-piece regressions; no model or quality evaluation."""

from __future__ import annotations

import hashlib
import re
from contextlib import closing
from pathlib import Path

import pytest

from goldmine.chunker import CHUNKER_VERSION, chunk_text_tokens, piece_char_offsets


def pieces_for(parts):
    data = [part.encode("utf-8") for part in parts]

    def tokenize(text):
        assert b"".join(data).decode("utf-8") == text
        return data

    return tokenize


def compact(text):
    return re.sub(r"\s", "", text)


@pytest.mark.parametrize("parts,budget", [
    (["ALPHA", "\n\n", "BETA"], 2),
    (["甲乙丙丁", "\n\n", "戊己庚辛"], 2),
    (["alpha", " ", "beta", "\n\n", "完整", "事实", "保留"], 4),
    (list("ALPHA\n\nBETA"), 7),
    (list("甲乙丙丁\n\n戊己庚辛"), 6),
])
def test_paragraph_keeps_first_post_boundary_token(parts, budget):
    text = "".join(parts)
    chunks = chunk_text_tokens(text, budget_tokens=budget, overlap_tokens=0,
                               tokenize=pieces_for(parts))
    assert len(chunks) == 2
    assert compact("".join(c.text for c in chunks)) == compact(text)
    assert all(c.text in text and 0 < c.tokens <= budget for c in chunks)
    assert [(c.loc, c.loc_end) for c in chunks] == [(1, 3), (3, 3)]


@pytest.mark.parametrize("parts,budget", [
    (["AB", "", "\n\n", "", "CD", "", "EF"], 4),
    (["AB", "", "\n", "", "\n", "", "CD", "", "EF"], 6),
    (["AB", "\n\n", "", "", "", "CD"], 3),
    # The second newline shares a token with content. Starting at that
    # token repeats whitespace, which is safe; skipping it would lose CD.
    (["AB", "\n", "\nCD", "EF"], 2),
])
def test_repeated_offsets_and_newline_piece_edges_conserve_content(parts, budget):
    text = "".join(parts)
    assert piece_char_offsets(text, [p.encode() for p in parts]) is not None
    chunks = chunk_text_tokens(text, budget_tokens=budget, overlap_tokens=0,
                               tokenize=pieces_for(parts), max_chunks=32)
    assert compact("".join(c.text for c in chunks)) == compact(text)
    assert 1 <= len(chunks) <= len(parts)
    for c in chunks:
        assert c.text in text
        assert 0 < c.tokens <= budget
        assert 1 <= c.loc <= c.loc_end <= 3


def test_bounded_paragraph_matrix_preserves_unique_source_characters():
    # 24 small piece layouts, with explicit empty tokens around cuts; no
    # corpus-derived text, tokenizer inference, or wall-time performance gate.
    for empty_before in range(3):
        for empty_after in range(4):
            for split_newline in (False, True):
                newline = ["\n", "\n"] if split_newline else ["\n\n"]
                first = ["AB", "CD", *([""] * empty_before), *newline]
                parts = first + [*([""] * empty_after), "EF", "GH", "IJ"]
                text = "".join(parts)
                chunks = chunk_text_tokens(text, budget_tokens=len(first),
                    overlap_tokens=0, tokenize=pieces_for(parts), max_chunks=64)
                assert compact("".join(c.text for c in chunks)) == "ABCDEFGHIJ"
                assert len(chunks) <= len(parts)
                assert all(0 < c.tokens <= len(first) for c in chunks)


def test_repeated_paragraphs_finish_at_source_end_and_honor_chunk_cap():
    parts = ["AA", "\n\n", "BB", "\n\n", "CC", "\n\n", "DD"]
    text = "".join(parts)
    full = chunk_text_tokens(text, budget_tokens=2, overlap_tokens=0,
                             tokenize=pieces_for(parts), max_chunks=20)
    assert compact("".join(c.text for c in full)) == "AABBCCDD"
    assert [(c.loc, c.loc_end) for c in full] == [(1, 3), (3, 5), (5, 7), (7, 7)]
    limited = chunk_text_tokens(text, budget_tokens=2, overlap_tokens=0,
                                tokenize=pieces_for(parts), max_chunks=2)
    assert len(limited) == 2 and limited == full[:2]


def test_whitespace_only_tail_and_empty_pieces_do_not_loop():
    # _walk_tokens' input can contain trailing whitespace after normal
    # cleaning, so exercise that private seam explicitly and bound output.
    from goldmine.chunker import _TokenIndex, _walk_tokens

    text = "AB\n\n"
    parts = [b"AB", b"\n\n", b"", b""]
    chunks = _walk_tokens(text, _TokenIndex(text, parts), 2, 0, 10)
    assert [c.text for c in chunks] == ["AB"]
    assert chunks[0].tokens == 2


def test_plain_ingest_refreshes_only_stale_content_then_becomes_unchanged(tmp_path, monkeypatch):
    # Genuine Store + ordinary pipeline path, with explicit deterministic
    # in-process mock vectors. These are mechanical fixtures, not IR quality.
    from goldmine.backends.mock import MockBackend
    from goldmine.config import Config
    from goldmine.ingest import IngestPipeline
    from goldmine.ingest import pipeline as pipeline_module
    from goldmine.store import FileRec, SegmentRec, Store

    assert CHUNKER_VERSION == "chunk.v6-char-tail"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    content = corpus / "document.txt"
    metadata = corpus / "unextractable.txt"
    content.write_text("Ordinary source paragraph.\n\nA complete second fact.\n", encoding="utf-8")
    metadata.write_text("Owned unextractable fixture.\n", encoding="utf-8")
    cfg = Config()
    cfg.home = tmp_path / "home"
    cfg.ingest.workers = 1
    monkeypatch.setattr(pipeline_module.fingerprints, "current_extractors", lambda: {"text.v1"})
    with closing(Store(cfg.home / "index.sqlite")) as store:
        with MockBackend(dim=768) as backend:
            initial = IngestPipeline(cfg, store, backend).run([str(content)])
        assert initial.content_indexed == 1 and initial.failed == 0
        fid = store.find_by_path(str(content)).id
        with store.write_transaction():
            store.db.execute("UPDATE files SET chunker_version=?,extractor_version=? WHERE id=?",
                             ("chunk.v3-page", "text.v1", fid))
            # A metadata-only record deliberately keeps its old chunker
            # marker: it has no content chunks that require rechunking.
            st = metadata.stat()
            mid = store.upsert_file(FileRec(path=str(metadata), size=st.st_size,
                mtime=st.st_mtime, sha256=hashlib.sha256(metadata.read_bytes()).hexdigest(),
                state="done", capability="metadata_only", kind="doc",
                chunker_version="chunk.v3-page", extractor_version="text.v1"))
            store.replace_segments(mid, [SegmentRec(kind="name", modality="none",
                                                   text_for_fts="owned fixture metadata")])
        old_content_sids = [r[0] for r in store.db.execute("SELECT id FROM segments WHERE file=?", (fid,))]
        old_metadata = store.db.execute("SELECT * FROM files WHERE id=?", (mid,)).fetchone()
        old_metadata_segments = store.db.execute("SELECT * FROM segments WHERE file=?", (mid,)).fetchall()
        with MockBackend(dim=768) as backend:
            refreshed = IngestPipeline(cfg, store, backend).run([str(corpus)])
        assert refreshed.unchanged == 1 and refreshed.content_indexed == 1 and refreshed.failed == 0
        new_content_sids = [r[0] for r in store.db.execute("SELECT id FROM segments WHERE file=?", (fid,))]
        assert old_content_sids != new_content_sids
        rec = store.find_by_path(str(content))
        assert rec.chunker_version == CHUNKER_VERSION
        assert store.db.execute("SELECT * FROM files WHERE id=?", (mid,)).fetchone() == old_metadata
        assert store.db.execute("SELECT * FROM segments WHERE file=?", (mid,)).fetchall() == old_metadata_segments
        # The real extractor value from the refresh is current for this
        # second pass; avoid native/version probes unrelated to this test.
        monkeypatch.setattr(pipeline_module.fingerprints, "current_extractors", lambda: {rec.extractor_version})
        with MockBackend(dim=768) as backend:
            again = IngestPipeline(cfg, store, backend).run([str(corpus)])
        assert again.unchanged == 2 and again.failed == 0
