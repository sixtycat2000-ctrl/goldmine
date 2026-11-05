"""Installer transaction/ownership counterexamples; real artifacts get a separate smoke."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from standalone import install as installer


@pytest.fixture
def setup(tmp_path, monkeypatch):
    prefix = tmp_path / "app with spaces and 'quotes' $literal"
    bin_dir = tmp_path / "commands with spaces"
    home = tmp_path / "data"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.delenv("ZDOTDIR", raising=False)
    monkeypatch.setenv("GOLDMINE_HOME", str(home))
    monkeypatch.setenv("GOLDMINE_CONFIG", str(tmp_path / "unused.toml"))
    monkeypatch.setattr(installer, "probe", lambda *args: None)
    monkeypatch.setattr(installer, "prepare_model", lambda *args: pytest.fail("unexpected model preparation"))
    args = argparse.Namespace(prefix=prefix, bin_dir=bin_dir, skip_model=True, model=None, no_path=False)
    prefix.mkdir()
    return tmp_path, home, args


def payload(root: Path, text="first") -> Path:
    bundle = root / text
    (bundle / "bin").mkdir(parents=True)
    (bundle / "native").mkdir()
    script = bundle / "bin/goldmine"
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' {text}\n")
    script.chmod(0o755)
    for name in installer.NATIVE:
        native = bundle / "native" / name
        native.write_text(text)
    (bundle / "cacert.pem").write_text("fixture CA")
    (bundle / "bundle-manifest.json").write_text(json.dumps({"kind": "goldmine.standalone.v1",
        "version": "0.2.0", "files": installer.inventory(bundle)}))
    return bundle


def test_install_upgrade_and_uninstall_preserve_user_data_and_profile(setup):
    root, home, args = setup
    profile = root / ".zshrc"
    original = "# user settings\nexport CUSTOM=retained\n"
    profile.write_text(original)
    (home / "models").mkdir(parents=True)
    model = home / "models/user-model"
    model.write_bytes(b"retained model")
    (home / "library.sqlite").write_bytes(b"retained index")
    first, second = payload(root), payload(root, "second")
    result = installer.install(first, args)
    current = args.prefix / "current"
    first_target = current.resolve()
    cli = args.bin_dir / "goldmine"
    assert subprocess.check_output([str(cli)], text=True).strip() == "first"
    assert result["semantic_ready"] is False
    installer.install(first, args)
    assert profile.read_text().count("# >>> goldmine PATH >>>") == 1
    installer.install(second, args)
    assert current.resolve() != first_target and first_target.is_dir()
    assert subprocess.check_output([str(cli)], text=True).strip() == "second"
    installer.uninstall(args.prefix)
    assert not cli.exists() and not cli.is_symlink()
    assert not current.exists()
    assert model.read_bytes() == b"retained model"
    assert (home / "library.sqlite").read_bytes() == b"retained index"
    assert profile.read_text() == original
    assert (root / ".zshrc.before-goldmine").read_text() == original


@pytest.mark.parametrize("conflict", ["command", "native", "launcher"])
def test_foreign_files_are_never_overwritten(setup, conflict):
    root, home, args = setup
    target = {"command": args.bin_dir / "goldmine", "native": home / "bin/llama-server",
              "launcher": args.prefix / "launch"}[conflict]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("foreign")
    with pytest.raises(ValueError, match="replace"):
        installer.install(payload(root), args)
    assert target.read_text() == "foreign"
    assert not (args.prefix / "current").exists()


def test_failed_upgrade_keeps_working_old_activation(setup, monkeypatch):
    root, _, args = setup
    installer.install(payload(root), args)
    before = os.readlink(args.prefix / "current")
    state = (args.prefix / "installation.json").read_bytes()
    profile = (root / ".zshrc").read_bytes()
    monkeypatch.setattr(installer, "probe", lambda *a: (_ for _ in ()).throw(ValueError("probe failed")))
    with pytest.raises(ValueError, match="probe failed"):
        installer.install(payload(root, "second"), args)
    assert os.readlink(args.prefix / "current") == before
    assert (args.prefix / "installation.json").read_bytes() == state
    assert (root / ".zshrc").read_bytes() == profile
    assert subprocess.check_output([str(args.bin_dir / "goldmine")], text=True).strip() == "first"


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_failed_publication_rolls_back_every_owned_entry(setup, monkeypatch, failure):
    root, _, args = setup
    installer.install(payload(root), args)
    before = os.readlink(args.prefix / "current")
    original = installer.write_atomic

    def fail_state(path, text, mode=0o644):
        if path.name == "installation.json":
            raise failure("simulated state-write failure")
        return original(path, text, mode)

    monkeypatch.setattr(installer, "write_atomic", fail_state)
    with pytest.raises(failure, match="state-write"):
        installer.install(payload(root, "second"), args)
    assert os.readlink(args.prefix / "current") == before
    assert subprocess.check_output([str(args.bin_dir / "goldmine")], text=True).strip() == "first"


@pytest.mark.parametrize("damage", ["source", "symlink", "mode"])
def test_payload_tampering_and_escaping_symlinks_are_refused(setup, damage):
    root, _, args = setup
    bundle = payload(root)
    target = bundle / "bin/goldmine"
    if damage == "source":
        target.write_text("changed")
    elif damage == "mode":
        target.chmod(0o644)
    else:
        target.unlink()
        target.symlink_to(root / "outside")
    with pytest.raises(ValueError, match="manifest|escapes"):
        installer.install(bundle, args)
    assert not (args.prefix / "current").exists()


def test_corrupt_installed_release_and_modified_launcher_are_refused(setup):
    root, _, args = setup
    bundle = payload(root)
    installer.install(bundle, args)
    target = (args.prefix / "current/bin/goldmine")
    target.write_text("changed installed payload")
    with pytest.raises(ValueError, match="manifest"):
        installer.install(bundle, args)
    (args.prefix / "launch").write_text("user changed launcher")
    with pytest.raises(ValueError, match="modified"):
        installer.uninstall(args.prefix)


def test_no_path_mode_and_concurrent_installer_lock(setup):
    root, _, args = setup
    args.no_path = True
    installer.install(payload(root), args)
    assert not (root / ".zshrc").exists()
    with installer.install_lock(args.prefix):
        with pytest.raises(ValueError, match="another"):
            with installer.install_lock(args.prefix):
                pytest.fail("concurrent lock entered")


def test_symlinked_profile_keeps_target_and_original_bytes(setup):
    root, _, args = setup
    target = root / "dotfiles/zsh-config"
    target.parent.mkdir()
    original = b"# user CRLF settings\r\nexport A=1\r\n"
    target.write_bytes(original)
    profile = root / ".zshrc"
    profile.symlink_to(target)
    installer.install(payload(root), args)
    assert profile.is_symlink()
    assert target.read_bytes().startswith(original)
    installer.uninstall(args.prefix)
    assert profile.is_symlink() and target.read_bytes() == original


def test_reinstall_through_a_symlinked_parent_directory(setup):
    root, _, args = setup
    actual = root / "real-parent"
    actual.mkdir()
    alias = root / "alias-parent"
    alias.symlink_to(actual, target_is_directory=True)
    args.prefix = alias / "application"
    args.prefix.mkdir()
    bundle = payload(root)
    installer.install(bundle, args)
    installer.install(bundle, args)
    assert subprocess.check_output([str(args.bin_dir / "goldmine")], text=True).strip() == "first"


def test_build_refuses_an_escaping_runtime_before_running_it(tmp_path, monkeypatch):
    from standalone.build import build

    monkeypatch.setattr(installer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(installer.platform, "machine", lambda: "arm64")
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/python3").symlink_to(sys.executable)
    with pytest.raises(ValueError, match="escapes"):
        build(tmp_path, runtime, tmp_path / "unused.whl", tmp_path, tmp_path / "unused.tar", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_unsupported_platform_fails_before_state_mutation(setup, monkeypatch):
    _, _, args = setup
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    assert installer.main(["--prefix", str(args.prefix)]) == 1
    assert not (args.prefix / ".install.lock").exists()
