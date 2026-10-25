"""Strict local references and the fixed comparison scope."""
from __future__ import annotations

import os
from pathlib import Path
import stat

from answer_eval.protocol import EvaluationError, canonical_sha256, fields, parse_json, sha, sha256_file
from search_comparison.source_views.sources import read_bounded
from product_eval.schema import CLASSES

SUITE = "comparison.engine-capture/v1"
VIEWS = ("native", "common")
MANDATORY = ("goldmine.fast", "goldmine.quality-rerank", "qmd.search", "qmd.vsearch", "qmd.query")
CONTROLS = ("goldmine.lexical-control", "goldmine.quality-multi")
OPTIONAL = ()
LIMITS = {"prepare_wall_s": 1800, "query_wall_s": 600, "stream_bytes": 16 * 1024 * 1024,
          "owned_bytes": 4 * 1024 ** 3, "nofile": 512}
MAX_ROWS = 100


def read(path, limit=8 * 1024 * 1024):
    return parse_json(read_bounded(path, limit).decode("utf-8"), "comparison local JSON")


def local_path(name):
    if not isinstance(name, str) or not name or "\x00" in name or not Path(name).is_absolute():
        raise EvaluationError("explicit absolute local path required", status="MISSING")
    supplied = Path(name).absolute()
    if supplied != supplied.resolve():
        raise EvaluationError("comparison inputs must be canonical without symlink aliases", status="MISSING")
    return supplied


def reference(ref, *, bound=None, outside=()):
    fields(ref, {"path", "sha256"}, "local file reference")
    sha(ref["sha256"], "local file SHA")
    path = local_path(ref["path"])
    if any(path.is_relative_to(Path(root).resolve()) for root in outside):
        raise EvaluationError("dependency aliases a protected input/output tree", status="MISSING")
    if not path.is_file() or bound is not None and path.stat().st_size > bound or sha256_file(path) != ref["sha256"]:
        raise EvaluationError("missing/changed/oversized pinned local file", status="MISSING")
    return path


def inventory(root, *, symlinks=False, modes=False):
    """Hash regular bytes and explicit in-tree links; never follow outside roots."""
    root = local_path(str(root))
    if not root.is_dir():
        raise EvaluationError("pinned directory is absent", status="MISSING")
    result = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        row = {}
        if stat.S_ISLNK(mode):
            if not symlinks or not path.resolve().is_relative_to(root) or not path.resolve().exists():
                raise EvaluationError("directory contains an unsafe/unregistered link", status="MISSING")
            row = {"kind": "symlink", "target": os.readlink(path)}
        elif stat.S_ISREG(mode):
            row = {"kind": "file", "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        elif stat.S_ISDIR(mode):
            if not modes:
                continue
            row = {"kind": "directory"}
        else:
            raise EvaluationError("directory contains a special node", status="MISSING")
        if modes:
            row["mode"] = stat.S_IMODE(mode)
        result[path.relative_to(root).as_posix()] = row
    return result


def directory(ref):
    fields(ref, {"path", "files", "sha256"}, "pinned dependency directory")
    sha(ref["sha256"], "dependency directory SHA")
    path = local_path(ref["path"])
    actual = inventory(path, symlinks=True, modes=True)
    if not actual or actual != ref["files"] or canonical_sha256(actual) != ref["sha256"]:
        raise EvaluationError("dependency directory inventory/permissions changed", status="MISSING")
    return path


def resource(ref, *, executable=False, fixture=False):
    required = {"path", "sha256", "bytes", "provenance", "license"}
    fields(ref, required | ({"probe"} if executable else set()), "engine resource")
    path = reference({k: ref[k] for k in ("path", "sha256")})
    if type(ref["bytes"]) is not int or ref["bytes"] <= 0 or path.stat().st_size != ref["bytes"]:
        raise EvaluationError("resource byte identity changed", status="MISSING")
    reference(ref["provenance"], bound=8 * 1024 * 1024)
    reference(ref["license"], bound=8 * 1024 * 1024)
    if executable and not os.access(path, os.X_OK):
        raise EvaluationError("engine/runtime is not executable", status="MISSING")
    if fixture and executable and ref["probe"] is not None:
        raise EvaluationError("mechanical probes must be explicitly null")
    return path


def expected_arms(value, *, fixture):
    if not isinstance(value, list) or not value or any(not isinstance(v, str) for v in value) or len(value) != len(set(value)):
        raise EvaluationError("arm registry is not an ordered unique list")
    if set(value) - set(MANDATORY + CONTROLS + OPTIONAL):
        raise EvaluationError("unregistered comparison mode")
    if not fixture and (value[:len(MANDATORY + CONTROLS)] != list(MANDATORY + CONTROLS)
                        or value[len(MANDATORY + CONTROLS):] not in ([], list(OPTIONAL))):
        raise EvaluationError("real capture requires fixed mandatory modes and separate controls")
    return tuple(value)
