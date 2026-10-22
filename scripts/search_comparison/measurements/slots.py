"""Complete cell-owned observations; the root holds only fixed ordered refs."""
from pathlib import Path

from answer_eval.protocol import EvaluationError, canonical_sha256
from search_comparison.engine_capture.protocol import local_path, read, reference
from . import storage
from .selection import ARMS, VIEWS

FORMAT = "comparison.measurement-slot-shards/v2"
KEYS = ("ordinal", "id", "view", "arm")
AUTHORITY = "complete original raw command streams and closed index snapshots"


def _identity(slot):
    if (not isinstance(slot, dict) or type(slot.get("ordinal")) is not int or slot["ordinal"] < 1
            or not isinstance(slot.get("id"), str) or not slot["id"]
            or slot.get("view") not in VIEWS or slot.get("arm") not in ARMS):
        raise EvaluationError("slot requires its exact registered ordinal/ID/view/arm")
    return {k: slot[k] for k in KEYS}


def _path(owner, slot):
    _identity(slot)
    return local_path(str(owner)) / "cells" / slot["view"] / slot["arm"] / "slots" / f"{slot['ordinal']:05d}.json"


def _observation(row, slot):
    if (not isinstance(row, dict) or type(row.get("ordinal")) is not int
            or row["ordinal"] != slot["ordinal"] or row.get("id") != slot["id"]):
        raise EvaluationError("full observation does not belong to its fixed slot")
    return row


def retain(cell, slot, row):
    """Write every cache witness/clock before the cell's final inventory/census."""
    path = cell.owner / "slots" / f"{slot['ordinal']:05d}.json"
    _identity(slot)
    if cell.owner.name != slot["arm"] or cell.owner.parent.name != slot["view"] or cell.owner.parent.parent.name != "cells":
        raise EvaluationError("slot owner differs from its fixed cell")
    value = {"schema": 2, "format": FORMAT, "slot": slot, "observation": _observation(row, slot)}
    ref = storage.bounded_object(path, value, replay=cell.replay is not None)
    if ref.get("status") == "MISSING_DERIVED_RECORD_LIMIT":
        # No prefix or substituted hash represents an over-limit full record.
        # Its complete raw command streams/snapshots remain the authority.
        row = {**row, "status": "MISSING", "error": "complete slot record exceeds accepted derived record cap; raw retained",
               "storage_error": "accepted complete slot record limit exceeded"}
    else:
        ref = {**ref, "bytes": path.stat().st_size}
    return row, {**_identity(slot), "observation": ref}


def _read(entry, slot, owner):
    if (not isinstance(entry, dict) or set(entry) != {*KEYS, "observation"}
            or canonical_sha256({k: entry[k] for k in KEYS}) != canonical_sha256(_identity(slot))):
        raise EvaluationError("slot refs have a missing/extra/reordered/substituted fixed identity")
    ref, path = entry["observation"], _path(owner, slot)
    if not isinstance(ref, dict) or ref.get("path") != str(path):
        raise EvaluationError("full slot shard escaped its fixed cell-owned path")
    if ref.get("status") == "MISSING_DERIVED_RECORD_LIMIT":
        if (set(ref) != {"status", "path", "serialized_bytes", "accepted_record_bytes", "retained_authority"}
                or type(ref["serialized_bytes"]) is not int or ref["serialized_bytes"] <= storage.RECORD_BYTES
                or type(ref["accepted_record_bytes"]) is not int or ref["accepted_record_bytes"] != storage.RECORD_BYTES
                or ref["retained_authority"] != AUTHORITY or path.exists() or path.is_symlink()):
            raise EvaluationError("slot overflow marker was substituted or claims a materialized prefix")
        return None
    if (set(ref) != {"path", "sha256", "bytes"} or type(ref["bytes"]) is not int
            or not 0 < ref["bytes"] <= storage.RECORD_BYTES):
        raise EvaluationError("full slot shard needs exact finite size/SHA identity")
    reference({k: ref[k] for k in ("path", "sha256")}, bound=storage.RECORD_BYTES)
    if path.stat().st_size != ref["bytes"]:
        raise EvaluationError("full slot shard byte count changed")
    value = read(path, limit=storage.RECORD_BYTES)
    if (not isinstance(value, dict) or set(value) != {"schema", "format", "slot", "observation"}
            or type(value["schema"]) is not int or value["schema"] != 2 or value["format"] != FORMAT
            or canonical_sha256(value["slot"]) != canonical_sha256(slot)):
        raise EvaluationError("full slot shard format or complete registered schedule changed")
    return _observation(value["observation"], slot)


def index(owner, schedule, entries, *, replay=False):
    if (not isinstance(entries, list) or not isinstance(schedule, list) or len(entries) != len(schedule)
            or any(type(s.get("ordinal")) is not int or s["ordinal"] != n for n, s in enumerate(schedule, 1))):
        raise EvaluationError("slot index must retain the complete ordered schedule")
    for entry, slot in zip(entries, schedule):
        _read(entry, slot, owner)
    ref = storage.lines(Path(owner) / "slots.jsonl", entries, replay=replay)
    return {"format": FORMAT, **ref}


def entries(ref, schedule, owner):
    """Authenticate every ref/shard before any raw replay or materialization."""
    if (not isinstance(ref, dict) or set(ref) != {"format", "path", "sha256", "rows"}
            or ref["format"] != FORMAT or ref["path"] != str(Path(owner) / "slots.jsonl")
            or type(ref["rows"]) is not int or ref["rows"] != len(schedule)):
        raise EvaluationError("unregistered slot-ref framing or root path")
    # The unchanged total root allowance is also a finite pre-allocation bound.
    reference({k: ref[k] for k in ("path", "sha256")}, bound=storage.ROOT_ADMIN_BYTES)
    rows = storage.read_lines({k: ref[k] for k in ("path", "sha256", "rows")}, len(schedule))
    if any(type(s.get("ordinal")) is not int or s["ordinal"] != n for n, s in enumerate(schedule, 1)):
        raise EvaluationError("slot schedule ordinal registry changed")
    for entry, slot in zip(rows, schedule):
        _read(entry, slot, owner)
    return rows


def observations(ref, schedule, owner):
    """Complete rehydration for consumers; a cap marker is never accepted proof."""
    result = []
    for entry, slot in zip(entries(ref, schedule, owner), schedule):
        row = _read(entry, slot, owner)
        if row is None:
            raise EvaluationError("complete slot exceeds accepted derived record cap; raw retained; MISSING", status="MISSING")
        result.append(row)
    return result
