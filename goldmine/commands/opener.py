"""Opener command adapters."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
from ..backends import BackendUnavailable
from ..backends.base import EmbeddingBackend
from ..config import Config
from ..errors import UserFacingError
from ..search import Searcher
from ..store import Store
from ..command_support import (EXIT_OK, EXIT_RUNTIME, EXIT_USAGE, _db_path, _emit_error, _emit_json, _envelope, _err_no_index, _g, _load_config, _make_backend)


def _open_resolve_hit(args: argparse.Namespace, cfg: Config, n: int, query: str):
    """Re-run the search deterministically and return (path, kind, loc) of hit
    N — avoids shell-quoting paths in widgets/scripts (03 §5.6). Degrades to
    lexical ranking when no backend is available, stated on stderr."""
    from ..search import Searcher

    db = _db_path(args, cfg)
    if not db.exists():
        raise _err_no_index(db)
    store = Store(db, ro=True)
    backend: EmbeddingBackend | None = None
    try:
        scorer = cfg.search.scorer
        try:
            backend = _make_backend(cfg, _g(args, "backend"))
        except BackendUnavailable:
            scorer = "lexical"
            print(
                "goldmine open: no backend — ranked lexically "
                "(hit order may differ from a semantic search)",
                file=sys.stderr,
            )
        # W3: the old dead `--limit` read is gone — `open` defines no such
        # flag; search deep enough for hit N, nothing more
        limit = max(n, cfg.search.limit)
        hits = Searcher(store, backend).search(query, limit=limit, scorer=scorer)
        hit = next((h for h in hits if h.rank == n), None)
        if hit is None:
            raise UserFacingError(
                kind="hit_out_of_range",
                message=f"no hit {n} (search returned {len(hits)} hit(s) for {query!r})",
                why=f"requested rank {n}, got {len(hits)} hits",
                fixes=["goldmine search <query> --limit 50", "goldmine open --path <path>"],
                doctor=False,
                exit_code=EXIT_USAGE,
            )
        return hit.path, hit.kind, hit.loc
    finally:
        if backend is not None:
            backend.close()
        store.close()


def cmd_open(args: argparse.Namespace) -> int:
    """`goldmine open` (03 §5.3): open a hit or file at its moment. Jump
    mechanism -> plain open -> printed instruction; nothing fails the
    command after a target is resolved (exit stays 0)."""
    import shlex

    from ..opener import kind_for_path, plan_open, run_plan

    cfg = _load_config(args)
    json_mode = bool(_g(args, "json", False))
    dry_run = bool(_g(args, "dry_run", False))
    hit_n = _g(args, "hit")
    query_parts = _g(args, "query")
    path = _g(args, "path")
    kind = _g(args, "kind")
    loc = _g(args, "loc")

    try:
        if hit_n is not None or query_parts:
            if hit_n is None or not query_parts:
                raise UserFacingError(
                    kind="usage",
                    message="open: --hit N and --query go together",
                    why="--hit re-runs a search; it needs the query text",
                    fixes=["goldmine open --hit 1 --query <terms>"],
                    doctor=False,
                    exit_code=EXIT_USAGE,
                )
            target_path, hit_kind, hit_loc = _open_resolve_hit(
                args, cfg, hit_n, " ".join(query_parts)
            )
            target = path or target_path
            target_kind = kind or hit_kind
            target_loc = loc if loc is not None else hit_loc
        elif path:
            target = str(Path(path).expanduser())
            target_kind = kind
            target_loc = loc
        else:
            raise UserFacingError(
                kind="usage",
                message="open: nothing to open",
                why="give --path, or --hit N with --query",
                fixes=["goldmine open --path <path>", "goldmine open --hit 1 --query <terms>"],
                doctor=False,
                exit_code=EXIT_USAGE,
            )
        if kind is None:
            target_kind = target_kind or kind_for_path(target)
        if not Path(target).exists() and not str(target).startswith(("http://", "https://")):
            raise UserFacingError(
                kind="path_not_found",
                message=f"no such file: {target}",
                why="the file is gone (moved or deleted since it was indexed?)",
                fixes=["goldmine search <terms>"],
                doctor=False,
                exit_code=EXIT_RUNTIME,
            )
    except UserFacingError as exc:
        _emit_error(exc, json_mode=json_mode)
        return exc.exit_code

    plan = plan_open(target, target_kind, target_loc)
    plan = run_plan(plan, dry_run=dry_run)
    if dry_run and plan.argv:
        print(f"would run: {shlex.join(plan.argv)}", file=sys.stderr)
    if json_mode:
        _emit_json(_envelope(
            "open",
            path=plan.path,
            kind=plan.kind,
            loc=target_loc,
            how=plan.how,
            jumped=plan.jumped,
            dry_run=dry_run,
            note=plan.note or None,
        ))
    else:
        print(plan.result_line())
    return EXIT_OK
