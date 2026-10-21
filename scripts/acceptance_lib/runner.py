"""Fixed acceptance execution and replayable report validation."""
from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path
import subprocess

from . import checks
from .evidence import (InvalidEvidence, artifact, candidate_content, now, read_json, sha256,
                       verify_artifact, write_json)
from .pytest_checks import preflight_pytest, run_pytest, validate_pytest
from .registry import expand, fingerprint


def parser_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(bytes.fromhex(sha256(path)))
    return digest.hexdigest()


def code_identity(root: Path, out: Path | None = None) -> dict:
    def git(*args):
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
        if proc.returncode:
            raise InvalidEvidence("acceptance source must have readable Git identity")
        return proc.stdout

    commit = git("rev-parse", "HEAD").decode().strip()
    diff = git("diff", "--binary", "HEAD")
    untracked = {}
    for raw in git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if not raw:
            continue
        relative = raw.decode()
        path = (root / relative).resolve()
        if out and path.is_relative_to(out.resolve()):
            continue
        if relative.split("/")[0] in ("reports", "dist") or relative.startswith("evals/history/"):
            continue
        if path.is_file():
            untracked[relative] = sha256(path)
    dirty = bool(diff or untracked)
    combined = diff + json.dumps(untracked, sort_keys=True).encode()
    return {"git_commit": commit, "dirty_diff_sha256": hashlib.sha256(combined).hexdigest() if dirty else None,
            "untracked_sha256": untracked, "content_sha256": candidate_content(root)}


def overall(records: list[dict]) -> str:
    if any(r["status"] == "FAIL" for r in records):
        return "FAIL"
    if not records or any(r["status"] != "PASS" for r in records):
        return "MISSING"
    return "PASS"


def exit_code(status: str) -> int:
    return {"PASS": 0, "FAIL": 1, "MISSING": 2}[status]


def _plain(check, root: Path, inputs: checks.Inputs, targets: dict) -> tuple[str, dict]:
    if check.kind == "history":
        return "PASS", checks.history(root)
    if check.kind == "ir":
        from .ir_checks import check as ir_check

        return ir_check(root, inputs, check.argument, targets)
    if check.kind == "plan":
        return "PASS", checks.plan(root, targets)
    if check.kind == "file":
        path = inputs.file(check.argument)
        return "PASS", {"path": str(path), "sha256": sha256(path)}
    if check.kind == "public_assets":
        return "PASS", checks.public_assets(root, inputs)
    if check.kind == "paired":
        return checks.paired(inputs, check.argument, targets)
    if check.kind == "latency":
        from .performance import fast

        return fast(inputs, targets)
    if check.kind == "mcp_latency":
        from .performance import mcp

        return mcp(inputs, targets)
    if check.kind == "scale":
        from .scale import check as scale

        return scale(root, inputs, targets)
    if check.kind == "distribution":
        from .delivery import distribution

        return distribution(inputs, targets)
    if check.kind == "lifecycle":
        from .lifecycle import check as lifecycle

        return lifecycle(inputs, check.argument)
    if check.kind == "ci_local":
        from .ci_evidence import check as ci_local

        return ci_local(inputs, targets)
    if check.kind == "distill":
        from .distill_evidence import check as real_distill

        return real_distill(inputs, targets)
    if check.kind == "documentation":
        from .documentation import check as documentation

        return documentation(inputs, targets)
    if check.kind == "holdout":
        from .holdout import check as holdout

        return holdout(root, inputs, targets)
    if check.kind == "unimplemented":
        return "MISSING", {"reason": f"raw evidence adapter not implemented: {check.id}"}
    raise InvalidEvidence(f"unknown registered check kind: {check.kind}")


def run(root: Path, out: Path, profile: str, *, task: str | None = None,
        inputs_path: Path | None = None, db: Path | None = None) -> dict:
    required = expand(profile, task)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise InvalidEvidence("output directory must be new or empty")
    out.mkdir(parents=True, exist_ok=True)
    targets_path = root / "evals/acceptance_targets.json"
    targets = read_json(targets_path)
    identity = code_identity(root, out)
    input_data = read_json(inputs_path) if inputs_path else None
    inputs = checks.Inputs(input_data)
    if db is not None:
        ref = inputs.get("legacy.reference_index")
        if not isinstance(ref.get("path"), str):
            raise InvalidEvidence("--db requires a pinned legacy reference index path")
        actual = (inputs.base / ref["path"]).resolve()
        if db.resolve() != actual:
            raise InvalidEvidence("--db must match explicitly pinned legacy.reference_index")
    snapshot = None
    if input_data is not None:
        write_json(out / "inputs.json", input_data)
        snapshot = artifact(out, out / "inputs.json")
    records = []
    started = now()
    for check in required:
        metadata = {}
        try:
            if check.kind == "source":
                final_identity = code_identity(root, out)
                details = {"started": identity, "finished": final_identity}
                status = "PASS" if identity["content_sha256"] == final_identity["content_sha256"] else "MISSING"
                write_json(out / "source-stability.json", details)
                metadata = {"snapshot": artifact(out, out / "source-stability.json")}
            elif check.kind == "pytest":
                directory = out / "checks" / check.id
                directory.mkdir(parents=True)
                status, details, refs = run_pytest(root, directory, check.argument, identity)
                metadata = {name: artifact(out, verify_artifact(directory, ref))
                            for name, ref in refs.items()}
            else:
                status, details = _plain(check, root, inputs, targets)
        except (InvalidEvidence, OSError, KeyError, TypeError, ValueError) as exc:
            status, details = "MISSING", {"reason": str(exc)}
            if check.kind == "pytest":
                directory = out / "checks" / check.id
                for key, name in (("command", "execution"), ("collection", "collection")):
                    path = directory / name / "manifest.json"
                    if path.is_file():
                        metadata[key] = artifact(out, path)
        record = {"id": check.id, "kind": check.kind, "status": status,
                  "details": details, "evidence": metadata,
                  "remediation": check.remediation if status != "PASS" else None}
        records.append(record)
        print(f"{check.id}: {status}", flush=True)
    result = {
        "schema": 1, "kind": "goldmine.acceptance", "profile": "local_rc" if profile == "release" else profile,
        "requested_profile": profile, "task_id": task, "started_at": started, "finished_at": now(),
        **identity, "platform": {"os": platform.system(), "arch": platform.machine(),
                                  "python": platform.python_version()},
        "registry_sha256": fingerprint(), "parser_sha256": parser_fingerprint(),
        "targets_sha256": sha256(targets_path), "inputs": snapshot,
        "required_checks": [c.id for c in required], "checks": records,
        "passed": [r["id"] for r in records if r["status"] == "PASS"],
        "failed": [r["id"] for r in records if r["status"] == "FAIL"],
        "missing": [r["id"] for r in records if r["status"] == "MISSING"],
        "status": overall(records),
    }
    write_json(out / "manifest.json", result)
    return result


def validate(root: Path, report: Path) -> dict:
    data = read_json(report)
    expected_keys = {"schema", "kind", "profile", "requested_profile", "task_id", "started_at",
        "finished_at", "git_commit", "dirty_diff_sha256", "untracked_sha256", "content_sha256", "platform",
        "registry_sha256", "parser_sha256", "targets_sha256", "inputs", "required_checks",
        "checks", "passed", "failed", "missing", "status"}
    if not isinstance(data, dict) or set(data) != expected_keys or data.get("schema") != 1 or data.get("kind") != "goldmine.acceptance":
        raise InvalidEvidence("acceptance report fields do not match schema")
    required = expand(data["requested_profile"], data["task_id"])
    canonical = "local_rc" if data["requested_profile"] == "release" else data["requested_profile"]
    if data["profile"] != canonical or data["required_checks"] != [c.id for c in required]:
        raise InvalidEvidence("required check registry mismatch")
    if data["registry_sha256"] != fingerprint() or data["parser_sha256"] != parser_fingerprint():
        raise InvalidEvidence("trusted registry/parser changed; report needs explicit recapture")
    targets_path = root / "evals/acceptance_targets.json"
    if data["targets_sha256"] != sha256(targets_path):
        raise InvalidEvidence("acceptance targets changed")
    inputs_data = read_json(verify_artifact(report.parent, data["inputs"])) if data["inputs"] else None
    inputs, targets = checks.Inputs(inputs_data), read_json(targets_path)
    records = data["checks"]
    if not isinstance(records, list) or [r.get("id") for r in records] != [c.id for c in required]:
        raise InvalidEvidence("missing, duplicate or unknown checks")
    recomputed = []
    for check, record in zip(required, records, strict=True):
        if set(record) != {"id", "kind", "status", "details", "evidence", "remediation"} or record["kind"] != check.kind:
            raise InvalidEvidence("check record fields/kind mismatch")
        try:
            if check.kind == "source":
                if set(record["evidence"]) != {"snapshot"}:
                    raise InvalidEvidence("source identity snapshot missing")
                details = read_json(verify_artifact(report.parent, record["evidence"]["snapshot"]))
                if set(details) != {"started", "finished"}:
                    raise InvalidEvidence("invalid source identity snapshot")
                recorded_start = {k: data[k] for k in ("git_commit", "dirty_diff_sha256", "untracked_sha256", "content_sha256")}
                if details["started"] != recorded_start:
                    raise InvalidEvidence("source snapshot differs from recorded run identity")
                same_content = (details["started"].get("content_sha256") ==
                                details["finished"].get("content_sha256") == candidate_content(root))
                status = "PASS" if same_content else "MISSING"
            elif check.kind == "pytest":
                preflight_pytest(root, check.argument)
                refs = record["evidence"]
                if set(refs) != {"command", "collection"}:
                    raise InvalidEvidence("pytest raw execution evidence missing")
                status, details = validate_pytest(root,
                    verify_artifact(report.parent, refs["command"]),
                    verify_artifact(report.parent, refs["collection"]), check.argument,
                    identity={k: data[k] for k in ("git_commit", "dirty_diff_sha256", "untracked_sha256", "content_sha256")})
            else:
                if record["evidence"]:
                    raise InvalidEvidence("unexpected evidence fields")
                status, details = _plain(check, root, inputs, targets)
        except (InvalidEvidence, OSError, KeyError, TypeError, ValueError) as exc:
            status, details = "MISSING", {"reason": str(exc)}
        if status != record["status"] or details != record["details"]:
            raise InvalidEvidence(f"summary does not match raw evidence: {check.id}")
        expected_remediation = check.remediation if status != "PASS" else None
        if record["remediation"] != expected_remediation:
            raise InvalidEvidence("remediation changed")
        recomputed.append({"id": check.id, "status": status})
    for key, status in (("passed", "PASS"), ("failed", "FAIL"), ("missing", "MISSING")):
        if data[key] != [r["id"] for r in recomputed if r["status"] == status]:
            raise InvalidEvidence("edited summary status list")
    if data["status"] != overall(recomputed):
        raise InvalidEvidence("edited overall status")
    return data
