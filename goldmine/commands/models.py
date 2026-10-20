"""Models command adapters."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from .. import __version__
from ..backends import BackendUnavailable, create_backend, probe_all
from ..config import Config, resolve_home
from ..errors import UserFacingError
from ..command_support import (EXIT_OK, EXIT_PREREQ, EXIT_RUNTIME, EXIT_USAGE, _emit_error, _emit_json, _envelope, _g, _load_config, runtime)


def _git_describe() -> str | None:
    """`git describe --tags --always --dirty` at the repo root; None outside a
    checkout (installed copies) or when git is unusable (e.g. no commits)."""
    import subprocess

    from ..backends.llama import REPO_ROOT

    if not (REPO_ROOT / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "describe", "--tags", "--always", "--dirty"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip() or None


def _sidecar_sha256(model: Path) -> tuple[str | None, str | None]:
    """(sha256, source) read from the `<model>.sha256` sidecar — NO recompute
    (version --full must stay cheap; `model verify` does the real hashing)."""
    sidecar = model.with_name(model.name + ".sha256")
    try:
        cached = json.loads(sidecar.read_text())
        return str(cached["sha256"]), "sidecar"
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None, None


def _version_full(cfg: Config) -> dict:
    from .. import migrations
    from ..backends.llama import (
        home_server_path,
        probe_version,
        resolve_model_path,
        resolve_server_path,
    )

    server, source = resolve_server_path(cfg.llama)
    model = resolve_model_path(cfg.llama)
    sha, sha_source = _sidecar_sha256(model) if model.is_file() else (None, None)
    backends = probe_all(cfg)
    return {
        "git": _git_describe(),
        "llama_server": {
            "path": str(server) if server else None,
            "source": source,
            "build": probe_version(server) if server else None,
        },
        "model": {
            "path": str(model),
            "id": model.stem,
            "sha256": sha,
            "sha256_source": sha_source,
            "present": model.is_file(),
        },
        "schema_version_supported": migrations.TARGET,
        "backends": {
            backend_id: bool(info.get("available"))
            for backend_id, info in sorted(backends.items())
        },
        "home": str(resolve_home(cfg)),
        "home_server_path": str(home_server_path(cfg)),
    }


def cmd_version(args: argparse.Namespace) -> int:
    import platform

    cfg = _load_config(args)
    info: dict = {
        "goldmine": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    if _g(args, "full", False):
        info.update(_version_full(cfg))
    if _g(args, "json", False):
        # F11: the standard envelope (init/open style)
        _emit_json(_envelope("version", **info))
        return EXIT_OK
    lines = [f"goldmine {info['goldmine']} (python {info['python']}, {info['platform']})"]
    if _g(args, "full", False):
        lines.append(f"git: {info.get('git') or 'not a git checkout'}")
        server = info["llama_server"]
        lines.append(
            f"llama-server: {server['path'] or 'not found'} "
            f"(source: {server['source']}, build: {server['build'] or '?'})"
        )
        model = info["model"]
        if model["sha256"]:
            lines.append(
                f"model: {model['id']} ({model['path']}) "
                f"sha256 {model['sha256'][:16]}… ({model['sha256_source']})"
            )
        else:
            lines.append(
                f"model: {model['id']} ({model['path']}) — no sidecar sha"
                if model["present"] else
                f"model: {model['id']} — file missing at {model['path']}"
            )
        lines.append(f"schema: supports up to v{info['schema_version_supported']}")
        backends = ", ".join(
            f"{bid} {'available' if ok else 'unavailable'}"
            for bid, ok in info["backends"].items()
        )
        lines.append(f"backends: {backends}")
        lines.append(f"home: {info['home']}")
    print("\n".join(lines))
    return EXIT_OK


def cmd_model(args: argparse.Namespace) -> int:
    from .. import model as model_mod

    cfg = _load_config(args)
    verb = _g(args, "model_verb")  # never None: argparse requires a verb (S3)
    json_mode = bool(_g(args, "json", False))
    manager = model_mod.ModelManager(cfg)
    try:
        if verb == "list":
            rows = manager.listing()
            print(model_mod.render_listing(rows, json_mode))
            return EXIT_OK
        if verb == "install":
            from_path = _g(args, "from_path")
            path = manager.install(
                _g(args, "name"),
                from_path=Path(from_path) if from_path else None,
                alias=_g(args, "alias"),
            )
            if json_mode:
                _emit_json({"installed": _g(args, "name"), "path": str(path)})
            else:
                print(str(path))
            return EXIT_OK
        if verb == "verify":
            results = manager.verify(_g(args, "name"))
            print(model_mod.render_verify(results, json_mode))
            failed = [r for r in results if r.get("bytes_ok") is False or r.get("sha_ok") is False]
            return EXIT_RUNTIME if failed else EXIT_OK
        if verb == "use":
            path = manager.use(_g(args, "name"))
            if json_mode:
                _emit_json({"use": _g(args, "name"), "path": str(path)})
            else:
                print(str(path))
            return EXIT_OK
        if verb == "adopt":
            path = manager.adopt(Path(_g(args, "path")).expanduser(), _g(args, "name"))
            if json_mode:
                _emit_json({"adopted": _g(args, "name"), "path": str(path)})
            else:
                print(str(path))
            return EXIT_OK
        if verb == "check":
            findings = manager.check()
            print(model_mod.render_check(findings, json_mode))
            return EXIT_OK if findings.get("ok") else EXIT_PREREQ
        # unreachable: argparse (required + choices) only passes the six verbs
        # above (S3/W2 — the old dead `unknown verb` branch is gone)
        raise model_mod.ModelError(f"unknown verb {verb!r}", usage=True)
    except model_mod.ModelError as exc:
        # v2 item 17: universal shape ("model missing" retrofit row)
        usage = bool(getattr(exc, "usage", False))
        _emit_error(UserFacingError(
            kind="model_error",
            message=f"model {verb}: {exc}",
            why=str(exc),
            fixes=[exc.fix_hint] if exc.fix_hint else ["goldmine model list"],
            doctor=not usage,  # caller errors don't need a doctor row
            exit_code=EXIT_USAGE if usage else EXIT_RUNTIME,
        ), json_mode=json_mode)
        return EXIT_USAGE if usage else EXIT_RUNTIME


def _probe_backend_live(cfg: Config, backend_id: str) -> dict:
    """--probe: a live embed round trip per backend. Spawns llama-server ONLY
    here (explicit user intent); failures mark the backend unavailable."""
    try:
        backend = create_backend(cfg, backend_id)
    except BackendUnavailable as exc:
        return {"available": False, "detail": str(exc)}
    except ValueError as exc:
        return {"available": False, "detail": str(exc)}
    try:
        backend.startup()
        vectors = backend.embed_texts(["goldmine backends --probe"])
        info = {
            "available": True,
            "dim": int(vectors.shape[1]),
            "probe": "embedded 1 text ok",
        }
        return info
    except Exception as exc:  # startup/embed failure = honest unavailability
        return {
            "available": False,
            "detail": f"probe failed: {type(exc).__name__}: {exc}",
            "error_tail": getattr(exc, "tail", "")[:2000],
        }
    finally:
        backend.close()


def cmd_backends(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    probe = bool(_g(args, "probe", False))
    infos = probe_all(cfg)
    if probe:
        for backend_id in sorted(infos):
            infos[backend_id].update((runtime().probe_backend or _probe_backend_live)(cfg, backend_id))

    json_mode = bool(_g(args, "json", False))
    if json_mode:
        # F11: the standard envelope (init/open style)
        _emit_json(_envelope("backends",
                             backends=[infos[k] for k in sorted(infos)]))
        return EXIT_OK
    for backend_id in sorted(infos):
        info = infos[backend_id]
        cap = info.get("capability") or {}
        cap_str = (
            f"text {'yes' if cap.get('text') else 'no'} · "
            f"image {'yes' if cap.get('image') else 'no'} · "
            f"audio {'yes' if cap.get('audio') else 'no'}"
        )
        line = (
            f"{backend_id:<10} {'available' if info.get('available') else 'unavailable'}"
            + (f"  dim {info['dim']}" if info.get("dim") else "")
            + f"  {cap_str}"
        )
        if info.get("server"):
            line += f"  [{info['server']}]"
        if info.get("socket"):
            line += f"  [{info['socket']}]"
        if info.get("probe"):
            line += f"  probe: {info['probe']}"
        print(line)
        if not info.get("available") and info.get("detail"):
            print(f"           {info['detail']}")
        if not info.get("available") and info.get("fix_hint"):
            print(f"           fix: {info['fix_hint']}")
    return EXIT_OK
