"""Fresh staging, deterministic archive metadata, and atomic local publication."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager

from acceptance_lib.evidence import sha256, write_json
from .source import BuildError


@contextmanager
def open_native_archive(path: Path):
    """Some pyenv builds lack _lzma; preserve real xz format using explicit xz."""
    try:
        archive = tarfile.open(path)
    except (tarfile.CompressionError, tarfile.ReadError):
        with path.open("rb") as stream:
            if stream.read(6) != b"\xfd7zXZ\x00":
                raise
        tool = shutil.which("xz")
        if tool is None:
            raise BuildError("xz support unavailable: install Python with lzma or prepare the xz build tool")
        with tempfile.TemporaryDirectory(prefix="goldmine-xz-") as temp:
            unpacked = Path(temp) / "archive.tar"
            with unpacked.open("wb") as stream:
                subprocess.run([tool, "--decompress", "--stdout", str(path)], stdout=stream, check=True)
            with tarfile.open(unpacked) as archive:
                yield archive
    else:
        with archive:
            yield archive


def package_asset(server: Path, ocr: Path | None, out: Path, name: str,
                  metadata: dict, *, epoch: int, license_file: Path,
                  embed: Path | None = None) -> Path:
    destination = out / name
    if destination.exists():
        raise BuildError(f"asset already exists; choose a new output directory: {destination}")
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".native-stage-", dir=out) as tmp:
        stage = Path(tmp)
        shutil.copy2(server, stage / "llama-server")
        os.chmod(stage / "llama-server", 0o755)
        if ocr is not None:
            shutil.copy2(ocr, stage / "goldmine-ocr")
            os.chmod(stage / "goldmine-ocr", 0o755)
        if embed is not None:
            # digup-plan-002: the media-embedding helper ships beside the
            # other two binaries (<home>/bin/goldmine-embed).
            shutil.copy2(embed, stage / "goldmine-embed")
            os.chmod(stage / "goldmine-embed", 0o755)
        shutil.copy2(license_file, stage / "LLAMA-LICENSE.txt")
        files = {p.name: sha256(p) for p in sorted(stage.iterdir())}
        write_json(stage / "native-manifest.json", {"schema": 1, **metadata,
                   "files": files,
                   "release_eligible": ocr is not None and embed is not None})
        uncompressed = stage / "asset.tar"
        with tarfile.open(uncompressed, "w") as tar:
            for path in sorted(stage.iterdir()):
                if path == uncompressed:
                    continue
                info = tar.gettarinfo(str(path), arcname=path.name)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = epoch
                with path.open("rb") as stream:
                    tar.addfile(info, stream)
        archive = stage / "asset.tar.xz"
        try:
            import lzma
        except ImportError:
            tool = shutil.which("xz")
            if tool is None:
                raise BuildError("native packaging needs Python lzma or the xz build tool")
            with archive.open("wb") as stream:
                subprocess.run([tool, "--stdout", str(uncompressed)], stdout=stream, check=True)
        else:
            with uncompressed.open("rb") as source, lzma.open(archive, "wb") as output:
                shutil.copyfileobj(source, output)
        with archive.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(archive, destination)
        write_json(out / f"{name}.manifest.json", {"schema": 1,
            "asset": {"path": name, "sha256": sha256(destination)},
            "native_manifest_sha256": sha256(stage / "native-manifest.json"),
            "release_eligible": ocr is not None and embed is not None, **metadata})
    return destination
