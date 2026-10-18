"""Tier-1/2: token-true chunking (v2 item D3, 01 §5.5 / REDESIGN C5+C6).

- token-budget chunking unit (prose / CJK / code) with a realistic piece
  tokenizer (substrings, SPM-style space attachment)
- fake-server /tokenize contract (ids, with_pieces, add_special, byte arrays,
  failure fallback)
- ctx-guard SPLIT-not-fail on oversized chunks
- chunker-stale files re-done by plain ingest (no exit-3, B11)
- mock estimator fallback (deterministic, kind-aware)
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import numpy as np
import pytest

from goldmine.backends.llama import LlamaServerBackend
from goldmine.backends.mock import MockBackend
from goldmine.chunker import (
    CHUNKER_VERSION,
    chunk_code_tokens,
    chunk_text,
    chunk_text_tokens,
    effective_token_budget,
    est_tokens,
    piece_char_offsets,
    split_text_by_tokens,
)
from goldmine.config import Config, LlamaCfg
from goldmine.ingest import IngestPipeline
from goldmine.store import Store

FAKE_SERVER = Path(__file__).resolve().parent / "fake_llama_server.py"


# ------------------------------------------------------------ fake tokenizer

def spm_like(text: str) -> list[bytes]:
    """Deterministic realistic pieces: ASCII words split at <=5 chars with the
    preceding space glued to the FOLLOWING piece (SPM style, like the real
    gemma /tokenize verified live); \n runs their own piece; non-ASCII chars
    one piece each. Pieces concatenate back to the text exactly."""
    pieces: list[bytes] = []
    cur = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\n":
            if cur:
                pieces.append(cur.encode())
                cur = ""
            j = i
            while j < len(text) and text[j] == "\n":
                j += 1
            pieces.append(text[i:j].encode())
            i = j
            continue
        if ord(ch) > 127:
            if cur:
                pieces.append(cur.encode())
                cur = ""
            pieces.append(ch.encode())
            i += 1
            continue
        cur += ch
        if len(cur) >= 5:
            pieces.append(cur.encode())
            cur = ""
        i += 1
    if cur:
        pieces.append(cur.encode())
    return pieces


def count_spm(text: str) -> int:
    return len(spm_like(text))


# ------------------------------------------------------------ piece alignment

def test_piece_char_offsets_exact():
    text = "hello world\n\nhéllo 中"
    pieces = spm_like(text)
    offsets = piece_char_offsets(text, pieces)
    assert offsets is not None
    assert offsets[0] == 0 and offsets[-1] == len(text)
    assert offsets == sorted(offsets)  # non-decreasing boundaries
    # the text up to each offset is exactly the consumed pieces
    data = text.encode("utf-8")
    ptr = 0
    for off, piece in zip(offsets[1:], pieces):
        ptr += len(piece)
        assert len(data[:ptr].decode("utf-8", "surrogateescape")) == off


def test_piece_char_offsets_tolerates_spm_dummy_prefix():
    # classic SPM prepends a dummy space to the first piece
    text = "hello world"
    pieces = [b" hello", b" world"]
    assert piece_char_offsets(text, pieces) == [0, 5, 11]


def test_piece_char_offsets_mismatch_returns_none():
    assert piece_char_offsets("abc", [b"xyz"]) is None
    assert piece_char_offsets("abc", [b"ab"]) is None  # short concat
    assert piece_char_offsets("abc", [b"abc", b"d"]) is None  # over concat


def test_byte_array_pieces_align():
    # byte-fallback tokens: pieces arrive as byte arrays of single utf-8 chars
    text = "a中b"
    pieces = [b"a", "中".encode(), b"b"]
    assert piece_char_offsets(text, pieces) == [0, 1, 2, 3]


# ------------------------------------------- piece_char_offsets linear scan (F4)


def _reference_offsets(text: str, pieces: list[bytes]) -> list[int] | None:
    """The PRE-FIX quadratic implementation, kept verbatim as the equivalence
    reference (06-perf F4: it decoded data[:ptr] per piece — O(bytes x pieces),
    391 s on 5 MB of code). The shipped function must stay byte-identical."""
    data = text.encode("utf-8")
    n = len(data)
    ptr = 0
    offsets = [0]
    for i, piece in enumerate(pieces):
        if not piece:
            offsets.append(len(data[:ptr].decode("utf-8", "surrogateescape")))
            continue
        if (ptr == 0 and i == 0 and piece[:1] == b" " and data[:1] != b" "
                and len(piece) > 1 and data.startswith(piece[1:])):
            piece = piece[1:]
        end = ptr + len(piece)
        if end > n or data[ptr:end] != piece:
            return None
        ptr = end
        offsets.append(len(data[:ptr].decode("utf-8", "surrogateescape")))
    if ptr != n:
        return None
    return offsets


def _byte_fallback(text: str) -> list[bytes]:
    """Worst-case tokenizer: every multi-byte char split into SINGLE-BYTE
    pieces (4 pieces per emoji), like a byte-fallback BPE under pressure."""
    pieces: list[bytes] = []
    cur = ""
    for ch in text:
        if ord(ch) > 127:
            if cur:
                pieces.append(cur.encode())
                cur = ""
            pieces.extend(bytes([b]) for b in ch.encode("utf-8"))
        else:
            cur += ch
            if len(cur) >= 5:
                pieces.append(cur.encode())
                cur = ""
    if cur:
        pieces.append(cur.encode())
    return pieces


def _combining(text: str) -> list[bytes]:
    """Text with combining marks (e + U+0301 = TWO chars -> two pieces)."""
    return spm_like(text)


@pytest.mark.parametrize("tok", [spm_like, _byte_fallback, _combining],
                         ids=["spm", "byte-fallback", "combining"])
@pytest.mark.parametrize("text", [
    "",
    "hello world",
    " leading space text",
    "emoji 🚀👍 family 🧑‍👩‍👧 and 中text",
    "combining: café résumé — élan",
    "mixed 中英 🚀 é…\n\nsecond paragraph\ttab",
])
def test_piece_char_offsets_identical_to_reference(text, tok):
    pieces = tok(text)
    assert piece_char_offsets(text, pieces) == _reference_offsets(text, pieces)


def test_piece_char_offsets_zero_width_pieces_match_reference():
    """Zero-width (special-token) pieces take the `offsets.append(chars)`
    branch — must land on the same char offsets as the reference."""
    text = "alpha beta gamma delta epsilon zeta"
    pieces = []
    for i, p in enumerate(spm_like(text)):
        pieces.append(p)
        if i % 3 == 2:
            pieces.append(b"")  # id-less special token
    assert piece_char_offsets(text, pieces) == _reference_offsets(text, pieces)


def test_piece_char_offsets_dummy_prefix_matches_reference():
    text = "hello world"
    pieces = [b" hello", b" world"]  # classic SPM dummy prefix
    assert piece_char_offsets(text, pieces) == [0, 5, 11]
    assert piece_char_offsets(text, pieces) == _reference_offsets(text, pieces)


WORDS = ("gateway timeout retry index segment vector query fusion lexical semantic "
         "chunk token buffer pipeline backend latency throughput normalize pooling "
         "matrix score file store embed model batch slot queue kernel metal ").split()


@pytest.mark.slow
def test_piece_char_offsets_linear_on_5mb():
    """06-perf F4 regression gate: offsets for a 5 MB corpus with >1M pieces
    must complete in seconds, not minutes (the pre-fix code spent 391 s on
    5 MB of code — O(bytes x pieces) prefix decodes). Generous 10 s ceiling
    vs the ~1 s measured; fully offline. Also pins byte-identity against the
    reference on a 1 MB slice (the 5 MB reference run is the 391 s thing)."""
    import time

    code = "".join(
        "def fn_%d(x):\n    return x*%d+%d  # pad pad pad\n" % (i, i, i * 7)
        for i in range(130_000)
    )[:5_242_880]
    pieces = spm_like(code)
    assert len(pieces) > 1_000_000  # the piece density that made it quadratic
    t0 = time.perf_counter()
    offsets = piece_char_offsets(code, pieces)
    elapsed = time.perf_counter() - t0
    assert offsets is not None and offsets[-1] == len(code)
    assert elapsed < 10.0, f"piece_char_offsets went quadratic again: {elapsed:.1f}s"
    # byte-identity vs the pre-fix implementation on a slice the reference
    # can afford (~1 MB, the quadratic cost is ~linear*pieces/5)
    ref = _reference_offsets(code[:1_048_576], spm_like(code[:1_048_576]))
    assert piece_char_offsets(code[:1_048_576], spm_like(code[:1_048_576])) == ref


# ------------------------------------------------------------ prose chunking

def test_token_budget_respected_prose():
    text = "\n\n".join(
        " ".join(f"word{i}" for i in range(w, w + 40)) for w in range(0, 400, 40)
    )
    chunks = chunk_text_tokens(text, budget_tokens=50, overlap_tokens=8,
                               tokenize=spm_like)
    assert len(chunks) > 1
    for c in chunks:
        assert count_spm(c.text) <= 50 + 1, c.text[:60]  # 1 = leading-space piece
    # every chunk is a verbatim slice of the cleaned text
    clean = text  # already clean
    for c in chunks:
        assert c.text in clean


def test_paragraph_boundary_preferred_in_token_space():
    paras = ["sentence one two three four. " * 8,
             "second paragraph words here. " * 8,
             "third paragraph final words. " * 8]
    text = "\n\n".join(paras)
    chunks = chunk_text_tokens(text, budget_tokens=60, overlap_tokens=8,
                               tokenize=spm_like)
    # cuts land on paragraph ends where the budget allows
    para_ends = [p.rstrip() for p in paras]
    hits = sum(1 for c in chunks if any(c.text.endswith(e) for e in para_ends))
    assert hits >= 1


def test_cjk_no_whitespace_chunks_fine():
    """The point of D3: CJK text has no whitespace; cuts must land on sentence
    punctuation (。！？…) or token boundaries — never mid-ideograph."""
    text = ("今天天气很好。" + "我们去公园散步！" + "风景优美…" + "人们快乐。") * 30
    chunks = chunk_text_tokens(text, budget_tokens=40, overlap_tokens=5,
                               tokenize=spm_like)
    assert len(chunks) > 3
    for c in chunks:
        assert count_spm(c.text) <= 40 + 1
    # never mid-ideograph: every boundary is a real char boundary (implicit —
    # pieces are whole chars) and text is a verbatim slice
    for c in chunks:
        assert c.text in text


def test_cjk_prefers_sentence_punctuation():
    text = ("月亮升起来了。" * 12) + "\n\n" + ("星星也很亮！" * 12)
    chunks = chunk_text_tokens(text, budget_tokens=30, overlap_tokens=4,
                               tokenize=spm_like)
    sentence_endings = sum(
        1 for c in chunks if c.text.endswith(("。", "！", "…"))
    )
    assert sentence_endings >= len(chunks) // 2


def test_overlap_in_token_space():
    text = " ".join(f"tok{i}" for i in range(600))  # no paragraphs
    chunks = chunk_text_tokens(text, budget_tokens=40, overlap_tokens=10,
                               tokenize=spm_like)
    assert len(chunks) > 2
    # some content shared between consecutive chunks (~10 tokens, not all)
    first_words = set(chunks[0].text.split())
    second_words = set(chunks[1].text.split())
    shared = first_words & second_words
    assert 1 <= len(shared) <= 12


def test_short_text_single_chunk_tokens():
    text = "just one small paragraph"
    chunks = chunk_text_tokens(text, tokenize=spm_like)
    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].loc == 1 and chunks[0].loc_end == 1


def test_max_chunks_cap_tokens():
    text = "\n\n".join(f"paragraph {i} " + "word " * 60 for i in range(60))
    chunks = chunk_text_tokens(text, budget_tokens=50, overlap_tokens=8,
                               tokenize=spm_like, max_chunks=3)
    assert len(chunks) == 3


# ------------------------------------------------------------- code chunking

def test_code_lines_accumulate_to_token_budget():
    lines = [f"fn_{i:04d}(x) = (x * {i}) // {i}  # dense punctuation" for i in range(200)]
    text = "\n".join(lines)
    chunks = chunk_code_tokens(text, budget_tokens=30, tokenize=spm_like)
    assert len(chunks) > 3
    for c in chunks:
        assert count_spm(c.text) <= 30 + 2  # newline pieces count slack
        for line in c.text.split("\n"):
            assert line in lines  # whole lines only
    assert chunks[0].loc == 1


def test_code_overbudget_single_line_hard_cut():
    """01 §5.5: a single over-budget line is hard-cut at token boundaries —
    the whole-line-own-chunk rule (which shipped an 82929-token JSON line into
    a 2048 ceiling) now applies only to lines WITHIN budget."""
    giant = '{"vectors":[0.0023304052,-0.021797102,-0.025986522]}' * 400
    text = "short = 1\n" + giant + "\nlast = 2\n"
    chunks = chunk_code_tokens(text, budget_tokens=50, tokenize=spm_like)
    giant_parts = [c for c in chunks if "vectors" in c.text]
    assert len(giant_parts) > 1
    for c in giant_parts:
        assert count_spm(c.text) <= 50 + 1
    # stitched back together, the giant line's text survives whole
    assert "".join(p.text for p in giant_parts).startswith(giant[:60])
    # line-number locs: all sub-chunks point at line 2; neighbors at 1 and 3
    assert any(c.loc == 1 and c.text == "short = 1" for c in chunks)
    assert any(c.loc == 3 and c.text == "last = 2" for c in chunks)
    assert all(c.loc == 2 and c.loc_end == 2 for c in giant_parts)


# ------------------------------------------------------------ estimator path

def test_estimator_fallback_is_v1_equivalent():
    """No tokenizer -> budget converts kind-aware to chars and the v1 char
    walk runs unchanged (deterministic offline path, C6)."""
    text = " ".join(f"word{i}" for i in range(2000))
    tok = chunk_text_tokens(text, budget_tokens=450, overlap_tokens=48)
    ref = chunk_text(text, size=1800, overlap=192)
    assert [c.text for c in tok] == [c.text for c in ref]


def test_estimator_code_density_shrinks_stretches():
    text = "\n".join(f"line_{i} = {i}  # pad" for i in range(400))
    chunks = chunk_code_tokens(text, budget_tokens=450)
    # 450 tokens * 0.67 chars/token = 301-char stretches
    assert max(len(c.text) for c in chunks) <= 302


def test_mock_backend_count_tokens_is_len_quarter():
    mock = MockBackend()
    assert mock.count_tokens(["x" * 400, "y" * 100]) == [100, 25]
    assert mock.count_tokens([]) == []
    assert mock.token_pieces("anything") is None  # estimator path offline


def test_effective_token_budget():
    assert effective_token_budget(None) == 450
    assert effective_token_budget(2048) == 450
    assert effective_token_budget(512) == 512 - 40 - 24
    assert effective_token_budget(100) == 64  # floor
    assert effective_token_budget(64) == 64


def test_misaligned_pieces_fall_back_to_estimator():
    def bad_tokenize(text):
        return [b"\xff\xfe"]  # does not align

    text = "hello world " * 100
    tok = chunk_text_tokens(text, budget_tokens=450, overlap_tokens=48,
                            tokenize=bad_tokenize)
    ref = chunk_text(text, size=1800, overlap=192)
    assert [c.text for c in tok] == [c.text for c in ref]


# --------------------------------------------------- split-not-fail (guard)

class _CountingBackend(MockBackend):
    """Mock + a tiny token ceiling + exact spm_like counting: exercises the
    pipeline's ctx-guard split against a REAL count source."""

    def __init__(self, ceiling: int):
        super().__init__(dim=768)
        self.ceiling = ceiling

    def max_input_tokens(self) -> int | None:
        return self.ceiling

    def count_tokens(self, texts):
        return [count_spm(t) for t in texts]

    def token_pieces(self, text):
        return spm_like(text)


def test_split_text_by_tokens_pieces_and_estimator():
    text = " ".join(f"w{i}" for i in range(500))
    parts = split_text_by_tokens(text, 40, tokenize=spm_like)
    assert len(parts) > 2
    for p in parts:
        assert count_spm(p.text) <= 40
    assert "".join(p.text for p in parts) == text
    # estimator mode: proportional char slices, whole text covered
    parts2 = split_text_by_tokens(text, 10)
    assert len(parts2) >= len(text) // 40
    assert "".join(p.text for p in parts2) == text


def test_ctx_guard_splits_oversized_chunk_instead_of_failing(tmp_path):
    """The measured failure class: one chunk whose prompted token count
    exceeds the ceiling (2670 tokens vs 2048 / 82929 vs 2048). With the guard,
    the file is content-indexed with split segments — never failed."""
    from goldmine.config import load

    root = tmp_path / "corpus"
    root.mkdir()
    # one giant single line (no newline): estimator code chunker would make it
    # ONE chunk (v1 whole-line rule); budget 450 with pieces keeps it small,
    # so force the paranoid path with a TINY ceiling backend
    giant = "word " * 4000  # ~20000 chars, one line
    (root / "giant.txt").write_text(giant, encoding="utf-8")

    backend = _CountingBackend(ceiling=200)  # far below the 450 budget
    cfg = load()
    cfg.home = tmp_path / "home"
    store = Store(cfg.home / "idx.sqlite")
    try:
        with backend:
            pipeline = IngestPipeline(cfg, store, backend)
            report = pipeline.run([str(root)])
        assert report.failed == 0
        row = store.db.execute(
            "SELECT capability, detail FROM files WHERE path LIKE '%giant.txt'"
        ).fetchone()
        assert row[0] == "content_indexed"
        segs = store.db.execute(
            "SELECT fts.body FROM fts JOIN segments s ON fts.rowid = s.id "
            "JOIN files fi ON s.file = fi.id "
            "WHERE fi.path LIKE '%giant.txt' AND s.kind != 'name'"
        ).fetchall()
        assert len(segs) > 3  # split into many within-ceiling rows
        for (t,) in segs:
            assert count_spm(f"title: giant | text: {t}") <= 200
        vecs = store.db.execute(
            "SELECT COUNT(*) FROM segments s JOIN files f ON s.file = f.id "
            "WHERE f.path LIKE '%giant.txt' AND vec IS NOT NULL"
        ).fetchone()[0]
        assert vecs == len(segs)  # every split part embedded
    finally:
        store.close()


# ------------------------------------------------------ staleness re-do path

def test_chunker_stale_files_redone_by_plain_ingest(tmp_path):
    """v1-char index + new binary: plain ingest re-chunks the stale files —
    no refusal, no exit-3 semantics, no --reindex needed (D3/B11)."""
    from goldmine.config import load

    root = tmp_path / "corpus"
    root.mkdir()
    text = "\n\n".join(
        f"paragraph {i} " + "filler words here " * 30 for i in range(6)
    )
    f = root / "doc.txt"
    f.write_text(text, encoding="utf-8")

    cfg = load()
    cfg.home = tmp_path / "home"
    backend = MockBackend(dim=768)
    db = cfg.home / "idx.sqlite"
    store = Store(db)
    try:
        with backend:
            IngestPipeline(cfg, store, backend).run([str(root)])
        first = store.db.execute(
            "SELECT fts.body FROM fts JOIN segments s ON fts.rowid = s.id "
            "JOIN files fi ON s.file = fi.id "
            "WHERE fi.path = ? AND s.kind = 'chunk'", (str(f.resolve()),),
        ).fetchall()
        assert first
        # age the file: v1-char rows from an older binary
        store.db.execute(
            "UPDATE files SET chunker_version = 'chunk.v1-char' "
            "WHERE path = ?", (str(f.resolve()),),
        )
        store.db.commit()
        store.close()

        # plain ingest with the CURRENT binary: re-chunks, skips nothing
        store = Store(db)
        with MockBackend(dim=768) as backend2:
            report = IngestPipeline(cfg, store, backend2).run([str(root)])
        assert report.unchanged == 0, "chunker-stale file must not be skipped"
        assert report.failed == 0
        assert report.content_indexed == 1
        row = store.db.execute(
            "SELECT chunker_version, capability FROM files WHERE path = ?",
            (str(f.resolve()),),
        ).fetchone()
        assert row == (CHUNKER_VERSION, "content_indexed")
        second = store.db.execute(
            "SELECT fts.body FROM fts JOIN segments s ON fts.rowid = s.id "
            "JOIN files fi ON s.file = fi.id "
            "WHERE fi.path = ? AND s.kind = 'chunk'", (str(f.resolve()),),
        ).fetchall()
        assert second and [t for (t,) in second] != [t for (t,) in first] or second
        # nothing refused: no FingerprintMismatch path is even reachable here
        from goldmine import fingerprints

        report2 = fingerprints.staleness_report(
            store, fingerprints.live_embed_id(MockBackend(dim=768))
        )
        assert report2["chunker_stale"] == 0
    finally:
        store.close()


def test_fresh_ingest_is_unchanged_on_second_run(tmp_path):
    from goldmine.config import load

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.txt").write_text("some words to index here\n" * 20, encoding="utf-8")
    cfg = load()
    cfg.home = tmp_path / "home"
    store = Store(cfg.home / "idx.sqlite")
    try:
        with MockBackend(dim=768) as b:
            IngestPipeline(cfg, store, b).run([str(root)])
            report = IngestPipeline(cfg, store, b).run([str(root)])
        assert report.unchanged == 1
    finally:
        store.close()


# ------------------------------------------------- fake-server /tokenize API

def _fake_model(tmp_path: Path) -> Path:
    model = tmp_path / "fake-embedding-model.gguf"
    model.write_bytes(b"GGUF" + b"\x00" * 1024)
    return model


@pytest.fixture
def fake_cfg(tmp_path) -> LlamaCfg:
    FAKE_SERVER.chmod(
        FAKE_SERVER.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return LlamaCfg(
        server=FAKE_SERVER,
        server_configured=True,
        model=_fake_model(tmp_path),
        ctx=512,
        batch=2,
        startup_timeout=10.0,
        request_timeout=10.0,
    )


def test_count_tokens_contract_against_fake_server(fake_cfg):
    """count_tokens hits POST /tokenize with add_special=true (the embeddings
    path adds gemma BOS/EOS — 2 extra ids), one request per text."""
    import json as _json
    import urllib.request

    with LlamaServerBackend(fake_cfg) as backend:
        counts = backend.count_tokens(["hello world", "a", ""])
        assert counts is not None
        # fake tokenizer: "hello world" -> hello/ worl/d = 3 pieces + BOS + EOS
        assert counts[0] == 5
        assert counts[1] == 3  # "a" + BOS + EOS
        assert counts[2] == 2  # "" + BOS + EOS
        with urllib.request.urlopen(
            f"http://127.0.0.1:{backend._port}/stats", timeout=5
        ) as resp:
            stats = _json.loads(resp.read())
        assert stats["tokenize_requests"] == 3


def test_token_pieces_contract_against_fake_server(fake_cfg):
    with LlamaServerBackend(fake_cfg) as backend:
        pieces = backend.token_pieces("hello world\n\nhéllo 中")
        assert pieces is not None
        assert pieces[0] == b"hello"
        assert pieces[1] == b" worl"  # SPM-style: space glues forward
        assert b"\n\n" in pieces
        assert "é".encode() in pieces  # non-ascii pieces decode from byte arrays
        assert "中".encode() in pieces
        assert b"".join(pieces) == "hello world\n\nhéllo 中".encode()


def test_chunker_uses_server_pieces_end_to_end(fake_cfg, tmp_path):
    """Full loop: pipeline chunks a doc through the managed server's
    /tokenize (real spawn path), every chunk within the token budget."""
    from goldmine.config import load

    root = tmp_path / "corpus"
    root.mkdir()
    text = "\n\n".join(
        " ".join(f"word{i}" for i in range(w, w + 60)) for w in range(0, 300, 60)
    )
    (root / "doc.txt").write_text(text, encoding="utf-8")
    cfg = load()
    cfg.home = tmp_path / "home"
    cfg.llama = fake_cfg
    store = Store(cfg.home / "idx.sqlite")
    try:
        with LlamaServerBackend(fake_cfg) as backend:
            pipeline = IngestPipeline(cfg, store, backend)
            report = pipeline.run([str(root)])
        assert report.failed == 0
        assert report.content_indexed == 1
        segs = store.db.execute(
            "SELECT fts.body FROM fts JOIN segments s ON fts.rowid = s.id "
            "WHERE s.kind = 'chunk'"
        ).fetchall()
        budget = effective_token_budget(fake_cfg.ctx)  # 512-40-24
        for (t,) in segs:
            # count via the same fake tokenizer semantics (client side)
            assert len(t) <= budget * 5 + 6, (len(t), budget)
    finally:
        store.close()


def test_tokenize_failure_falls_back_to_estimator(fake_cfg, monkeypatch, tmp_path):
    """A 500ing /tokenize (server hiccup) never fails the run: the chunker
    falls back to the kind-aware estimator and the file still indexes."""
    import json as _json
    import urllib.request

    from goldmine.config import load

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "doc.txt").write_text(
        "estimator fallback words " * 200, encoding="utf-8"
    )
    cfg = load()
    cfg.home = tmp_path / "home"
    cfg.llama = fake_cfg
    store = Store(cfg.home / "idx.sqlite")
    try:
        with LlamaServerBackend(fake_cfg) as backend:
            assert backend.token_pieces("probe") is not None
            request = urllib.request.Request(
                f"http://127.0.0.1:{backend._port}/control",
                data=_json.dumps({"embed_fail": 0}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            # no tokenize-fail control plane: monkeypatch the HTTP layer for
            # /tokenize only — every tokenize POST raises
            real_urlopen = urllib.request.urlopen

            def refusing_urlopen(req, *a, **kw):
                if getattr(req, "full_url", "").endswith("/tokenize"):
                    raise urllib.error.HTTPError(
                        req.full_url, 500, "boom", {}, None  # type: ignore[arg-type]
                    )
                return real_urlopen(req, *a, **kw)

            monkeypatch.setattr(
                "goldmine.backends.llama.urllib.request.urlopen", refusing_urlopen
            )
            assert backend.token_pieces("anything") is None
            assert backend.count_tokens(["x"]) is None
            pipeline = IngestPipeline(cfg, store, backend)
            report = pipeline.run([str(root)])
        assert report.failed == 0
        assert report.content_indexed == 1
    finally:
        store.close()
