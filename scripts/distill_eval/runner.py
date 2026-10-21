"""Evaluate copies only; preserve raw cards, metrics and incremental differences."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import tempfile
import time

from acceptance_lib.evidence import InvalidEvidence, sha256
from .labels import load_labels
from .identity import assert_prepared_copies, index_identity
from .preparation import validate_preparation
from .scoring import excerpt_audit, membership_changes, topic_metrics


def copy_index(source, target):
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.commit()


def save(store, result):
    stats = result["stats"]
    store.save_run({"id": stats["run_id"], "algo": stats["algo"], "tau": stats["tau"],
                   "tau_source": stats["tau_source"], "scope_json": "{\"after\": null, \"kinds\": null, \"under\": null}",
                   "corpus_hash": stats["corpus_hash"], "mode": stats["mode"], "stats": stats,
                   "view_id": stats["view_id"]}, result["cards"])


def evaluate(db_path: Path, labels_path: Path, corpus_root: Path, incremental_db: Path | None, preparation_path: Path | None = None):
    from goldmine import distill
    from goldmine.citations import read_evidence, resolve_citation
    from goldmine.contracts import ApplicationError
    from goldmine.store import Store

    if preparation_path is None:
        raise InvalidEvidence("strict real distill requires source-bound native preparation receipts")
    preparation = validate_preparation(preparation_path, db_path, labels_path, corpus_root, incremental_db)
    labels_data, labels = load_labels(labels_path, corpus_root)
    frozen_labels_sha = sha256(labels_path)
    if incremental_db is None or not incremental_db.is_file():
        raise InvalidEvidence("supply an index produced by adding the10 frozen incremental documents")
    source_hash = sha256(db_path)
    new_hash = sha256(incremental_db)
    with tempfile.TemporaryDirectory(prefix="gm-distill-real-") as temporary:
        base_path, next_path = Path(temporary) / "base.sqlite", Path(temporary) / "next.sqlite"
        copy_index(db_path, base_path)
        copy_index(incremental_db, next_path)
        assert_prepared_copies(index_identity(base_path), index_identity(next_path), preparation)
        with closing(Store(base_path)) as base, closing(Store(next_path)) as next_store:
            fingerprint = json.loads(base.meta("fingerprint_json") or "{}")
            if fingerprint.get("backend") != "llama":
                raise InvalidEvidence("real quality evidence requires an actual llama index; mock remains synthetic")
            if next_store.meta("fingerprint_id") != base.meta("fingerprint_id"):
                raise InvalidEvidence("incremental index uses a different embedding fingerprint")
            expected_base = {path for path, row in labels.items() if row["split"] == "base"}
            expected_next = set(labels)
            for store, expected in ((base, expected_base), (next_store, expected_next)):
                indexed = {path: pin for path, pin in store.db.execute("SELECT path,sha256 FROM files")}
                if set(indexed) != expected:
                    raise InvalidEvidence("index documents do not exactly match the frozen base/+10 source split")
                if any(indexed[path] != labels[path]["asset_sha256"] for path in indexed):
                    raise InvalidEvidence("index file bytes differ from licensed frozen product assets")
            scope = distill.Scope()
            started = time.perf_counter()
            baseline = distill.run_distill(base, scope=scope, refresh=True)
            elapsed = time.perf_counter() - started
            items, _ = distill.dedupe(distill._corpus(base, scope))
            metrics = topic_metrics(baseline["cards"], items, {p: labels[p] for p in expected_base}, labels_data["topics"])
            audit = excerpt_audit(base, baseline["cards"])
            save(next_store, baseline)
            started = time.perf_counter()
            incremental = distill.run_distill(next_store, scope=scope)
            incremental_elapsed = time.perf_counter() - started
            full = distill.run_distill(next_store, scope=scope, refresh=True, threshold=baseline["stats"]["tau"])
            inc_audit = excerpt_audit(next_store, incremental["cards"])
            full_audit = excerpt_audit(next_store, full["cards"])
            changes = membership_changes(incremental["cards"], full["cards"])
            recluster = incremental["stats"]["reclustered_chunks"] / max(1, incremental["stats"]["after_dedupe"])
            # Exercise deletion and old-citation refusal only on this temporary copy.
            candidate = next((m for c in baseline["cards"] if c.status == "active" for m in c.members), None)
            if candidate is None:
                old_refused, deleted_absent = False, False
            else:
                old = read_evidence(next_store, candidate.sid).citation.citation_id
                next_store.delete_file(candidate.fid)
                try:
                    resolve_citation(next_store, old)
                    old_refused = False
                except ApplicationError as exc:
                    old_refused = exc.kind == "stale_citation"
                deleted = distill.run_distill(next_store, scope=scope)
                deleted_absent = all(m.fid != candidate.fid for c in deleted["cards"] for m in c.members)
            gates = {"real_purity": metrics["purity"] >= .70, "topic_recovery": metrics["topics_recovered"] >= 6,
                     "max_card_share": metrics["max_card_share"] <= .50,
                     "all_original_excerpts": audit["checked"] > 0 and audit["passed"] == audit["checked"],
                     "incremental_original_excerpts": inc_audit["checked"] > 0 and inc_audit["checked"] == inc_audit["passed"],
                     "full_same_tau_original_excerpts": full_audit["checked"] > 0 and full_audit["checked"] == full_audit["passed"],
                     "all_emitted_member_provenance": all(a["members_checked"] > 0 and
                         a["members_passed"] == a["members_checked"] for a in (audit, inc_audit, full_audit)),
                     "title_source_support": bool(audit["fixed_card_review"]) and all(r["verdict"] == "PASS" for r in audit["fixed_card_review"]),
                     "incremental_mode": incremental["stats"]["mode"] == "incremental",
                     "tau_inherited": incremental["stats"]["tau_source"] == "inherited" and incremental["stats"]["tau"] == baseline["stats"]["tau"],
                     "incremental_recluster_share": recluster <= .10,
                     "deleted_members_absent": deleted_absent, "old_citation_refused": old_refused}
            if sha256(db_path) != source_hash or sha256(incremental_db) != new_hash or sha256(labels_path) != frozen_labels_sha:
                raise InvalidEvidence("original input index changed during read-only evaluation")
            return {"schema": 2, "kind": "goldmine.distill-real", "evidence_mode": "real", "status": "PASS" if all(gates.values()) else "FAIL",
                    "preparation": preparation,
                    "labels_sha256": frozen_labels_sha, "source_index_sha256": source_hash,
                    "incremental_index_sha256": new_hash, "fingerprint": fingerprint, "metrics": metrics, "gates": gates,
                    "baseline_stats": baseline["stats"], "tau": asdict(baseline["tau"]), "wall_ms": elapsed*1000,
                    "incremental_stats": incremental["stats"], "incremental_wall_ms": incremental_elapsed*1000,
                    "incremental_recluster_share": recluster, "membership_changes_vs_same_tau_full": changes,
                    "excerpt_audit": audit, "incremental_excerpt_audit": inc_audit,
                    "full_same_tau_excerpt_audit": full_audit,
                    "cards": distill.render_json(baseline["cards"], baseline["stats"]),
                    "incremental_cards": distill.render_json(incremental["cards"], incremental["stats"]),
                    "full_same_tau_cards": distill.render_json(full["cards"], full["stats"]),
                    "original_indexes_unchanged": True}
