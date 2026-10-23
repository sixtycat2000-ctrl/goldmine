"""Fail-closed, append-only exposure receipts for the one sealed release audit."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from answer_eval.protocol import (EvaluationError, canonical_json, canonical_sha256,
                                  fields, parse_json, read_json, read_text, sha, timestamp)


def _atomic_json(path, value):
    temp = path.with_name(path.name + ".tmp-" + secrets.token_hex(8))
    try:
        with temp.open("x", encoding="utf-8") as stream:
            stream.write(canonical_json(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def initialize_ledger(path):
    root = Path(path).resolve()
    try:
        root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise EvaluationError("exposure ledger must be initialized once in a new directory") from exc
    payload = {"event": "initialized", "nonce": secrets.token_hex(32),
               "created_at": datetime.now(timezone.utc).isoformat()}
    record = {"schema": 1, "sequence": 0, "previous_sha256": None, "payload": payload}
    record["sha256"] = canonical_sha256(record)
    with (root / "holdout_exposure.jsonl").open("x", encoding="utf-8") as stream:
        stream.write(canonical_json(record) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    _atomic_json(root / "state.json", {"schema": 1, "anchor_sha256": record["sha256"],
                                       "head_sha256": record["sha256"], "count": 1})
    return {"ledger_path": str(root), "anchor_sha256": record["sha256"]}


@contextmanager
def _lock(root):
    if not root.is_dir():
        raise EvaluationError("exposure ledger has not been initialized", status="MISSING")
    with (root / "ledger.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read(root):
    state = read_json(root / "state.json")
    fields(state, {"schema", "anchor_sha256", "head_sha256", "count"}, "ledger state")
    text = read_text(root / "holdout_exposure.jsonl")
    if not text or not text.endswith("\n"):
        raise EvaluationError("exposure ledger was cleared or incompletely written")
    records = [parse_json(line, "ledger record") for line in text.splitlines()]
    previous = None
    for sequence, record in enumerate(records):
        fields(record, {"schema", "sequence", "previous_sha256", "payload", "sha256"}, "ledger record")
        if not isinstance(record["payload"], dict):
            raise EvaluationError("invalid exposure ledger event payload")
        digest = canonical_sha256({k: v for k, v in record.items() if k != "sha256"})
        if record["schema"] != 1 or record["sequence"] != sequence or record["previous_sha256"] != previous or record["sha256"] != digest:
            raise EvaluationError("exposure ledger hash chain was modified")
        previous = digest
    if (state["schema"] != 1 or state["count"] != len(records)
            or state["head_sha256"] != previous or state["anchor_sha256"] != records[0]["sha256"]):
        raise EvaluationError("exposure ledger state/chain mismatch; do not reset or hide partial exposure")
    return state, records


def authorize_holdout(path, freeze, *, identity, candidate_code_sha256, parameters_sha256,
                      dependency_lock_sha256, recheck=False):
    """Consume exposure BEFORE querying. A failed/crashed audit is still spent."""
    fields(freeze, {"schema", "purpose", "frozen_at", "bundle_identity", "candidate_code_sha256",
                    "parameters_sha256", "dependency_lock_sha256", "ledger_anchor_sha256"}, "release freeze")
    if freeze["schema"] != 1 or freeze["purpose"] != "final_joint_release_audit":
        raise EvaluationError("holdout requires the frozen final joint release audit, not a task check")
    if timestamp(freeze["frozen_at"], "frozen_at") > datetime.now(timezone.utc):
        raise EvaluationError("candidate freeze is dated in the future")
    for name, value in (("candidate_code_sha256", candidate_code_sha256),
                        ("parameters_sha256", parameters_sha256),
                        ("dependency_lock_sha256", dependency_lock_sha256)):
        sha(freeze[name], name)
        if freeze[name] != value:
            raise EvaluationError(f"frozen {name} differs from the actual candidate")
    if freeze["bundle_identity"] != identity:
        raise EvaluationError("frozen suite/source/query/split identity differs")
    root = Path(path).resolve()
    with _lock(root):
        state, records = _read(root)
        if freeze["ledger_anchor_sha256"] != state["anchor_sha256"]:
            raise EvaluationError("ledger was replaced/reinitialized after candidate freeze")
        exposures = [r for r in records if r["payload"].get("event") in ("holdout_exposed", "deterministic_recheck")]
        frozen_sha = canonical_sha256(freeze)
        if exposures and (not recheck or exposures[0]["payload"]["freeze_sha256"] != frozen_sha):
            raise EvaluationError("holdout has already been exposed; new candidates need new unseen sources")
        if recheck and not exposures:
            raise EvaluationError("deterministic recheck requires a prior audit of the identical frozen candidate")
        payload = {"event": "deterministic_recheck" if recheck else "holdout_exposed",
                   "audited_at": datetime.now(timezone.utc).isoformat(), "freeze_sha256": frozen_sha,
                   "candidate_code_sha256": candidate_code_sha256, "parameters_sha256": parameters_sha256,
                   "dependency_lock_sha256": dependency_lock_sha256, "bundle_identity": identity,
                   "purpose": "final_joint_release_audit", "result_scope": "aggregate_and_per_query",
                   "prior_exposures": len(exposures)}
        record = {"schema": 1, "sequence": len(records), "previous_sha256": state["head_sha256"], "payload": payload}
        record["sha256"] = canonical_sha256(record)
        with (root / "holdout_exposure.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(canonical_json(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _atomic_json(root / "state.json", {"schema": 1, "anchor_sha256": state["anchor_sha256"],
                                           "head_sha256": record["sha256"], "count": len(records) + 1})
        return record
