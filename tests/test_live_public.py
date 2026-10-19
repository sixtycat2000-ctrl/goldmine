"""R5 LIVE: the real scifact exam end-to-end (GOLDMINE_LIVE + network).

Requires GOLDMINE_LIVE=1, network (real BEIR download from the UKP mirror),
and the local llama-server + pinned model for the hybrid arm — the same
prerequisites as test_live_llama.py. Skips (never fails) when a prerequisite
is absent, with the doctor-style hint.

1. test_live_scifact_end_to_end: download (sha verified) → ingest 5,183 docs
   (real llama; wall time recorded) → hybrid + lexical arms over the 300
   test queries → the neutral comparison table's first row with real numbers.
2. test_live_scifact_double_run_determinism (review F8): the scifact run
   executes TWICE and the 4-dp metrics must be IDENTICAL — determinism is
   measured here, never assumed.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import eval_public as ep  # noqa: E402
import eval_real as er  # noqa: E402
from goldmine.backends.base import BackendStartupError  # noqa: E402
from goldmine.backends.llama import VENDOR_SERVER, LlamaServerBackend  # noqa: E402
from goldmine.config import Config  # noqa: E402
from goldmine.store import Store  # noqa: E402

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("GOLDMINE_LIVE"),
        reason="GOLDMINE_LIVE=1 required for live public-suite tests",
    ),
]

MODEL = REPO_ROOT / "models" / "embeddinggemma-2-Q8_0.gguf"

_skip_binary = pytest.mark.skipif(
    not VENDOR_SERVER.is_file(),
    reason=(f"vendor llama-server not built at {VENDOR_SERVER} "
            "(brew llama.cpp cannot load gemma-embedding2)"),
)
_skip_model = pytest.mark.skipif(
    not MODEL.is_file(), reason=f"model GGUF missing at {MODEL}"
)


def _network_up() -> bool:
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/",
                method="HEAD"), timeout=10.0):
            return True
    except OSError:
        return False


#: network probe runs ONLY when the LIVE gate is already open (offline pytest
#: imports never touch the network). skipif takes a bool, not a callable — a
#: lambda would be truthy and always skip (caught on the first live run).
_skip_network = pytest.mark.skipif(
    bool(os.environ.get("GOLDMINE_LIVE")) and not _network_up(),
    reason="BEIR mirror unreachable",
)


def _live_backend(tmp_path: Path) -> LlamaServerBackend:
    """A started llama-server backend, or skip with the doctor-style hint."""
    cfg = Config(home=str(tmp_path / "home"))
    cfg.llama.startup_timeout = 180.0  # cold 310 MB load on the mini
    backend = LlamaServerBackend(cfg.llama)
    try:
        backend.startup()
    except BackendStartupError as exc:
        pytest.skip(f"llama-server cannot start: {exc}")
    return backend


def _prepare(root: Path, name: str, manifest: dict) -> tuple[dict, dict]:
    """ensure_zip (sha verified) → prepare (llama ingest); returns (entry, prep)."""
    entry = manifest["datasets"][name]
    zp = ep.Fetcher(root).ensure_zip(name, entry, allow_download=True,
                                     force_refresh=False)
    assert (ep.sha256_and_size(zp)
            == (entry["sha256"], entry["bytes"])), "zip pin failed LIVE"
    prep = ep.prepare_dataset(name, entry, root, backend_name="llama")
    return entry, prep


def _run_arms(root: Path, name: str, entry: dict, backend) -> dict:
    store = Store(ep.dataset_dir(root, name) / "index.sqlite")
    try:
        return ep.run_dataset_arms(store, backend, name, entry,
                                   ep.dataset_dir(root, name),
                                   ["hybrid", "lexical"])
    finally:
        store.close()


def _live_root(tmp_path: Path) -> Path:
    """The cache root: GOLDMINE_PUBLIC_DATA when set (so repeated live runs
    reuse the downloaded zip + llama index — the prepare marker is keyed by
    zip sha + backend), else an isolated tmp root."""
    env = os.environ.get("GOLDMINE_PUBLIC_DATA")
    root = Path(env).expanduser() if env else tmp_path / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root


@_skip_network
@_skip_binary
@_skip_model
def test_live_scifact_end_to_end(tmp_path):
    """Download (sha verified) → ingest 5,183 docs (real llama) → hybrid run
    over the 300 test queries → first row of the comparison table, real."""
    manifest = ep.load_manifest()
    name = "scifact"
    root = _live_root(tmp_path)
    backend = _live_backend(tmp_path)
    try:
        t0 = time.monotonic()
        entry, prep = _prepare(root, name, manifest)
        ingest_s = time.monotonic() - t0
        t1 = time.monotonic()
        out = _run_arms(root, name, entry, backend)
        search_s = time.monotonic() - t1
    finally:
        backend.close()
    total_s = time.monotonic() - t0

    assert prep["n_docs"] == 5183
    assert prep["report"]["content_indexed"] == 5183
    assert out["n_queries"] == 300
    arms = {arm: er.summarize(rows) for arm, rows in out["rows_by_arm"].items()}
    ndcg_lex = arms["lexical"]["nDCG@10"]["value"]
    ndcg_hyb = arms["hybrid"]["nDCG@10"]["value"]
    # sanity window, NOT a gate (spec §9): our BM25 vs the published 0.665
    assert 0.3 <= ndcg_lex <= 0.8, f"lexical nDCG@10 {ndcg_lex} outside sanity band"
    assert 0.0 <= ndcg_hyb <= 1.0
    # the comparison table with real numbers (first suite row)
    results = {name: {"n_queries": out["n_queries"], "n_corpus": 5183,
                      "wall_search_s": search_s,
                      "arms": {arm: {"metrics": m,
                                     "cis": ep.headline_cis_public(out["rows_by_arm"][arm])}
                               for arm, m in arms.items()}}}
    table = ep.render_table(results, manifest, backend="llama")
    print(f"\n[live] ingest 5,183 docs: {ingest_s:.0f}s; "
          f"search 300 queries x 2 arms: {search_s:.0f}s; total {total_s:.0f}s")
    print(table)
    assert "paper 0.6650" in table


@_skip_network
@_skip_binary
@_skip_model
def test_live_scifact_double_run_determinism(tmp_path):
    """Review F8: determinism is MEASURED — two full passes of the scifact
    exam (same index, fresh search) must produce identical 4-dp metrics."""
    manifest = ep.load_manifest()
    name = "scifact"
    root = _live_root(tmp_path)
    backend = _live_backend(tmp_path)
    runs = []
    try:
        entry, _prep = _prepare(root, name, manifest)
        for _ in range(2):
            out = _run_arms(root, name, entry, backend)
            runs.append({arm: er.summarize(rows)
                         for arm, rows in out["rows_by_arm"].items()})
    finally:
        backend.close()
    a, b = runs
    for arm in a:
        for metric in ("success@5", "success@1", "MRR", "nDCG@10"):
            va, vb = a[arm][metric]["value"], b[arm][metric]["value"]
            assert round(va, 4) == round(vb, 4), (
                f"nondeterministic {arm}/{metric}: {va} vs {vb}")
    print("\n[live] determinism: two full scifact runs identical at 4 dp —",
          {arm: round(a[arm]["nDCG@10"]["value"], 4) for arm in a})
