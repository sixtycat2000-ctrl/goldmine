"""Validate actual CLI output against the synthetic source, not reported PASS."""
from __future__ import annotations

import json
import math
import re

from .protocol import PARAMETERS, body, file_id, file_path


def json_value(raw: bytes):
    def pairs(items):
        value = {}
        for key, entry in items:
            if key in value:
                raise ValueError("duplicate CLI JSON key")
            value[key] = entry
        return value

    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite CLI JSON")))


def location(row, rows: int, per_file: int) -> tuple[int, int]:
    path, loc = row.get("path"), row.get("loc")
    match = re.fullmatch(r"/goldmine-scale/([1-9][0-9]*)\.txt", path or "")
    if not match or type(loc) not in (float, int) or not math.isfinite(loc) or int(loc) != loc:
        raise ValueError("CLI location must identify a real fixture row")
    fid, sid = int(match[1]), int(loc)
    if not 1 <= sid <= rows or fid != file_id(sid, rows, per_file) or row.get("loc_end") is not None:
        raise ValueError("CLI returned wrong synthetic file/segment/location mapping")
    if row.get("segment_id", sid) != sid:
        raise ValueError("CLI opaque segment ID disagrees with SQLite location")
    return fid, sid


def scores(row) -> dict:
    result = {}
    for key in ("score", "z", "cosine", "coverage"):
        value = row.get(key)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("CLI lacks finite semantic scores")
        result[key] = value
    return result


def structured(raw: bytes, form: str, rows: int, per_file: int, expected_query: str) -> dict:
    value = json_value(raw)
    records = value if form == "search" else value.get("records") if isinstance(value, dict) else None
    if not isinstance(records, list) or not 1 <= len(records) <= PARAMETERS["limit"]:
        raise ValueError("capacity must exercise nonempty actual details/packs, at most the fixed limit")
    if form == "pack_json":
        if value.get("schema") != "goldmine.pack/v1" or value.get("query") != expected_query:
            raise ValueError("pack schema/query mismatch")
        settings = value.get("settings", {})
        for key in ("scorer", "z_gate", "limit", "context", "budget"):
            if settings.get(key) != PARAMETERS[key]:
                raise ValueError("pack settings differ from the predeclared capacity control")
        if value.get("token_mode") != "tokenize" or value.get("fixpoint_converged") is not True:
            raise ValueError("mock pack must expose its actual token count and converged budget")
        used = value.get("used_tokens")
        if type(used) is not int or not 0 < used <= PARAMETERS["budget"]:
            raise ValueError("pack budget exceeded or missing")
    result, locations, fids = [], [], set()
    for number, record in enumerate(records, 1):
        if not isinstance(record, dict) or record.get("rank" if form == "search" else "n") != number:
            raise ValueError("CLI record ranks must be complete and ordered")
        fid, sid = location(record, rows, per_file)
        if fid in fids or record.get("kind") != "doc":
            raise ValueError("CLI returned repeated files or the wrong kind")
        fids.add(fid)
        locations.append((fid, sid))
        context = record.get("context", [])
        if not isinstance(context, list) or len(context) > 2:
            raise ValueError("capacity context exceeds its explicit one-neighbor radius")
        parts = {sid: body(sid)}
        for neighbor in context:
            _, other = location({"path": file_path(fid), **neighbor}, rows, per_file)
            if other == sid or neighbor.get("role") not in ("before", "after"):
                raise ValueError("invalid context role/location")
            if neighbor["role"] == "before" and other >= sid or neighbor["role"] == "after" and other <= sid:
                raise ValueError("context order disagrees with its stored location")
            if abs(other - sid) != math.ceil(rows / per_file):
                raise ValueError("context is not the immediate stored neighbor for context1")
            if form == "search" and neighbor.get("text") != body(other):
                raise ValueError("CLI context body does not match actual FTS source")
            parts[other] = body(other)
            locations.append((fid, other))
        expected_text = "\n".join(parts[key] for key in sorted(parts))
        if len(parts) != len(context) + 1:
            raise ValueError("duplicate synthetic context evidence")
        if form == "search":
            if record.get("scorer") != "semantic" or record.get("excerpt") != body(sid):
                raise ValueError("CLI excerpt/scorer differs from its stored semantic source")
        elif record.get("text") != expected_text:
            raise ValueError("pack must contain only complete, actual stored source/context bodies")
        if form == "pack_json" and (record.get("note") is not None or record.get("also_hits") != []):
            raise ValueError("synthetic source rows must remain complete independent content records")
        result.append({"file_id": fid, "segment_id": sid, **scores(record), "text": expected_text,
                       "part_ids": sorted(parts)})
    parsed = {"rows": result, "locations": [list(pair) for pair in sorted(set(locations))],
              "used_tokens": None if form == "search" else value["used_tokens"]}
    if form == "pack_json":
        counts = {key: value.get(key, 0) for key in ("duplicates_folded", "also_folded", "skipped_over_budget", "skipped_by_record_limit")}
        if any(type(count) is not int or count < 0 for count in counts.values()) or not isinstance(value.get("index", {}).get("line"), str):
            raise ValueError("pack counters/index identity missing")
        parsed["human"] = {"query": expected_query, "index_line": value["index"]["line"], **counts}
    return parsed


def human_pack(raw: bytes, pack: dict, *, fixture: dict, n_hits: int) -> None:
    from .human import validate

    validate(raw, pack, fixture=fixture, n_hits=n_hits)


def equivalent(left: dict, right: dict) -> bool:
    if len(left["rows"]) != len(right["rows"]):
        return False
    for a, b in zip(left["rows"], right["rows"]):
        if any(a[key] != b[key] for key in ("file_id", "segment_id", "text")):
            return False
        if any(abs(a[key] - b[key]) > 1e-5 for key in ("score", "z", "cosine", "coverage")):
            return False
    return True
