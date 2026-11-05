"""Second-stage installer, executed by the bundled Python, never system Python."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
NATIVE = ("llama-server", "goldmine-ocr", "goldmine-embed")
OWNER = "goldmine.standalone-install.v1"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if name == "bundle-manifest.json":
            continue
        if path.is_symlink():
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"bundle symlink escapes payload: {name}")
            result[name] = {"link": os.readlink(path)}
        elif path.is_file():
            result[name] = {"sha256": digest(path), "mode": path.stat().st_mode & 0o777}
        elif not path.is_dir():
            raise ValueError(f"unexpected payload member: {name}")
    return result


def verify_bundle(root: Path) -> tuple[dict, str]:
    manifest = root / "bundle-manifest.json"
    data = json.loads(manifest.read_text())
    if data.get("kind") != "goldmine.standalone.v1" or data.get("files") != inventory(root):
        raise ValueError("bundle contents do not match manifest")
    return data, digest(manifest)


@contextmanager
def install_lock(prefix: Path):
    prefix.mkdir(parents=True, exist_ok=True)
    with (prefix / ".install.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another Goldmine installer is running") from None
        yield


def write_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".goldmine-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def require_owned_link(path: Path, target: Path) -> None:
    if path.is_symlink() and os.readlink(path) == str(target):
        return
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to replace an existing file: {path}")


def path_block(bin_dir: Path) -> str:
    quoted = shlex.quote(str(bin_dir))
    return ("\n# >>> goldmine PATH >>>\n"
            f"case \":$PATH:\" in *:{quoted}:*) ;; *) export PATH={quoted}:\"$PATH\" ;; esac\n"
            "# <<< goldmine PATH <<<\n")


def profile_path() -> Path:
    shell = Path(os.environ.get("SHELL", "/bin/zsh")).name
    if shell == "zsh":
        return (Path(os.environ.get("ZDOTDIR", str(Path.home()))) / ".zshrc").resolve()
    if shell == "bash":
        return (Path.home() / ".bash_profile").resolve()
    raise ValueError("automatic PATH setup supports zsh/bash; use --no-path for this shell")


def prepare_model(cfg, source: Path | None) -> Path:
    from goldmine.model import ModelManager, sha256_and_size
    from goldmine.model_manifest import DEFAULT_EMBEDDING_KEY, by_key

    manager = ModelManager(cfg, err=lambda line: print(line, file=sys.stderr))
    entry = by_key(DEFAULT_EMBEDDING_KEY)
    if source is not None:
        source = source.expanduser().resolve()
        if sha256_and_size(source) != (entry.sha256, entry.bytes):
            raise ValueError("local embedding model does not match the pinned size/SHA256")
        destination = manager.install_dest(entry)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".goldmine-model-", dir=destination.parent)
            os.close(fd)
            try:
                shutil.copyfile(source, name)
                if sha256_and_size(Path(name)) != (entry.sha256, entry.bytes):
                    raise ValueError("local model changed during copy")
                os.replace(name, destination)
            finally:
                Path(name).unlink(missing_ok=True)
    return manager.install(DEFAULT_EMBEDDING_KEY)


def probe(bundle: Path, cfg, model: Path | None) -> None:
    import subprocess

    for name in NATIVE:
        subprocess.run([str(bundle / "native" / name), "--version"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
    if model is not None:
        from goldmine.backends import create_backend

        cfg.llama.server = bundle / "native/llama-server"
        cfg.llama.server_configured = True
        cfg.llama.model = model
        cfg.llama.model_configured = True
        # The core install has no mmproj; do not inherit a separate media override.
        cfg.llama.embed_helper = bundle / "native/goldmine-embed"
        cfg.llama.embed_helper_configured = True
        cfg.llama.mmproj = bundle / "no-mmproj-in-core-install"
        cfg.llama.mmproj_configured = True
        backend = create_backend(cfg, "llama")
        try:
            backend.startup()
            if backend.embed_texts(["Goldmine installation check."]).shape != (1, 768):
                raise ValueError("native embedding probe did not return 768 dimensions")
        finally:
            backend.close()


def read_state(prefix: Path) -> dict:
    state = prefix / "installation.json"
    if not state.exists():
        return {}
    data = json.loads(state.read_text())
    if data.get("owner") != OWNER or data.get("prefix") != str(prefix):
        raise ValueError("installation ownership record is invalid")
    return data


def uninstall(prefix: Path) -> None:
    state = read_state(prefix)
    if not state:
        raise ValueError(f"no managed installation at {prefix}")
    links = {Path(k): Path(v) for k, v in state["links"].items()}
    for path, target in links.items():
        require_owned_link(path, target)
    launcher = prefix / "launch"
    if launcher.exists() and digest(launcher) != state["launcher_sha256"]:
        raise ValueError(f"managed launcher was modified: {launcher}")
    for path in links:
        path.unlink(missing_ok=True)
    if state.get("profile"):
        profile = Path(state["profile"])
        if profile.exists():
            text = profile.read_bytes().decode("utf-8")
            block = state["path_block"]
            if block in text:
                write_atomic(profile, text.replace(block, "", 1), profile.stat().st_mode & 0o777)
    # Only the explicit installer-owned directories are removed. Index/config/models stay.
    current = prefix / "current"
    if current.is_symlink():
        current.unlink()
    shutil.rmtree(prefix / "releases", ignore_errors=False)
    launcher.unlink(missing_ok=True)
    (prefix / "installation.json").unlink()
    print("Goldmine uninstalled. Your indexes, configuration and models are preserved.")


def install(bundle: Path, args) -> dict:
    from goldmine.config import load

    data, identity = verify_bundle(bundle)
    prefix, bin_dir = args.prefix, args.bin_dir
    state = read_state(prefix)
    cfg = load()
    home = cfg.home.expanduser().absolute()
    if state and (state["data_home"] != str(home) or state["bin_dir"] != str(bin_dir)):
        raise ValueError("existing install uses different data/bin directories; uninstall it first")
    current = prefix / "current"
    if current.exists() or current.is_symlink():
        if not state or not current.is_symlink() or not current.resolve().is_relative_to((prefix / "releases").resolve()):
            raise ValueError(f"unmanaged current installation: {current}")
    links = {bin_dir / "goldmine": prefix / "launch"}
    links.update({home / "bin" / name: current / "native" / name for name in NATIVE})
    for path, target in links.items():
        require_owned_link(path, target)
    launcher = prefix / "launch"
    if launcher.exists() and (not state or digest(launcher) != state["launcher_sha256"]):
        raise ValueError(f"refusing to replace modified launcher: {launcher}")
    profile = Path(state["profile"]) if state.get("profile") else (None if args.no_path else profile_path())
    block = state.get("path_block") or path_block(bin_dir)
    if profile:
        content = profile.read_bytes().decode("utf-8") if profile.exists() else ""
        if "# >>> goldmine PATH >>>" in content and block not in content:
            raise ValueError(f"another Goldmine PATH block exists in {profile}")

    releases = prefix / "releases"
    releases.mkdir(exist_ok=True)
    destination = releases / f"{data['version']}-{identity[:16]}"
    if destination.exists():
        if verify_bundle(destination)[1] != identity:
            raise ValueError("installed release has conflicting contents")
    else:
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=releases) as stage:
            copy = Path(stage) / "payload"
            shutil.copytree(bundle, copy, symlinks=True)
            if verify_bundle(copy)[1] != identity:
                raise ValueError("bundle changed while copying")
            os.replace(copy, destination)

    model = None if args.skip_model else prepare_model(cfg, args.model)
    probe(destination, cfg, model)
    shell_launcher = ("#!/bin/sh\nset -eu\n"
                      'if [ -z "${SSL_CERT_FILE:-}" ]; then\n'
                      f"  export SSL_CERT_FILE={shlex.quote(str(current / 'cacert.pem'))}\nfi\n"
                      f"exec {shlex.quote(str(current / 'bin/goldmine'))} \"$@\"\n")
    # Single activation point. Prepare all fallible checks and payload first.
    created = []
    old_launcher = launcher.read_text() if launcher.exists() else None
    old_current = os.readlink(current) if current.is_symlink() else None
    old_profile = profile.read_bytes() if profile and profile.exists() else None
    try:
        write_atomic(launcher, shell_launcher, 0o755)
        for path, target in links.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.is_symlink():
                path.symlink_to(target)
                created.append(path)
        if profile:
            text = profile.read_bytes().decode("utf-8") if profile.exists() else ""
            if block not in text:
                backup = profile.with_name(profile.name + ".before-goldmine")
                if profile.exists() and not backup.exists():
                    shutil.copy2(profile, backup)
                write_atomic(profile, text + block, profile.stat().st_mode & 0o777 if profile.exists() else 0o644)
        temporary = prefix / ".current-next"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(destination.relative_to(prefix))
        os.replace(temporary, current)
        result = {"owner": OWNER, "prefix": str(prefix), "bin_dir": str(bin_dir),
                  "data_home": str(home), "version": data["version"], "bundle_sha256": identity,
                  "links": {str(k): str(v) for k, v in links.items()},
                  "launcher_sha256": digest(launcher), "profile": str(profile) if profile else None,
                  "path_block": block, "semantic_ready": model is not None}
        write_atomic(prefix / "installation.json", json.dumps(result, indent=2) + "\n")
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        if old_launcher is None:
            launcher.unlink(missing_ok=True)
        else:
            write_atomic(launcher, old_launcher, 0o755)
        if old_current is None:
            current.unlink(missing_ok=True)
        else:
            rollback = prefix / ".current-rollback"
            rollback.unlink(missing_ok=True)
            rollback.symlink_to(old_current)
            os.replace(rollback, current)
        if profile:
            if old_profile is None:
                profile.unlink(missing_ok=True)
            else:
                profile.write_bytes(old_profile)
        raise
    print(f"Goldmine {data['version']} installed ({'semantic + keyword search' if model else 'keyword only; model skipped'}).")
    print(f"Start now: {shlex.quote(str(bin_dir / 'goldmine'))} init")
    if profile:
        print(f"New terminals can use: goldmine init (PATH added to {profile})")
    print("No folders have been indexed. PDF text/audio/video tools are optional; see docs/INSTALL.md.")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=Path(os.environ.get("GOLDMINE_INSTALL_DIR", Path.home() / ".local/share/goldmine")))
    parser.add_argument("--bin-dir", type=Path, default=Path(os.environ.get("GOLDMINE_BIN_DIR", Path.home() / ".local/bin")))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--model", type=Path)
    group.add_argument("--skip-model", action="store_true")
    parser.add_argument("--no-path", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)
    args.prefix, args.bin_dir = args.prefix.expanduser().absolute(), args.bin_dir.expanduser().absolute()
    try:
        if platform.system() != "Darwin" or platform.machine() != "arm64" or int(platform.mac_ver()[0].split('.')[0]) < 14:
            raise ValueError("Goldmine standalone requires macOS 14+ on Apple Silicon")
        os.environ.setdefault("SSL_CERT_FILE", str(Path(__file__).parent / "cacert.pem"))
        with install_lock(args.prefix):
            if args.uninstall:
                uninstall(args.prefix)
            else:
                install(Path(__file__).resolve().parent, args)
        return 0
    except KeyboardInterrupt:
        print("Goldmine installation interrupted; the previous activation is retained.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Goldmine installation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
