"""Tier-3 LIVE: pack emit on the REAL eval index (docs/v2.1-pack-eval.md §4.7-8).

Requires GOLDMINE_LIVE=1, the vendor llama-server, the pinned model, and the
built eval index (evals/eval.sqlite, corpus v3). Skips (never fails) when a
prerequisite is absent. Canaries: byte-identical double emit, count_tokens
recount <= budget for every committed question, the ans-001/011 fact strings,
and the neighbor SQL's segments_file index usage.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from goldmine.backends.llama import VENDOR_SERVER
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
DB = REPO_ROOT / "evals" / "eval.sqlite"

_skip_binary = pytest.mark.skipif(
    not VENDOR_SERVER.is_file(),
    reason=f"vendor llama-server not built at {VENDOR_SERVER}",
)
_skip_model = pytest.mark.skipif(
    not MODEL.is_file(), reason="model GGUF missing (see models/SHA256SUMS)"
)
_skip_db = pytest.mark.skipif(
    not DB.is_file(), reason="eval index missing — run scripts/build_eval_corpus.py"
)


def _backend():
    from goldmine.backends import create_backend
    from goldmine.config import Config

    cfg = Config(home=str(DB.parent / ".evalhome"))
    cfg.backend = "llama"
    return create_backend(cfg, "llama")


def _settings():
    sys_path = str(REPO_ROOT / "scripts")
    if sys_path not in __import__("sys").path:
        __import__("sys").path.insert(0, sys_path)
    import eval_answer as ea  # noqa: E402  (scripts import after sys.path)

    return ea


@pytest.mark.skipif(
    not (VENDOR_SERVER.is_file() and MODEL.is_file() and DB.is_file()),
    reason="live prerequisites missing",
)
def test_pack_emit_live_canaries():
    """§4.7: double emit byte-identical; count_tokens(final) <= 4000 for all
    20 committed questions; the ans-001/ans-011 fact strings present."""
    ea = _settings()
    store, backend = None, None
    try:
        store, backend = ea.open_index(DB, "llama")
        qs = ea.load_questions()
        first = {}
        for q in qs:
            text, meta, _ = ea.build_question_pack(
                store, backend, q, ea.PACK_SETTINGS)
            if q["id"] in first:
                assert text == first[q["id"]], f"{q['id']}: double emit differs"
            else:
                first[q["id"]] = text
            assert meta["used_tokens"] <= ea.PACK_SETTINGS["budget"], q["id"]
            assert meta["token_mode"] == "tokenize"
        assert "DEFAULT_TOKEN_OVERLAP = 48" in first["ans-001"]
        assert "reached 2670 tokens against the 2048" in first["ans-011"]
    finally:
        if store is not None:
            store.close()
        if backend is not None:
            backend.close()


def test_neighbor_sql_uses_segments_file_index():
    """§4.8: the neighbor candidate SQL is served by segments_file (EXPLAIN
    QUERY PLAN) — offline, no backend needed."""
    if not DB.is_file():
        pytest.skip("eval index missing — run scripts/build_eval_corpus.py")
    from goldmine.pack import NeighborIndex

    store = Store(DB)
    try:
        fid = store.db.execute("SELECT id FROM files LIMIT 1").fetchone()[0]
        sql = NeighborIndex._SQL
        plan = " ".join(
            str(r[-1]) for r in store.db.execute(
                "EXPLAIN QUERY PLAN " + sql, (fid,)).fetchall())
        assert "segments_file" in plan
    finally:
        store.close()
