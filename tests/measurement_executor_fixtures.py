"""Declared tiny synthetic sources/SQLite, never product documents or models."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from answer_eval.protocol import canonical_sha256, sha256_file
from search_comparison.measurements.selection import ARMS, VIEWS, select_requests, select_sources, schedule, verify_schedule, KINDS
from search_comparison.measurements.recipes import operations
from product_eval.schema import CLASSES


def requests():
    return [{"id": f"fixture-{c}-{n:02d}", "q": f"synthetic {c} {n} 中文", "primary_class": c, "source_group": f"fixture-group-{n}"} for c in CLASSES for n in range(30)]


def fake_plan(root):
    sources = {f"fixture/{kind}-{n}.txt": {"path": f"fixture/{kind}-{n}.txt", "kind": kind} for kind, count in KINDS.items() for n in range(count)}
    bundle = SimpleNamespace(sources=sources, identity={"fixture": True}, fixture=False, verify=lambda: None)
    plan = SimpleNamespace(path=root / "never-engine-plan.json", sha256="a" * 64,
                           value={"registered_at": "2026-01-01T00:00:00+00:00", "engines": {n: None for n in ("goldmine", "qmd", "recoll")}},
                           missing={v: "declared mechanical fixture has no real source view" for v in VIEWS}, views={}, engines={}, bundle=bundle)
    selection = select_requests(requests()); cohorts = select_sources(sources)
    packet = {"registered_at": "2026-01-01T00:00:01+00:00", "selection": selection, "source_cohorts": cohorts,
              "schedule": schedule(selection), "update_operations": operations(cohorts), "resident_track": {"status": "CONDITIONAL_MISSING"}}
    packet["denominators"] = verify_schedule(selection, packet["schedule"])
    from search_comparison.measurements.packet import identity
    packet["implementation"] = identity()
    baseline = {"status": "MISSING", "systems": [{"view": v, "arm": a, "resource_observations": [], "prepared_snapshot": None} for v in VIEWS for a in ARMS]}
    return plan, packet, baseline


def marker(owner, engine="goldmine", view="common"):
    owner.mkdir(parents=True, exist_ok=True)
    (owner / "cwd").mkdir(exist_ok=True)
    value = {"schema": 1, "suite_id": "comparison.measurements/v1", "owner": str(owner), "view": view,
             "arm": engine + ".fast" if engine == "goldmine" else engine + ".search", "packet_sha256": "a" * 64}
    (owner / "cell-ownership.json").write_text(json.dumps(value))


def source(root, name="source.txt", text="declared synthetic 中文\n", identity="fixture/source.txt"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
    return {"source_path": identity, "view_path": name, "input": str(path), "sha256": sha256_file(path), "kind": "doc"}


def gm_database(path, corpus, rows, *, text="fixture body", info='{"exif_reader":"fixture","ocr_ms":1}'):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript('''PRAGMA user_version=6;
    CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
    CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT,source TEXT,kind TEXT,size INTEGER,sha256 TEXT,state TEXT,capability TEXT,detail TEXT,extractor_version TEXT,chunker_version TEXT,embed_id TEXT,content_date REAL,info TEXT);
    CREATE TABLE segments(id INTEGER PRIMARY KEY,file INTEGER,kind TEXT,modality TEXT,loc REAL,loc_end REAL,vec BLOB,excerpt TEXT);
    CREATE TABLE fts(rowid INTEGER PRIMARY KEY,name TEXT,body TEXT,original TEXT);
    CREATE TABLE fts_cjk(rowid INTEGER PRIMARY KEY,tokens TEXT);
    CREATE TABLE lookup_terms(normalized_term TEXT,raw_term TEXT,file_id INTEGER,segment_id INTEGER,source TEXT,match_type TEXT);''')
    db.execute("INSERT INTO meta VALUES('fingerprint_json',?)", (json.dumps({"backend": "llama", "model_sha256": "b" * 64}),))
    for n, row in enumerate(rows, 1):
        db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (n, str(corpus / row["view_path"]), str(corpus), "doc", Path(row["input"]).stat().st_size, row["sha256"], "ready", "full", None, "fixture", "fixture", "fixture", None, info))
        db.execute("INSERT INTO segments VALUES(?,?,?,?,?,?,?,?)", (n, n, "text", "text", 1, 1, b"1234", text))
        db.execute("INSERT INTO fts VALUES(?,?,?,NULL)", (n, row["view_path"], text))
        db.execute("INSERT INTO fts_cjk VALUES(?,?)", (n, "declared synthetic tokens"))
        db.execute("INSERT INTO lookup_terms VALUES(?,?,?,?,?,?)", ("fixture", "fixture", n, n, "body", "raw"))
    db.commit(); db.close()


def qmd_database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE llm_cache(hash TEXT PRIMARY KEY,result TEXT,created_at TEXT); CREATE TABLE documents(id INTEGER PRIMARY KEY,body TEXT); INSERT INTO documents VALUES(1,'fixture');")


def fake_event(label, *, code=0, timed_out=False, status="PASS"):
    return {"label": label, "phase": "queries", "command": {"path": "/synthetic-only/manifest.json", "sha256": "c" * 64},
            "error": "controlled synthetic failure" if code else None,
            "clock": {"wall_ms": 12.5, "exit_code": code, "timed_out": timed_out},
            "disposition": {"observation": {"sampled_resource_status": status}}}
