"""Explicit source removal and persistent exclusions for ingest/watch."""
from __future__ import annotations

import json
from contextlib import nullcontext

from .index_state import canonical_source, contains, json_list, select_files


def list_sources(store):
    return {"roots": json_list(store, "roots_json"),
            "forgotten": json_list(store, "forgotten_sources_json"),
            "files": store.counts().files}


def forget_sources(store, targets, *, dry_run=False):
    transaction = nullcontext() if store.db.in_transaction else (
        store.read_snapshot() if dry_run else store.write_transaction())
    with transaction:
        return _forget_sources(store, targets, dry_run=dry_run)


def _forget_sources(store, targets, *, dry_run):
    targets = list(dict.fromkeys(canonical_source(target) for target in targets))
    if not targets:
        raise ValueError("forget needs at least one file, directory or URL")
    records = select_files(store, targets)
    ids = [rec.id for rec in records]
    old_roots = json_list(store, "roots_json")
    roots = [root for root in old_roots if not any(contains(target, root) for target in targets)]
    exclusions = list(dict.fromkeys([*json_list(store, "forgotten_sources_json"), *targets]))
    affected_cards = {row[0] for row in store.db.execute(
        "SELECT DISTINCT cc.card FROM card_chunks cc JOIN segments s ON s.id=cc.chunk "
        "WHERE s.file IN (SELECT value FROM json_each(?))", (json.dumps(ids),))}
    # Excerpts are stored snapshots and may outlive a removed membership.
    # Explicit forgetting removes those snapshots as well.
    parents = {}
    for cid, parent, raw in store.db.execute("SELECT id,parent_id,excerpts FROM cards"):
        parents[cid] = parent
        excerpts = json.loads(raw)
        if any(isinstance(part, dict) and isinstance(part.get("path"), str)
               and any(contains(target, part["path"]) for target in targets) for part in excerpts):
            affected_cards.add(cid)
    for cid in list(affected_cards):
        parent = parents.get(cid)
        while parent is not None and parent not in affected_cards:
            affected_cards.add(parent)
            parent = parents.get(parent)
    report = {"targets": targets, "files_removed": len(ids), "cards_removed": len(affected_cards),
              "roots_removed": [root for root in old_roots if root not in roots], "dry_run": dry_run}
    if dry_run:
        return report
    with store.write_transaction():
        card_ids = json.dumps(sorted(affected_cards))
        store.db.execute("UPDATE cards SET parent_id=NULL WHERE parent_id IN (SELECT value FROM json_each(?))", (card_ids,))
        store.db.execute("DELETE FROM card_chunks WHERE card IN (SELECT value FROM json_each(?))", (card_ids,))
        store.db.execute("DELETE FROM cards WHERE id IN (SELECT value FROM json_each(?))", (card_ids,))
        for fid in ids:
            store.delete_file(fid)
        store.set_meta("roots_json", json.dumps(roots))
        store.set_meta("forgotten_sources_json", json.dumps(exclusions))
        if affected_cards:
            store.set_meta("cards_v2_rebuild_note", "sources removed; re-run goldmine distill to regroup remaining evidence")
    return report


def restore_sources(store, targets):
    with store.write_transaction():
        return _restore_sources(store, targets)


def _restore_sources(store, targets):
    targets = [canonical_source(target) for target in targets]
    exclusions = json_list(store, "forgotten_sources_json")
    kept = [root for root in exclusions if not any(contains(target, root) for target in targets)]
    blocked = [root for root in kept if any(contains(root, target) for target in targets)]
    if blocked:
        raise ValueError(f"restore the explicitly forgotten parent first: {blocked[0]}")
    store.set_meta("forgotten_sources_json", json.dumps(kept))
    return {"restored": [root for root in exclusions if root not in kept],
            "next": "run goldmine ingest on the restored sources to index them again"}
