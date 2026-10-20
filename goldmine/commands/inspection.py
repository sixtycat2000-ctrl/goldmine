"""Inspection command adapters."""
from __future__ import annotations
import argparse
import json
import os
import time
from .. import fingerprints
from ..backends import probe_all
from ..config import resolve_home
from ..errors import UserFacingError
from ..store import Store
from ..util import human_size
from ..command_support import (EXIT_OK, EXIT_PREREQ, EXIT_USAGE, _db_path, _emit_error, _emit_json, _g, _gate_no_index, _index_roots, _load_config, _staleness_hint)


def cmd_status(args: argparse.Namespace) -> int:
    from ..application import Application
    from ..watch import render_status_line, watch_status_payload

    cfg = _load_config(args)
    db = _db_path(args, cfg)
    json_mode = bool(_g(args, "json", False))
    refused = _gate_no_index(db, json_mode=json_mode, data_home=str(resolve_home(cfg)))
    if refused is not None:
        return refused
    limit = _g(args, "failures")
    snapshot = Application(db).status(include_details=True, failure_limit=10 if limit is None else limit)
    backends = probe_all(cfg)
    payload = _status_payload(snapshot, db, backends, watch_status_payload(cfg))
    if json_mode:
        _emit_json(payload)
    else:
        print(_render_status(payload, render_status_line(cfg)))
    return EXIT_OK


def _status_payload(snapshot, db, backends, watch):
    details, counts = snapshot.details, snapshot.counts
    fp = details["fingerprint"] or {}
    roots = details["roots"]
    n_stale = snapshot.staleness.get("stale_files", 0)
    stale_hint = ""
    if n_stale:
        shown = " ".join(roots[:2]) + (f" (+{len(roots) - 2} more)" if len(roots) > 2 else "") if roots else "<roots>"
        stale_hint = f"re-run `goldmine ingest {shown}` to refresh {n_stale} stale files"
    notices = _status_notices(snapshot, backends)
    return {
        "index": str(db), "index_bytes": db.stat().st_size,
        "schema_version": details["schema_version"], "roots": roots,
        "fingerprint": {"id": snapshot.fingerprint_id, "backend": fp.get("backend"),
                        "model_id": fp.get("model_id"), "dim": fp.get("dim")},
        "backends": backends,
        "counts": {"files": counts.files, "segments": counts.segments, "vectors": counts.vectors,
                   "bytes": counts.bytes, **{cap: counts.totals.get(cap, 0) for cap in
                   ("content_indexed", "metadata_only", "unsupported", "failed")},
                   "pending": counts.states.get("pending", 0)},
        "by_kind": counts.by_kind, "staleness": {**snapshot.staleness, "rerun_hint": stale_hint},
        "failures": details["failures"], "pending": details["pending"],
        "metadata_reasons": details["metadata_reasons"], "notices": notices, "watch": watch,
        "capabilities": snapshot.capabilities, "evidence": snapshot.evidence,
    }


def _status_notices(snapshot, backends):
    notices = []
    stored_build = snapshot.details["llama_build"]
    live = (backends.get("llama") or {}).get("build")
    if stored_build and live and stored_build != live:
        notices.append({"code": "build_changed", "message":
            f"llama.cpp build changed since last ingest (recorded: {stored_build}, installed: {live}) — reindex recommended"})
    drift = snapshot.staleness["extractor_stale"]
    if drift:
        notices.append({"code": "extractor_drift", "message":
            f"{drift} content-indexed files were extracted by a different extractor version — `goldmine ingest --reindex` refreshes"})
    note = snapshot.details["cards_rebuild_note"]
    if note:
        notices.append({"code": "cards_rebuild", "message": note})
    return notices


def _render_status(payload, watch_line):
    fp, counts, stale = payload["fingerprint"], payload["counts"], payload["staleness"]
    schema_line = f"schema v{payload['schema_version'] or Store.SCHEMA_VERSION}"
    schema_line += f" · fingerprint {fp['id']}" if fp["id"] else " · no fingerprint (keyword-only)"
    if fp["backend"] is not None:
        schema_line += f" ({fp['backend']}/{fp['model_id']}/{fp['dim']})"
    lines = [f"index: {payload['index']} ({human_size(payload['index_bytes'])})", schema_line]
    roots = payload["roots"]
    if roots:
        shown = ", ".join(roots[:3]) + (f" (+{len(roots) - 3} more)" if len(roots) > 3 else "")
        lines.append(f"roots: {shown}")
    for backend_id, info in payload["backends"].items():
        if info.get("available"):
            lines.append(f"backend {backend_id}: available" + (f" (dim {info.get('dim')})" if info.get("dim") else ""))
        else:
            lines.append(f"backend {backend_id}: unavailable — {info.get('detail', 'unknown')}")
    lines.append(f"files: {counts['files']} · segments: {counts['segments']} · vectors: {counts['vectors']} · bytes: {human_size(counts['bytes'])}")
    stale_line = (f"stale: {stale['stale_files']} of {stale['files']} files "
                  f"(embed {stale['embed_stale']} · chunker {stale['chunker_stale']} · extractor {stale['extractor_stale']})")
    oldest = stale.get("oldest_stale_indexed_at")
    if oldest:
        stale_line += f" — since {time.strftime('%Y-%m-%d', time.localtime(oldest))}"
    lines.append(stale_line)
    if stale["rerun_hint"]:
        lines.append(f"  {stale['rerun_hint']}")
    lines.append("capability grid (kind × capability):")
    for kind, grid in sorted(payload["by_kind"].items()):
        cells = " · ".join(f"{cap} {n}" for cap, n in sorted(grid.items()))
        lines.append(f"  {kind:<8} {cells}")
    if payload["pending"]:
        lines.append(f"pending: {len(payload['pending'])}")
        lines.extend(f"  {path}" for path in payload["pending"][:5])
    if payload["failures"]:
        lines.append(f"failed: {len(payload['failures'])} shown")
        lines.extend(f"  {failure['path']}: {failure['detail']}" for failure in payload["failures"])
    lines.extend(f"metadata-only: {item['reason']} × {item['count']}" for item in payload["metadata_reasons"])
    lines.extend(f"notice: [{item['code']}] {item['message']}" for item in payload["notices"])
    if watch_line:
        lines.append(watch_line)
    return "\n".join(lines)


def cmd_doctor(args: argparse.Namespace) -> int:
    from .. import doctor

    cfg = _load_config(args)
    db = _db_path(args, cfg)
    # W1: the doctor parser defines only --deep; the old dead `--probe` read
    # is gone (the live probe flag belongs to `backends --probe`)
    live = bool(os.environ.get("GOLDMINE_LIVE"))
    deep = bool(_g(args, "deep", False))
    results = doctor.run_checks(cfg, db, probe=live, deep=deep)
    json_mode = bool(_g(args, "json", False))
    summary = doctor.summary(results)  # W10: computed once, not three times
    if json_mode:
        _emit_json({
            "checks": [
                {
                    "name": r.name, "status": r.status,
                    "detail": r.detail, "fix_hint": r.fix_hint,
                }
                for r in results
            ],
            "summary": summary,
        })
    else:
        for r in results:
            marker = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}[r.status]
            print(f"{marker} {r.name}: {r.detail}")
            if r.fix_hint:
                print(f"       fix: {r.fix_hint}")
        print(
            f"doctor: {summary['counts']['ok']} ok, {summary['counts']['warn']} warn, "
            f"{summary['counts']['fail']} fail"
        )
    return EXIT_PREREQ if not summary["ok"] else EXIT_OK


def cmd_help(args: argparse.Namespace) -> int:
    from .. import help as help_mod

    topic = _g(args, "topic")
    if topic is None:
        print(help_mod.render_topic_list())
        return EXIT_OK
    text = help_mod.TOPICS.get(topic)
    if text is None:
        # S4 (v2 item 17 sweep): the refusal gets the universal shape — the
        # topic list rides in `why:`, `fix: goldmine help` lists them, and in
        # --json mode the error object reaches stdout (never empty stdout)
        _emit_error(UserFacingError(
            kind="unknown_help_topic",
            message=f"unknown help topic: {topic!r}",
            why="known topics: " + ", ".join(help_mod.TOPICS),
            fixes=["goldmine help"],
            doctor=False,
            exit_code=EXIT_USAGE,
        ), json_mode=bool(_g(args, "json", False)))
        return EXIT_USAGE
    print(text)
    return EXIT_OK
