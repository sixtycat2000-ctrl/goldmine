"""Refuse wrong or locally altered upstream sources without resetting them."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

REVISION = "4d756bc72bf00a4aacf410ae15a2d315f3db400d"
TAG = "b11461"
PATCH_SHA256 = "33c8c4812a4307aef3f398e5ba622edcc54ef421054edd98ca0f71906b9a2d7e"


class BuildError(ValueError):
    pass


def git(source: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(source), *args], capture_output=True, check=False)
    if check and proc.returncode:
        raise BuildError(proc.stderr.decode(errors="replace").strip())
    return proc


def verify_source(source: Path, patch: Path, *, revision: str = REVISION,
                  patch_sha256: str = PATCH_SHA256) -> dict:
    patch_bytes = patch.read_bytes()
    if hashlib.sha256(patch_bytes).hexdigest() != patch_sha256:
        raise BuildError("patch SHA does not match the pinned patch")
    actual = git(source, "rev-parse", "HEAD").stdout.decode().strip()
    if actual != revision:
        raise BuildError(f"upstream revision {actual} differs from pinned {revision}")
    diff = git(source, "diff", "--binary", "HEAD").stdout
    if not diff:
        if git(source, "apply", "--check", str(patch.resolve()), check=False).returncode:
            raise BuildError("pinned patch cannot apply to clean source")
        git(source, "apply", str(patch.resolve()))
    elif git(source, "apply", "--reverse", "--check", str(patch.resolve()), check=False).returncode:
        raise BuildError("source differs from both clean and exactly patched state")
    actual_diff = git(source, "diff", "--binary", "HEAD").stdout
    if actual_diff != patch_bytes:
        raise BuildError("source has changes beyond the exact pinned patch")
    return {"revision": actual, "patch_sha256": patch_sha256,
            "patched_diff_sha256": hashlib.sha256(actual_diff).hexdigest()}


def system_dependencies(otool_output: str) -> list[str]:
    dependencies = []
    for line in otool_output.splitlines()[1:]:
        dependency = line.strip().split(" (compatibility version", 1)[0]
        if not dependency:
            continue
        if not dependency.startswith(("/usr/lib/", "/System/Library/")):
            raise BuildError(f"non-system runtime dependency omitted by static package: {dependency}")
        dependencies.append(dependency)
    if not dependencies:
        raise BuildError("otool produced no runtime dependency information")
    return dependencies
