"""Search command adapters."""
from __future__ import annotations
import argparse
import sys
import time
from ..backends import BackendUnavailable
from ..errors import UserFacingError
from ..render import _render_loc, render_hits
from ..search import parse_filters
from ..command_support import (EXIT_OK, EXIT_PREREQ, EXIT_USAGE, STALE_SCORER_LABEL, _db_path, _emit_error, _emit_json, _err_backend, _g, _gate_no_index, _load_config, _make_backend, _parse_kinds)

import logging
logger = logging.getLogger("goldmine.cli")


def _search_pack_flags(args: argparse.Namespace) -> tuple[bool, int, int]:
    """Validate the v2.1 pack/context flags (docs/v2.1-pack-eval.md §1.2/§2.1).
    Returns (pack, context, budget). Rules: one --context knob, clamp <= 3
    (stderr notice), negative -> usage error; pack defaults to context 1,
    list/--json to 0; --budget needs --pack; --pack and --format fzf
    conflict (both exit 2)."""
    pack = bool(_g(args, "pack", False))
    fmt = _g(args, "format")
    budget_arg = _g(args, "budget")
    if budget_arg is not None and not pack:
        raise UserFacingError(
            kind="usage_error",
            message="--budget needs --pack",
            why="--budget sizes the answer-material pack; the hit list has "
                "no budget",
            fixes=["goldmine search <query> --pack --budget 4000",
                   "goldmine search --help"],
            doctor=False,
            exit_code=EXIT_USAGE,
        )
    if pack and fmt == "fzf":
        raise UserFacingError(
            kind="usage_error",
            message="--pack and --format fzf conflict",
            why="the pack is one pasted document; fzf needs one line per hit",
            fixes=["goldmine search <query> --pack",
                   "goldmine search <query> --format fzf"],
            doctor=False,
            exit_code=EXIT_USAGE,
        )
    from ..pack import clamp_budget, clamp_context

    context_arg = _g(args, "context")
    if context_arg is not None and context_arg < 0:
        raise UserFacingError(
            kind="usage_error",
            message=f"--context must be >= 0, got {context_arg}",
            why="negative neighbor radius is meaningless",
            fixes=["goldmine search <query> --context 2", "goldmine search --help"],
            doctor=False,
            exit_code=EXIT_USAGE,
        )
    context = (1 if pack else 0) if context_arg is None else clamp_context(context_arg)
    if context_arg is not None and context_arg > 3:
        print(f"--context {context_arg} clamped to {context}", file=sys.stderr)
    budget = clamp_budget(budget_arg) if budget_arg is not None else 4000
    if pack and budget_arg is not None and budget != budget_arg:
        print(f"--budget {budget_arg} clamped to {budget}", file=sys.stderr)
    return pack, context, budget


def _search_backend(cfg, args, *, semantic: bool):
    if semantic:
        from ..backends.serve import connect_or_cold

        backend, _ = connect_or_cold(cfg, _g(args, "backend"),
                                     no_serve=bool(_g(args, "no_serve", False)))
        return backend
    try:
        return _make_backend(cfg, _g(args, "backend"))
    except BackendUnavailable:
        return None


def cmd_search(args: argparse.Namespace) -> int:
    from ..application import Application
    from ..contracts import ApplicationError, SearchRequest

    cfg = _load_config(args)
    db = _db_path(args, cfg)
    json_mode = bool(_g(args, "json", False))
    refused = _gate_no_index(db, json_mode=json_mode)
    if refused is not None:
        return refused
    try:
        pack, context, budget = _search_pack_flags(args)
        query = " ".join(args.query)
        clean_query, filters = parse_filters(query)
        lexical = bool(_g(args, "lexical", False))
        scorer = _g(args, "scorer") or cfg.search.scorer
        semantic = scorer in ("hybrid", "semantic", "rrf") and not lexical
        excerpt_chars = _g(args, "excerpt_chars", 240)
        excerpt_chars = 240 if excerpt_chars is None else excerpt_chars
        why = bool(_g(args, "why", False))
        limit = _g(args, "limit")
        z_gate = _g(args, "z_gate")
        request = SearchRequest(
            query=clean_query, filters=filters, scorer=scorer,
            limit=cfg.search.limit if limit is None else limit,
            z_gate=cfg.search.z_gate if z_gate is None else z_gate,
            kinds=_parse_kinds(_g(args, "kinds")), snippet_width=excerpt_chars or None,
            why=why, pack=pack, context=context, budget=budget, lexical_only=lexical,
            mode=_g(args, "mode", "fast"), rerank=_g(args, "rerank", "off"),
            multi_query=bool(_g(args, "multi_query", False)),
        )
        started = time.monotonic()
        def reranker():
            from ..rerank import from_config

            return from_config(cfg)

        result = Application(db, lambda: _search_backend(cfg, args, semantic=semantic),
                             reranker_factory=reranker).search(request)
    except UserFacingError as exc:
        _emit_error(exc, json_mode=json_mode)
        return exc.exit_code
    except BackendUnavailable as exc:
        _emit_error(_err_backend(exc, message="search: semantic scoring needs a backend",
                                fixes=["goldmine search <query> --scorer lexical", "goldmine model check"]),
                    json_mode=json_mode)
        return EXIT_PREREQ
    except ApplicationError as exc:
        if exc.kind == "invalid_index" and exc.__cause__ is not None:
            raise exc.__cause__
        _emit_error(UserFacingError(kind=exc.kind, message=str(exc), why=str(exc),
                                    fixes=["goldmine search --help", "goldmine doctor"],
                                    doctor=False, exit_code=exc.exit_code), json_mode=json_mode)
        return exc.exit_code
    if semantic:
        print(f"via: {result.via} ({time.monotonic() - started:.2f}s)", file=sys.stderr)
    hits = result.hits
    if result.settings["mode"] == "quality":
        print("quality: unvalidated experiment" +
              (f"; fallback={result.fallback_reason}" if result.fallback_reason else ""), file=sys.stderr)
        if result.pack_meta is not None:
            result.pack_meta["settings"].update(result.settings)
            result.pack_meta["fallback_reason"] = result.fallback_reason
    if result.stale:
        for hit in hits:
            hit.scorer = STALE_SCORER_LABEL
    if pack:
        _emit_json(result.pack_meta) if json_mode else print(result.pack_text, end="")
        if not hits and not json_mode:
            print(f"nothing found for {query!r} — the pack footer says so", file=sys.stderr)
        return EXIT_OK
    return _render_results(args, result, query, filters, excerpt_chars, why, json_mode)


def _render_results(args, result, query, filters, excerpt_chars, why, json_mode):
    hits = result.hits
    if _g(args, "format") == "fzf":
        from ..output import fzf_lines

        rows = fzf_lines(hits, loc_fn=_render_loc)
        if rows:
            print("\n".join(rows))
        return EXIT_OK
    context_of = None
    if result.contexts:
        context_of = {
            rank: (value.parts if json_mode else [p for p in value.parts if p["role"] != "hit"], value.note)
            for rank, value in result.contexts.items()
        }
    rendered = render_hits(hits, json_mode=json_mode, excerpt_chars=excerpt_chars,
                           why=why, raw=bool(_g(args, "raw", False)),
                           filters=filters, context_of=context_of)
    if json_mode and result.settings["mode"] == "quality":
        import json

        rendered = json.dumps({"hits": json.loads(rendered), "settings": result.settings,
                               "fallback_reason": result.fallback_reason}, ensure_ascii=False, indent=2)
    if rendered:
        print(rendered)
    if not hits and not json_mode:
        print(f"nothing found for {query!r}")
        print("try: fewer/shorter words · drop kind:/under: filters · goldmine help search-syntax",
              file=sys.stderr)
    return EXIT_OK
