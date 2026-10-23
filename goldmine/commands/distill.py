"""Distill command adapters."""
from __future__ import annotations
import argparse
from dataclasses import dataclass
import json
import sys
import time
from pathlib import Path
from ..backends import BackendUnavailable
from ..errors import UserFacingError
from ..store import Store
from ..command_support import (EXIT_OK, EXIT_PREREQ, EXIT_RUNTIME, EXIT_USAGE, _db_path, _emit_error, _err_backend, _fingerprint_refusal, _g, _gate_no_index, _load_config, _make_backend, _parse_kinds)

import logging
logger = logging.getLogger("goldmine.cli")


def _after_ts(date_str: str) -> float:
    """YYYY-MM-DD (local midnight) -> epoch seconds for the --after mtime
    floor (§6.2). argparse restricts nothing; a bad date is a usage error."""
    try:
        import calendar

        return float(calendar.timegm(time.strptime(date_str, "%Y-%m-%d")))
    except ValueError as exc:
        raise UserFacingError(
            kind="usage_error",
            message=f"distill: bad --after date {date_str!r} (want YYYY-MM-DD)",
            why=str(exc),
            fixes=["goldmine distill --after 2024-10-01"],
            doctor=False,
            exit_code=EXIT_USAGE,
        )


@dataclass(frozen=True)
class DistillOptions:
    scope: object
    threshold: float | None
    min_members: int
    max_chunks: int
    limit_cards: int


def _resolve_options(store, cfg, args, db):
    from ..distill import Scope

    threshold = _g(args, "threshold")
    min_members = _g(args, "min_members")
    min_members = cfg.distill.min_members if min_members is None else min_members
    max_chunks = _g(args, "max_chunks")
    max_chunks = cfg.distill.max_chunks if max_chunks is None else max_chunks
    limit_cards = _g(args, "limit_cards")
    limit_cards = 20 if limit_cards is None else limit_cards
    if _g(args, "rerun"):
        view = store.get_view(_g(args, "rerun"))
        if view is None:
            raise UserFacingError(kind="view_not_found",
                message=f"distill: no saved view named {_g(args, 'rerun')!r}",
                why=f"distill_views has no row {str(_g(args, 'rerun'))!r} in {db}",
                fixes=[f"goldmine distill --as {args.rerun} --under <path>", "goldmine distill --preview"],
                doctor=False, exit_code=EXIT_RUNTIME)
        scope_data, params = json.loads(view.scope_json), json.loads(view.params_json)
        scope = Scope(kinds=set(scope_data["kinds"]) if scope_data.get("kinds") else None,
                      under=scope_data.get("under"), after=scope_data.get("after"))
        if threshold is None:
            threshold = params.get("threshold")
        if _g(args, "min_members") is None:
            min_members = params.get("min_members", min_members)
    else:
        under = [str(Path(value).expanduser()) for value in (_g(args, "under") or [])]
        after = _g(args, "after")
        scope = Scope(kinds=_parse_kinds(_g(args, "kinds")), under=under or None,
                      after=_after_ts(after) if after else None)
    if threshold is None:
        threshold = cfg.distill.threshold
    return DistillOptions(scope, threshold, min_members, max_chunks, limit_cards)


def _verify_fingerprint(store, cfg, args, json_mode):
    if store.meta("fingerprint_id") is None:
        return None
    try:
        backend = _make_backend(cfg, _g(args, "backend"))
    except BackendUnavailable as exc:
        _emit_error(_err_backend(exc,
            message="distill refused: cannot verify the index fingerprint without a backend",
            fixes=["goldmine model check"]), json_mode=json_mode)
        return EXIT_PREREQ
    try:
        return _fingerprint_refusal(store, backend, command="distill", json_mode=json_mode)
    finally:
        backend.close()


def cmd_distill(args: argparse.Namespace) -> int:
    from .. import distill
    from ..distill import DistillRefused, run_distill

    cfg = _load_config(args)
    db = _db_path(args, cfg)
    json_mode = bool(_g(args, "json", False))
    refused = _gate_no_index(db, json_mode=json_mode)
    if refused is not None:
        return refused
    store = Store(db)
    try:
        refused = _verify_fingerprint(store, cfg, args, json_mode)
        if refused is not None:
            return refused
        options = _resolve_options(store, cfg, args, db)
        vectors = store.counts().vectors
        if vectors < 2:
            raise UserFacingError(kind="insufficient_content",
                message=f"distill needs at least 2 vectorized chunks (index has {vectors})",
                why=f"{vectors} vectorized chunks in {db}", fixes=["goldmine ingest <roots>"],
                doctor=False, exit_code=EXIT_RUNTIME)
        view_id = distill.view_id_for(options.scope)
        if _g(args, "as_name"):
            store.upsert_view(_g(args, "as_name"), options.scope.canonical_json(), json.dumps({
                "threshold": float(options.threshold) if options.threshold is not None else None,
                "min_members": int(options.min_members)}))
        try:
            result = run_distill(store, scope=options.scope,
                threshold=float(options.threshold) if options.threshold is not None else None,
                min_members=int(options.min_members), max_chunks=int(options.max_chunks),
                refresh=bool(_g(args, "refresh", False)), preview=bool(_g(args, "preview", False)))
        except DistillRefused as exc:
            raise UserFacingError(kind="distill_refused", message=f"distill refused: {exc}",
                why=str(exc), fixes=["goldmine distill --max-chunks <N>"],
                doctor=False, exit_code=EXIT_RUNTIME) from exc
        if _g(args, "preview", False):
            return _render_preview(result, json_mode)
        return _save_and_render(store, result, options.scope, view_id, options.limit_cards, args, json_mode)
    finally:
        store.close()


def _render_preview(result, json_mode):
    from .. import distill

    # --preview writes NOTHING (no save_run, no cards mutation) — §6.1
    if json_mode:
        tau = result["tau"]
        print(json.dumps({
            "schema": "goldmine.distill-preview/v1",
            "stats": {
                k: result["stats"][k]
                for k in distill.PUBLIC_STATS_KEYS
                if k in result["stats"]
            },
            "tau_raw": tau.tau_raw,
            "tau_source": tau.tau_source,
            "fallback_reason": tau.fallback_reason,
            "quartiles": list(tau.quartiles),
            "view_id": result["stats"]["view_id"],
            "intent": result["intent"],
        }, indent=2, ensure_ascii=False))
    else:
        print(result["preview"])
    return EXIT_OK




def _save_and_render(store, result, scope, view_id, limit_cards, args, json_mode):
    from .. import distill

    cards = result["cards"]
    stats = result["stats"]
    run_row = {
        "id": stats["run_id"],
        "algo": stats["algo"],
        "tau": result["tau"].tau,
        "tau_source": stats["tau_source"],
        "scope_json": scope.canonical_json(),
        "corpus_hash": stats["corpus_hash"],
        "mode": stats["mode"],
        "stats": stats,
        "view_id": view_id,
    }
    store.save_run(run_row, cards)
    # F11: the cards-v2 rebuild note is cleared by the first successful
    # run of ANY view.
    store.set_meta("cards_v2_rebuild_note", None)

    out_arg = _g(args, "out")
    if json_mode:
        print(json.dumps(
            distill.render_json(cards[:limit_cards], stats),
            indent=2, ensure_ascii=False,
        ))
        return EXIT_OK

    header = distill.header_line(stats)
    markdown = distill.render_markdown(
        cards, header, limit_cards=limit_cards,
    )
    if out_arg:
        out = Path(out_arg).expanduser()
        # dir mode (write cards.md + cards.json): existing dir, trailing
        # slash, or an extension-less path (SPEC's `--out FILE` markdown
        # mode stays for real file paths)
        dir_mode = out.is_dir() or str(out_arg).endswith("/") or not out.suffix
        if dir_mode:
            out.mkdir(parents=True, exist_ok=True)
            md_path = out / "cards.md"
            json_path = out / "cards.json"
            md_path.write_text(markdown, encoding="utf-8")
            json_path.write_text(
                json.dumps(distill.render_json(cards, stats), indent=2,
                           ensure_ascii=False),
                encoding="utf-8",
            )
            print(
                f"wrote {md_path} and {json_path} "
                f"({stats['clusters_kept']} cards); full output above",
                file=sys.stderr,
            )
            print(markdown)  # SPEC: default output is markdown on stdout
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(markdown, encoding="utf-8")
            logger.info("distill: wrote %s", out)
    else:
        print(markdown)
    return EXIT_OK
