"""Offline assembly of an explicit runtime, wheelhouse and reviewed native asset."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile

from acceptance_lib.evidence import capture_command, read_command_run, sha256, write_json
from acceptance_lib.runner import code_identity
from distribution_lib import inspect_wheel
from native_build_lib.archive import open_native_archive
from native_build_lib.source import PATCH_SHA256, REVISION, BuildError, system_dependencies
from .install import NATIVE, inventory


def build(root: Path, runtime: Path, wheel: Path, wheelhouse: Path, native_tar: Path, out: Path) -> Path:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise BuildError("standalone builds require macOS arm64")
    if (runtime / "pyvenv.cfg").exists() or not (runtime / "bin/python3").is_file():
        raise BuildError("provide a complete relocatable Python runtime, not a venv")
    runtime_files = inventory(runtime)
    if out.exists() and any(out.iterdir()):
        raise BuildError("use a new or empty standalone output directory")
    info = inspect_wheel(wheel)
    expected = {str(p.relative_to(root)): sha256(p) for p in (root / "goldmine").rglob("*.py")}
    if info["source_modules"] != expected:
        raise BuildError("wheel is not the current source snapshot")
    out.mkdir(parents=True, exist_ok=True)
    before = code_identity(root, out)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "DYLD_", "GOLDMINE_", "PIP_"))}
    env.update({"PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    count = 0

    def command(argv: list[str], cwd: Path) -> str:
        nonlocal count
        count += 1
        run = out / "commands" / f"{count:03}"
        capture_command(run, f"standalone.build.{count:03}", argv, cwd=cwd,
                        env=env, identity=before, suite_id="standalone.v1", timeout=300)
        _, receipt, stdout = read_command_run(run / "manifest.json")
        if receipt["exit_code"]:
            raise BuildError(f"standalone build command failed; see {run}")
        return stdout.read_text()

    with tempfile.TemporaryDirectory(prefix=".standalone-", dir=out) as temporary:
        stage = Path(temporary) / "goldmine"
        stage.mkdir()
        native = stage / "native"
        with open_native_archive(native_tar) as archive:
            members = archive.getmembers()
            names = {*NATIVE, "native-manifest.json", "LLAMA-LICENSE.txt"}
            if len(members) != len(names) or {m.name for m in members} != names or any(not m.isfile() for m in members):
                raise BuildError("native asset must contain the five declared regular files")
            archive.extractall(native, filter="data")
        metadata = json.loads((native / "native-manifest.json").read_text())
        if (metadata.get("release_eligible") is not True or metadata.get("architecture") != "arm64"
                or metadata.get("revision") != REVISION or metadata.get("patch_sha256") != PATCH_SHA256
                or set(metadata.get("files", {})) != names - {"native-manifest.json"}):
            raise BuildError("native asset has wrong lineage or incomplete manifest")
        for name, pin in metadata["files"].items():
            if sha256(native / name) != pin:
                raise BuildError(f"native SHA mismatch: {name}")
        for key, source in (("ocr_source_sha256", "tools/ocr/ocr_helper.swift"),
                            ("embed_source_sha256", "tools/embed/goldmine-embed.c")):
            if metadata.get(key) != sha256(root / source):
                raise BuildError(f"native helper does not match current source: {source}")
        for name in NATIVE:
            if command(["/usr/bin/lipo", "-archs", str(native / name)], root).strip() != "arm64":
                raise BuildError("native architecture is not arm64")
            system_dependencies(command(["/usr/bin/otool", "-L", str(native / name)], root))
            load = command(["/usr/bin/otool", "-l", str(native / name)], root)
            minimum = re.search(r"cmd LC_BUILD_VERSION\s+cmdsize \d+\s+platform \d+\s+minos ([\d.]+)"
                                r"|cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version ([\d.]+)", load)
            if not minimum or tuple(map(int, (minimum[1] or minimum[2]).split(".")[:2])) > (14, 0):
                raise BuildError(f"{name} must be built for macOS14.0 or earlier; set MACOSX_DEPLOYMENT_TARGET=14.0")
            command(["/usr/bin/codesign", "--verify", "--strict", str(native / name)], root)

        python_root = stage / "python"
        shutil.copytree(runtime, python_root, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "site-packages"))
        # This copy becomes an application-owned runtime. Never alter the input
        # uv installation or bypass protection for the user's system Python.
        (python_root / "lib/python3.12/EXTERNALLY-MANAGED").unlink(missing_ok=True)
        python = python_root / "bin/python3"
        provenance = json.loads(command([str(python), "-I", "-B", "-c",
            "import json,platform,sys; print(json.dumps({'version':platform.python_version(),"
            "'machine':platform.machine(),'prefix':sys.prefix}))"], stage))
        if provenance["machine"] != "arm64" or not provenance["version"].startswith("3.12."):
            raise BuildError("standalone runtime must be CPython3.12 arm64")
        command([str(python), "-I", "-B", "-m", "ensurepip"], stage)
        command([str(python), "-I", "-B", "-m", "pip", "install", "--no-compile", "--no-index",
                 "--find-links", str(wheelhouse), "--constraint", str(root / "requirements/standalone.lock"),
                 str(wheel) + "[mcp]", "certifi"], stage)
        command([str(python), "-I", "-B", "-m", "pip", "check"], stage)
        installed = json.loads(command([str(python), "-I", "-B", "-c",
            "import importlib.metadata,json; import goldmine,mcp,numpy,ssl,sqlite3; "
            "print(json.dumps({d.metadata['Name']:d.version for d in importlib.metadata.distributions()}))"], stage))
        ca = Path(command([str(python), "-I", "-B", "-c", "import certifi; print(certifi.where())"], stage).strip())
        shutil.copyfile(ca, stage / "cacert.pem")
        # Host-specific shebangs from pip are not part of the public command surface.
        for path in (python_root / "bin").iterdir():
            if path.name not in ("python", "python3", "python3.12"):
                path.unlink()
        for cache in python_root.rglob("__pycache__"):
            shutil.rmtree(cache)
        shutil.copyfile(root / "scripts/standalone/install.py", stage / "install.py")
        (stage / "bin").mkdir()
        shutil.copyfile(root / "scripts/standalone/launcher.sh", stage / "bin/goldmine")
        (stage / "bin/goldmine").chmod(0o755)
        for name in ("LICENSE", "README.md"):
            shutil.copyfile(root / name, stage / name)
        (stage / "docs").mkdir()
        shutil.copyfile(root / "docs/INSTALL.md", stage / "docs/INSTALL.md")
        write_json(stage / "bundle-manifest.json", {"kind": "goldmine.standalone.v1", "version": info["version"],
                   "platform": "macos-arm64", "minimum_macos": "14", "python_version": provenance["version"],
                   "wheel_sha256": info["sha256"], "native_sha256": sha256(native_tar),
                   "dependencies": installed, "source_commit": before["git_commit"], "files": inventory(stage)})
        # Execute after moving the payload so build paths cannot accidentally satisfy imports.
        moved = Path(temporary) / "relocated payload"
        stage.rename(moved)
        command([str(moved / "bin/goldmine"), "--version"], moved)
        asset = out / "goldmine-macos-arm64.tar.gz"
        with tarfile.open(asset, "w:gz", compresslevel=6) as archive:
            archive.add(moved, arcname="goldmine", recursive=True)
    after = code_identity(root, out)
    if before != after:
        raise BuildError("source changed during standalone build")
    if inventory(runtime) != runtime_files:
        raise BuildError("input Python runtime changed during build")
    write_json(out / "manifest.json", {"kind": "goldmine.standalone-build.v1", "before": before, "after": after,
               "asset": {"path": asset.name, "sha256": sha256(asset)}, "wheel_sha256": info["sha256"],
               "native_sha256": sha256(native_tar), "runtime_files": runtime_files})
    (out / "SHA256SUMS").write_text(f"{sha256(asset)}  {asset.name}\n")
    shutil.copyfile(root / "install.sh", out / "install.sh")
    (out / "install.sh").chmod(0o755)
    return asset
