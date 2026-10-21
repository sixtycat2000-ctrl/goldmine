"""Source-bound prepared index audits. Content/vector identity excludes caches."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from answer_eval.protocol import EvaluationError, canonical_sha256, parse_json, sha256_file
from .protocol import inventory
from .sqlite_digest import physical
from .recoll import DENIED_PROBE, body_argv, coverage as recoll_coverage, metadata_argv, parse_metadata, stored_body


def _sqlite(path):
    path = Path(path)
    if not path.is_file():
        raise EvaluationError("prepared SQLite index is absent", status="MISSING")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    db.row_factory = sqlite3.Row
    return db


def goldmine(values, view):
    path = Path(values["goldmine_db"])
    before = sha256_file(path)
    with closing(_sqlite(path)) as db:
        schema = db.execute("PRAGMA user_version").fetchone()[0]
        if schema != 6:
            raise EvaluationError("current Goldmine capture requires exactschema6", status="MISSING")
        meta = dict(db.execute("SELECT key,value FROM meta"))
        fingerprint = parse_json(meta.get("fingerprint_json", "null"), "embedding fingerprint")
        if not isinstance(fingerprint, dict) or fingerprint.get("backend") != "llama" or fingerprint.get("model_sha256") != values["embedding_sha256"]:
            raise EvaluationError("Goldmine actual embedding backend/model differs", status="MISSING")
        root = Path(view["corpus"])
        expected = {str(root / d["view_path"]): d for d in view["documents"]}
        files = [dict(r) for r in db.execute("SELECT * FROM files ORDER BY id")]
        if {r["path"] for r in files} != set(expected) or len(files) != len(expected):
            raise EvaluationError("Goldmine index omits/adds source identities", status="MISSING")
        result = {}
        for r in files:
            d = expected[r["path"]]
            if r["sha256"] != d["sha256"]:
                raise EvaluationError("Goldmine indexed file SHA differs from actual shared input")
            parts = [dict(s) for s in db.execute("SELECT s.id,s.kind,s.modality,s.loc,s.loc_end,s.vec,coalesce(t.original,t.body) AS text FROM segments s LEFT JOIN fts t ON t.rowid=s.id WHERE s.file=? ORDER BY s.id", (r["id"],))]
            for s in parts:
                vec = s.pop("vec")
                s["vector"] = {"bytes": len(vec), "sha256": hashlib.sha256(vec).hexdigest()} if vec is not None else None
            result[d["source_path"]] = {"file": r, "parts": parts, "status": "OBSERVED_INDEXED_TEXT" if any(s["kind"] != "name" and s["text"] for s in parts) else "METADATA_ONLY"}
        stable = {"schema": schema, "meta": meta, "sources": result}
    if sha256_file(path) != before:
        raise EvaluationError("Goldmine index changed during read-only audit")
    return {"stable": stable, "stable_sha256": canonical_sha256(stable), "observed_files": {str(path): before}, "documents": []}


def validate_qmd(value, values, view):
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise EvaluationError("QMD readonly native SQLite audit is absent", status="MISSING")
    expected = {d["view_path"]: d for d in view["documents"]
                if view["name"] == "common" or Path(d["view_path"]).suffix in {".md", ".rst", ".py", ".c"}}
    rows = value.get("documents")
    if (not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows)
            or len(rows) != len(expected) or {r.get("path") for r in rows} != set(expected)
            or any(r.get("active") != 1 or r.get("collection") != "sources" for r in rows)):
        raise EvaluationError("QMD active document/mask/full-file denominator differs", status="MISSING")
    docs = []
    for r in rows:
        d = expected[r["path"]]
        body = (Path(view["corpus"]) / d["view_path"]).read_bytes().decode("utf-8")
        if r["body"] != body or r["hash"] != hashlib.sha256(body.encode()).hexdigest():
            raise EvaluationError("QMD stored full body/hash differs from original view bytes")
        docs.append({"source_path": d["source_path"], "view_path": d["view_path"], "hash": r["hash"],
                     "uri": "qmd://sources/" + d["view_path"] + "?index=comparison"})
    cfg = value.get("configuration")
    models = {r: values[r] for r in ("embed", "generate", "rerank") if r in values}
    mask = "**/*.md" if view["name"] == "common" else "**/*.{md,rst,py,c}"
    if (not isinstance(cfg, dict) or set(cfg) != {"models", "collections"} or cfg["models"] != models
            or cfg["collections"] != {"sources": {"path": view["corpus"], "pattern": mask}}):
        raise EvaluationError("QMD actual private model/collection config contains drift/hooks")
    if (len(value.get("collections", [])) != 1 or value["collections"][0].get("name") != "sources"
            or value["collections"][0].get("path") != view["corpus"] or value["collections"][0].get("pattern") != mask
            or value["collections"][0].get("update_command") not in (None, "")):
        raise EvaluationError("QMD SQL collection snapshot differs from fixed source config")
    if any(not Path(p).is_relative_to(Path(values["installation"])) for p in value.get("native_paths", [])):
        raise EvaluationError("QMD native SQLite import escaped its pinned dependency snapshot")
    if (not isinstance(value.get("fts"), list) or len(value["fts"]) != len(rows) or {r.get("rowid") for r in value["fts"]} != {r.get("id") for r in rows}
            or set(value.get("fts_tables", {})) != {"documents_fts_" + s for s in ("data", "idx", "content", "docsize", "config")}):
        raise EvaluationError("QMD actual full FTS/inverted-index proof is absent", status="MISSING")
    hashes = {r["hash"] for r in docs}
    vectors = value.get("vectors")
    vector_rows = value.get("vector_rows")
    if not isinstance(vectors, list) or not isinstance(vector_rows, list):
        raise EvaluationError("QMD vector readback arrays are malformed")
    if values["semantic"]:
        if value.get("has_vector_table") is not True or not vectors or not vector_rows or not value.get("fingerprint"):
            raise EvaluationError("QMD semantic vectors are absent; [] cannot be a successful abstention", status="MISSING")
        expected_keys = set()
        for h in hashes:
            v = [r for r in vectors if r.get("hash") == h]
            if not v or any(type(r.get("total_chunks")) is not int or r["total_chunks"] < 1 for r in v):
                raise EvaluationError("QMD source embedding coverage is missing", status="MISSING")
            n = v[0]["total_chunks"]
            if (len(v) != n or {r.get("seq") for r in v} != set(range(n))
                    or any(r["total_chunks"] != n or r.get("model") != values["embed"] or r.get("embed_fingerprint") != value["fingerprint"] for r in v)):
                raise EvaluationError("QMD chunk/model/fingerprint coverage is incomplete", status="MISSING")
            expected_keys.update(h + "_" + str(i) for i in range(n))
        if ({r.get("hash") for r in vectors} != hashes or len(vectors) != len(expected_keys)
                or {r.get("hash_seq") for r in vector_rows} != expected_keys or len(vector_rows) != len(expected_keys)
                or any(type(r.get("bytes")) is not int or r["bytes"] <= 0 or not isinstance(r.get("sha256"), str) or len(r["sha256"]) != 64 for r in vector_rows)):
            raise EvaluationError("QMD actual vector table and metadata rows disagree", status="MISSING")
    sources = {d["source_path"]: {"status": "COMPLETE_STORED_TEXT" if d["view_path"] in expected else "UNSUPPORTED_NATIVE_FORMAT"} for d in view["documents"]}
    stable = {"documents": rows, "vectors": vectors, "vector_rows": vector_rows, "configuration": cfg,
              "fingerprint": value["fingerprint"], "sources": sources, "has_vector_table": value.get("has_vector_table"),
              "fts": value["fts"], "fts_tables": value["fts_tables"], "physical": value.get("physical")}
    return {"stable": stable, "stable_sha256": canonical_sha256(stable), "documents": docs}


def qmd(commands, values, view):
    db = Path(values["qmd_db"])
    before = sha256_file(db)
    raw = commands.call("qmd.audit", [values["node"], str(Path(__file__).with_name("_qmd_audit.mjs")), values["qmd_package"], str(db),
                        str(Path(values["qmd_db"]).parents[3] / "qmd-config/comparison.yml"), values.get("embed", "-")], phase="audit")
    value = parse_json(raw.decode("utf-8"), "QMD readonly native audit stdout")
    result = validate_qmd(value, values, view)
    cfg_path = Path(values["qmd_db"]).parents[3] / "qmd-config/comparison.yml"
    if sha256_file(db) != before or value.get("physical") != physical(db) or value.get("configuration_sha256") != sha256_file(cfg_path):
        raise EvaluationError("QMD readonly audit changed SQLite bytes")
    result["observed_files"] = {str(db): value.get("database_sha256")}
    return result


def recoll(commands, values, view, mapping):
    directory = Path(values["recoll_db"])
    before = inventory(directory)
    raw = commands.call("recoll.metadata", metadata_argv(values["recollq"], values["recoll_config_dir"], view["corpus"]), phase="audit")
    metadata = parse_metadata(raw.decode("utf-8"), mapping)
    # Prove the actual audit policy denies source reads on every source, not an
    # unrelated canary. No source/index permissions or bytes are changed.
    for d in view["documents"]:
        raw = commands.call("recoll.denied-source", [values["sandbox"], "-f", values["sandbox_profile"], values["python"], "-I", "-S", "-B", "-c", DENIED_PROBE,
                                                    str(Path(view["corpus"]) / d["view_path"])], phase="audit")
        if raw != b"DENIED_SOURCE_READ\n":
            raise EvaluationError("Recoll audit source-read deny policy is ineffective", status="MISSING")
    bodies = [stored_body(commands.call("recoll.stored-body", body_argv(values["sandbox"], values["sandbox_profile"], values["recollq"],
                               values["recoll_config_dir"], view["corpus"], row["ordinal"]), phase="audit"), row) for row in metadata]
    sources = recoll_coverage(view, metadata, bodies)
    if inventory(directory) != before:
        raise EvaluationError("Recoll audit changed the finalized native index")
    stable = {"files": before, "metadata": metadata, "sources": sources}
    return {"stable": stable, "stable_sha256": canonical_sha256(stable), "observed_files": {str(directory): canonical_sha256(before)}, "documents": []}
