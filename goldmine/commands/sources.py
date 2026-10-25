"""Source-management adapters; no filesystem deletion or implicit ingestion."""
from __future__ import annotations

from contextlib import closing

from ..command_support import (_db_path, _emit_json, _envelope, _g, _gate_no_index, _load_config)
from ..errors import UserFacingError
from ..sources import forget_sources, list_sources, restore_sources
from ..store import Store


def cmd_sources(args):
    cfg = _load_config(args)
    db, json_mode = _db_path(args, cfg), bool(_g(args, "json", False))
    refused = _gate_no_index(db, json_mode=json_mode)
    if refused is not None:
        return refused
    verb = "remove" if args.cmd == "forget" else (_g(args, "sources_verb") or "list")
    dry_run = bool(_g(args, "dry_run", False))
    try:
        with closing(Store(db, ro=verb == "list" or dry_run)) as store:
            if verb == "list":
                result = list_sources(store)
            elif verb == "restore":
                result = restore_sources(store, args.targets)
            else:
                result = forget_sources(store, args.targets, dry_run=dry_run)
    except ValueError as exc:
        raise UserFacingError(kind="invalid_params", message=str(exc), why=str(exc),
                              fixes=["goldmine sources --help"], doctor=False, exit_code=2) from exc
    if json_mode:
        _emit_json(_envelope("sources" if args.cmd != "forget" else "forget", action=verb, **result))
    elif verb == "list":
        for root in result["roots"]:
            print(f"root: {root}")
        for root in result["forgotten"]:
            print(f"forgotten: {root}")
        print(f"indexed files: {result['files']}")
    elif verb == "restore":
        print(f"restored {len(result['restored'])} scope(s); {result['next']}")
    else:
        print(f"{'would remove' if dry_run else 'removed'} {result['files_removed']} indexed file(s), "
              f"{result['cards_removed']} card(s); {'would exclude' if dry_run else 'excluded'} from future ingest/watch")
    return 0
