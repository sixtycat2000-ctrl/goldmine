#!/usr/bin/env python3
"""Local bench sliver (v2.0 scope 04 §7 / review C2; storage gate added 03 F2).

Gate 1 (offline, always runs): in-process scan+fusion p50 on 100k synthetic
fp16 rows must be < 100 ms. Measures the vectorized path end to end —
blockwise fp16->f32 convert + matmul (numpy fp16-native matmul is ~10x
slower, 04 §3.1), zscores, the vectorized per-file aggregation, and the
argpartition top-K candidate step — with NO db and NO server.

Gate 1b (offline, always runs; storage critique 03 F2): the same pipeline
but the rows come out of a REAL store database — fresh (uncached) Store ->
load_vectors() (SQL blob fetch + fp16->f32) -> matmul -> zscores -> per-file
aggregate — p50 < 150 ms at 100k vectors. The db-inclusive number exists
because gate 1 alone hid the real storage path (load_vectors measured
102-118 ms/query @49k vectors — 2-3x the whole gate-1 budget, invisible to
the old gate).

Gate 2 (behind GOLDMINE_LIVE=1): real-server embed rate on ~1800-char
chunks must be >= 14 texts/s (22% under the measured 17.9 texts/s, 04 §3.2
thermal-noise tolerance). Spawns the real llama-server via the configured
backend; skipped offline.

--gate exits 1 on any enabled-gate failure. All numbers are always printed.

Usage:
  python3 scripts/bench.py            # gates 1 + 1b (offline)
  GOLDMINE_LIVE=1 python3 scripts/bench.py --gate
  python3 scripts/bench.py --rows 500000 --runs 9 --json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from goldmine.search import Searcher  # noqa: E402

GATE1_ROWS_DEFAULT = 100_000
GATE1_BUDGET_MS = 100.0     # 04 §7: scan+fusion p50 @100k, generous vs the ~30ms design
GATE1B_BUDGET_MS = 150.0    # 03 F2: db-inclusive (cold load_vectors + scan) @100k vectors
GATE2_MIN_TPS = 14.0        # 04 §7: >= 14 texts/s (22% under measured 17.9)
GATE2_TEXTS = 64
BLOCK = 4096                # 04 §3.4: block size is insensitive 4096-16384


def _synthetic(rows: int, seed: int = 1234):
    """100k-style workload: fp16 vectors, ~8 segments per file, shuffled row
    order (rows come from SQLite ordered by segment id, files interleaved)."""
    rng = np.random.default_rng(seed)
    vecs = np.empty((rows, 768), dtype=np.float16)
    for start in range(0, rows, BLOCK):
        block = rng.standard_normal((min(BLOCK, rows - start), 768), dtype=np.float32)
        vecs[start:start + len(block)] = block.astype(np.float16)
    n_files = max(1, rows // 8)
    file_ids = np.arange(rows, dtype=np.int64) % n_files
    rng.shuffle(file_ids)  # exercise the stable groupby sort
    seg_ids = np.arange(rows, dtype=np.int64)
    mods = np.full(rows, "text", dtype=object)
    mods[:rows // 20] = "image"
    mods[rows // 20:rows // 10] = "audio"
    rng.shuffle(mods)
    q = rng.standard_normal(768).astype(np.float32)
    q /= float(np.linalg.norm(q))
    return vecs, file_ids, seg_ids, mods, q


def scan_fusion_once(vecs, file_ids, seg_ids, mods, q, top_k: int = 50):
    """One scan+fusion pass over the fp16 matrix (the vectorized search path,
    no store): blockwise f32 convert + matmul -> zscores -> per-file max ->
    argpartition top-K files by fused score."""
    n = vecs.shape[0]
    scores = np.empty(n, dtype=np.float32)
    for start in range(0, n, BLOCK):
        block = vecs[start:start + BLOCK].astype(np.float32)
        scores[start:start + BLOCK] = block @ q
    zvals = Searcher.zscores(scores, mods)
    _best, z_by_file = Searcher._aggregate_by_file(file_ids, zvals, scores, seg_ids)
    # candidate + top-K step on the small per-file arrays (argpartition AFTER
    # exact aggregation — row-level pre-trim is unsafe: one file's lesser rows
    # can monopolize the global top-K, see _aggregate_by_file's docstring)
    fids = np.fromiter(z_by_file.keys(), dtype=np.int64, count=len(z_by_file))
    zmax = np.fromiter(z_by_file.values(), dtype=np.float64, count=len(z_by_file))
    gated = zmax >= 2.0
    k = min(top_k, max(1, int(gated.sum())))
    top = np.argpartition(-zmax[gated], k - 1)[:k] if gated.any() else np.array([], dtype=int)
    return top, fids[gated][top] if gated.any() else fids[gated]


def bench_gate1(rows: int, runs: int) -> dict:
    vecs, file_ids, seg_ids, mods, q = _synthetic(rows)
    scan_fusion_once(vecs, file_ids, seg_ids, mods, q)  # warmup
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        top, _ = scan_fusion_once(vecs, file_ids, seg_ids, mods, q)
        times.append((time.perf_counter() - t0) * 1000.0)
    p50 = statistics.median(times)
    return {
        "rows": rows,
        "dim": 768,
        "runs": runs,
        "p50_ms": round(p50, 2),
        "min_ms": round(min(times), 2),
        "max_ms": round(max(times), 2),
        "top_k_files": int(len(top)),
        "budget_ms": GATE1_BUDGET_MS,
        "pass": p50 < GATE1_BUDGET_MS,
    }


def build_vector_db(rows: int, db_path: Path) -> None:
    """A real goldmine index holding `rows` vector segments (~rows/8 files,
    one vectorless name row each) — same distribution as `_synthetic`, written
    through the real schema. Rebuilt fresh on every call so the gate never
    measures a stale- or mixed-schema file."""
    from goldmine.store import Store

    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    store = Store(db_path)
    rng = np.random.default_rng(4321)
    n_files = max(1, rows // 8)
    mods = np.array(
        ["text"] * (rows - rows // 10) + ["image"] * (rows // 20) + ["audio"] * (rows // 20),
        dtype=object,
    )
    rng.shuffle(mods)
    try:
        with store.write_transaction():
            store.db.executemany(
                "INSERT INTO files(id, path, kind, size, state, capability, "
                "extractor_version, chunker_version, embed_id, indexed_at) "
                "VALUES(?, ?, 'doc', 1, 'done', 'content_indexed', 'txt-stdlib.v1', "
                "'chunk.v1', NULL, 0.0)",
                ((i, f"/bench/f{i % 97}/file{i}.txt") for i in range(n_files)),
            )
            def seg_rows():
                for start in range(0, rows, BLOCK):
                    block = rng.standard_normal((min(BLOCK, rows - start), 768), dtype=np.float32).astype(np.float16)
                    for offset, vec in enumerate(block):
                        i = start + offset
                        yield (n_files + i, i % n_files, "chunk", str(mods[i]),
                               float(i), None, None, vec.tobytes())
            store.db.executemany(
                "INSERT INTO segments(id, file, kind, modality, loc, loc_end, excerpt, vec) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                seg_rows(),
            )
    finally:
        store.close()


def bench_gate1b(rows: int, runs: int, db_path: Path | None = None) -> dict:
    """Gate 1b: the db-inclusive search path (03 F2). Every run opens a FRESH
    Store instance (the per-instance VectorTable cache is therefore cold) and
    times load_vectors() -> matmul -> zscores -> per-file aggregate. This is
    the number gate 1 used to hide: the SQL blob fetch + fp16->f32 conversion
    cost 2-3x the whole no-db gate-1 budget."""
    import tempfile

    from goldmine.store import Store

    if db_path is None:
        db_path = Path(tempfile.gettempdir()) / f"goldmine-bench-{rows}.sqlite"
    db_path = Path(db_path)
    build_vector_db(rows, db_path)
    _vecs, _file_ids, _seg_ids, _mods, q = _synthetic(rows, seed=99)
    times = []
    vectors = 0
    for _ in range(runs):
        store = Store(db_path)   # fresh instance: cold load_vectors, open not timed
        try:
            t0 = time.perf_counter()
            table = store.load_vectors()
            scores = table.matrix @ q
            zvals = Searcher.zscores(scores, np.asarray(table.modalities))
            best, z_by_file = Searcher._aggregate_by_file(
                np.asarray(table.file_ids, dtype=np.int64),
                zvals,
                scores,
                np.asarray(table.ids, dtype=np.int64),
            )
            times.append((time.perf_counter() - t0) * 1000.0)
            vectors = len(table.ids)
            del best, z_by_file
        finally:
            store.close()
    p50 = statistics.median(times)
    return {
        "rows": rows,
        "vectors_loaded": vectors,
        "runs": runs,
        "p50_ms": round(p50, 2),
        "min_ms": round(min(times), 2),
        "max_ms": round(max(times), 2),
        "db_bytes": db_path.stat().st_size,
        "budget_ms": GATE1B_BUDGET_MS,
        "pass": p50 < GATE1B_BUDGET_MS,
    }


def bench_gate2() -> dict:
    from goldmine.backends import create_backend
    from goldmine.config import Config

    cfg = Config()
    backend = create_backend(cfg, "llama")
    try:
        text = ("chunk of prose words " * 100)[:1800]
        texts = [f"{i} {text}" for i in range(GATE2_TEXTS)]
        backend.embed_texts(texts[:4])  # warmup (model load, page cache)
        t0 = time.perf_counter()
        backend.embed_texts(texts)
        elapsed = time.perf_counter() - t0
        tps = len(texts) / elapsed
        return {
            "texts": len(texts),
            "chars_each": len(text),
            "elapsed_s": round(elapsed, 2),
            "texts_per_s": round(tps, 2),
            "min_texts_per_s": GATE2_MIN_TPS,
            "pass": tps >= GATE2_MIN_TPS,
        }
    finally:
        backend.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="goldmine local bench — two-gate sliver (04 §7)",
    )
    parser.add_argument("--rows", type=int, default=GATE1_ROWS_DEFAULT)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--gate", action="store_true",
                        help="exit 1 on any enabled gate failure")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    live = bool(os.environ.get("GOLDMINE_LIVE"))
    gate1 = bench_gate1(args.rows, args.runs)
    gate1b = bench_gate1b(args.rows, args.runs)
    gate2 = None
    if live:
        gate2 = bench_gate2()

    if args.json:
        print(json.dumps({"gate1": gate1, "gate1b": gate1b, "gate2": gate2}, indent=1))
    else:
        print(
            f"gate 1 (offline): scan+fusion p50 {gate1['p50_ms']:.1f} ms @ "
            f"{gate1['rows']} fp16 rows over {gate1['runs']} runs "
            f"(budget < {GATE1_BUDGET_MS:.0f} ms) -> "
            f"{'PASS' if gate1['pass'] else 'FAIL'}"
        )
        print(
            f"gate 1b (offline): cold load_vectors + scan p50 {gate1b['p50_ms']:.1f} ms @ "
            f"{gate1b['vectors_loaded']} db vectors over {gate1b['runs']} runs "
            f"(budget < {GATE1B_BUDGET_MS:.0f} ms) -> "
            f"{'PASS' if gate1b['pass'] else 'FAIL'}"
        )
        if gate2 is None:
            print(f"gate 2 (GOLDMINE_LIVE): skipped — offline")
        else:
            print(
                f"gate 2 (live): embed rate {gate2['texts_per_s']:.1f} texts/s on "
                f"{gate2['texts']} ~1800-char chunks "
                f"(floor >= {GATE2_MIN_TPS:.0f} texts/s) -> "
                f"{'PASS' if gate2['pass'] else 'FAIL'}"
            )

    if args.gate:
        failed = [
            n for n, r in (("gate1", gate1), ("gate1b", gate1b), ("gate2", gate2))
            if r and not r["pass"]
        ]
        if failed:
            print(f"BENCH GATE FAILED: {', '.join(failed)}", file=sys.stderr)
            return 1
        print("bench gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
