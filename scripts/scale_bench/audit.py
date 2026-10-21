"""Strict primitives for replaying captured capacity measurements."""
from __future__ import annotations

import math
from pathlib import Path
import re

from .output import json_value
from .protocol import DIM, body, db_snapshot, file_id, file_path, sha, unchanged


def hashed(root: Path, entry: dict) -> Path:
    if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
        raise ValueError("capacity artifact requires exact path/sha256 fields")
    name = entry["path"]
    if (not isinstance(name, str) or not name or Path(name).is_absolute() or
            ".." in Path(name).parts or "\\" in name or not re.fullmatch(r"[0-9a-f]{64}", str(entry["sha256"]))):
        raise ValueError("unsafe capacity artifact path/hash")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or sha(path) != entry["sha256"]:
        raise ValueError("capacity artifact is missing or its hash changed")
    return path


def load(root: Path, entry: dict):
    return json_value(hashed(root, entry).read_bytes())


def finite(value, *, positive=False) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)


def check_rss(value: dict, *, scope=None, unit=None):
    if (not isinstance(value, dict) or not finite(value.get("raw"), positive=True) or
            value.get("raw_unit") not in ("bytes", "KiB") or type(value.get("bytes")) is not int or
            value["bytes"] != int(value["raw"] * (1 if value["raw_unit"] == "bytes" else 1024))):
        raise ValueError("RSS requires platform-original units and exact bytes conversion")
    if scope is not None and value.get("scope") != scope:
        raise ValueError("RSS lifetime is not the declared independent process/arm")
    if unit is not None and value["raw_unit"] != unit:
        raise ValueError("RSS original unit differs from the measured operating system")


def check_snapshot(value):
    if not isinstance(value, dict) or set(value) != {"sqlite", "wal", "shm", "journal"}:
        raise ValueError("all SQLite content and coordination sidecars must be recorded")
    for key, entry in value.items():
        if entry is None and key != "sqlite":
            continue
        if (not isinstance(entry, dict) or set(entry) != {"bytes", "sha256"} or
                type(entry["bytes"]) is not int or entry["bytes"] < 0 or
                not re.fullmatch(r"[0-9a-f]{64}", str(entry["sha256"]))):
            raise ValueError("invalid SQLite snapshot size/hash")
    if value["sqlite"]["bytes"] <= 0:
        raise ValueError("empty SQLite fixture")


def check_command(directory: Path, receipt: dict, expected_argv: list, *, warm: bool, env: dict):
    common = {"schema", "kind", "argv", "cwd", "env", "process_id", "exit_code", "timed_out",
              "started_ns", "finished_ns", "wall_ms", "rss", "stdout", "stderr"}
    keys = common | ({"invocation"} if warm else {"spawn_error"})
    if not isinstance(receipt, dict) or set(receipt) != keys or receipt.get("schema") != 1:
        raise ValueError("raw capacity command receipt has unknown/missing fields")
    if receipt["kind"] != ("goldmine.scale.warm-command" if warm else "goldmine.scale.command"):
        raise ValueError("capacity receipt invocation kind changed")
    if receipt["argv"] != expected_argv or receipt["env"] != env:
        raise ValueError("actual CLI argv/source/config differs from the fixed protocol")
    if warm and receipt["invocation"] != "goldmine.cli.main(argv)":
        raise ValueError("warm calls must use the actual CLI entrypoint")
    if receipt["cwd"] != str(directory.parent.parent.resolve()):
        raise ValueError("actual CLI must run in its isolated owned arm directory")
    if (type(receipt["process_id"]) is not int or receipt["process_id"] <= 0 or
            type(receipt["exit_code"]) is not int or type(receipt["timed_out"]) is not bool):
        raise ValueError("missing actual CLI process status")
    start, end = receipt["started_ns"], receipt["finished_ns"]
    if (type(start) is not int or type(end) is not int or start <= 0 or end <= start or
            receipt["wall_ms"] != (end - start) / 1e6):
        raise ValueError("CLI timing differs from its raw monotonic interval")
    check_rss(receipt["rss"], scope="one-arm-warm-worker" if warm else "fresh-process")
    return hashed(directory, receipt["stdout"]).read_bytes(), hashed(directory, receipt["stderr"]).read_text()


def check_proofs(proofs: list, expected_locations, rows, per_file):
    if not isinstance(proofs, list):
        raise ValueError("SQLite readback proofs must be a complete list")
    locations = []
    for proof in proofs:
        if not isinstance(proof, dict) or set(proof) != {"file_id", "segment_id", "path", "loc", "body", "vector_sha256"}:
            raise ValueError("readback proof fields differ from the native SQLite audit")
        fid, sid = proof["file_id"], proof["segment_id"]
        if (type(fid) is not int or type(sid) is not int or not 1 <= sid <= rows or
                fid != file_id(sid, rows, per_file) or proof["path"] != file_path(fid) or
                proof["loc"] != sid or proof["body"] != body(sid) or
                not re.fullmatch(r"[0-9a-f]{64}", str(proof["vector_sha256"]))):
            raise ValueError("returned evidence does not match deterministic native fixture source")
        locations.append([fid, sid])
    if locations != expected_locations:
        raise ValueError("native SQLite proof set differs from actual emitted CLI evidence")


def check_readonly(audit: dict, expected_snapshot: dict):
    before, after = audit["database_before"], audit["database_after"]
    check_snapshot(before)
    check_snapshot(after)
    if before["sqlite"] != expected_snapshot["sqlite"] or not unchanged(before, after):
        raise ValueError("CLI changed immutable SQLite content or left a write journal")
