"""Maintenance command adapters."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from ..backends import BackendUnavailable
from ..config import Config, resolve_home
from ..errors import UserFacingError
from ..ingest import IngestPipeline
from ..render import render_ingest_summary
from ..store import Store
from ..util import human_size
from ..command_support import (EXIT_INTERRUPTED, EXIT_OK, EXIT_PARTIAL, EXIT_PREREQ, EXIT_RUNTIME, EXIT_USAGE, _db_path, _emit_error, _emit_ingest_partial, _emit_json, _envelope, _err_backend, _err_no_index, _g, _index_roots, _load_config, _make_backend)


def cmd_reindex(args: argparse.Namespace) -> int:
    """Explicit stored-text upgrade. Never constructs a model or scans roots."""
    from contextlib import closing
    from ..store import Store

    cfg = _load_config(args)
    db = _db_path(args, cfg)
    json_mode = bool(_g(args, "json", False))
    component = _g(args, "component", "lexical")
    enable, disable = bool(_g(args, "enable_cache", False)), bool(_g(args, "disable_cache", False))
    if component != "vectors" and (enable or disable):
        raise UserFacingError(kind="invalid_params", message="cache flags need reindex vectors",
                              why="lexical reindex does not manage vector caches",
                              fixes=["goldmine reindex --help"], doctor=False, exit_code=EXIT_USAGE)
    if not db.is_file():
        _emit_error(_err_no_index(db), json_mode=json_mode)
        return EXIT_PREREQ
    with closing(Store(db)) as store:
        if component == "vectors":
            from ..vector_cache import build_vector_cache, set_vector_cache_enabled

            if disable:
                set_vector_cache_enabled(store, False)
                report = {"status": "DISABLED", "enabled": False}
            else:
                report = build_vector_cache(store)
                if enable:
                    set_vector_cache_enabled(store, True)
                    report["enabled"] = True
        else:
            report = store.reindex_lexical()
    if json_mode:
        _emit_json({"component": component, **report})
    else:
        print(f"{component} derived data updated (no model or source reads)")
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return EXIT_OK

import logging
logger = logging.getLogger("goldmine.cli")


def cmd_backup(args: argparse.Namespace) -> int:
    """goldmine backup (05 §4.8): VACUUM INTO a verified single-file copy.
    Restore = stop anything using the index, copy the file back."""
    from .. import migrations

    cfg = _load_config(args)
    db = _db_path(args, cfg)
    if not db.exists():
        _emit_error(
            UserFacingError(
                kind="no_index",
                message=f"no index at {db} — nothing to back up",
                why=f"no index file at {db}",
                fixes=["goldmine ingest <roots>"],
                doctor=False,
                exit_code=EXIT_PREREQ,
            ),
            json_mode=bool(_g(args, "json", False)),
        )
        return EXIT_PREREQ
    out_arg = _g(args, "out")
    keep = _g(args, "keep", 5)
    keep = 5 if keep is None else keep
    try:
        info = migrations.create_backup(
            db,
            out=Path(out_arg).expanduser() if out_arg else None,
            keep=keep,
            home=resolve_home(cfg),
        )
    except (RuntimeError, OSError) as exc:
        # (F13: the shape below is the message — no duplicate logger line)
        _emit_error(UserFacingError(
            kind="backup_failed",
            message="backup failed",
            why=str(exc),
            fixes=["goldmine backup --out <path>", "goldmine doctor --deep"],
            doctor=True,
            exit_code=EXIT_RUNTIME,
        ), json_mode=bool(_g(args, "json", False)))
        return EXIT_RUNTIME
    if bool(_g(args, "json", False)):
        # F11: the standard envelope (init/open style)
        _emit_json(_envelope("backup", **info))
    else:
        print(
            f"backup: {info['path']} ({human_size(info['bytes'])}) — "
            f"integrity {info['integrity']}"
        )
        pruned = info.get("pruned") or []
        if pruned:
            print(f"pruned {len(pruned)} older backup(s) beyond keep {keep}")
    return EXIT_OK


def _index_files(db: Path) -> list[Path]:
    return [db, Path(str(db) + "-wal"), Path(str(db) + "-shm")]


def cmd_reset(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    db = _db_path(args, cfg)
    yes = bool(_g(args, "yes", False))
    reindex = bool(_g(args, "reindex", False))
    json_mode = bool(_g(args, "json", False))

    if reindex:
        if not db.exists():
            # S1 (v2 item 17 sweep): the missing-index refusal gets the
            # universal shape — exit 4 like every other no-index path, with
            # the error object on stdout in --json mode
            _emit_error(_err_no_index(db), json_mode=json_mode)
            return EXIT_PREREQ
        store = Store(db)
        try:
            roots = _index_roots(store)
            if not roots:
                # S2: same shape — the remedies are the point of the message
                _emit_error(UserFacingError(
                    kind="reset_no_roots",
                    message="reset --reindex needs stored roots "
                            "(no `roots_json` in meta)",
                    why=f"the index at {db} has no recorded ingest roots",
                    fixes=["goldmine ingest <roots>", "goldmine reset --yes"],
                    doctor=False,
                    exit_code=EXIT_USAGE,
                ), json_mode=json_mode)
                return EXIT_USAGE
        finally:
            store.close()
        logger.info("reset: rebuilding %d recorded root(s)", len(roots))
        return _run_reindex_ingest(
            cfg, db, roots,
            backend_name=_g(args, "backend"),
            json_mode=json_mode,
        )

    targets = [p for p in _index_files(db) if p.exists()]
    if not targets:
        print(f"nothing to delete (no index at {db})")
        return EXIT_OK
    if not yes:
        size = sum(p.stat().st_size for p in targets)
        print(
            f"would delete {len(targets)} file(s): "
            + ", ".join(str(p) for p in targets)
            + f" ({human_size(size)})"
        )
        if sys.stdin.isatty():
            answer = input(f"delete index at {db}? [y/N] ")
            if answer.strip().lower() in ("y", "yes"):
                yes = True
        else:
            # v2 item 17: the "already exemplary" reset refusal promoted to
            # the universal shape (still exit 2, still nothing deleted); the
            # error object reaches stdout in --json mode too
            _emit_error(UserFacingError(
                kind="reset_needs_confirmation",
                message="reset refused: deleting the index needs confirmation",
                why="non-interactive session (stdin is not a TTY)",
                fixes=[f"goldmine reset --yes --db {db}"],
                doctor=False,
                exit_code=EXIT_USAGE,
            ), json_mode=json_mode)
            return EXIT_USAGE
    if yes:
        for path in targets:
            path.unlink()
            logger.info("reset: deleted %s", path)
        print(f"deleted {len(targets)} file(s) — index at {db} is gone")
    return EXIT_OK


def _run_reindex_ingest(cfg: Config, db: Path, roots: list[str], *,
                        backend_name: str | None, json_mode: bool) -> int:
    """reset --reindex step 2: fresh ingest over the stored roots."""
    from ..ingest import IngestPipeline

    backend = None
    try:
        backend = _make_backend(cfg, backend_name)
    except BackendUnavailable as exc:
        _emit_error(_err_backend(
            exc,
            message="reset --reindex: no backend — existing index retained",
            fixes=["goldmine model check", "goldmine ingest --lexical-only"],
        ), json_mode=json_mode)
        return EXIT_PREREQ
    store = Store(db)
    try:
        pipeline = IngestPipeline(cfg, store, backend)
        include_code = store.db.execute("SELECT 1 FROM files WHERE kind='code' AND capability='content_indexed' LIMIT 1").fetchone() is not None
        report = pipeline.run(list(roots), reindex=True, code=include_code)
        if not report.cancelled and not report.failed:
            with store.write_transaction():
                store.db.execute("DELETE FROM card_chunks")
                store.db.execute("DELETE FROM cards")
                store.db.execute("DELETE FROM distill_runs")
                store.db.execute("DELETE FROM distill_views")
                store.set_meta("cards_v2_rebuild_note", None)
        print(render_ingest_summary(report, json_mode=json_mode))
        if report.cancelled:
            return EXIT_INTERRUPTED
        if report.failed >= 1:
            # S5: same honesty as `ingest` — exit 5 with the ingest_partial
            # shape, not a bare exit code
            _emit_ingest_partial(report, json_mode)
            return EXIT_PARTIAL
        return EXIT_OK
    finally:
        store.close()
        backend.close()
