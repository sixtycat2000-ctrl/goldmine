"""Opt-in comparison resource contract; no imports on the default capture path."""
from __future__ import annotations

import hashlib
import json
import platform
import sys
from pathlib import Path

from .evidence import InvalidEvidence, is_sha, sha256

SCHEMA = 1
LIMITS = {"owned_bytes": 4 * 1024**3, "stdout_bytes": 16 * 1024**2,
          "stderr_bytes": 16 * 1024**2, "nofile": 512}
PHASES = {"prepare": 1800, "query": 600}
MODULES = ("__init__.py", "evidence.py", "owned_process.py", "resource_policy.py",
           "resource_observer.py", "resource_tools.py", "resource_bridge.py", "resource_validation.py", "resource_publication.py")
TOOLS = {"ps": "/bin/ps", "lsof": "/usr/sbin/lsof"}
OUTPUTS = {"resource_observation": "resource-observation.json", "resource_samples": "resource-samples.jsonl",
           "resource_publication": "resource-publication.json"}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def policy(phase: str, owned_root: Path, *, source: dict, immutable_external: list[dict] | None = None,
           fixture_limits: dict | None = None) -> dict:
    """Register the whole writable owner, exact source context, and fixed formal caps.

    Fixture overrides are conspicuous and can never satisfy formal acceptance.
    External input digests are declarations bound by the caller's source context;
    this observer does not claim sandboxing or audit writes outside owned_root.
    """
    if not isinstance(phase, str) or phase not in PHASES:
        raise InvalidEvidence("resource phase must be prepare or query")
    result = {"schema": SCHEMA, "mode": "comparison", "phase": phase,
              "owned_root": str(Path(owned_root).absolute()), "source": source,
              "immutable_external": immutable_external or [], "limits": dict(LIMITS),
              "wall_seconds": PHASES[phase], "interval_seconds": .2, "tool_timeout_seconds": 1.0,
              "coverage": "sampled-private-session-v1"}
    if fixture_limits is not None:
        if not isinstance(fixture_limits, dict) or not set(fixture_limits) <= set(LIMITS) | {"wall_seconds", "interval_seconds"}:
            raise InvalidEvidence("unknown fixture resource override")
        result["mode"] = "fixture"
        for key, value in fixture_limits.items():
            if key in LIMITS:
                result["limits"][key] = value
            else:
                result[key] = value
    validate_policy(result)
    return result


def validate_policy(value: dict) -> None:
    keys = {"schema", "mode", "phase", "owned_root", "source", "immutable_external", "limits",
            "wall_seconds", "interval_seconds", "tool_timeout_seconds", "coverage"}
    if not isinstance(value, dict) or set(value) != keys or type(value["schema"]) is not int or value["schema"] != 1:
        raise InvalidEvidence("resource policy fields/schema mismatch")
    if not isinstance(value["phase"], str) or not isinstance(value["mode"], str) or value["phase"] not in PHASES or value["mode"] not in {"comparison", "fixture"}:
        raise InvalidEvidence("resource policy mode/phase mismatch")
    if not isinstance(value["owned_root"], str):
        raise InvalidEvidence("resource owner must be an absolute string")
    root = Path(value["owned_root"])
    if not root.is_absolute() or str(root.absolute()) != value["owned_root"] or ".." in root.parts:
        raise InvalidEvidence("resource owner must be an absolute lexical path")
    if not isinstance(value["source"], dict) or not value["source"]:
        raise InvalidEvidence("exact nonempty resource source context required")
    try:
        encoded = json.dumps(value["source"], ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > 1024 * 1024:
            raise ValueError("source context exceeds1MiB")
        digest(value["source"])
    except (TypeError, ValueError) as exc:
        raise InvalidEvidence("resource source context must be bounded finite JSON") from exc
    limits = value["limits"]
    if not isinstance(limits, dict) or set(limits) != set(LIMITS):
        raise InvalidEvidence("resource limits fields mismatch")
    for key, cap in limits.items():
        if type(cap) is not int or cap <= 0 or cap > LIMITS[key]:
            raise InvalidEvidence("resource limit must be positive and no larger than formal limit")
    if limits["nofile"] < 16:
        raise InvalidEvidence("fixture fd limit too small for bridge startup")
    for key, ceiling in (("wall_seconds", PHASES[value["phase"]]), ("interval_seconds", .2)):
        number = value[key]
        if type(number) not in (int, float) or not 0 < number <= ceiling:
            raise InvalidEvidence("resource interval/deadline outside fixed bounds")
    if value["mode"] == "comparison" and (limits != LIMITS or value["wall_seconds"] != PHASES[value["phase"]]
                                          or value["interval_seconds"] != .2):
        raise InvalidEvidence("formal policy cannot override fixed limits/cadence")
    if value["tool_timeout_seconds"] != 1.0 or value["coverage"] != "sampled-private-session-v1":
        raise InvalidEvidence("unsupported resource observation method")
    refs = value["immutable_external"]
    if not isinstance(refs, list):
        raise InvalidEvidence("immutable external inputs must be explicit refs")
    paths = []
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"path", "sha256", "kind"}:
            raise InvalidEvidence("external resource input needs path/sha256/kind")
        if not isinstance(ref["path"], str) or not isinstance(ref["kind"], str):
            raise InvalidEvidence("external resource path/kind must be strings")
        path = Path(ref["path"])
        if not path.is_absolute() or ".." in path.parts or path.is_relative_to(root) or ref["kind"] not in {"file", "tree"} or not is_sha(ref["sha256"]):
            raise InvalidEvidence("invalid external resource declaration")
        paths.append(str(path))
    if len(set(paths)) != len(paths):
        raise InvalidEvidence("duplicate external resource input")


def runtime_identity() -> dict:
    package = Path(__file__).resolve().parent
    executable = Path(sys.executable).resolve()
    return {"schema": 1, "platform": sys.platform, "release": platform.release(),
            "machine": platform.machine(), "python": {"path": str(executable), "sha256": sha256(executable),
            "version": sys.version}, "modules": {name: sha256(package / name) for name in MODULES},
            "tools": {name: {"path": path, "sha256": sha256(Path(path))} for name, path in TOOLS.items()}}


def bridge_argv(spec: Path, start_fd: int, ready_fd: int, inner_argv: list[str], *, runtime: dict | None = None) -> list[str]:
    python = (runtime or runtime_identity())["python"]["path"]
    bridge = str(Path(__file__).resolve().with_name("resource_bridge.py"))
    return [python, "-I", "-S", "-B", bridge, "--policy", str(spec), "--start-fd", str(start_fd),
            "--ready-fd", str(ready_fd), "--", *inner_argv]


def check_run_location(run_dir: Path, value: dict) -> None:
    root = Path(value["owned_root"])
    if not root.exists() or not root.is_dir() or root.resolve() != root:
        raise InvalidEvidence("resource owner must be an existing nonsymlink directory")
    if not run_dir.absolute().is_relative_to(root) or run_dir.absolute() == root:
        raise InvalidEvidence("resource run must be below the whole owner directory")
    for parent in (run_dir, *run_dir.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise InvalidEvidence("symlink resource run path")
