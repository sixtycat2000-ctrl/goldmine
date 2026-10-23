"""Source-bound material capture; the fixed worker never sees labels as queries."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

from .preparation import file_ref, load_preparation, owned_output
from .protocol import (EvaluationError, canonical_sha256, fields, read_json,
                       sha256_file, timestamp)


def implementation(root):
    root = Path(root).resolve()
    paths = [root / "scripts/prepare_answer.py", root / "scripts/eval_answer.py"]
    # answer_eval eagerly imports its workflow; product runner imports scoring,
    # alignment and original support. Bind the whole local package closures.
    paths += list((root / "goldmine").rglob("*.py"))
    for package in ("answer_eval", "product_eval"):
        paths += list((root / "scripts" / package).glob("*.py"))
    paths += [root / "scripts/acceptance_lib" / name for name in ("__init__.py", "evidence.py", "owned_process.py", "ir_checks.py")]
    return {p.relative_to(root).as_posix(): sha256_file(p) for p in sorted(paths)}


def _database(path):
    """Hash every common stored row, including non-vector and metadata evidence."""
    path = Path(path).resolve()
    before = sha256_file(path)
    for suffix in ("-wal", "-journal"):
        side = Path(str(path) + suffix)
        if side.exists() and side.stat().st_size:
            raise EvaluationError("material index needs a closed standalone snapshot")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        meta = dict(db.execute("SELECT key,value FROM meta"))
        hashes, counts = {}, {}
        for table, order in (("files", "id"), ("segments", "id"), ("fts", "rowid")):
            cursor = db.execute(f"SELECT {'rowid,*' if table == 'fts' else '*'} FROM {table} ORDER BY {order}")
            h, n = hashlib.sha256(), 0
            for row in cursor:
                values = [{"blob_sha256": hashlib.sha256(v).hexdigest(), "bytes": len(v)}
                          if isinstance(v, bytes) else v for v in row]
                h.update((json.dumps(values, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode())
                n += 1
            hashes[table], counts[table] = h.hexdigest(), n
        identity = {"sha256": before, "schema": db.execute("PRAGMA user_version").fetchone()[0],
                    "meta": meta, "rows_sha256": hashes, "counts": counts}
    if sha256_file(path) != before:
        raise EvaluationError("index changed during material audit")
    return identity


def role_inputs(prep, role):
    from product_eval.runner import check_build, production_identity, production_content
    value = prep.plan["materials"][role]
    fields(value, {"production_root", "index", "build_receipt", "embedding_model", "server",
                   "settings", "variables", "index_prepare", "prepare_evaluator"}, "material role")
    paths = {key: file_ref(value[key], "material." + key)
             for key in ("index", "build_receipt", "embedding_model", "server", "index_prepare")}
    code = production_identity(value["production_root"], reference_run=role == "reference")
    sources = {s["path"]: s for s in prep.corpus["sources"]}
    receipt = read_json(paths["build_receipt"])
    source = prep.corpus["source_bundle"]
    expected = {"manifest_sha256": source["manifest_sha256"], "queries_sha256": source["queries_sha256"],
                "splits_sha256": source["splits_sha256"], "corpus_sha256": canonical_sha256(prep.corpus["sources"]),
                "reference_source_commit": "769483752f49fb0bf8241dca778b1895b9a1b70b"}
    bundle = SimpleNamespace(identity=expected, sources=sources)
    corpus = check_build(bundle, paths["build_receipt"].parent)
    if receipt["diagnostic_only"] is not False:
        raise EvaluationError("diagnostic material build cannot prepare genuine jobs", status="MISSING")
    from acceptance_lib.evidence import read_command_run
    from acceptance_lib.ir_checks import evaluator_files
    fields(value["prepare_evaluator"], {"root", "files", "python"}, "prepare evaluator")
    evaluated = value["prepare_evaluator"]
    python = file_ref(evaluated["python"], "prepare Python")
    if evaluator_files(Path(evaluated["root"]), "product") != evaluated["files"]:
        raise EvaluationError("index preparation evaluator source changed")
    manifest, command, stdout = read_command_run(paths["index_prepare"], suite_id="product.v4")
    result = read_json(stdout)
    source_equal = result.get("production") == code if role == "reference" else production_content(result.get("production")) == production_content(code)
    if (command["exit_code"] != 0 or command["timed_out"] or result.get("status") != "PREPARED"
            or result.get("diagnostic_only") is not False or not source_equal
            or Path(result.get("db", "/")).resolve() != paths["index"] or result.get("bundle_identity") != expected):
        raise EvaluationError("actual successful source-bound index preparation receipt required", status="MISSING")
    if manifest["identity"] != {"evaluator_files": evaluated["files"], "production_source": result["production"],
            "model_sha256": value["embedding_model"]["sha256"], "server_sha256": value["server"]["sha256"],
            "build_receipt_sha256": value["build_receipt"]["sha256"]}:
        raise EvaluationError("index capture lacks exact evaluator/model/server/build provenance")
    argv = command["argv"]
    expected_argv = [str(python), str(Path(evaluated["root"]).resolve() / "scripts/eval_product.py")] + ["--prepare", "--bundle", str(Path(prep.plan["bundle_root"]).resolve()),
        "--build", str(paths["build_receipt"].parent), "--db", str(paths["index"]),
        "--production-root", str(Path(value["production_root"]).resolve()), "--backend", "llama",
        "--model", str(paths["embedding_model"]), "--server", str(paths["server"]), "--json"]
    if role == "reference":
        expected_argv.append("--reference-code")
    if (len(argv) < 2 or Path(argv[1]).name != "eval_product.py" or argv != expected_argv
            or timestamp(command["finished_at"], "index prepared") > timestamp(prep.plan["registered_at"], "registered plan")):
        raise EvaluationError("index receipt needs the fixed non-diagnostic raw prepare command before registration")
    fields(value["variables"], {"ranking", "chunker", "embedding", "lexical", "reranker", "pack"}, "material variables")
    if any(v in (None, "", {}) for v in value["variables"].values()):
        raise EvaluationError("all material identities/settings must be explicit")
    return value, paths, code, bundle, corpus


def validate_settings(parameters, role):
    from product_eval.candidate import validate_parameters
    validate_parameters(parameters, reference_run=role == "reference")
    if (parameters["scorer"] not in ("hybrid", "semantic", "lexical", "rrf")
            or type(parameters["z_gate"]) not in (int, float) or not math.isfinite(parameters["z_gate"])
            or type(parameters["limit"]) is not int or not 5 <= parameters["limit"] <= 50
            or type(parameters["context"]) is not int or not 0 <= parameters["context"] <= 3
            or type(parameters["budget"]) is not int or not 512 <= parameters["budget"] <= 32768):
        raise EvaluationError("invalid existing material parameter types/ranges")
    if role == "reference" and set(parameters) != {"scorer", "z_gate", "limit", "context", "budget"}:
        raise EvaluationError("reference uses the existing fixed five material parameters")


def _hits(hits):
    names = ("rank", "segment_id", "path", "kind", "loc", "loc_end", "score", "z", "excerpt")
    return [{key: getattr(h, key, None) for key in names} for h in hits]


def build_reference(store, backend, query, parameters):
    """One search, two representations of exactly the same hit objects."""
    import eval_answer
    clean, filters, hits = eval_answer.search_one(store, backend, {"q": query}, parameters)
    settings = eval_answer.PackSettings(query=clean, filters=filters, **parameters)
    pack, meta = eval_answer.build_pack_from_hits(store, backend, settings, hits)
    return hits, pack, meta, eval_answer.render_list_arm(hits)


def material_worker(plan, role, output, *, root):
    """Called only by the fixed capture CLI in a fresh interpreter."""
    from acceptance_lib.evidence import now, write_json
    from product_eval.candidate import retrieve
    from product_eval.reference import add_original_segment_sidecars, open_evaluation_store
    from product_eval.runner import make_backend, production_identity, production_content, select_production
    prep = load_preparation(plan, require_ready=True, verify_runtime=True)
    output = owned_output(prep.plan, output)
    value, paths, code, bundle, corpus = role_inputs(prep, role)
    validate_settings(value["settings"], role)
    parameters = value["settings"]
    start, before = now(), _database(paths["index"])
    meta = before["meta"]
    recorded_source = json.loads(meta.get("product.prepared_source_identity", "null"))
    source_equal = recorded_source == code if role == "reference" else production_content(recorded_source) == production_content(code)
    if (meta.get("product.backend") != "llama" or not source_equal
            or meta.get("product.build_receipt_sha256") != value["build_receipt"]["sha256"]
            or meta.get("product.bundle_identity") != canonical_sha256(bundle.identity)):
        raise EvaluationError("index was prepared from another source/build/backend")
    fingerprint = json.loads(meta.get("fingerprint_json", "null"))
    if (not isinstance(fingerprint, dict) or fingerprint.get("backend") != "llama"
            or fingerprint.get("model_sha256") != value["embedding_model"]["sha256"]):
        raise EvaluationError("prepared native embedding model identity differs")
    select_production(value["production_root"])
    output.mkdir(parents=True, exist_ok=False)
    cfg, backend = make_backend("llama", paths["index"], model=paths["embedding_model"], server=paths["server"])
    queries, files = {}, {}
    with closing(backend), closing(open_evaluation_store(paths["index"], original=role == "reference")) as store:
        store.db.execute("BEGIN")
        # Require the exact full asset inventory; no labels, oracle views or extra documents.
        actual = {Path(row[0]).resolve().relative_to(corpus).as_posix() for row in store.db.execute("SELECT path FROM files")}
        if actual != set(bundle.sources):
            raise EvaluationError("indexed corpus must contain exactly122 registered original assets")
        from product_eval.evidence import index_capability_missing
        needs = {"asr_text": "asr", "ocr_text": "ocr", "name_metadata": "exif"}
        bundle.queries = [{"expect": q["expect"], "needs": sorted({needs[e["evidence_type"]] for e in q["expect"] if e["evidence_type"] in needs and e.get("storage_field") not in ("basename", "path")})} for q in prep.questions.values()]
        missing = index_capability_missing(store, bundle, corpus, original_reference={"production_root": value["production_root"], "production": code} if role == "reference" else None)
        if missing:
            raise EvaluationError("missing actual indexed capabilities: " + "; ".join(missing), status="MISSING")
        for qid, q in prep.questions.items():
            if role == "reference":
                hits, pack, info, listing = build_reference(store, backend, q["q"], parameters)
                from goldmine.pack import PackSettings
                from goldmine.search import parse_filters
                clean, filters = parse_filters(q["q"])
                add_original_segment_sidecars(store, PackSettings(query=clean, filters=filters, **parameters), hits, info)
                representations = {"reference_LIST": listing, "reference_PACK": pack}
            else:
                from goldmine.search import parse_filters
                clean, filters = parse_filters(q["q"])
                hits, pack, info = retrieve(store, backend, cfg, clean, filters, parameters)
                representations = {"candidate_PACK": pack}
            hit_rows = _hits(hits)
            queries[qid] = {"query_sha256": canonical_sha256(q["q"]), "hits": hit_rows,
                            "hits_sha256": canonical_sha256(hit_rows), "pack_metadata": info,
                            "representations": {}}
            for arm, text in representations.items():
                destination = output / arm / (qid + ".md")
                destination.parent.mkdir(exist_ok=True)
                destination.write_text(text, encoding="utf-8", newline="\n")
                relative = destination.relative_to(output).as_posix()
                files[relative] = sha256_file(destination)
                queries[qid]["representations"][arm] = {"path": relative, "sha256": files[relative]}
        store.db.rollback()
    after = _database(paths["index"])
    if before != after or production_identity(value["production_root"], reference_run=role == "reference") != code:
        raise EvaluationError("material source/index changed while captured")
    modules = {name: str(Path(mod.__file__).resolve()) for name, mod in sys.modules.items()
               if name.startswith("goldmine") and getattr(mod, "__file__", None)}
    if any(not Path(path).is_relative_to(Path(value["production_root"]).resolve()) for path in modules.values()):
        raise EvaluationError("material imported another production source")
    report = {"schema": 1, "protocol": "answer-eval/v3", "status": "MATERIAL_PREPARED", "role": role,
              "started_at": start, "completed_at": now(), "plan_sha256": sha256_file(prep.path),
              "production": code, "index": before, "build_receipt_sha256": value["build_receipt"]["sha256"],
              "embedding_model": value["embedding_model"], "server": value["server"],
              "settings": parameters, "variables": value["variables"], "evaluator_files": implementation(root),
              "module_paths": modules, "files": files, "questions": queries,
              "answer_or_judge_completions": 0, "release_eligible": False}
    write_json(output / "material.json", report)
    return report
