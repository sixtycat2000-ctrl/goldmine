"""Targeted repair adapter; --plan opens the index read-only."""
from __future__ import annotations

from contextlib import closing, nullcontext
import shlex

from ..backends import create_backend
from ..command_support import (_db_path, _emit_error, _emit_json, _envelope, _g, _gate_no_index, _load_config)
from ..errors import UserFacingError
from ..repair import execute_repairs, plan_repairs
from ..store import Store


def cmd_repair(args):
    cfg = _load_config(args)
    db, json_mode = _db_path(args, cfg), bool(_g(args, "json", False))
    refused = _gate_no_index(db, json_mode=json_mode)
    if refused is not None:
        return refused
    lexical = bool(_g(args, "lexical_only", False))
    if lexical and _g(args, "missing_vectors", False):
        raise UserFacingError(kind="invalid_params", message="--missing-vectors needs an embedding backend",
                              why="lexical-only mode does not produce vectors", fixes=["goldmine repair --help"],
                              doctor=False, exit_code=2)
    preview = bool(_g(args, "plan", False))
    with closing(create_backend(cfg, _g(args, "backend"))) if not lexical else nullcontext(None) as backend:
        with closing(Store(db, ro=preview)) as store:
            plan = plan_repairs(store, cfg, backend, _g(args, "targets", []),
                                failed_only=bool(_g(args, "failed_only", False)),
                                missing_vectors=bool(_g(args, "missing_vectors", False)))
            result = {"plan": plan, "dry_run": preview}
            if not preview:
                result.update(execute_repairs(store, cfg, backend, plan, lexical_only=lexical))
    partial = None
    if result.get("failures"):
        partial = UserFacingError(kind="repair_partial", message="repair completed with failed sources",
                                  why=result["failures"][0]["detail"],
                                  fixes=[f"goldmine repair --db {shlex.quote(str(db))}"],
                                  doctor=False, exit_code=5)
        result["error"] = partial.to_json()["error"]
    if json_mode:
        _emit_json(_envelope("repair", **result))
    elif preview:
        for item in plan:
            print(f"{item['action']}: {item['path']} — {', '.join(item['reasons'])}")
        print(f"{len(plan)} file(s) need attention")
    else:
        print(f"repair: {len(plan)} selected, {result['files_backfilled']} backfilled, "
              f"{result['vectors_added']} vectors added, {len(result['failures'])} failed")
        for failure in result["failures"]:
            print(f"failed: {failure['path']} — {failure['detail']}")
    if partial is not None:
        _emit_error(partial, json_mode=False)
    return 130 if result.get("cancelled") else (5 if result.get("failures") else 0)
