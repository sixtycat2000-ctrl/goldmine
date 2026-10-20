"""Explicit index build and read-only search; no automatic asset downloads."""

from __future__ import annotations

from contextlib import closing
import importlib
import importlib.metadata
import hashlib
import math
import json
import platform
from pathlib import Path
import subprocess
import sys

from answer_eval.protocol import (EvaluationError, canonical_sha256, read_json,
                                  reference, sha256_file)
from .bundle import REFERENCE_COMMIT
from .evidence import emitted_spans, index_capability_missing, read_segment_spans, relative_source
from .ledger import authorize_holdout
from .reference import add_original_segment_sidecars, open_evaluation_store
from .scoring import score_query, summarize


def production_identity(root, *, reference_run=False):
    root = Path(root).resolve()
    if not (root / "goldmine/__init__.py").is_file():
        raise EvaluationError("production root is not a goldmine source checkout", status="MISSING")
    commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    if commit.returncode:
        raise EvaluationError("cannot determine production checkout identity")
    if reference_run:
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", "goldmine", "pyproject.toml"], capture_output=True, text=True)
        if commit.stdout.strip() != REFERENCE_COMMIT or dirty.stdout.strip():
            raise EvaluationError("reference needs an independent clean checkout at the original production commit")
    files = {p.relative_to(root).as_posix(): sha256_file(p) for p in sorted((root / "goldmine").rglob("*.py"))}
    files["pyproject.toml"] = sha256_file(root / "pyproject.toml")
    return {"git_commit": commit.stdout.strip(), "source_sha256": canonical_sha256(files), "files": files}


def select_production(root):
    root = Path(root).resolve()
    for name, module in list(sys.modules.items()):
        if name == "goldmine" or name.startswith("goldmine."):
            location = getattr(module, "__file__", None)
            if location is not None and not Path(location).resolve().is_relative_to(root):
                raise EvaluationError("another production checkout was already imported; use a fresh interpreter")
    sys.path.insert(0, str(root))
    module = importlib.import_module("goldmine")
    if not Path(module.__file__).resolve().is_relative_to(root):
        raise EvaluationError("production import was shadowed by current candidate modules")


def make_backend(name, db, *, model=None, server=None):
    from goldmine.backends import create_backend
    from goldmine.config import Config
    cfg = Config(home=Path(db).parent / ".product-eval-home")
    cfg.backend = name
    cfg.llama.home = cfg.home
    if name == "llama":
        if model is None or server is None or not Path(model).is_file() or not Path(server).is_file():
            raise EvaluationError("real evaluation requires explicit existing --model and --server assets", status="MISSING")
        cfg.llama.model, cfg.llama.model_configured = Path(model).resolve(), True
        cfg.llama.server, cfg.llama.server_configured = Path(server).resolve(), True
    try:
        return cfg, create_backend(cfg, name)
    except (RuntimeError, ValueError, OSError) as exc:
        raise EvaluationError(f"backend unavailable: {exc}", status="MISSING") from exc


def check_build(bundle, build):
    root = Path(build).resolve()
    receipt = read_json(root / "build.json")
    if receipt.get("identity") != bundle.identity:
        raise EvaluationError("built corpus identity differs from audited bundle")
    for path, source in bundle.sources.items():
        if sha256_file(root / "corpus" / path) != source["asset"]["sha256"]:
            raise EvaluationError("built source bytes changed")
    return root / "corpus"


def prepare_index(bundle, build, db, *, backend_name, production_root, model=None, server=None, diagnostic=False, reference_run=False):
    if (bundle.missing or backend_name == "mock") and not diagnostic:
        raise EvaluationError("partial/mock inputs cannot prepare a formal product index", status="MISSING")
    identity = production_identity(production_root, reference_run=reference_run)
    select_production(production_root)
    corpus = check_build(bundle, build)
    path = Path(db).resolve()
    if path.exists():
        raise EvaluationError("explicit prepare refuses an existing index; choose a fresh database")
    path.parent.mkdir(parents=True, exist_ok=True)
    from goldmine.ingest import IngestPipeline
    from goldmine.store import Store
    cfg, backend = make_backend(backend_name, path, model=model, server=server)
    with closing(backend), closing(Store(path)) as store:
        report = IngestPipeline(cfg, store, backend).run([str(corpus)], code=True)
        if report.failed:
            raise EvaluationError(f"explicit ingest failed: {report.failures}", status="MISSING")
        store.set_meta("product.bundle_identity", canonical_sha256(bundle.identity))
        store.set_meta("product.prepared_code", canonical_sha256(identity))
        store.set_meta("product.backend", backend_name)
    return {"status": "PREPARED", "db": str(path), "production": identity,
            "diagnostic_only": diagnostic, "bundle_identity": bundle.identity}


def evaluate(bundle, build, db, *, production_root, backend_name="llama", split="dev", parameters=None,
             model=None, server=None, diagnostic=False, freeze=None, ledger=None, recheck=False, reference_run=False,
             seal_reference=False):
    parameters = parameters or {"scorer": "hybrid", "z_gate": 3.0, "limit": 10, "context": 1, "budget": 4000}
    from .candidate import validate_parameters
    validate_parameters(parameters, reference_run=reference_run)
    if (parameters["scorer"] not in ("hybrid", "semantic", "lexical", "rrf")
            or type(parameters["z_gate"]) not in (int, float) or not math.isfinite(parameters["z_gate"])
            or type(parameters["limit"]) is not int or not 5 <= parameters["limit"] <= 50
            or type(parameters["context"]) is not int or not 0 <= parameters["context"] <= 3
            or type(parameters["budget"]) is not int or not 512 <= parameters["budget"] <= 32768):
        raise EvaluationError("search parameter types/ranges are invalid")
    if (bundle.missing or backend_name == "mock") and not diagnostic:
        raise EvaluationError("complete real source/model inputs are required; denominator cannot shrink", status="MISSING")
    if split == "train":
        split = "dev"
    if split not in ("dev", "holdout", "all"):
        raise EvaluationError("unknown split")
    identity = production_identity(production_root, reference_run=reference_run)
    environment = {"python": platform.python_version(), "numpy": importlib.metadata.version("numpy")}
    lock_path = reference(bundle.root, bundle.manifest["reference_environment"], "reference environment")
    lock = read_json(lock_path)
    if not diagnostic and any(lock.get(k) != v for k, v in environment.items()):
        raise EvaluationError("runtime differs from the frozen reference/candidate dependency environment")
    exposure = None
    if split in ("holdout", "all"):
        sealed_original = reference_run and seal_reference and split == "holdout"
        if diagnostic or bundle.missing or (not sealed_original and (freeze is None or ledger is None or reference_run)):
            raise EvaluationError("sealed holdout requires final frozen real candidate and explicit ledger; task checks use dev")
    if seal_reference and not (reference_run and split == "holdout"):
        raise EvaluationError("reference sealing is only for original-code holdout, never candidate results")
    select_production(production_root)
    corpus = check_build(bundle, build)
    if not Path(db).is_file():
        raise EvaluationError("evaluation index does not exist; prepare explicitly", status="MISSING")
    from goldmine import fingerprints
    from goldmine.pack import PackSettings, build_pack_from_hits
    from goldmine.search import Searcher, parse_filters
    from goldmine.store import Store
    cfg, backend = make_backend(backend_name, db, model=model, server=server)
    rows = []
    with closing(backend), closing(open_evaluation_store(db, original=reference_run)) as store:
        store.db.execute("BEGIN")
        if store.meta("product.bundle_identity") != canonical_sha256(bundle.identity) or store.meta("product.backend") != backend_name:
            raise EvaluationError("index identity/backend differs from bundle; never mix mock and real vectors")
        if store.meta("product.prepared_code") != canonical_sha256(identity):
            raise EvaluationError("production code changed since this index was prepared; explicitly rebuild the matching candidate index")
        missing = index_capability_missing(store, bundle, corpus)
        if missing:
            raise EvaluationError("required indexed capabilities missing: " + "; ".join(missing), status="MISSING")
        try:
            fingerprints.gate(store.meta, fingerprints.build(backend))
        except fingerprints.FingerprintMismatch as exc:
            raise EvaluationError(str(exc)) from exc
        if split in ("holdout", "all") and not seal_reference:
            exposure = authorize_holdout(ledger, freeze, identity=bundle.identity,
                                         candidate_code_sha256=identity["source_sha256"],
                                         parameters_sha256=canonical_sha256(parameters),
                                         dependency_lock_sha256=sha256_file(lock_path), recheck=recheck)
        ids = set(bundle.splits[split]) if bundle.splits and split != "all" else {q["id"] for q in bundle.queries}
        for q in bundle.queries:
            if q["id"] not in ids:
                continue
            query, filters = parse_filters(q["q"])
            if reference_run:
                hits = Searcher(store, backend).search(query, limit=parameters["limit"], scorer=parameters["scorer"],
                                                      z_gate=parameters["z_gate"], filters=filters, snippet_width=None)
                settings = PackSettings(query=query, filters=filters, **parameters)
                text, meta = build_pack_from_hits(store, backend, settings, hits)
            else:
                from .candidate import retrieve

                hits, text, meta = retrieve(store, backend, cfg, query, filters, parameters)
            if reference_run:
                add_original_segment_sidecars(store, settings, hits, meta)
            portable_hits = []
            for hit in hits:
                path = relative_source(hit.path, corpus, bundle)
                spans = read_segment_spans(store, hit.segment_id, path, hit.path, bundle) if hit.segment_id is not None else []
                portable_hits.append({"path": path, "spans": spans, "kind": hit.kind, "loc": hit.loc, "loc_end": hit.loc_end})
            sent = emitted_spans(store, meta, text, corpus, bundle)
            rows.append({"id": q["id"], "primary_class": q["primary_class"], "tags": q["tags"], "source_group": q["source_group"],
                         "metrics": score_query(q, portable_hits, sent), "hits": portable_hits, "emitted_spans": sent,
                         "pack_records": meta["records"],
                         "context_text": text, "pack_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                         "used_tokens": meta["used_tokens"], "token_mode": meta["token_mode"]})
        backend_identity = backend.fingerprint_fields()
        index_identity = {"schema": store.db.execute("PRAGMA user_version").fetchone()[0],
                          "fingerprint_id": store.meta("fingerprint_id"), "fingerprint_json": store.meta("fingerprint_json"),
                          "lexical_version": store.meta("lexical_version"),
                          "chunker_versions": [r[0] for r in store.db.execute("SELECT DISTINCT chunker_version FROM files ORDER BY 1")]}
    if {r["id"] for r in rows} != ids:
        raise EvaluationError("incomplete output query set; no denominator intersection allowed")
    if production_identity(production_root, reference_run=reference_run) != identity:
        raise EvaluationError("production source files changed during evaluation")
    return {"schema": 1, "suite_id": "product.v4", "split": split, "bundle_identity": bundle.identity,
            "production": identity, "environment": environment, "backend": backend_name, "backend_identity": backend_identity,
            "index_identity": index_identity, "role": "reference" if reference_run else "candidate", "reference_verified": reference_run,
            "parameters": parameters, "summary": summarize(rows), "rows": rows,
            "diagnostic_only": diagnostic, "quality_evidence": not diagnostic, "exposure": exposure,
            "sealed_reference": seal_reference,
            "fixed_suite_targets": {"queries": 420, "answerable": 360, "noise": 60}, "missing": bundle.missing}
