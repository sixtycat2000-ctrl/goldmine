"""Fixed hosted-CI identities and fail-closed archive boundaries (no network)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import unicodedata
import zipfile
import zlib

from .evidence import InvalidEvidence, candidate_content, sha256

API_VERSION = "2026-03-10"
WORKFLOW = ".github/workflows/ci.yml"
MAX_ZIP_BYTES = 2 * 1024**3
MAX_EXPANDED_BYTES = 4 * 1024**3
MAX_ENTRIES = 100_000


@dataclass(frozen=True)
class Job:
    artifact: str
    profile: str
    os: str
    python: str
    label: str
    step: str


JOBS = {
    **{f"base-{os_}-{py}": Job(f"base-{os_}-{py}", "base", system, py, os_, "Hermetic source checks")
       for os_, system in (("ubuntu-latest", "Linux"), ("macos-14", "Darwin"))
       for py in ("3.12", "3.13")},
    "native-macos-arm64": Job("native-macos-arm64", "native", "Darwin", "3.12", "macos-14", "Native source checks"),
    **{f"optional-{extra}": Job(f"optional-{extra}", extra, "Darwin", "3.12", "macos-14", "SDK protocol checks")
       for extra in ("mcp", "foresight")},
    "package-install": Job("package-install", "package", "Darwin", "3.12", "macos-14", "Portable package proof"),
}


def selection(value):
    if not isinstance(value, dict) or set(value) != {"repository", "run_id", "run_attempt", "head_sha"}:
        raise InvalidEvidence("name repository, run_id, run_attempt and full head_sha explicitly")
    if not isinstance(value["repository"], str) or not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", value["repository"]) or ".." in value["repository"]:
        raise InvalidEvidence("repository must be an explicit github.com OWNER/REPO")
    if any(type(value[k]) is not int or value[k] < 1 for k in ("run_id", "run_attempt")):
        raise InvalidEvidence("run ID and attempt must be positive integers")
    if not isinstance(value["head_sha"], str) or not re.fullmatch("[0-9a-f]{40}", value["head_sha"]):
        raise InvalidEvidence("head_sha must be the full committed SHA")
    return dict(value)


def git(root, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    process = subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, check=False)
    if process.returncode:
        raise InvalidEvidence("readable committed Git source is required")
    return process.stdout


def committed_source(root):
    """Reports may be untracked outputs; every tracked byte must be committed."""
    from .runner import code_identity

    root = Path(root).resolve()
    if git(root, "rev-parse", "--show-toplevel").decode().strip() != str(root):
        raise InvalidEvidence("source root must be the actual repository root")
    if git(root, "diff", "--binary", "HEAD"):
        raise InvalidEvidence("hosted evidence cannot cover dirty tracked source")
    identity = code_identity(root)
    if identity["dirty_diff_sha256"] is not None or identity["untracked_sha256"]:
        raise InvalidEvidence("hosted evidence requires clean committed executable source")
    tracked = set(git(root, "ls-files", "-z").decode().split("\0"))
    if not set(candidate_content(root)).issubset(tracked):
        raise InvalidEvidence("executable source includes uncommitted/ignored files")
    if any((root / name).is_symlink() for name in tracked if name):
        raise InvalidEvidence("hosted source must not depend on checkout symlink targets")
    return {"identity": identity, "workflow_sha256": sha256(root / WORKFLOW),
            "targets_sha256": sha256(root / "evals/acceptance_targets.json"),
            "inventory_sha256": sha256(root / "evals/acceptance_test_inventory.json")}


def hosted_context(profile, root, *, env=None):
    """A local run is not a hosted run. CI captures its own immutable run identity."""
    env = os.environ if env is None else env
    if env.get("GITHUB_ACTIONS") != "true":
        return None
    selected = selection({"repository": env.get("GITHUB_REPOSITORY"),
        "run_id": _number(env.get("GITHUB_RUN_ID")), "run_attempt": _number(env.get("GITHUB_RUN_ATTEMPT")),
        "head_sha": env.get("GITHUB_SHA")})
    key = env.get("GOLDMINE_CI_JOB_KEY")
    if key not in JOBS or JOBS[key].profile != profile:
        raise InvalidEvidence("hosted profile lacks its workflow-defined expanded job key")
    expected = JOBS[key]
    observed = {"os": platform.system(), "arch": platform.machine(), "python": platform.python_version()}
    if observed["os"] != expected.os or not observed["python"].startswith(expected.python + "."):
        raise InvalidEvidence("actual runner OS/Python differs from the fixed matrix")
    arch = "ARM64" if observed["arch"].lower() in {"arm64", "aarch64"} else "X64" if observed["arch"].lower() in {"amd64", "x86_64"} else None
    if env.get("RUNNER_ARCH") != arch or arch != ("ARM64" if expected.os == "Darwin" else "X64"):
        raise InvalidEvidence("actual architecture differs from the declared hosted runner")
    if env.get("RUNNER_OS") != ("macOS" if expected.os == "Darwin" else "Linux"):
        raise InvalidEvidence("runner environment OS differs from the actual process")
    source = committed_source(root)
    if selected["head_sha"] != source["identity"]["git_commit"]:
        raise InvalidEvidence("GITHUB_SHA differs from the actual clean checkout")
    if env.get("GITHUB_WORKSPACE") != str(Path(root).resolve()):
        raise InvalidEvidence("runner workspace differs from the actual checkout")
    return {"schema": 1, **selected, "job_key": key, "github_job": env.get("GITHUB_JOB"),
            "workspace": str(Path(root).resolve()), "runner": observed, "source": source}


def _number(value):
    return int(value) if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value) else None


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError("timezone missing")
        return result
    except (AttributeError, ValueError, TypeError) as exc:
        raise InvalidEvidence("explicit timezone timestamp required") from exc


def within(start, finish, job):
    start, finish = timestamp(start), timestamp(finish)
    # GitHub fields have second resolution; no inferred/repaired chronology.
    if not timestamp(job["started_at"]) - timedelta(seconds=3) <= start <= finish <= timestamp(job["completed_at"]) + timedelta(seconds=3):
        raise InvalidEvidence("raw evidence was not produced inside this actual job interval")


def _zip_index(path, *, max_archive=MAX_ZIP_BYTES, max_expanded=MAX_EXPANDED_BYTES,
             max_member=MAX_ZIP_BYTES, max_entries=MAX_ENTRIES):
    """Validate the entire central directory before creating any output paths."""
    if path.stat().st_size > max_archive:
        raise InvalidEvidence("artifact ZIP exceeds the fixed 2GiB input bound")
    seen, files, spellings, total = set(), set(), {}, 0
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if not entries or len(entries) > max_entries:
            raise InvalidEvidence("artifact ZIP has zero/too many entries")
        for row in entries:
            if row.orig_filename != row.filename:
                raise InvalidEvidence("ZIP original filename differs from its decoded canonical name")
            name = row.filename
            stripped = name[:-1] if row.is_dir() else name
            parts = PurePosixPath(stripped).parts
            if (not stripped or name != unicodedata.normalize("NFC", name) or "\\" in name or "\x00" in name or ":" in name or
                    name.startswith("/") or not parts or "/".join(parts) != stripped or
                    any(p in {".", ".."} or p.endswith((".", " ")) for p in parts)):
                raise InvalidEvidence("unsafe or noncanonical ZIP path")
            key = stripped.casefold()
            for i in range(1, len(parts) + 1):
                prefix = "/".join(parts[:i])
                folded = prefix.casefold()
                if folded in spellings and spellings[folded] != prefix:
                    raise InvalidEvidence("case-ambiguous ZIP ancestor path")
                spellings[folded] = prefix
            if key in seen:
                raise InvalidEvidence("duplicate or case-ambiguous ZIP path")
            seen.add(key)
            mode = row.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if kind not in ({0, stat.S_IFDIR} if row.is_dir() else {0, stat.S_IFREG}):
                raise InvalidEvidence("ZIP symlinks/special files are forbidden")
            if row.flag_bits & 1 or row.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise InvalidEvidence("encrypted/unsupported ZIP encoding")
            if not row.is_dir():
                if row.file_size > max_member or row.file_size > max(1, row.compress_size) * 2000:
                    raise InvalidEvidence("ZIP member exceeds the fixed expansion bound")
                files.add(key)
                total += row.file_size
        conflicts = any("/".join(path.split("/")[:i]) in files
                        for path in seen for i in range(1, len(path.split("/"))))
        if total > max_expanded or conflicts:
            raise InvalidEvidence("ZIP expansion/path-prefix conflict")
    return entries


def safe_zip(path, **limits):
    try:
        return _zip_index(path, **limits)
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        raise InvalidEvidence("malformed or corrupt artifact ZIP") from exc


def _extract_zip(path, destination):
    entries = safe_zip(path)
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(path) as archive:
        for row in entries:
            target = destination.joinpath(*PurePosixPath(row.filename).parts)
            if row.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(row) as source, target.open("xb") as output:
                    while data := source.read(1024 * 1024):
                        output.write(data)


def extract_zip(path, destination):
    try:
        return _extract_zip(path, destination)
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        raise InvalidEvidence("corrupt artifact ZIP data/CRC") from exc
