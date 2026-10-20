"""Bounded JSON/artifact handling and honest subprocess receipts."""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


class InvalidEvidence(ValueError):
    """Missing, malformed, or inconsistent evidence (acceptance exit 2)."""


def candidate_content(root: Path) -> dict[str, str]:
    """Content identity survives commits and derived task-status updates.

    Runtime, validators, their tests/fixtures, dependency locks and workflows
    are executable candidate inputs. Progress/task metadata and report outputs
    cannot make their own post-validation state a prerequisite for validation.
    """
    files = []
    ignored = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    for name in ("goldmine", "scripts", "tests", "requirements", ".github"):
        files.extend(p for p in (root / name).rglob("*") if p.is_file()
                     and not ignored.intersection(p.parts) and p.name != ".DS_Store")
    if (root / "pyproject.toml").is_file():
        files.append(root / "pyproject.toml")
    return {str(p.relative_to(root)): sha256(p) for p in sorted(files)}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def is_sha(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        c in "0123456789abcdef" for c in value)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidEvidence(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidEvidence("nonfinite metric")
    if isinstance(value, dict):
        for item in value.values():
            finite(item)
    elif isinstance(value, list):
        for item in value:
            finite(item)


def read_json(path: Path):
    try:
        if path.stat().st_size > 64 * 1024 * 1024:
            raise InvalidEvidence(f"JSON exceeds 64MiB: {path}")
        result = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object)
        finite(result)
        return result
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidEvidence(f"cannot read JSON {path}: {exc}") from exc


def write_json(path: Path, value) -> None:
    finite(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def bound_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise InvalidEvidence("artifact needs a nonempty relative path")
    if ".." in Path(relative).parts:
        raise InvalidEvidence("artifact path traversal")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise InvalidEvidence("artifact escapes run directory")
    return path


def artifact(root: Path, path: Path) -> dict:
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    return {"path": relative, "sha256": sha256(path)}


def verify_artifact(root: Path, ref: dict) -> Path:
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
        raise InvalidEvidence("artifact must contain only path and sha256")
    path = bound_path(root, ref["path"])
    if not is_sha(ref["sha256"]) or not path.is_file() or sha256(path) != ref["sha256"]:
        raise InvalidEvidence(f"missing or changed artifact: {path}")
    return path


def external_ref(base: Path, ref: dict, *, manifest: bool = False) -> Path:
    key = "manifest_path" if manifest else "path"
    if not isinstance(ref, dict) or set(ref) != {key, "sha256"}:
        raise InvalidEvidence(f"external ref needs {key}/sha256")
    name = ref[key]
    if not isinstance(name, str) or not name or not is_sha(ref["sha256"]):
        raise InvalidEvidence(f"fill {key} and sha256 before running")
    path = Path(name)
    path = path.resolve() if path.is_absolute() else (base / path).resolve()
    if not path.is_file() or sha256(path) != ref["sha256"]:
        raise InvalidEvidence(f"missing or changed explicit input: {path}")
    return path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def capture_command(run_dir: Path, check_id: str, argv: list[str], *, cwd: Path,
                    env: dict[str, str] | None = None, identity: dict | None = None,
                    suite_id: str = "acceptance", timeout: float = 900,
                    outputs: dict[str, str] | None = None) -> dict:
    """Run explicit argv; record raw bytes and process status without trusting output."""
    if not argv or not all(isinstance(a, str) for a in argv):
        raise InvalidEvidence("command argv must be a nonempty string array")
    run_dir.mkdir(parents=True, exist_ok=False)
    run_id, started = str(uuid.uuid4()), now()
    stdout, stderr = run_dir / "stdout.log", run_dir / "stderr.log"
    timed_out = False
    with stdout.open("wb") as out, stderr.open("wb") as err:
        wall_started = time.perf_counter()
        try:
            proc = subprocess.run(argv, cwd=cwd, env=env, stdout=out, stderr=err,
                                  stdin=subprocess.DEVNULL, timeout=timeout, check=False)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            exit_code, timed_out = 124, True
        except OSError as exc:
            err.write(f"{type(exc).__name__}: {exc}\n".encode())
            exit_code = 127
        wall_ms = (time.perf_counter() - wall_started) * 1000
    visible = ("GOLDMINE_HOME", "GOLDMINE_CONFIG", "GOLDMINE_LIVE",
               "GOLDMINE_OCR_AUTOCOMPILE", "GOLDMINE_ACCEPTANCE_HERMETIC",
               "GOLDMINE_SERVE_PRESSURE_LEVEL", "GOLDMINE_MODEL", "GOLDMINE_LLAMA_SERVER",
               "GOLDMINE_SERVE", "PYTHONPATH")
    receipt = {
        "schema": 1, "run_id": run_id, "check_id": check_id, "argv": argv,
        "cwd": str(cwd.resolve()), "env": {k: (env or os.environ)[k] for k in visible
                                                  if k in (env or os.environ)},
        "started_at": started, "finished_at": now(), "exit_code": exit_code,
        "wall_ms": wall_ms,
        "timed_out": timed_out, "stdout": artifact(run_dir, stdout),
        "stderr": artifact(run_dir, stderr), "artifacts": {
            name: artifact(run_dir, bound_path(run_dir, path))
            for name, path in (outputs or {}).items() if bound_path(run_dir, path).is_file()},
    }
    write_json(run_dir / "receipt.json", receipt)
    manifest = {"schema": 1, "kind": "goldmine.command-run", "run_id": run_id,
                "check_id": check_id, "suite_id": suite_id, "identity": identity or {},
                "receipt": artifact(run_dir, run_dir / "receipt.json")}
    write_json(run_dir / "manifest.json", manifest)
    return manifest


def read_command_run(path: Path, *, check_id: str | None = None,
                     suite_id: str | None = None) -> tuple[dict, dict, Path]:
    manifest = read_json(path)
    required = {"schema", "kind", "run_id", "check_id", "suite_id", "identity", "receipt"}
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise InvalidEvidence("command manifest fields do not match schema")
    if manifest["schema"] != 1 or manifest["kind"] != "goldmine.command-run":
        raise InvalidEvidence("unsupported command manifest schema/kind")
    if check_id and manifest["check_id"] != check_id:
        raise InvalidEvidence("wrong command check_id")
    if suite_id and manifest["suite_id"] != suite_id:
        raise InvalidEvidence("wrong command suite_id")
    receipt_path = verify_artifact(path.parent, manifest["receipt"])
    receipt = read_json(receipt_path)
    keys = {"schema", "run_id", "check_id", "argv", "cwd", "env", "started_at",
            "finished_at", "exit_code", "timed_out", "stdout", "stderr", "artifacts"}
    if not isinstance(receipt, dict) or set(receipt) not in (keys, keys | {"wall_ms"}):
        raise InvalidEvidence("receipt fields do not match schema")
    if receipt["schema"] != 1 or receipt["run_id"] != manifest["run_id"]:
        raise InvalidEvidence("receipt run identity mismatch")
    if receipt["check_id"] != manifest["check_id"]:
        raise InvalidEvidence("receipt check identity mismatch")
    if type(receipt["exit_code"]) is not int or type(receipt["timed_out"]) is not bool:
        raise InvalidEvidence("invalid process status")
    if "wall_ms" in receipt and (type(receipt["wall_ms"]) not in (int, float) or receipt["wall_ms"] <= 0):
        raise InvalidEvidence("invalid subprocess wall time")
    if not isinstance(receipt["argv"], list) or not receipt["argv"] or not all(
            isinstance(a, str) for a in receipt["argv"]):
        raise InvalidEvidence("invalid receipt argv")
    for key in ("started_at", "finished_at"):
        try:
            stamp = datetime.fromisoformat(receipt[key])
            if stamp.utcoffset() is None:
                raise ValueError("timezone missing")
        except (ValueError, TypeError) as exc:
            raise InvalidEvidence("invalid receipt timestamp") from exc
    if receipt["finished_at"] < receipt["started_at"]:
        raise InvalidEvidence("receipt finishes before start")
    verify_artifact(path.parent, receipt["stderr"])
    if not isinstance(receipt["artifacts"], dict):
        raise InvalidEvidence("receipt artifacts must be a mapping")
    for ref in receipt["artifacts"].values():
        verify_artifact(path.parent, ref)
    stdout = verify_artifact(path.parent, receipt["stdout"])
    return manifest, receipt, stdout


def process_status(receipt: dict) -> tuple[str, str]:
    code = receipt["exit_code"]
    if receipt["timed_out"] or code == 127 or code == 2:
        return "MISSING", f"command incomplete (exit {code})"
    if code != 0:
        return "FAIL", f"command failed (exit {code})"
    return "PASS", "command exited 0"
