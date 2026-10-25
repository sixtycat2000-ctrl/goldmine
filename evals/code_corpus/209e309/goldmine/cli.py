"""CLI entry, error funnel, and compatibility exports for command adapters."""
from __future__ import annotations
import argparse
import json
import logging
import os
import sqlite3
import sys
import signal
import threading
from contextlib import contextmanager
from .backends import BackendUnavailable, create_backend
from .backends.base import BackendStartupError, EmbeddingError
from .config import Config
from .contracts import ApplicationError
from .errors import UserFacingError, render_error
from .fingerprints import FingerprintMismatch
from .ingest import IngestPipeline
from .parser import BACKEND_CHOICES, _JsonAwareParser, _rewrite_models_alias, build_parser
from .render import _render_loc, render_hits, render_ingest_summary
from .command_support import (
    CommandRuntime, command_runtime, CURRENT_EXTRACTORS, STALE_SCORER_LABEL,
    _g, _db_path, _load_config,
    _emit_error, _err_backend, _err_index_corrupt, _is_corruption_error,
    _crawl_bounds, _envelope, EXIT_OK, EXIT_RUNTIME, EXIT_USAGE,
    EXIT_FINGERPRINT, EXIT_PREREQ, EXIT_PARTIAL, EXIT_INTERRUPTED,
)
from .commands import ingest as ingest_commands
from .commands.search import _search_pack_flags, cmd_search
from .commands.inspection import cmd_status, cmd_doctor, cmd_help
from .commands.models import _git_describe, _sidecar_sha256, _version_full, cmd_version, cmd_model, _probe_backend_live, cmd_backends
from .commands.distill import _after_ts, cmd_distill
from .commands.maintenance import cmd_backup, _index_files, cmd_reset, _run_reindex_ingest, cmd_reindex
from .commands.opener import _open_resolve_hit, cmd_open
from .commands.lifecycle import cmd_init, cmd_serve_dispatch, cmd_watch_dispatch, cmd_mcp

logger = logging.getLogger("goldmine.cli")


def _make_backend(cfg: Config, name: str | None):
    return create_backend(cfg, name)


def _runtime() -> CommandRuntime:
    return CommandRuntime(make_backend=_make_backend, pipeline_type=IngestPipeline,
                          render_error=render_error, probe_backend=_probe_backend_live)


def cmd_ingest(args: argparse.Namespace) -> int:
    # Preserve direct goldmine.cli.cmd_ingest callers and dependency overrides.
    with command_runtime(_runtime()):
        return ingest_commands.cmd_ingest(args)


_DISPATCH = {
    "ingest": cmd_ingest,
    "search": cmd_search,
    "status": cmd_status,
    "version": cmd_version,
    "model": cmd_model,
    "distill": cmd_distill,
    "doctor": cmd_doctor,
    "backup": cmd_backup,
    "backends": cmd_backends,
    "reset": cmd_reset,
    "reindex": cmd_reindex,
    "open": cmd_open,
    "init": cmd_init,
    "help": cmd_help,
    "serve": cmd_serve_dispatch,
    "watch": cmd_watch_dispatch,
    "mcp": cmd_mcp,
}

def setup_logging(args: argparse.Namespace) -> None:
    quiet = bool(_g(args, "quiet", False))
    verbose = _g(args, "verbose", 0) or 0
    if quiet:
        level = logging.ERROR
    elif verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    else:
        env = os.environ.get("GOLDMINE_LOG_LEVEL", "").upper()
        level = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
                 "WARNING": logging.WARNING, "ERROR": logging.ERROR}.get(
            env, logging.WARNING
        )
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger("goldmine")
    root.setLevel(logging.DEBUG)
    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(level)
    stream.setFormatter(formatter)
    root.handlers[:] = [stream]
    log_file = _g(args, "log_file")
    if log_file:
        handler = logging.FileHandler(log_file)
        handler.setLevel(logging.DEBUG)  # full-level logs to the file
        handler.setFormatter(formatter)
        root.addHandler(handler)


def _unhandled_error(exc: Exception, json_mode: bool) -> int:
    """The catch-all tail (v2 item 17 + F4c): type+message line, doctor
    pointer and report recipe on stderr; the error object on stdout in --json
    mode. `logger.exception` stays — it is the ONLY stack-trace record."""
    logger.exception("unhandled error: %s", exc)
    print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
    print("more: goldmine doctor", file=sys.stderr)
    print("report: goldmine status --json > goldmine-bug.txt", file=sys.stderr)
    if json_mode:
        print(json.dumps(UserFacingError(
            kind="internal_error",
            message=f"{type(exc).__name__}: {exc}",
            why="unhandled error — the traceback is in the log (-v/--log-file)",
            fixes=[
                "goldmine -v <command>   # re-run verbose for the traceback",
                "goldmine status --json > goldmine-bug.txt",
            ],
            doctor=True,
        ).to_json(), indent=2, ensure_ascii=False))
    return EXIT_RUNTIME


def _run(argv: list[str] | None = None) -> int:
    """Entry point; never raises, always returns an int (MODULE_MAP contract)."""
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    argv = _rewrite_models_alias(list(argv))
    _JsonAwareParser._argv = argv  # F4: --json usage errors read this
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already printed usage/errors
        return int(exc.code or 0)
    setup_logging(args)
    if args.cmd is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    json_mode = bool(_g(args, "json", False))
    try:
        return _DISPATCH[args.cmd](args)
    except UserFacingError as exc:
        _emit_error(exc, json_mode=json_mode)
        return exc.exit_code
    except ApplicationError as exc:
        if exc.kind == "invalid_index" and isinstance(exc.__cause__, sqlite3.DatabaseError):
            cfg = _load_config(args)
            if _is_corruption_error(str(exc.__cause__)):
                _emit_error(_err_index_corrupt(_db_path(args, cfg), exc.__cause__, cfg), json_mode=json_mode)
                return EXIT_RUNTIME
        _emit_error(UserFacingError(kind=exc.kind, message=str(exc), why=str(exc),
                                    fixes=["goldmine doctor", "goldmine --help"],
                                    doctor=False, exit_code=exc.exit_code), json_mode=json_mode)
        return exc.exit_code
    except FingerprintMismatch as exc:
        # v2 item 17: the fingerprint refusal in the universal shape. The
        # message carries message+remediation (from fingerprints.gate); the
        # mechanical escapes mirror _err_fingerprint's fixes. (F13: no
        # logger.error beside the shape — the shape IS the message.)
        _emit_error(UserFacingError(
            kind="fingerprint_mismatch",
            message="refused: the index was built with a different embedding fingerprint",
            why=str(exc),
            fixes=["goldmine reset --reindex", "goldmine search <query> --lexical"],
            doctor=True,
            exit_code=EXIT_FINGERPRINT,
        ), json_mode=json_mode)
        return EXIT_FINGERPRINT
    except BackendUnavailable as exc:
        _emit_error(_err_backend(
            exc,
            message="refused: no embedding backend available",
            fixes=["goldmine model check", "goldmine backends"],
            with_hint=True,
        ), json_mode=json_mode)
        return EXIT_PREREQ
    except BackendStartupError as exc:
        # startup failure is exit 2 per ARCHITECTURE §8 ("llama-server fails to
        # start -> command fails (exit 2)"), distinct from a mid-ingest crash
        # (exit 1) and from per-file failures (exit 5 partial). DEVIATION vs
        # SPEC §1.1 (2 = usage error) resolved in ARCHITECTURE's favor by the
        # build scope — kept as-is in v2 (03 §5.7: "the exit-2-vs-1 deviation
        # gets revisited in SPEC", not silently changed here).
        tail = (getattr(exc, "tail", "") or "").strip()
        why = tail.splitlines()[-1] if tail else str(exc)
        _emit_error(UserFacingError(
            kind="backend_startup_failed",
            message="backend failed to start",
            why=why,
            fixes=["goldmine model check", "goldmine backends --probe"],
            doctor=True,
            exit_code=EXIT_USAGE,
        ), json_mode=json_mode)
        return EXIT_USAGE
    except EmbeddingError as exc:
        # include the server's own error line (the 2670-token class, 04 §6.2);
        # v2.0's token guard removes the common cause
        tail = (getattr(exc, "tail", "") or "").strip()
        why = str(exc)
        if tail:
            first_line = tail.splitlines()[-1].strip()
            if first_line and first_line not in why:
                why = f"{why} — server said: {first_line}"
        _emit_error(UserFacingError(
            kind="embedding_failed",
            message="embedding backend failed mid-run",
            why=why,
            fixes=["goldmine ingest --lexical-only", "goldmine model check"],
            doctor=True,
            exit_code=EXIT_RUNTIME,
        ), json_mode=json_mode)
        return EXIT_RUNTIME
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except RuntimeError as exc:
        # index newer than this build (migrations/store refuse on open) —
        # a specific, mechanical remedy exists, so it gets the real shape
        if "newer than this build" in str(exc):
            _emit_error(UserFacingError(
                kind="index_schema_newer",
                message="index schema is newer than this build supports",
                why=str(exc),
                fixes=["pip install --upgrade goldmine"],
                doctor=False,
                exit_code=EXIT_RUNTIME,
            ), json_mode=json_mode)
            return EXIT_RUNTIME
        return _unhandled_error(exc, json_mode)  # main() never raises
    except sqlite3.DatabaseError as exc:
        # F5: a corrupt / non-SQLite --db file is a diagnosed failure with the
        # universal shape (kind index_corrupt) — never a raw traceback. Any
        # OTHER DatabaseError (locked db, logic error) falls into the shared
        # catch-all tail — main() still never raises.
        if not _is_corruption_error(str(exc)):
            return _unhandled_error(exc, json_mode)
        cfg = _load_config(args)
        _emit_error(
            _err_index_corrupt(_db_path(args, cfg), exc, cfg),
            json_mode=json_mode,
        )
        return EXIT_RUNTIME
    except Exception as exc:
        # v2 item 17: the catch-all keeps its type+message line and gains the
        # doctor pointer + a report recipe (03 §5.7 last row). F4c: in --json
        # mode the error object ALWAYS reaches stdout — even this path.
        return _unhandled_error(exc, json_mode)


@contextmanager
def _termination_interrupt():
    # Raise through the ordinary ownership contexts so a SIGTERM closes CLI
    # model children. Daemons/SDK may install their own scoped signal handling.
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)
    def terminate(signum, frame):
        raise KeyboardInterrupt("terminated")
    signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(argv: list[str] | None = None) -> int:
    with _termination_interrupt(), command_runtime(_runtime()):
        return _run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
