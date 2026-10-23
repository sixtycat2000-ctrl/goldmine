#!/usr/bin/env python3
"""Build pinned static llama/OCR binaries and prove runtime dependency closure."""
from __future__ import annotations

import argparse
from pathlib import Path
import os
import platform
import shutil
import subprocess
import sys
import tempfile

from acceptance_lib.evidence import capture_command, read_command_run, sha256
from native_build_lib.archive import open_native_archive, package_asset
from native_build_lib.source import BuildError, TAG, git, system_dependencies, verify_source


def build(root: Path, source: Path, out: Path, *, jobs: int = 2, dev_skip_ocr: bool = False) -> Path:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise BuildError("this native asset recipe is verified only for macOS arm64")
    if os.environ.get("LLAMA_TAG", TAG) != TAG:
        raise BuildError(f"LLAMA_TAG must remain the pinned {TAG}")
    if not (1 <= jobs <= 8):
        raise BuildError("jobs must be in1..8; default2 limits shared-host memory pressure")
    for tool in ("git", "cmake", "otool", "lipo", "codesign", "swiftc"):
        if not shutil.which(tool) and not (tool == "swiftc" and dev_skip_ocr):
            raise BuildError(f"required native build tool missing: {tool}")
    if not (source / ".git").exists():
        if source.exists() and any(source.iterdir()):
            raise BuildError("source directory is nonempty and not a Git checkout")
        source.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "1", "--branch", TAG,
                        "https://github.com/ggml-org/llama.cpp", str(source)], check=True)
    metadata = verify_source(source, root / "scripts/patches/llama.cpp-no-logits.patch")
    out.mkdir(parents=True, exist_ok=True)
    logs = Path(tempfile.mkdtemp(prefix="native-commands-", dir=out))
    count = 0
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("DYLD_")}

    def command(argv: list[str], cwd: Path = root) -> str:
        nonlocal count
        count += 1
        directory = logs / f"{count:02}"
        capture_command(directory, f"native.build.{count:02}", argv, cwd=cwd,
                        env=clean_env, identity=metadata, suite_id="native.build.v1", timeout=1800)
        _, receipt, stdout = read_command_run(directory / "manifest.json")
        if receipt["exit_code"]:
            raise BuildError(f"native command failed ({receipt['exit_code']}), logs: {directory}")
        return stdout.read_text(errors="replace") + (directory / "stderr.log").read_text(errors="replace")

    with tempfile.TemporaryDirectory(prefix=".native-work-", dir=out) as tmp:
        work = Path(tmp)
        build_dir = work / "build"
        options = ["-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=OFF",
            "-DGGML_BACKEND_DL=OFF", "-DGGML_METAL=ON", "-DGGML_METAL_EMBED_LIBRARY=ON",
            "-DLLAMA_OPENSSL=OFF", "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF",
            "-DLLAMA_BUILD_SERVER=ON", "-DLLAMA_BUILD_APP=OFF"]
        command(["cmake", "-S", str(source), "-B", str(build_dir), *options])
        command(["cmake", "--build", str(build_dir), "--target", "llama-server", "--parallel", str(jobs)])
        server = build_dir / "bin/llama-server"
        ocr = None
        if not dev_skip_ocr:
            ocr = work / "goldmine-ocr"
            command(["swiftc", "-O", str(root / "tools/ocr/ocr_helper.swift"), "-o", str(ocr)])
        dependencies = {}
        for binary in [server, *([ocr] if ocr else [])]:
            arch = command(["lipo", "-archs", str(binary)]).strip()
            if arch != "arm64":
                raise BuildError(f"asset architecture must be arm64, got {arch}")
            dependencies[binary.name] = system_dependencies(command(["otool", "-L", str(binary)]))
            command(["codesign", "--force", "--sign", "-", str(binary)])
            command(["codesign", "--verify", "--strict", str(binary)])
        version = command([str(server), "--version"])
        if not version.strip():
            raise BuildError("llama-server version output missing")
        if ocr is not None:
            if command([str(ocr), "--version"]).strip() != "goldmine-ocr 1":
                raise BuildError("unexpected OCR handshake")
        metadata.update({"tag": TAG, "architecture": "arm64", "platform": platform.platform(),
            "cmake_options": options, "jobs": jobs, "dependencies": dependencies,
            "version": version.strip(), "ocr_source_sha256": sha256(root / "tools/ocr/ocr_helper.swift"),
            "runtime_dyld_overrides": False, "command_logs": logs.name})
        epoch = int(git(source, "show", "-s", "--format=%ct", "HEAD").stdout)
        suffix = "-dev-no-ocr" if dev_skip_ocr else ""
        asset = package_asset(server, ocr, out, f"llama-server-macos-arm64-{TAG}{suffix}.tar.xz",
                              metadata, epoch=epoch, license_file=source / "LICENSE")
    import tarfile
    try:
        with tempfile.TemporaryDirectory(prefix=".native-extract-", dir=out) as tmp:
            with open_native_archive(asset) as archive:
                archive.extractall(tmp, filter="data")
            command([str(Path(tmp) / "llama-server"), "--version"], Path(tmp))
            if not dev_skip_ocr:
                command([str(Path(tmp) / "goldmine-ocr"), "--version"], Path(tmp))
    except (BuildError, OSError, tarfile.TarError):
        asset.unlink(missing_ok=True)
        (out / f"{asset.name}.manifest.json").unlink(missing_ok=True)
        raise
    return asset


def main(argv=None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=root / "vendor/llama.cpp")
    parser.add_argument("--out", type=Path, default=Path(os.environ.get("OUT_DIR", root / "dist")))
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--dev-skip-ocr", action="store_true", default=os.environ.get("GOLDMINE_SKIP_OCR") == "1")
    args = parser.parse_args(argv)
    try:
        print(build(root, args.source.resolve(), args.out.resolve(), jobs=args.jobs, dev_skip_ocr=args.dev_skip_ocr))
        return 0
    except (BuildError, OSError, subprocess.SubprocessError) as exc:
        print(f"native build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
