"""Regression tests for the per-request context guard.

Backstory: llama.cpp b11461 treats `-c` as the TOTAL KV cache across `-np`
slots, so the default spawn (ctx 2048, batch 16) left each request 256 tokens
and real 1800-char chunks died with HTTP 400 exceed_context, aborting whole
ingests. Three guards now exist, tested here:
  1. _spawn sizes -c as ctx * batch (per-slot context == cfg.ctx)
  2. chunker.effective_chunk_size caps chunk chars to the backend ceiling
  3. an EmbeddingError mid-ingest fails THAT file only; the run continues
"""

from __future__ import annotations

import stat
from pathlib import Path

import numpy as np
import pytest

from goldmine.backends.llama import LlamaServerBackend
from goldmine.backends.mock import MockBackend
from goldmine.chunker import DEFAULT_SIZE, chunk_text, effective_chunk_size
from goldmine.config import LlamaCfg
from goldmine.ingest import IngestPipeline
from goldmine.store import Store

FAKE_SERVER = Path(__file__).resolve().parent / "fake_llama_server.py"


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
        request_timeout=15.0,
    )


def test_spawn_sizes_context_for_all_slots(fake_cfg, monkeypatch, tmp_path):
    """-np is a FIXED 2 (04 §6.2: beyond 2 buys nothing measured, costs KV
    memory) and -c/-b must be ctx * slots so each slot sees the full
    configured ctx. P3-15: also pin the PHYSICAL batch flags — n_batch (-b)
    must cover the whole input (the server clamps it down to n_ubatch) and
    n_ubatch (-ub) must cover one full input (ctx), or mean pooling breaks."""
    import goldmine.backends.llama as llama_mod

    captured: dict = {}

    class FakeProc:
        pid = -1
        def __init__(self, argv, **kw):
            captured["argv"] = argv

    monkeypatch.setattr(llama_mod.subprocess, "Popen", FakeProc)
    fake_cfg.batch = 16  # a LARGE client batch must still never map onto -np
    backend = LlamaServerBackend(fake_cfg)
    backend._spawn(18123)
    argv = captured["argv"]
    i = argv.index("-c")
    assert argv[i + 1] == str(fake_cfg.ctx * llama_mod.SERVER_SLOTS)
    j = argv.index("-np")
    assert argv[j + 1] == "2"
    assert argv[j + 1] != str(fake_cfg.batch)  # client batch never maps to -np
    b = argv.index("-b")
    assert argv[b + 1] == str(fake_cfg.ctx * llama_mod.SERVER_SLOTS)
    ub = argv.index("-ub")
    assert argv[ub + 1] == str(fake_cfg.ctx)


def test_effective_chunk_size_ceiling():
    assert effective_chunk_size(None) == DEFAULT_SIZE          # unknown -> default
    assert effective_chunk_size(2048) == DEFAULT_SIZE          # generous -> default
    assert effective_chunk_size(512) == 512 - 96               # framing+slack reserved
    assert effective_chunk_size(200) == 256                    # floor
    assert effective_chunk_size(0) == 256


def test_chunker_respects_small_size():
    text = " ".join(f"word{i}" for i in range(2000))
    chunks = chunk_text(text, size=416)
    assert chunks, "no chunks produced"
    assert max(len(c.text) for c in chunks) <= 416 + 1  # +1 boundary slack


def test_embedding_error_fails_file_and_run_continues(fake_cfg, tmp_path):
    """A 400-rejected input fails its OWN file only; every other file in the
    batch wave still indexes with real vectors (per-input 400 isolation at
    file granularity — v2 falsifiable claim 2's isolation half; full
    vectorless-with-detail isolation rides D3's /tokenize counting, C6)."""
    import sys

    # The backend spawns cfg.server itself, so smuggle the rejection flag in
    # via a wrapper script: the spawned server 400s any input containing the
    # marker, exactly like a real ctx-overflow response.
    wrapper = tmp_path / "rejecting-server.sh"
    wrapper.write_text(
        f'#!/bin/sh\nexec {sys.executable} {FAKE_SERVER} '
        f'--reject-substr CTXOVERLOAD "$@"\n'
    )
    wrapper.chmod(0o755)

    from goldmine.config import Config

    cfg = LlamaCfg(
        server=wrapper, server_configured=True,
        model=_fake_model(tmp_path), ctx=512, batch=2,
        startup_timeout=10.0, request_timeout=15.0,
    )
    c = Config()
    c.home = tmp_path / "gmhome"
    c.backend = "llama"
    c.llama = cfg

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "poison.md").write_text(
        "CTXOVERLOAD reject me please " * 120 + "\n", encoding="utf-8"
    )
    (corpus / "healthy.md").write_text(
        "# healthy\n\nthe quick brown fox jumps over the lazy dog " * 8,
        encoding="utf-8",
    )
    # a second healthy file processed in the SAME extraction wave: an
    # oversized input in one file's batch must not leak into its neighbors
    (corpus / "healthy2.md").write_text(
        "# healthy two\n\npack my box with five dozen liquor jugs " * 8,
        encoding="utf-8",
    )

    store = Store(c.home / "index.sqlite")
    from goldmine.backends.llama import LlamaServerBackend as L

    with L(cfg) as backend:
        pipeline = IngestPipeline(c, store, backend)
        report = pipeline.run([str(corpus)])

    caps = {
        row[0]: (row[1], row[2])
        for row in store.db.execute(
            "SELECT path, capability, detail FROM files").fetchall()
    }
    poisoned = [v for k, v in caps.items() if k.endswith("poison.md")]
    healthy = [v for k, v in caps.items() if k.endswith("healthy.md")]
    healthy2 = [v for k, v in caps.items() if k.endswith("healthy2.md")]
    assert poisoned and poisoned[0][0] == "failed", caps
    assert "embedding failed" in (poisoned[0][1] or "")
    assert healthy and healthy[0][0] == "content_indexed", caps
    assert healthy2 and healthy2[0][0] == "content_indexed", caps
    assert report.failed == 1 and report.content_indexed == 2
    assert not report.cancelled, "the run must complete, not abort"
    # healthy files have real vectors in the store
    n = store.db.execute(
        "SELECT COUNT(*) FROM segments WHERE modality='text' AND vec IS NOT NULL"
    ).fetchone()[0]
    assert n >= 2


# ------------------------------------------- guard skip for token-true chunks


def _corpus_of_n(tmp_path: Path, n: int) -> Path:
    root = tmp_path / "corpus50"
    root.mkdir()
    for i in range(n):
        (root / f"doc{i:03d}.md").write_text(
            f"# doc {i}\n\nthe quick brown fox jumps over the lazy dog\n" * 60,
            encoding="utf-8",
        )
    return root


class _GuardCountingBackend(MockBackend):
    """Text-capable mock with a hard ceiling, exact pieces (token-true
    chunking), and a TALLIED count_tokens — the offline proxy for the guard's
    per-chunk /tokenize round trip (06-perf F2: 250 of 300 tokenize calls per
    50-file ingest were the ctx guard, +0.43 s on the critical path)."""

    def __init__(self, ceiling: int, *, pieces: bool = True):
        super().__init__(dim=768)
        self.ceiling = ceiling
        self._pieces = pieces
        self.count_calls = 0
        self.counted_texts = 0

    def max_input_tokens(self) -> int | None:
        return self.ceiling

    def count_tokens(self, texts):
        self.count_calls += 1
        self.counted_texts += len(texts)
        return [len(t) // 4 for t in texts]

    def token_pieces(self, text):
        from test_token_chunking import spm_like

        return spm_like(text) if self._pieces else None


def test_token_true_chunks_skip_the_ctx_guard(tmp_path):
    """Token-true chunks cannot exceed the ceiling by construction (budget +
    framing(40) + slack(24) <= ceiling), so the guard must not re-count them:
    a 50-file ingest makes ZERO count_tokens calls (was ~250 = one per
    chunk). This is the deterministic offline gate for the +0.43 s -> ~free
    timing change; each avoided call is one avoided /tokenize round trip."""
    import time

    from goldmine.config import load

    root = _corpus_of_n(tmp_path, 50)
    cfg = load()
    cfg.home = tmp_path / "home"
    store = Store(cfg.home / "idx.sqlite")
    backend = _GuardCountingBackend(ceiling=512, pieces=True)
    try:
        with backend:
            t0 = time.perf_counter()
            report = IngestPipeline(cfg, store, backend).run([str(root)])
            elapsed = time.perf_counter() - t0
        assert report.failed == 0 and report.content_indexed == 50
        assert backend.count_calls == 0, (
            f"guard re-counted token-true chunks "
            f"({backend.counted_texts} texts, {elapsed:.2f}s wall)"
        )
        assert backend.counted_texts == 0
        # every chunk really was token-true (the skip was justified)
        from goldmine.chunker import DEFAULT_TOKEN_BUDGET, effective_token_budget

        budget = effective_token_budget(512)
        assert budget <= DEFAULT_TOKEN_BUDGET
        segs = store.db.execute(
            "SELECT COUNT(*) FROM segments WHERE kind='chunk' AND vec IS NOT NULL"
        ).fetchone()[0]
        assert segs > 50  # several chunks per file, all embedded
    finally:
        store.close()


def test_estimator_chunks_are_still_guarded(tmp_path):
    """The other half of the invariant: chunks WITHOUT a token-true count
    (estimator path — no /tokenize pieces) keep the per-chunk guard."""
    from goldmine.config import load

    root = _corpus_of_n(tmp_path, 3)
    cfg = load()
    cfg.home = tmp_path / "home"
    store = Store(cfg.home / "idx.sqlite")
    backend = _GuardCountingBackend(ceiling=512, pieces=False)
    try:
        with backend:
            report = IngestPipeline(cfg, store, backend).run([str(root)])
        assert report.failed == 0 and report.content_indexed == 3
        assert backend.count_calls >= 1
        assert backend.counted_texts >= 3  # >= one guarded chunk per file
    finally:
        store.close()


def test_tiny_ceiling_guards_even_token_true_chunks(tmp_path):
    """Below ~128 tokens the 64-token budget floor exceeds the true allowance
    (budget + framing + slack > ceiling), so the skip invariant does NOT hold
    and the guard counts every chunk — never a 400 exceed_context."""
    from goldmine.config import load

    root = _corpus_of_n(tmp_path, 2)
    cfg = load()
    cfg.home = tmp_path / "home"
    store = Store(cfg.home / "idx.sqlite")
    backend = _GuardCountingBackend(ceiling=100, pieces=True)
    try:
        with backend:
            report = IngestPipeline(cfg, store, backend).run([str(root)])
        assert report.failed == 0 and report.content_indexed == 2
        assert backend.count_calls >= 1, (
            "tiny ceiling: the budget floor breaks the invariant — must guard"
        )
    finally:
        store.close()
