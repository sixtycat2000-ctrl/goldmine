"""Delivery counterexamples: wrong native lineage and missing wheel contents fail."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from acceptance_lib.evidence import read_json
from distribution_lib import REQUIRED_MODULES, extract_native, inspect_wheel
from native_build_lib.archive import open_native_archive, package_asset
from native_build_lib.source import BuildError, git, system_dependencies, verify_source


@pytest.fixture
def upstream(tmp_path):
    repo = tmp_path / "upstream"
    repo.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "fixture@invalid.example"),
                 ("config", "user.name", "Fixture")):
        git(repo, *args)
    (repo / "context.cpp").write_text("line one\nbool logits = true;\nline three\n")
    git(repo, "add", "context.cpp")
    git(repo, "commit", "-qm", "fixture source")
    revision = git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    (repo / "context.cpp").write_text("line one\nbool logits = false;\nline three\n")
    patch = tmp_path / "pinned.patch"
    patch.write_bytes(git(repo, "diff", "--binary", "HEAD").stdout)
    git(repo, "restore", "context.cpp")
    return repo, patch, revision, hashlib.sha256(patch.read_bytes()).hexdigest()


def test_exact_source_patch_is_idempotent(upstream):
    repo, patch, revision, pin = upstream
    first = verify_source(repo, patch, revision=revision, patch_sha256=pin)
    assert verify_source(repo, patch, revision=revision, patch_sha256=pin) == first
    assert first["patched_diff_sha256"] == pin


def test_wrong_revision_or_patch_pin_fails_without_reset(upstream):
    repo, patch, revision, pin = upstream
    before = (repo / "context.cpp").read_bytes()
    with pytest.raises(BuildError, match="revision"):
        verify_source(repo, patch, revision="0" * 40, patch_sha256=pin)
    with pytest.raises(BuildError, match="patch SHA"):
        verify_source(repo, patch, revision=revision, patch_sha256="0" * 64)
    assert (repo / "context.cpp").read_bytes() == before


def test_unknown_patch_and_unrelated_changes_fail(upstream):
    repo, patch, revision, pin = upstream
    verify_source(repo, patch, revision=revision, patch_sha256=pin)
    (repo / "context.cpp").write_text((repo / "context.cpp").read_text() + "unrelated change\n")
    with pytest.raises(BuildError, match="beyond|differs"):
        verify_source(repo, patch, revision=revision, patch_sha256=pin)
    git(repo, "restore", "context.cpp")
    patch.write_text(patch.read_text().replace("bool logits = true;", "unknown old code;"))
    with pytest.raises(BuildError, match="cannot apply"):
        verify_source(repo, patch, revision=revision,
                      patch_sha256=hashlib.sha256(patch.read_bytes()).hexdigest())


@pytest.mark.parametrize("dependency", ["@rpath/libllama.dylib", "@loader_path/libllama.dylib", "/opt/homebrew/lib/libssl.dylib"])
def test_native_dependency_closure_rejects_unshipped_libraries(dependency):
    with pytest.raises(BuildError, match="non-system"):
        system_dependencies("llama-server:\n\t" + dependency + " (compatibility version 1.0.0)\n")


def test_native_dependency_closure_needs_actual_system_rows():
    assert system_dependencies("server:\n\t/usr/lib/libc++.1.dylib (compatibility version 1.0.0)\n") == ["/usr/lib/libc++.1.dylib"]
    with pytest.raises(BuildError, match="no runtime"):
        system_dependencies("server:\n")


@pytest.mark.native
def test_fresh_staging_cannot_leak_old_ocr_and_dev_tar_is_rejected(tmp_path):
    source = tmp_path / "server"
    source.write_bytes(b"new executable")
    license_file = tmp_path / "LICENSE"
    license_file.write_text("fixture license")
    out = tmp_path / "dist"
    (out / "staging").mkdir(parents=True)
    (out / "staging/goldmine-ocr").write_bytes(b"old poison OCR")
    asset = package_asset(source, None, out, "dev.tar.xz", {"architecture": "arm64"}, epoch=0, license_file=license_file)
    with open_native_archive(asset) as archive:
        assert "goldmine-ocr" not in archive.getnames()
    assert read_json(out / "dev.tar.xz.manifest.json")["release_eligible"] is False
    with pytest.raises(BuildError):
        extract_native(asset, tmp_path / "extract")
    with pytest.raises(BuildError, match="already exists"):
        package_asset(source, None, out, "dev.tar.xz", {}, epoch=0, license_file=license_file)


def _wheel(path, *, omit=(), requirements=("numpy>=1.26",)):
    metadata = "Metadata-Version: 2.4\nName: goldmine\nVersion: 0.2.0\n" + "".join(f"Requires-Dist: {r}\n" for r in requirements)
    with zipfile.ZipFile(path, "w") as archive:
        for name in REQUIRED_MODULES:
            if name not in omit:
                archive.writestr(name, "# fixture module\n")
        archive.writestr("goldmine-0.2.0.dist-info/METADATA", metadata)


def test_wheel_missing_command_is_rejected_before_source_can_mask_it(tmp_path):
    wheel = tmp_path / "bad.whl"
    _wheel(wheel, omit=("goldmine/commands/search.py",))
    with pytest.raises(BuildError, match="missing required modules"):
        inspect_wheel(wheel)


def test_wheel_base_dependencies_remain_numpy_only(tmp_path):
    wheel = tmp_path / "valid.whl"
    _wheel(wheel, requirements=("numpy>=1.26", "mcp==2.0.0; extra == 'mcp'"))
    assert inspect_wheel(wheel)["version"] == "0.2.0"
    _wheel(wheel, requirements=("numpy>=1.26", "mcp==2.0.0"))
    with pytest.raises(BuildError, match="base runtime dependencies"):
        inspect_wheel(wheel)
