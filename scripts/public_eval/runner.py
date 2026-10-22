"""Explicit preparation or read-only real rankings over an isolated source root."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import importlib.metadata
import platform
from zipfile import BadZipFile

from answer_eval.protocol import EvaluationError, canonical_sha256, sha256_file
from product_eval.runner import make_backend, production_content, production_identity, select_production
from .protocol import ARMS, DATASETS, FETCH_LIMIT, SUITE, inspect_index, load_archive, preparation_identity, ranked_row, summarize


def _options(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", action="store_true", required=True)
    parser.add_argument("--prepare", action="store_true", help="Explicitly create new derived corpora/indexes; never download.")
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--production-root", type=Path, required=True)
    parser.add_argument("--reference-code", action="store_true")
    parser.add_argument("--backend", choices=("llama", "mock"), default="llama")
    parser.add_argument("--model", type=Path)
    parser.add_argument("--server", type=Path)
    parser.add_argument("--diagnostic", action="store_true", help="Allow mock mechanical checks; acceptance refuses their quality evidence.")
    parser.add_argument("--query-deadline-seconds", type=float, default=None,
                        help="Explicitly registered per-query request deadline installed on the "
                             "read transaction (identical for reference and candidate roles). A "
                             "timeout fails loudly with the query identity recorded; it is never "
                             "converted into empty hits. Omit to keep the historical no-deadline "
                             "behavior for reproduction runs.")
    parser.add_argument("--json", action="store_true", required=True)
    return parser.parse_args(argv)


def identity(cache, production, *, reference=False):
    from eval_public import PUBLIC_MANIFEST_JSON, load_manifest, zip_path

    manifest = load_manifest()
    if tuple(manifest["datasets"]) != DATASETS:
        raise EvaluationError("formal public manifest must name all three fixed datasets")
    source = production_identity(production, reference_run=reference)
    data = {name: load_archive(zip_path(cache, name), manifest["datasets"][name]) for name in DATASETS}
    return manifest, source, data, sha256_file(PUBLIC_MANIFEST_JSON)


def _context(cache, production_root, production, *, backend_name, model, server, reference, diagnostic):
    from eval_public import PUBLIC_MANIFEST_JSON

    return {"schema": 1, "suite": SUITE, "backend": backend_name,
            "role": "reference" if reference else "candidate", "diagnostic_only": diagnostic,
            "production_root": str(Path(production_root).resolve()), "production": production,
            "manifest_sha256": sha256_file(PUBLIC_MANIFEST_JSON), "cache_root": str(Path(cache).resolve()),
            "model_sha256": sha256_file(Path(model)) if model else None,
            "server_sha256": sha256_file(Path(server)) if server else None,
            "environment": {"python": platform.python_version(), "numpy": importlib.metadata.version("numpy")},
            "parameters": {"fetch_limit": FETCH_LIMIT, "top_k": 10, "drop_self_doc_before_cut": True},
            "datasets": {}, "release_eligible": False}


def prepare(cache, production, manifest, data, *, production_root, backend_name, model, server, reference=False):
    from goldmine.ingest import IngestPipeline
    from goldmine.store import Store
    from eval_public import CORPUS_MD_DIR, dataset_dir, write_corpus_md

    plans = [(name, dataset_dir(cache, name) / "raw-index.sqlite", dataset_dir(cache, name) / CORPUS_MD_DIR) for name in DATASETS]
    if any(db.exists() or corpus.exists() for _, db, corpus in plans):
        raise EvaluationError("public prepare refuses existing indexes/corpora; use a new explicit cache root", status="MISSING")
    result = _context(cache, production_root, production, backend_name=backend_name, model=model, server=server,
                      reference=reference, diagnostic=backend_name != "llama")
    result["status"] = "PREPARED"
    for name, db, corpus in plans:
        write_corpus_md([data[name]["corpus"][key] for key in sorted(data[name]["corpus"])], corpus)
        cfg, backend = make_backend(backend_name, db, model=model, server=server)
        with closing(backend), closing(Store(db)) as store:
            report = IngestPipeline(cfg, store, backend).run([str(corpus)], code=True)
            if report.failed or report.content_indexed != len(data[name]["corpus"]):
                raise EvaluationError(f"public {name}: incomplete real corpus ingest", status="MISSING")
            fingerprint = backend.fingerprint_fields()
            if "backend_identity" in result and fingerprint != result["backend_identity"]:
                raise EvaluationError("public preparation backend identity changed between datasets", status="MISSING")
            result["backend_identity"] = fingerprint
            for key, value in {"public.input_identity": canonical_sha256(data[name]["identity"]),
                               **preparation_identity(production, reference=reference), "public.backend": backend_name}.items():
                store.set_meta(key, value)
        # This is the explicit preparation phase, so normalize the derived
        # database to a single immutable scoring input. Scoring never does this.
        with closing(sqlite3.connect(db)) as checkpoint:
            checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if checkpoint.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise EvaluationError("public prepare could not checkpoint the derived index", status="MISSING")
        result["datasets"][name] = {"input_identity": data[name]["identity"],
                                    "index": inspect_index(db, data[name], production, corpus, backend_name, reference=reference)}
    return result


def _deadline_progress_factory(current_deadline):
    """Self-contained monotonic deadline + SQLite interrupt (SC-04).

    Deliberately independent of goldmine.deadline: the reference role runs the
    ORIGINAL production code, which does not provide that module. The policy is
    therefore identical for both roles on any goldmine version.
    """
    import time

    def progress() -> int:
        return 1 if time.monotonic() > current_deadline() else 0

    return progress


def evaluate(cache, production_root, *, reference=False, backend_name="llama", model=None, server=None, diagnostic=False,
             query_deadline=None):
    # This function runs in a NEW interpreter so candidate imports cannot shadow
    # the separately checked original checkout.
    import time

    production = production_identity(production_root, reference_run=reference)
    select_production(production_root)
    from goldmine import fingerprints
    from goldmine.search import Searcher
    from eval_public import CORPUS_MD_DIR, PUBLIC_MANIFEST_JSON, dataset_dir, docid_from_path, load_manifest, zip_path
    from product_eval.reference import open_evaluation_store

    manifest = load_manifest()
    if tuple(manifest["datasets"]) != DATASETS:
        raise EvaluationError("formal public manifest must name all three fixed datasets")
    data = {name: load_archive(zip_path(cache, name), manifest["datasets"][name]) for name in DATASETS}
    cfg, backend = make_backend(backend_name, Path(cache) / "raw-eval.sqlite", model=model, server=server)
    result = _context(cache, production_root, production, backend_name=backend_name, model=model, server=server,
                      reference=reference, diagnostic=diagnostic)
    result["advisory_published_baselines"] = True
    if query_deadline is not None:
        # Explicitly registered policy (SC-04): visible in the captured result,
        # identical for both roles, timeouts are failures — never empty hits.
        result["parameters"]["query_deadline_seconds"] = query_deadline
        result["query_timeout_errors"] = []
    with closing(backend):
        for name in DATASETS:
            db, corpus = dataset_dir(cache, name) / "raw-index.sqlite", dataset_dir(cache, name) / CORPUS_MD_DIR
            index = inspect_index(db, data[name], production, corpus, backend_name, reference=reference)
            arm_data = {}
            with closing(open_evaluation_store(db, original=reference)) as store:
                store.db.execute("BEGIN")
                fingerprints.gate(store.meta, fingerprints.build(backend))
                if query_deadline is not None:
                    deadline_at = time.monotonic() + query_deadline
                    store.db.set_progress_handler(_deadline_progress_factory(lambda: deadline_at), 1000)
                searcher = Searcher(store, backend)
                try:
                    for arm in ARMS:
                        rows = []
                        for qid in sorted(data[name]["qrels"]):
                            if query_deadline is not None:
                                deadline_at = time.monotonic() + query_deadline
                            try:
                                hits = searcher.search(data[name]["queries"][qid], limit=FETCH_LIMIT, scorer=arm)
                            except sqlite3.OperationalError as error:
                                if query_deadline is not None and "interrupted" in str(error) and time.monotonic() >= deadline_at:
                                    result["query_timeout_errors"].append(
                                        {"dataset": name, "arm": arm, "qid": qid,
                                         "deadline_seconds": query_deadline, "error": str(error)})
                                    raise EvaluationError(
                                        f"public query exceeded its registered {query_deadline}s deadline: "
                                        f"{name}/{arm}/{qid}; recorded failure, not empty hits", status="MISSING") from error
                                raise
                            if any(Path(hit.path).resolve().parent != corpus.resolve() for hit in hits):
                                raise EvaluationError("public search returned a path outside the prepared corpus")
                            fetched = [docid_from_path(hit.path) for hit in hits]
                            rows.append(ranked_row(qid, fetched, data[name], manifest["datasets"][name]["grade_range"][1]))
                        arm_data[arm] = {"rows": rows, "metrics": summarize(rows)}
                finally:
                    if query_deadline is not None:
                        store.db.set_progress_handler(None, 0)
            if sha256_file(db) != index["sha256"]:
                raise EvaluationError("public index changed during scoring", status="MISSING")
            result["datasets"][name] = {"input_identity": data[name]["identity"], "index": index, "arms": arm_data}
        result["backend_identity"] = backend.fingerprint_fields()
    after = production_identity(production_root, reference_run=reference)
    if (after != production if reference else production_content(after) != production_content(production)):
        raise EvaluationError("public production source changed during scoring", status="MISSING")
    return result


def main(argv=None):
    args = _options(argv)
    try:
        if args.backend != "llama" and not args.diagnostic:
            raise EvaluationError("mock public runs require --diagnostic; they cannot be quality evidence", status="MISSING")
        source = production_identity(args.production_root, reference_run=args.reference_code)
        select_production(args.production_root)
        if args.prepare:
            manifest, source, data, _ = identity(args.cache_root, args.production_root, reference=args.reference_code)
            result = prepare(args.cache_root, source, manifest, data, production_root=args.production_root, backend_name=args.backend, model=args.model, server=args.server,
                             reference=args.reference_code)
            after = production_identity(args.production_root, reference_run=args.reference_code)
            if (after != source if args.reference_code else production_content(after) != production_content(source)):
                raise EvaluationError("public production source changed during explicit preparation", status="MISSING")
        else:
            result = evaluate(args.cache_root, args.production_root, reference=args.reference_code, backend_name=args.backend,
                              model=args.model, server=args.server, diagnostic=args.diagnostic,
                              query_deadline=args.query_deadline_seconds)
        code = 0
    except (EvaluationError, OSError, ValueError, sqlite3.Error, BadZipFile) as error:
        result, code = {"schema": 1, "suite": SUITE, "status": "MISSING", "error": str(error), "release_eligible": False}, 2
    except RuntimeError as error:
        result, code = {"schema": 1, "suite": SUITE, "status": "MISSING", "error": str(error), "release_eligible": False}, 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return code
