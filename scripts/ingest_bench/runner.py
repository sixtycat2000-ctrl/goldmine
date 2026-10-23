"""First/unchanged/modified/fault/recovery/deleted sequence on licensed copies."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import shutil
import time

from acceptance_lib.evidence import InvalidEvidence, read_json, sha256, verify_artifact
from search_bench.session import package_identity
from .measure import BackendProxy, Meter, stored_snapshot


def run(out: Path, source: Path, manifest_path: Path):
    from goldmine import distill
    from goldmine.backends import create_backend
    from goldmine.config import load
    from goldmine.ingest import IngestPipeline
    from goldmine.store import Store

    manifest = read_json(manifest_path)
    if manifest.get("evidence_mode") != "real":
        raise InvalidEvidence("ingest resource evidence needs licensed real assets, not generated fixtures")
    corpus = out / "corpus"
    corpus.mkdir()
    for row in manifest.get("sources", []):
        asset = verify_artifact(manifest_path.parent, row["asset"])
        provenance = row.get("provenance", {})
        if provenance.get("type") != "real" or not provenance.get("license"):
            raise InvalidEvidence("every ingest source needs a real licensed provenance record")
        verify_artifact(manifest_path.parent, provenance["license_file"])
        target = (corpus / row["path"]).resolve()
        if not target.is_relative_to(corpus):
            raise InvalidEvidence("source path escapes disposable corpus")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(asset, target)
    candidates = sorted(p for p in corpus.rglob("*") if p.suffix.lower() in (".txt", ".md"))
    if len(candidates) < 2:
        raise InvalidEvidence("need at least2 real text documents for modification/deletion controls")
    document, removed = candidates[:2]
    config = load()
    if config.backend != "llama":
        raise InvalidEvidence("real resource run must use the llama backend")
    phases = []
    initial_source = package_identity(source)
    with closing(Store(out / "index.sqlite")) as store:
        def phase(name, fault=False):
            meter = Meter()
            backend = BackendProxy(create_backend(config, "llama"), meter, fail_embed=fault)
            before = stored_snapshot(store)
            started = time.perf_counter()
            try:
                with meter:
                    report = IngestPipeline(config, store, backend).run([str(corpus)], code=True, instrument=True)
            finally:
                backend.close()
            row = {"phase": name, "wall_ms": (time.perf_counter()-started)*1000, "report": report.to_json(),
                   "measurements": meter.snapshot(), "before": before, "after": stored_snapshot(store)}
            phases.append(row)
            return row

        first = phase("first")
        clustered = distill.run_distill(store, scope=distill.Scope(), refresh=True)
        stats = clustered["stats"]
        store.save_run({"id": stats["run_id"], "algo": stats["algo"], "tau": stats["tau"], "tau_source": stats["tau_source"],
            "scope_json": distill.Scope().canonical_json(), "corpus_hash": stats["corpus_hash"], "mode": stats["mode"],
            "stats": stats, "view_id": stats["view_id"]}, clustered["cards"])
        unchanged = phase("unchanged")
        document.write_text(document.read_text() + "\nAn independently recorded revision was appended for the ingest resource audit.\n")
        modified = phase("modified_one")
        row = store.find_by_path(str(document))
        before_fault = {"segments": [list(v) for v in store.db.execute("SELECT id,kind,loc,loc_end,hex(vec) FROM segments WHERE file=? ORDER BY id", (row.id,))]}
        document.write_text(document.read_text() + "\nA controlled failure tests atomic replacement.\n")
        failed = phase("injected_embedding_failure", fault=True)
        record = store.find_by_path(str(document))
        body_rows = store.db.execute("SELECT id,kind,loc,loc_end,hex(vec) FROM segments WHERE file=? AND kind!='name' ORDER BY id", (record.id,)).fetchall()
        old_body = [v for v in before_fault["segments"] if v[1] != "name"]
        atomic_failure = [list(v) for v in body_rows] == old_body or (record.state == "failed" and not body_rows)
        recovered = phase("recovery")
        removed.unlink()
        deleted = phase("deleted_one")
        gone = store.find_by_path(str(removed)) is None
        counts = unchanged["measurements"]["calls"]
        zero = all(counts.get(key, 0) == 0 for key in ("embed_texts", "embed_image", "embed_audio_wav", "ocr", "asr", "extract"))
        gates = {"unchanged_no_expensive_calls": zero, "unchanged_exact_stored_content": unchanged["before"] == unchanged["after"],
                 "initial_success": first["report"]["files"]["failed"] == 0,
                 "modified_exactly_one": modified["report"]["files"]["content_indexed"] == 1,
                 "controlled_failure_occurred": failed["report"]["files"]["failed"] == 1,
                 "atomic_failure_policy": atomic_failure, "recovery_success": recovered["report"]["files"]["failed"] == 0,
                 "deleted_exactly_one": deleted["report"]["files"]["deleted"] == 1 and gone}
        first_calls = first["measurements"]["calls"]
        exercised = all(first_calls.get(key, 0) > 0 for key in ("embed_texts", "ocr", "asr"))
        cards_exercised = unchanged["before"]["cards"]["rows"] > 0
        missing = not exercised or not cards_exercised or package_identity(source) != initial_source
        return {"schema": 1, "kind": "goldmine.repeated-ingest", "evidence_mode": "real", "phases": phases,
                "source_files": initial_source, "source_manifest_sha256": sha256(manifest_path),
                "gates": gates, "media_and_cards_exercised": exercised and cards_exercised,
                "status": "MISSING" if missing else "PASS" if all(gates.values()) else "FAIL",
                "reason": "all required real OCR/ASR/cards must be exercised; counters are inclusive busy time, not critical-path latency" if missing else None}
