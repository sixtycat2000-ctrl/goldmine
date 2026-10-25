"""Tier-3 LIVE: real llama-server spawn + real EmbeddingGemma 2 embeddings.

Requires GOLDMINE_LIVE=1 AND the locally built llama-server
(vendor/llama.cpp/build/bin/llama-server, tag b11461 — the brew build cannot
load the gemma-embedding2 arch) AND models/embeddinggemma-2-Q8_0.gguf.
Skips (never fails) when a prerequisite is absent, with the doctor-style hint.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from goldmine.backends.base import BackendStartupError
from goldmine.backends.llama import VENDOR_SERVER, LlamaServerBackend
from goldmine.config import Config, LlamaCfg
from goldmine.ingest import IngestPipeline
from goldmine.search import Searcher
from goldmine.store import Store

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("GOLDMINE_LIVE"),
        reason="GOLDMINE_LIVE=1 required for live llama tests",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL = REPO_ROOT / "models" / "embeddinggemma-2-Q8_0.gguf"

_skip_binary = pytest.mark.skipif(
    not VENDOR_SERVER.is_file(),
    reason=(
        f"vendor llama-server not built at {VENDOR_SERVER} "
        "(brew llama.cpp cannot load gemma-embedding2)"
    ),
)
_skip_model = pytest.mark.skipif(
    not MODEL.is_file(), reason=f"model GGUF missing at {MODEL} (see models/SHA256SUMS)"
)


def _cfg(tmp_path: Path) -> LlamaCfg:
    return LlamaCfg(
        server=VENDOR_SERVER,
        model=MODEL,
        ctx=2048,
        batch=4,
        startup_timeout=180.0,  # cold 310 MB load on the mini
        request_timeout=60.0,
    )


@_skip_binary
@_skip_model
def test_live_lexical_backfill_preserves_text_and_matches_document_embedding(tmp_path):
    from goldmine.backends.base import document_prompt
    from goldmine.repair import execute_repairs, plan_repairs
    from goldmine.util import pack_fp16

    root = tmp_path / "corpus"
    root.mkdir()
    body = "中文知识库：退款期限为四十五天。 Keep the original receipt."
    (root / "note.txt").write_text(body)
    cfg = Config(home=tmp_path / "home")
    cfg.llama = _cfg(tmp_path)
    store = Store(tmp_path / "backfill.sqlite")
    backend = LlamaServerBackend(cfg.llama)
    try:
        IngestPipeline(cfg, store, None, lexical_only=True).run([str(root)])
        before = store.db.execute("SELECT rowid,body,original FROM fts ORDER BY rowid").fetchall()
        plan = plan_repairs(store, cfg, backend, missing_vectors=True)
        assert len(plan) == 1 and plan[0]["action"] == "backfill"
        result = execute_repairs(store, cfg, backend, plan)
        assert result["vectors_added"] == 1 and not result["failures"]
        assert store.db.execute("SELECT rowid,body,original FROM fts ORDER BY rowid").fetchall() == before
        expected = backend.embed_texts([document_prompt("note", body)])[0]
        assert store.db.execute("SELECT vec FROM segments WHERE kind='chunk'").fetchone()[0] == pack_fp16(expected)
        assert plan_repairs(store, cfg, backend, missing_vectors=True) == []
    finally:
        backend.close()
        store.close()


@_skip_binary
@_skip_model
def test_live_spawn_embed_dim_and_build_recorded(tmp_path):
    with LlamaServerBackend(_cfg(tmp_path)) as backend:
        vectors = backend.embed_texts(["kubernetes pod eviction", "sourdough starter"])
        assert vectors.shape == (2, 768)
        norms = np.linalg.norm(vectors, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-3)  # L2-normalized
        assert backend.build_string  # e.g. "version: 0.5.x (build ...)"
        assert "build" in backend.build_string or "version" in backend.build_string
        assert backend.server_source in ("vendor", "env/config")
        fingerprint = backend.fingerprint_fields()
        assert fingerprint["model_sha256"] != "missing"
        assert fingerprint["normalization"] == "l2_post"


@_skip_binary
@_skip_model
def test_live_prompt_asymmetry(tmp_path):
    """query-prompt vector differs from the raw-text vector (TEST_PLAN §5)."""
    from goldmine.backends.base import query_prompt

    with LlamaServerBackend(_cfg(tmp_path)) as backend:
        raw = backend.embed_texts(["kubernetes pod eviction"])[0]
        framed = backend.embed_texts([query_prompt("kubernetes pod eviction")])[0]
        cosine = float(raw @ framed)
        assert cosine < 0.999  # prompts are part of the model's task space


@_skip_binary
@_skip_model
def test_live_fingerprint_stable_across_two_spawns(tmp_path):
    from goldmine import fingerprints

    fp_ids = []
    for _ in range(2):
        with LlamaServerBackend(_cfg(tmp_path)) as backend:
            fp_ids.append(fingerprints.fingerprint_id(fingerprints.build(backend)))
    assert fp_ids[0] == fp_ids[1]


@_skip_binary
@_skip_model
def test_live_ingest_and_hybrid_search_top1(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "k8s.txt").write_text(
        "The scheduler evicts a pod when a node is under pressure and the pod "
        "has no toleration. Kubelet reports eviction events. " * 4, encoding="utf-8"
    )
    (corpus / "food.txt").write_text(
        "Feed the sourdough starter daily and bulk ferment the dough overnight "
        "before shaping. " * 4, encoding="utf-8"
    )
    cfg = Config(home=str(tmp_path / "home"))
    cfg.llama = _cfg(tmp_path)
    store = Store(tmp_path / "home" / "index.sqlite")
    try:
        backend = LlamaServerBackend(cfg.llama)
        report = IngestPipeline(cfg, store, backend).run([str(corpus)])
        assert report.content_indexed == 2
        assert store.meta("llama_build")

        search_backend = LlamaServerBackend(cfg.llama)
        try:
            searcher = Searcher(store, search_backend)
            hits = searcher.search("kubernetes pod eviction pressure", limit=3)
        finally:
            search_backend.close()
        assert hits, "live hybrid search returned nothing"
        assert hits[0].path.endswith("k8s.txt"), (
            f"expected k8s.txt top-1, got {[h.path for h in hits]}"
        )
    finally:
        store.close()


@_skip_binary
@_skip_model
def test_live_close_leaves_no_child(tmp_path):
    backend = LlamaServerBackend(_cfg(tmp_path))
    backend.startup()
    child = backend._proc
    assert child is not None and child.poll() is None
    backend.close()
    assert child.poll() is not None  # reaped: no orphan llama-server left


@_skip_binary
@_skip_model
def test_live_brew_binary_refused_with_explanation(tmp_path):
    """If the brew binary exists it must be refused for the pinned model (the
    brew build cannot load arch gemma-embedding2)."""
    from goldmine.backends.llama import BREW_SERVER, resolve_server_path

    if not BREW_SERVER.is_file():
        pytest.skip("brew llama-server not installed")
    cfg = _cfg(tmp_path)
    cfg.server = BREW_SERVER
    cfg.server_configured = True  # explicit brew path must be honored as-is
    resolved, source = resolve_server_path(cfg)
    if resolved != BREW_SERVER:
        pytest.skip(f"explicit brew path not honored (resolved {resolved})")
    backend = LlamaServerBackend(cfg)
    # the brew build either exits with "unknown model architecture:
    # 'gemma-embedding2'" while loading the pinned model, or its --version
    # probe hangs/flakes (observed nondeterministic Metal init hang in 0.5.0).
    # Either way startup must refuse loudly — the brew build cannot serve this
    # model, and the error says which failure fired.
    with pytest.raises(BackendStartupError) as excinfo:
        backend.startup()
    tail = str(excinfo.value) + getattr(excinfo.value, "tail", "")
    assert (
        "gemma-embedding2" in tail
        or "failed to start" in tail
        or "--version" in tail
    )
