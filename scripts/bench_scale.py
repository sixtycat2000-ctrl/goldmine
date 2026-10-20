#!/usr/bin/env python3
"""Isolated SQLite/vector scale measurements; generation never shares search RSS.

Synthetic numbers measure capacity, not retrieval quality. Reference and candidate
workers import their explicitly selected source tree; the same frozen SQLite
fixture is opened read-only by both. No system cache or unrelated process is reset.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gc
import hashlib
import json
import math
from pathlib import Path
import platform
import resource
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
DIM = 768
SEED = 20261021
BLOCK = 4096
SCALES = (10_000, 100_000, 250_000, 500_000)
DISTRIBUTIONS = (1, 10, 100)


def rss() -> dict:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {"raw": raw, "raw_unit": "bytes" if sys.platform == "darwin" else "KiB",
            "bytes": int(raw if sys.platform == "darwin" else raw * 1024)}


def quantile(values, fraction):
    if not values or not 0 < fraction <= 1:
        raise ValueError("nonempty samples and fraction in (0,1] required")
    if any(not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("finite nonnegative samples required")
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def file_sha(file: Path) -> str:
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build_fixture(db: Path, rows: int, per_file: int) -> dict:
    import numpy as np
    from goldmine.store import Store

    if rows <= 0 or per_file <= 0:
        raise ValueError("rows and per-file must be positive")
    if db.exists():
        raise ValueError("refuse to replace an existing fixture")
    db.parent.mkdir(parents=True, exist_ok=True)
    store = Store(db)
    rng = np.random.default_rng(SEED)
    logical = hashlib.sha256()
    try:
        n_files = math.ceil(rows / per_file)
        scope = store.write_transaction() if hasattr(store, "write_transaction") else store.db
        with scope:
            store.db.executemany(
                "INSERT INTO files(id,path,kind,size,state,capability,sha256) "
                "VALUES(?,?,'doc',1,'done','content_indexed',?)",
                ((i + 1, f"/goldmine-scale/{i + 1}.txt", hashlib.sha256(str(i).encode()).hexdigest())
                 for i in range(n_files)))
            for start in range(0, rows, BLOCK):
                vectors = rng.standard_normal((min(BLOCK, rows - start), DIM), dtype=np.float32)
                vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
                vectors = vectors.astype("<f2")
                data = []
                for offset, vec in enumerate(vectors):
                    idx = start + offset
                    # Interleave files, including the heavy-file distribution.
                    fid = idx % n_files + 1
                    modality = "image" if idx % 20 == 0 else "audio" if idx % 20 == 1 else "text"
                    blob = vec.tobytes()
                    logical.update(f"{idx + 1}:{fid}:{modality}:".encode())
                    logical.update(blob)
                    data.append((idx + 1, fid, modality, float(idx + 1), blob))
                store.db.executemany("INSERT INTO segments(id,file,kind,modality,loc,loc_end,vec) "
                                     "VALUES(?,?,'chunk',?,?,NULL,?)", data)
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        store.close()
    return {"schema": 1, "kind": "synthetic-capacity/v1", "seed": SEED, "dim": DIM,
            "rows": rows, "segments_per_file": per_file, "files": n_files,
            "logical_sha256": logical.hexdigest(), "sqlite_sha256": file_sha(db),
            "db_bytes": db.stat().st_size, "fixture_peak_rss": rss(),
            "generation_block_rows": BLOCK, "generation_dtype": "float32", "stored_dtype": "<f2"}


def measure(db: Path, runs: int, *, stream=False) -> dict:
    import numpy as np
    from goldmine.search import Searcher
    from goldmine.store import Store

    if runs < 10:
        raise ValueError("scale measurements require at least 10 samples")
    rng = np.random.default_rng(SEED + 1)
    queries = rng.standard_normal((runs + 3, DIM), dtype=np.float32)
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)
    phases = {key: [] for key in ("open", "cold_load", "hot_cache", "cosine", "zscore",
                                  "aggregate", "admission_sort", "details")}
    predictions = []
    for sample in range(runs + 3):
        if stream and sys.stdin.readline().strip() != str(sample):
            raise ValueError("invalid interleaved sample handshake")
        gc.collect()
        started = time.perf_counter()
        # Old reference Store has no ro keyword. Never call its RW constructor.
        store = Store.__new__(Store)
        import sqlite3
        store.path = db
        store.ro = True
        store.db = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        store.db.execute("PRAGMA query_only=ON")
        store._vec_cache = None
        store._vec_cache_key = None
        store._vec_generation = 0
        opened = time.perf_counter()
        store.db.execute("BEGIN")
        table = store.load_vectors()
        loaded = time.perf_counter()
        cached = store.load_vectors()
        hot = time.perf_counter()
        scores = table.matrix @ queries[sample]
        cosine = time.perf_counter()
        zvals = Searcher.zscores(scores, np.asarray(table.modalities))
        z_done = time.perf_counter()
        best, maxima = Searcher._aggregate_by_file(np.asarray(table.file_ids, dtype=np.int64),
                        zvals, scores, np.asarray(table.ids, dtype=np.int64))
        aggregated = time.perf_counter()
        # Stable file ID ties, no segment-level pre-trimming.
        ranked = sorted(((int(fid), float(z)) for fid, z in maxima.items() if z >= 3),
                        key=lambda pair: (-pair[1], pair[0]))[:50]
        sorted_at = time.perf_counter()
        ids = [int(best[fid][2]) for fid, _ in ranked]
        if ids:
            marks = ",".join("?" for _ in ids)
            store.db.execute(f"SELECT id,file,loc,loc_end FROM segments WHERE id IN ({marks})", ids).fetchall()
        detailed = time.perf_counter()
        assert cached is table
        if sample >= 3:
            stamps = [started, opened, loaded, hot, cosine, z_done, aggregated, sorted_at, detailed]
            for key, before, after in zip(phases, stamps, stamps[1:]):
                phases[key].append((after - before) * 1000)
            predictions.append(ranked)
        store.db.rollback()
        store.close()
        del cached, table, store, best, maxima, scores, zvals
        if stream:
            print(json.dumps({"sample": sample, "completed": True}), flush=True)
    return {"schema": 1, "runs": runs, "warmups": 3,
            "cache_definition": "fresh Store cache per sample, OS page cache left untouched",
            "phases_ms": {key: {"samples": values, "p50": quantile(values, .5),
                                  "p95": quantile(values, .95)} for key, values in phases.items()},
            "top_files_and_z": predictions, "query_process_peak_rss": rss(),
            "model_process_rss": {"status": "not_used", "bytes": 0},
            "full_cli": {"status": "MISSING", "reason": "run the paired CLI latency benchmark separately"},
            "pack": {"status": "MISSING", "reason": "synthetic vector-only capacity fixture has no text packs"}}


def child(action, source, db, out, rows, per_file, runs):
    argv = [sys.executable, str(Path(__file__).resolve()), "--worker", action,
            "--source-root", str(source.resolve()), "--db", str(db.resolve()),
            "--out", str(out.resolve()), "--rows", str(rows), "--per-file", str(per_file),
            "--runs", str(runs)]
    started = time.monotonic()
    proc = subprocess.run(argv, capture_output=True)
    out.with_suffix(".stdout").write_bytes(proc.stdout)
    out.with_suffix(".stderr").write_bytes(proc.stderr)
    receipt = {"argv": argv, "exit_code": proc.returncode, "elapsed_s": time.monotonic() - started,
               "result_sha256": file_sha(out) if out.is_file() else None}
    out.with_suffix(".receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if proc.returncode:
        raise RuntimeError(f"{action} failed; see {out.with_suffix('.stderr')}")
    return json.loads(out.read_text())


def cache_benefit(arms):
    before, after = arms["reference"], arms["candidate"]
    time_ratio = after["phases_ms"]["cold_load"]["p50"] / before["phases_ms"]["cold_load"]["p50"]
    rss_ratio = after["query_process_peak_rss"]["bytes"] / before["query_process_peak_rss"]["bytes"]
    return {"cold_load_reduction": 1 - time_ratio, "peak_rss_reduction": 1 - rss_ratio,
            "meets_30pct_both": time_ratio <= .7 and rss_ratio <= .7}


def paired_measurements(args, db, stem):
    """One fresh process per arm/case; alternate every identical query sample."""
    import select

    children, outputs, started, execution_order = {}, {}, time.monotonic(), []
    try:
        for arm, source in (("reference", args.reference_root), ("candidate", args.candidate_root)):
            out = args.out / f"{stem}-{arm}.json"
            argv = [sys.executable, str(Path(__file__).resolve()), "--worker", "measure", "--stream",
                    "--source-root", str(source.resolve()), "--db", str(db.resolve()), "--out", str(out.resolve()),
                    "--runs", str(args.runs)]
            log = out.with_suffix(".stderr").open("wb")
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
            except BaseException:
                log.close()
                raise
            children[arm] = (proc, log, argv, out)
            outputs[arm] = []
        for sample in range(args.runs + 3):
            order = ("reference", "candidate") if sample % 2 == 0 else ("candidate", "reference")
            for arm in order:
                proc, _, _, out = children[arm]
                began = time.monotonic()
                proc.stdin.write(f"{sample}\n".encode()); proc.stdin.flush()
                if not select.select([proc.stdout], [], [], 60)[0]:
                    raise RuntimeError(f"{arm} capacity sample {sample} exceeded 60s")
                raw = proc.stdout.readline()
                outputs[arm].append(raw)
                if not raw or json.loads(raw) != {"sample": sample, "completed": True}:
                    raise RuntimeError(f"{arm} sample failed; see {out.with_suffix('.stderr')}")
                execution_order.append({"sample": sample, "arm": arm, "query_index": sample,
                                        "started_s": began - started, "finished_s": time.monotonic() - started})
        (args.out / f"{stem}-order.json").write_text(json.dumps(execution_order, indent=2) + "\n")
        reports = {}
        for arm, (proc, log, argv, out) in children.items():
            proc.stdin.close()
            code = proc.wait(timeout=10)
            log.flush()
            raw = b"".join(outputs[arm])
            out.with_suffix(".stdout").write_bytes(raw)
            out.with_suffix(".receipt.json").write_text(json.dumps({"argv": argv, "exit_code": code,
                "elapsed_s": time.monotonic() - started, "stdout_sha256": hashlib.sha256(raw).hexdigest(),
                "result_sha256": file_sha(out) if out.is_file() else None,
                "interleaved_samples": args.runs + 3, "process_id": proc.pid}, indent=2) + "\n")
            if code:
                raise RuntimeError(f"{arm} capacity worker exited {code}")
            reports[arm] = json.loads(out.read_text())
        return reports
    finally:
        for proc, log, _, _ in children.values():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=2)
            for stream in (proc.stdin, proc.stdout):
                if stream is not None and not stream.closed:
                    stream.close()
            log.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--reference-root", type=Path)
    parser.add_argument("--candidate-root", type=Path, default=ROOT)
    parser.add_argument("--scales", type=int, nargs="+", default=list(SCALES))
    parser.add_argument("--distributions", type=int, nargs="+", default=list(DISTRIBUTIONS))
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--cache-experiment", action="store_true",
                        help="explicitly build/enable derived cache; compare with independent SQLite reference")
    parser.add_argument("--worker", choices=["build", "measure", "cache"], help=argparse.SUPPRESS)
    parser.add_argument("--stream", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--db", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--rows", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--per-file", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.worker:
            sys.path.insert(0, str(args.source_root.resolve()))
            if args.worker == "build":
                result = build_fixture(args.db, args.rows, args.per_file)
            elif args.worker == "cache":
                from goldmine.store import Store
                from goldmine.vector_cache import build_vector_cache, set_vector_cache_enabled

                store = Store(args.db)
                try:
                    result = build_vector_cache(store)
                    set_vector_cache_enabled(store, True)
                    result.update({"builder_peak_rss": rss(), "sqlite_sha256_after_protocol": file_sha(args.db)})
                finally:
                    store.close()
            else:
                result = measure(args.db, args.runs, stream=args.stream)
            args.out.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
            return 0
        if args.reference_root is None or args.reference_root.resolve() == args.candidate_root.resolve():
            parser.error("independent --reference-root and --candidate-root are required")
        if args.out.exists() and any(args.out.iterdir()):
            parser.error("--out must be absent or empty")
        args.out.mkdir(parents=True, exist_ok=True)
        results = []
        for rows in args.scales:
            for per_file in args.distributions:
                stem = f"{rows}-{per_file}"
                db = args.out / (stem + ".sqlite")
                fixture = child("build", args.reference_root, db, args.out / (stem + "-fixture.json"), rows, per_file, args.runs)
                if args.cache_experiment:
                    fixture["cache_build"] = child("cache", args.candidate_root, db,
                        args.out / (stem + "-cache.json"), rows, per_file, args.runs)
                arms = paired_measurements(args, db, stem)
                a, b = arms["reference"]["top_files_and_z"], arms["candidate"]["top_files_and_z"]
                same = len(a) == len(b) and all(len(x) == len(y) and all(
                    left[0] == right[0] and abs(left[1] - right[1]) <= 1e-5
                    for left, right in zip(x, y)) for x, y in zip(a, b))
                candidate = arms["candidate"]
                trigger = rows >= 250_000 and (candidate["phases_ms"]["cold_load"]["p50"] > 500
                           or candidate["query_process_peak_rss"]["bytes"] > 1.5 * 1024 ** 3)
                results.append({"rows": rows, "segments_per_file": per_file,
                                "fixture": fixture, "arms": arms, "same_results": same,
                                "cache_experiment_triggered": trigger,
                                "cache_enabled_explicitly": args.cache_experiment,
                                "cache_benefit": cache_benefit(arms) if args.cache_experiment else None})
                print(f"{stem}: same_results={same}, cache_experiment_triggered={trigger}", flush=True)
        report = {"schema": 1, "suite_id": "scale.v1", "measured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "platform": platform.platform(), "quantile": "nearest-rank", "results": results,
                  "status": "PASS" if all(x["same_results"] for x in results) else "FAIL",
                  "scope": "vector capacity only; full CLI, model, text pack and any triggered cache experiment remain separate gates"}
        (args.out / "manifest.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
        return 0 if report["status"] == "PASS" else 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
