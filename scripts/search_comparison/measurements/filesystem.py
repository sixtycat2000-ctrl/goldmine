"""Typed, owned-only state transitions. No engine, shell, model or network API."""
from __future__ import annotations
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3

from answer_eval.protocol import EvaluationError, canonical_sha256, fields, sha256_file
from product_eval import materialization
from search_comparison.engine_capture.protocol import inventory, local_path, read
from search_comparison.engine_capture.sqlite_digest import physical

SUITE = "comparison.measurement-state/v1"
STATES = {"states/cold", "states/warm", "update/incremental", "update/fresh", "lifecycle/incremental", "lifecycle/fresh", "dependency/ocr"}
CORPORA = {"update/corpus", "lifecycle/corpus", "dependency/corpus"}
STAGES = {"base112", "incremental122", "fresh122", "lifecycle122", "removed121", "replaced122", "replacement_fresh122", "performance_cold_final", "performance_warm_final", "dependency_ocr"}


def inside(owner, name):
    path = local_path(name)
    if path == owner or not path.is_relative_to(owner):
        raise EvaluationError("state path must be below the new owned cell")
    return path


def index_path(owner, name, kind):
    path = inside(owner, name)
    endings = {"goldmine": "index.sqlite", "qmd": "xdg/cache/qmd/comparison.sqlite", "recoll": "recoll-db"}
    if kind not in endings or path.relative_to(owner).as_posix() not in {s + "/" + endings[kind] for s in STATES}:
        raise EvaluationError("unregistered owned working index path")
    return path


def state_name(owner, path, kind):
    relative = path.relative_to(owner).as_posix()
    return next(s for s in STATES if relative.startswith(s + "/"))


def corpus_path(owner, name):
    path = inside(owner, name)
    if path.relative_to(owner).as_posix() not in CORPORA:
        raise EvaluationError("unregistered owned working corpus path")
    return path


def snapshot_ref(path):
    files = inventory(path)
    if not files:
        raise EvaluationError("closed index snapshot is empty", status="MISSING")
    return {"path": str(path), "files": files, "sha256": canonical_sha256(files)}


def verify_snapshot(ref):
    fields(ref, {"path", "files", "sha256"}, "closed index snapshot")
    path = local_path(ref["path"])
    if snapshot_ref(path) != ref:
        raise EvaluationError("closed index snapshot bytes changed", status="MISSING")
    return path


def source_entries(owner, value):
    if not isinstance(value, list) or any(not isinstance(r, dict) for r in value):
        raise EvaluationError("every source transition row must be an object")
    seen, identities = set(), set()
    for row in value:
        fields(row, {"source_path", "view_path", "input", "sha256", "kind"}, "source transition")
        rel = Path(row["view_path"])
        if (not isinstance(row["source_path"], str) or not row["source_path"] or row["source_path"] in identities
                or rel.is_absolute() or ".." in rel.parts or not rel.parts or any(c in row["view_path"] for c in ("\x00", "\\"))
                or row["view_path"] in seen or row["kind"] not in {"doc", "code", "pdf", "screenshot", "image", "audio"}):
            raise EvaluationError("unsafe/duplicate source transition path or kind")
        seen.add(row["view_path"])
        identities.add(row["source_path"])
        src = local_path(row["input"])
        if src.is_relative_to(owner) or not src.is_file() or sha256_file(src) != row["sha256"]:
            raise EvaluationError("source transition input differs from immutable view")
    return value


def source_witness(corpus, entries, view):
    actual = inventory(corpus)
    if set(actual) != {r["view_path"] for r in entries}:
        raise EvaluationError("working corpus file denominator differs from registered stage")
    rows = {}
    for row in entries:
        path = corpus / row["view_path"]
        stat = path.stat()
        expected_flag = materialization.CAPTURE_VALUE.hex() if view == "native" and row["kind"] == "screenshot" else None
        flag = materialization.read_flag(path)
        if actual[row["view_path"]]["sha256"] != row["sha256"] or flag != expected_flag:
            raise EvaluationError("working source bytes/actual screenshot metadata differ")
        rows[row["source_path"]] = {"view_path": row["view_path"], "sha256": row["sha256"], "bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "ctime_ns": stat.st_ctime_ns, "birthtime": getattr(stat, "st_birthtime", None),
            "device": stat.st_dev, "inode": stat.st_ino, "screen_capture": flag}
    return {"corpus": str(corpus), "sources": rows, "sha256": canonical_sha256(rows)}


def _closed_sqlite(path):
    for suffix in ("-wal", "-journal"):
        extra = Path(str(path) + suffix)
        if extra.exists() and extra.stat().st_size:
            raise EvaluationError("index is not a closed standalone SQLite snapshot")


def index_witness(path, kind):
    if kind == "recoll":
        files = inventory(path)
        if not files:
            raise EvaluationError("native index directory is empty")
        return {"kind": kind, "path": str(path), "stable": files, "stable_sha256": canonical_sha256(files),
                "logical_bytes": sum(r["bytes"] for r in files.values()),
                "allocated_bytes": sum((path / n).stat().st_blocks * 512 for n in files), "cache": None}
    _closed_sqlite(path)
    before = sha256_file(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON"); db.execute("BEGIN")
        schema = db.execute("PRAGMA user_version").fetchone()[0]
        cache = None
        if kind == "qmd":
            columns = [r[1] for r in db.execute("PRAGMA table_info(llm_cache)")]
            if columns != ["hash", "result", "created_at"]:
                raise EvaluationError("QMD application cache schema differs")
            cache = [{"hash": r[0], "result": r[1], "created_at": r[2]} for r in db.execute("SELECT hash,result,created_at FROM llm_cache ORDER BY hash")]
        db.rollback()
    if sha256_file(path) != before:
        raise EvaluationError("read-only index witness changed actual bytes")
    stable = physical(path) if kind == "qmd" else {"sha256": before, "user_version": schema}
    return {"kind": kind, "path": str(path), "stable": stable, "stable_sha256": canonical_sha256(stable), "file_sha256": before,
            "logical_bytes": path.stat().st_size, "allocated_bytes": path.stat().st_blocks * 512,
            "cache": cache, "cache_sha256": canonical_sha256(cache)}


def verify_index_snapshot(ref, witness, kind):
    root = verify_snapshot(ref)
    actual = index_witness(root if kind == "recoll" else root / "index.sqlite", kind)
    keys = {"stable_sha256", "kind", "logical_bytes", "cache"}
    if kind != "recoll":
        keys |= {"file_sha256", "cache_sha256"}
    if any(actual[k] != witness[k] for k in keys):
        raise EvaluationError("closed snapshot actual content/cache differs from its command witness")
    return root


def execute(value):
    fields(value, {"schema", "suite_id", "owner", "operation", "kind", "arguments"}, "typed state request")
    if type(value["schema"]) is not int or value["schema"] != 1 or value["suite_id"] != SUITE:
        raise EvaluationError("unregistered state request schema/suite")
    owner = local_path(value["owner"])
    ownership = read(owner / "cell-ownership.json")
    if (set(ownership) != {"schema", "suite_id", "owner", "view", "arm", "packet_sha256"}
            or ownership["schema"] != 1 or ownership["suite_id"] != "comparison.measurements/v1"
            or ownership["owner"] != str(owner) or ownership["view"] not in {"native", "common"}
            or ownership["arm"].split(".")[0] != value["kind"]):
        raise EvaluationError("state transition lacks owned cell registration")
    op, kind, args = value["operation"], value["kind"], value["arguments"]
    if op == "goldmine_audit":
        fields(args, {"index", "corpus", "known", "embedding_sha256"}, "Goldmine audit arguments")
        if kind != "goldmine":
            raise EvaluationError("Goldmine audit used for another engine")
        from .database import goldmine
        path = index_path(owner, args["index"], kind)
        corpus = corpus_path(owner, args["corpus"])
        known = source_entries(owner, args["known"])
        _closed_sqlite(path)
        return goldmine(path, corpus, known, args["embedding_sha256"])
    if op in {"index_witness", "index_clone", "index_snapshot"}:
        path = index_path(owner, args["index"], kind)
        if op == "index_witness":
            fields(args, {"index"}, "index witness arguments")
            return index_witness(path, kind)
        if op == "index_clone":
            fields(args, {"index", "source", "reset_cold"}, "index clone arguments")
            if type(args["reset_cold"]) is not bool or args["reset_cold"] and state_name(owner, path, kind) != "states/cold":
                raise EvaluationError("reset flag applies only to the cold owned state")
            src = verify_snapshot(args["source"])
            if src == path or src.is_relative_to(path) or path.is_relative_to(src):
                raise EvaluationError("index copy input/output overlap")
            if path.exists():
                if args["reset_cold"] is not True or state_name(owner, path, kind) != "states/cold":
                    raise EvaluationError("existing state can only be reset in the owned cold index slot")
                if kind != "recoll":
                    _closed_sqlite(path)
                if path.is_dir(): shutil.rmtree(path)
                else: path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            if kind == "recoll": shutil.copytree(src, path)
            else:
                if set(args["source"]["files"]) != {"index.sqlite"}: raise EvaluationError("SQLite snapshot contains undeclared nodes")
                shutil.copyfile(src / "index.sqlite", path)
            return index_witness(path, kind)
        fields(args, {"index", "stage"}, "index snapshot arguments")
        if args["stage"] not in STAGES: raise EvaluationError("unregistered closed snapshot stage")
        target = inside(owner, str(owner / "snapshots" / args["stage"]))
        target.parent.mkdir(exist_ok=True)
        if target.exists(): raise EvaluationError("closed snapshots are immutable and cannot be overwritten")
        before = index_witness(path, kind)
        if kind == "recoll": shutil.copytree(path, target)
        else: target.mkdir(); shutil.copyfile(path, target / "index.sqlite")
        if index_witness(path, kind) != before: raise EvaluationError("index changed during closed snapshot copy")
        return {"snapshot": snapshot_ref(target), "witness": before}
    if op not in {"source_write", "source_remove", "source_witness"}:
        raise EvaluationError("unregistered typed state operation")
    if kind not in {"goldmine", "qmd", "recoll"}: raise EvaluationError("unregistered source-transition engine")
    fields(args, {"corpus", "view", "entries", "changed"}, "source state arguments")
    corpus = corpus_path(owner, args["corpus"])
    if args["view"] not in {"native", "common"}: raise EvaluationError("unknown source view")
    entries = source_entries(owner, args["entries"])
    changed = source_entries(owner, args["changed"])
    if op == "source_write":
        by_path = {r["view_path"]: r for r in entries}
        if any(by_path.get(r["view_path"]) != r for r in changed):
            raise EvaluationError("write subset differs from the complete declared target stage")
        corpus.mkdir(parents=True, exist_ok=True)
        for row in changed:
            dest = inside(owner, str(corpus / row["view_path"]))
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".measurement-tmp")
            if tmp.exists(): raise EvaluationError("owned temporary source slot already exists")
            shutil.copy2(row["input"], tmp)
            materialization.materialize(tmp, {"kind": row["kind"] if args["view"] == "native" else "doc"})
            os.replace(tmp, dest)
    elif op == "source_remove":
        if {r["view_path"] for r in entries} & {r["view_path"] for r in changed}:
            raise EvaluationError("removed source still appears in the target stage")
        for row in changed:
            dest = inside(owner, str(corpus / row["view_path"]))
            if not dest.is_file() or sha256_file(dest) != row["sha256"]:
                raise EvaluationError("source removal did not identify the registered original owned bytes")
            dest.unlink()
    elif changed:
        raise EvaluationError("read-only source witness cannot mutate paths")
    return source_witness(corpus, entries, args["view"])
