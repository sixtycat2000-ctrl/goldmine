"""Immutable numpy generations and atomic manifest publication.

Readers never unlink or mutate a generation. The manifest is the only replaceable
file, so an existing mmap remains valid when a later build is published.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid

import numpy as np

from .deadline import check_deadline

DIMENSION = 768
BLOCK_ROWS = 8192
ARRAYS = {"matrix": ("<f4", (DIMENSION,)), "ids": ("<i8", ()),
          "file_ids": ("<i8", ()), "modalities": ("<U5", ())}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            check_deadline()
            digest.update(block)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_generation(root: Path, rows, count: int) -> tuple[str, dict]:
    """Stream a pinned SQLite cursor, without materializing the full matrix."""
    root.mkdir(parents=True, exist_ok=True)
    generation = "g-" + uuid.uuid4().hex
    temporary = root / (".building-" + generation)
    temporary.mkdir()
    arrays = {}
    try:
        for name, (dtype, tail) in ARRAYS.items():
            arrays[name] = np.lib.format.open_memmap(temporary / (name + ".npy"), mode="w+",
                                                    dtype=dtype, shape=(count, *tail))
        offset = 0
        while batch := rows.fetchmany(BLOCK_ROWS):
            check_deadline()
            if any(row[2] not in ("text", "image", "audio", "none") for row in batch):
                raise ValueError("vector cache cannot encode an unknown modality")
            matrix = np.frombuffer(b"".join(row[3] for row in batch), dtype="<f2").astype("<f4")
            matrix = matrix.reshape(len(batch), DIMENSION)
            if not np.isfinite(matrix).all():
                raise ValueError("nonfinite SQLite vectors cannot form a vector cache")
            end = offset + len(batch)
            if end > count:
                raise ValueError("vector count changed inside a supposedly pinned snapshot")
            arrays["matrix"][offset:end] = matrix
            arrays["ids"][offset:end] = [row[0] for row in batch]
            arrays["file_ids"][offset:end] = [row[1] for row in batch]
            arrays["modalities"][offset:end] = [row[2] for row in batch]
            offset = end
        if offset != count:
            raise ValueError("vector cache is incomplete")
        descriptors = {}
        for name, array in arrays.items():
            array.flush()
            path = temporary / (name + ".npy")
            _fsync_file(path)
            stat = path.stat()
            descriptors[name] = {"name": path.name, "dtype": array.dtype.str,
                                 "shape": list(array.shape), "bytes": stat.st_size,
                                 "mtime_ns": stat.st_mtime_ns, "sha256": file_sha256(path)}
        # No live writable mmap may outlast the builder.
        for array in arrays.values():
            array._mmap.close()
        arrays.clear()
        _fsync_directory(temporary)
        temporary.rename(root / generation)
        _fsync_directory(root)
        return generation, descriptors
    finally:
        for array in arrays.values():
            array._mmap.close()
        # An unpublished complete generation may remain after a crash. It is
        # harmless and intentionally subject to manual cleanup, like old ones.
        if temporary.exists():
            shutil.rmtree(temporary)


def publish_manifest(root: Path, manifest: dict) -> str:
    temporary = root / (".manifest-" + uuid.uuid4().hex)
    encoded = (json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        check_deadline()
        os.replace(temporary, root / "manifest.json")
        _fsync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(encoded).hexdigest()


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate vector-cache manifest key")
        result[key] = value
    return result


def read_manifest(root: Path) -> dict:
    if root.is_symlink() or (root / "manifest.json").is_symlink():
        raise ValueError("vector cache must not follow symbolic links")
    with (root / "manifest.json").open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("oversized vector-cache manifest")
    value = json.loads(raw, object_pairs_hook=_unique_pairs,
                       parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    if not isinstance(value, dict):
        raise ValueError("vector-cache manifest must be an object")
    return value


def read_arrays(root: Path, manifest: dict, *, verify_checksums=False) -> dict:
    generation, count = manifest["generation"], manifest["rows"]
    if not isinstance(generation, str) or not re.fullmatch(r"g-[a-f0-9]{32}", generation):
        raise ValueError("unsafe vector-cache generation")
    directory = root / generation
    if directory.is_symlink() or set(manifest["arrays"]) != set(ARRAYS):
        raise ValueError("vector-cache generation/arrays invalid")
    loaded = {}
    try:
        for name, (dtype, tail) in ARRAYS.items():
            spec = manifest["arrays"][name]
            if set(spec) != {"name", "dtype", "shape", "bytes", "mtime_ns", "sha256"}:
                raise ValueError("vector-cache array descriptor invalid")
            shape = [count, *tail]
            if spec["name"] != name + ".npy" or spec["dtype"] != dtype or spec["shape"] != shape:
                raise ValueError("vector-cache array contract mismatch")
            path = directory / spec["name"]
            stat = path.stat()
            if path.is_symlink() or stat.st_size != spec["bytes"] or stat.st_mtime_ns != spec["mtime_ns"]:
                raise ValueError("vector-cache file stamp/length mismatch")
            # Every small identity array is checksummed. For the large matrix,
            # normal reads validate its immutable file stamp, header and size;
            # explicit verification also hashes the entire matrix.
            if (name != "matrix" or verify_checksums) and file_sha256(path) != spec["sha256"]:
                raise ValueError("vector-cache checksum mismatch")
            array = np.load(path, mmap_mode="r", allow_pickle=False)
            loaded[name] = array
            if (not isinstance(array, np.memmap) or array.dtype != np.dtype(dtype)
                    or list(array.shape) != shape or not array.flags.c_contiguous
                    or array.offset + array.nbytes != stat.st_size):
                raise ValueError("vector-cache numpy header/length mismatch")
            after = path.stat()
            if (stat.st_ino, stat.st_size, stat.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError("vector-cache file changed while opening")
        previous = 0
        for offset in range(0, count, BLOCK_ROWS):
            check_deadline()
            ids = loaded["ids"][offset:offset + BLOCK_ROWS]
            if (len(ids) and (ids[0] <= previous or np.any(np.diff(ids) <= 0))
                    or np.any(loaded["file_ids"][offset:offset + BLOCK_ROWS] <= 0)
                    or not np.isin(loaded["modalities"][offset:offset + BLOCK_ROWS], ["text", "image", "audio", "none"]).all()):
                raise ValueError("vector-cache row identities invalid")
            if len(ids):
                previous = ids[-1]
        return loaded
    except BaseException:
        for array in loaded.values():
            if isinstance(array, np.memmap):
                array._mmap.close()
        raise
