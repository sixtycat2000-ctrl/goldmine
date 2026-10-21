"""Account final capture metadata without a publication/receipt hash cycle.

Fixed-width decimal counters keep serialized sizes stable when publication is
repinned into receipt and receipt into manifest. No manifest hash is put back
into its own artifact. Whole-owner totals are observed after full publication;
actual retained control-file sizes are independently checked during replay.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import time

from .evidence import InvalidEvidence, artifact, now, read_json, verify_artifact, write_json
from .resource_policy import digest
from .resource_tools import disk_usage

NAME = "resource-publication.json"
FILES = ("manifest.json", "receipt.json", "resource-observation.json", "resource-samples.jsonl",
         "resource-bridge-policy.json", NAME, "stdout.log", "stderr.log")
COUNTERS = ("logical_bytes", "allocated_bytes", "entries", "raced_entries")


def decimal(value):
    if type(value) is not int or not 0 <= value < 10**20:
        raise InvalidEvidence("publication counter outside decimal20 range")
    return f"{value:020d}"


def number(value):
    if not isinstance(value, str) or len(value) != 20 or not value.isascii() or not value.isdigit():
        raise InvalidEvidence("publication counter must be canonical decimal20")
    return int(value)


def context(run_dir, manifest, binding):
    return {"run_id": manifest["run_id"], "check_id": manifest["check_id"], "suite_id": manifest["suite_id"],
            "run_dir": str(Path(run_dir).absolute()), "binding_sha256": digest(binding),
            "policy_sha256": digest(binding["policy"])}


def placeholder(run_dir):
    # Provisional counters cannot pass replay: actual control files are nonzero.
    write_json(run_dir / NAME, {"schema": 1, "kind": "goldmine.resource-publication", "phase": {},
               "observed_at": now(), "owner_identity": {"device": decimal(0), "inode": decimal(0)},
               "owned": {key: decimal(0) for key in COUNTERS},
               "metadata": {name: {"size": decimal(0), "allocated": decimal(0)} for name in FILES},
               "elapsed_nanoseconds": decimal(0)})


def metadata(run_dir):
    result = {}
    for name in FILES:
        path = run_dir / name
        if path.is_symlink() or not path.is_file():
            raise OSError(errno.EINVAL, "retained publication metadata missing/aliased")
        info = path.stat()
        result[name] = {"size": decimal(info.st_size), "allocated": decimal(info.st_blocks * 512)}
    return result


def publish(run_dir: Path, manifest: dict, receipt: dict, binding: dict, owner_identity: tuple[int, int]):
    """Bounded stabilization of final files; frozen receipt values never change.

    Each pass writes full provisional metadata, samples the whole owner, then
    repins fixed-length hashes. Any size/allocation drift requires another pass.
    No favorable fallback is emitted when stabilization fails.
    """
    started, deadline = time.monotonic_ns(), time.monotonic() + 2
    path, owner = run_dir / NAME, Path(binding["policy"]["owned_root"])
    row = read_json(path)
    if not isinstance(row, dict) or "metadata" not in row or "owned" not in row:
        raise InvalidEvidence("provisional resource publication is malformed")
    row["observed_at"] = now()
    row["phase"] = context(run_dir, manifest, binding)
    row["owner_identity"] = {"device": decimal(owner_identity[0]), "inode": decimal(owner_identity[1])}
    for _attempt in range(4):
        if time.monotonic() >= deadline:
            raise OSError(errno.ETIMEDOUT, "final resource publication deadline exceeded")
        # Phase context expands the initial placeholder once; subsequent values
        # have fixed width. Publish all metadata before taking the final census.
        write_json(path, row)
        receipt["artifacts"]["resource_publication"] = artifact(run_dir, path)
        write_json(run_dir / "receipt.json", receipt)
        manifest["receipt"] = artifact(run_dir, run_dir / "receipt.json")
        write_json(run_dir / "manifest.json", manifest)
        snapshot = disk_usage(owner, owner_identity, deadline)
        row["owned"] = {key: decimal(snapshot[key]) for key in COUNTERS}
        row["metadata"] = metadata(run_dir)
        row["elapsed_nanoseconds"] = decimal(time.monotonic_ns() - started)
        write_json(path, row)
        receipt["artifacts"]["resource_publication"] = artifact(run_dir, path)
        write_json(run_dir / "receipt.json", receipt)
        manifest["receipt"] = artifact(run_dir, run_dir / "receipt.json")
        write_json(run_dir / "manifest.json", manifest)
        check = disk_usage(owner, owner_identity, deadline)
        if metadata(run_dir) == row["metadata"] and all(decimal(check[key]) == row["owned"][key] for key in COUNTERS):
            return
    raise OSError(errno.EAGAIN, "final resource publication did not stabilize")


def validate(run_dir: Path, manifest: dict, receipt: dict, binding: dict):
    if "resource_publication_failure" in receipt["artifacts"]:
        raise InvalidEvidence("final resource publication failed; retained raw evidence is incomplete")
    ref = receipt["artifacts"].get("resource_publication")
    if not isinstance(ref, dict) or ref.get("path") != NAME:
        raise InvalidEvidence("complete final resource publication absent")
    row = read_json(verify_artifact(run_dir, ref))
    keys = {"schema", "kind", "phase", "observed_at", "owner_identity", "owned", "metadata", "elapsed_nanoseconds"}
    if (not isinstance(row, dict) or set(row) != keys or type(row["schema"]) is not int or row["schema"] != 1
            or row["kind"] != "goldmine.resource-publication" or row["phase"] != context(run_dir, manifest, binding)):
        raise InvalidEvidence("final resource publication schema/phase mismatch")
    if not isinstance(row["owned"], dict) or set(row["owned"]) != set(COUNTERS):
        raise InvalidEvidence("final owned-output counter schema mismatch")
    owned = {key: number(row["owned"][key]) for key in COUNTERS}
    if not isinstance(row["metadata"], dict) or set(row["metadata"]) != set(FILES):
        raise InvalidEvidence("final retained metadata inventory incomplete")
    if not isinstance(row["owner_identity"], dict) or set(row["owner_identity"]) != {"device", "inode"}:
        raise InvalidEvidence("final owner identity schema mismatch")
    root = Path(binding["policy"]["owned_root"])
    info = root.stat()
    if row["owner_identity"] != {"device": decimal(info.st_dev), "inode": decimal(info.st_ino)}:
        raise InvalidEvidence("final resource owner identity changed")
    try:
        actual = metadata(run_dir)
    except OSError as exc:
        raise InvalidEvidence("retained final metadata unavailable") from exc
    if row["metadata"] != actual:
        raise InvalidEvidence("retained receipt/manifest/publication bytes differ from final census")
    for name in FILES:
        values = row["metadata"][name]
        if not isinstance(values, dict) or set(values) != {"size", "allocated"}:
            raise InvalidEvidence("invalid final metadata node")
        number(values["size"]), number(values["allocated"])
    if (sum(number(item["size"]) for item in actual.values()) > owned["logical_bytes"]
            or sum(number(item["allocated"]) for item in actual.values()) > owned["allocated_bytes"]):
        raise InvalidEvidence("final owned totals omit retained capture metadata")
    return {"owned": owned, "elapsed_seconds": number(row["elapsed_nanoseconds"]) / 1e9,
            "observed_at": row["observed_at"]}
