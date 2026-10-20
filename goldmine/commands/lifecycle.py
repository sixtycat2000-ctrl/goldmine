"""Lifecycle command adapters."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
from ..config import default_config_path, resolve_home
from ..command_support import (EXIT_OK, _db_path, _g, _load_config)


def cmd_init(args: argparse.Namespace) -> int:
    """`goldmine init` (03 §5.1): the resumable onboarding state machine;
    the engine lives in init.py, this stays wiring."""
    from ..init import run_init

    cfg = _load_config(args)
    config_flag = _g(args, "config")
    env_config = os.environ.get("GOLDMINE_CONFIG")
    if config_flag:
        config_path = Path(config_flag)
    elif env_config:
        config_path = Path(env_config)
    elif os.environ.get("GOLDMINE_HOME"):
        # F12 (split-brain fix): a sandboxed GOLDMINE_HOME keeps ALL of init's
        # persisted files inside the sandbox — the [init] roots record belongs
        # next to init_state.json (both under resolve_home), never in the real
        # ~/.config. Without GOLDMINE_HOME today's XDG default is preserved.
        config_path = resolve_home(cfg) / "config.toml"
    else:
        config_path = default_config_path()
    return run_init(
        cfg,
        db=_db_path(args, cfg),
        config_path=config_path,
        yes=bool(_g(args, "yes", False)),
        media_flag=bool(_g(args, "media", False)),
        model_path=_g(args, "model_path"),
        roots_arg=list(_g(args, "roots", []) or []),
        restart=bool(_g(args, "restart", False)),
        skip_demo=bool(_g(args, "skip_demo", False)),
        backend_name=_g(args, "backend"),
        json_mode=bool(_g(args, "json", False)),
        quiet=bool(_g(args, "quiet", False)),
        no_color=bool(_g(args, "no_color", False)),
    )


def cmd_serve_dispatch(args: argparse.Namespace) -> int:
    """Route the `serve` group (start / status / stop) — imported lazily so a
    serve-module problem can never break the other 13 commands."""
    from .. import serve as serve_mod

    verb = _g(args, "serve_verb")
    if verb == "status":
        return serve_mod.cmd_serve_status(args)
    if verb == "stop":
        return serve_mod.cmd_serve_stop(args)
    return serve_mod.cmd_serve(args)


def cmd_watch_dispatch(args: argparse.Namespace) -> int:
    """Route the `watch` group (start / stop) — lazy import, same shape as
    serve (R9 D4)."""
    from .. import watch as watch_mod

    return watch_mod.cmd_watch_dispatch(args)


def cmd_mcp(args: argparse.Namespace) -> int:
    import importlib.util

    from ..application import Application
    from ..backends.serve import connect_or_cold
    from ..errors import UserFacingError
    from ..command_support import _db_path, _load_config

    if not (_g(args, "db") or _g(args, "index")):
        raise UserFacingError(kind="usage_error", message="mcp needs an explicit index",
                              why="select the index whose stored evidence this session may read",
                              fixes=["goldmine mcp --db PATH"], doctor=False, exit_code=2)
    if importlib.util.find_spec("mcp") is None:
        raise UserFacingError(kind="missing_capability", message="MCP extra is not installed",
                              why="stdio MCP uses the optional official Python SDK",
                              fixes=["pip install 'goldmine[mcp]'"], doctor=False, exit_code=4)
    from importlib.metadata import version

    if version("mcp") != "2.0.0":
        raise UserFacingError(kind="missing_capability", message="incompatible MCP SDK version",
                              why="this adapter is tested with the exact optional SDK mcp==2.0.0",
                              fixes=["pip install --upgrade 'goldmine[mcp]'"], doctor=False, exit_code=4)
    cfg = _load_config(args)
    db = _db_path(args, cfg)
    if not db.is_file():
        from ..command_support import _err_no_index

        raise _err_no_index(db)
    from ..mcp_server import run_stdio

    def backend():
        instance, _ = connect_or_cold(cfg, _g(args, "backend"))
        return instance

    def reranker():
        from ..rerank import from_config

        return from_config(cfg)

    run_stdio(Application(db, backend, reranker_factory=reranker))
    return EXIT_OK
