"""Prepare independent indexes and preserve every fixed dev query attempt."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import uuid

from answer_eval.protocol import EvaluationError, canonical_sha256, parse_json, sha256_file
from search_comparison.adapters import DocumentMap
from search_comparison.metrics import score_files
from search_comparison.source_views.run import owned_output, write_new
from .commands import Commands
from .identity import implementation
from . import indexes
from .plan import engine_resources, load_plan
from .protocol import SUITE, inventory, read
from .recipes import compile_recipe
from .results import parse_result
from .settings import setup
from .views import verify_view
from . import resources
from .native import readback as native_readback


def _artifacts(owner):
    result = inventory(owner)
    result.pop("report.json", None)
    return result


def _backup(owner, values, engine):
    src = Path(values[{"goldmine": "goldmine_db", "qmd": "qmd_db", "recoll": "recoll_db"}[engine]])
    target = owner / "prepared-index"
    if engine == "recoll":
        shutil.copytree(src, target)
        return {"path": str(target), "files": inventory(target)}
    for suffix in ("-wal", "-journal"):
        p = Path(str(src) + suffix)
        if p.exists() and p.stat().st_size:
            raise EvaluationError("prepared index is not a closed standalone snapshot", status="MISSING")
    target.mkdir()
    before = sha256_file(src)
    shutil.copyfile(src, target / "index.sqlite")
    if sha256_file(src) != before or sha256_file(target / "index.sqlite") != before:
        raise EvaluationError("index changed during prepared snapshot capture")
    return {"path": str(target), "files": inventory(target)}


def _index(engine, commands, values, view, mapping):
    if engine == "goldmine":
        return indexes.goldmine(values, view)
    if engine == "qmd":
        return indexes.qmd(commands, values, view)
    return indexes.recoll(commands, values, view, mapping)


def _prepare(engine, arm, commands, values, view, registration):
    if engine == "goldmine":
        raw = commands.call("goldmine.python-runtime", [values["python"], "-I", "-B", str(Path(__file__).with_name("_python_runtime.py"))])
        runtime = parse_json(raw.decode("utf-8"), "actual Goldmine Python runtime")
        if (runtime.get("python") != values["python"] or not Path(runtime.get("numpy", {}).get("path", "/")).is_relative_to(Path(values["python_environment"]))):
            raise EvaluationError("Goldmine imported NumPy outside the pinned numerical runtime", status="MISSING")
        native_readback(commands, values, registration)
        commands.call("goldmine.ingest", compile_recipe("goldmine.prepare", values))
    elif engine == "qmd":
        commands.call("qmd.collection-add", compile_recipe("qmd.prepare." + view["name"], values))
        if values["semantic"]:
            commands.call("qmd.embed", compile_recipe("qmd.embed", values))
    else:
        commands.call("recoll.index", compile_recipe("recoll.prepare", values))


def _metrics(request, qrel, result):
    q = {**request, "expect": [{"path": p, "grade": g} for p, g in qrel["relevance"].items()]}
    return score_files(q, result["window"]["files"])


def summarize(rows):
    groups = {"all": rows, "answerable": [r for r in rows if r["primary_class"] != "noise"], "noise": [r for r in rows if r["primary_class"] == "noise"]}
    groups.update({c: [r for r in rows if r["primary_class"] == c] for c in sorted({r["primary_class"] for r in rows})})
    result = {}
    for name, group in groups.items():
        good = [r for r in group if r["status"] == "OK"]
        metrics = ("empty_result",) if name == "noise" else ("success@1", "success@5", "MRR", "nDCG@10")
        result[name] = {"n_fixed": len(group), "n_measured": len(good), "failed_ids": [r["id"] for r in group if r["status"] != "OK"],
                        "metrics": {m: {"sum_observed": sum(r["metrics"][m] for r in good),
                             "value": sum(r["metrics"][m] for r in good) / len(group) if group and len(good) == len(group) else None,
                             "observed_only_diagnostic": sum(r["metrics"][m] for r in good) / len(good) if good else None} for m in metrics}}
    return result


def configure_arm(plan, view, arm, owner, *, replay=False):
    engine = arm.split(".")[0]
    paths = plan.engines[arm]
    values, env = setup(owner, engine, paths, view, replay=replay)
    values["semantic"] = arm in ("qmd.vsearch", "qmd.query")
    if engine == "goldmine":
        values["embedding_sha256"] = plan.value["engines"][engine]["model_roles"]["embedding"]["sha256"]
    context = {"implementation": implementation(), "plan_sha256": plan.sha256, "source_identity": plan.bundle.identity,
               "view_identity": view["identity"], "arm": arm, "engine_resources": plan.value["engines"][engine],
               "projection_identity": plan.projection["identity"], "registered_at": plan.value["registered_at"],
               "configuration_contract": resources.configuration(engine, values, view), "registered_resource_runtime": plan.value["resource_observer"],
               "input_refs": {"plan": {"path": str(plan.path), "sha256": plan.sha256}, "projection": plan.projection["reference"], "view": view["binding"]}}
    measured = resources.preflight(owner, context, env, fixture=plan.bundle.fixture)
    return values, env, context, measured


def capture_arm(plan, view, arm, owner, *, saved=None, configured=None):
    engine = arm.split(".")[0]
    paths = plan.engines[arm]
    values, env, context, preflight = configured or configure_arm(plan, view, arm, owner, replay=saved is not None)
    mapping = DocumentMap(view["corpus"], view["documents"], collection="sources", index="comparison")
    commands = Commands(owner, env, context, replay=saved["commands"] if saved else None, fixture=plan.bundle.fixture)
    prepared, backup, error = None, None, None
    try:
        verify_view(view)
        _prepare(engine, arm, commands, values, view, plan.value["engines"][engine])
        prepared = _index(engine, commands, values, view, mapping)
        if saved is None:
            backup = _backup(owner, values, engine)
        else:
            backup = saved["prepared_snapshot"]
            if not isinstance(backup, dict) or backup.get("path") != str(owner / "prepared-index") or backup.get("files") != inventory(owner / "prepared-index"):
                raise EvaluationError("prepared index snapshot bytes changed")
    except (EvaluationError, OSError, ValueError, TypeError, KeyError) as exc:
        error = str(exc)
    rows = []
    for ordinal, (request, qrel) in enumerate(zip(plan.projection["requests"], plan.projection["qrels"]), 1):
        row = {**request, "ordinal": ordinal, "status": "MISSING", "result": None, "metrics": None, "error": error}
        if error is None:
            try:
                before = _index(engine, commands, values, view, mapping) if engine == "qmd" else None
                if before is not None and before["stable_sha256"] != prepared["stable_sha256"]:
                    raise EvaluationError("prepared document/vector content changed before query")
                argv = compile_recipe(arm, {**values, "query": request["q"]})
                raw = commands.call("query." + arm, argv, phase="queries")
                result = parse_result(raw.decode("utf-8"), arm, mapping, prepared)
                status = "TRUNCATED" if result["window"]["status"] == "TRUNCATED" else "OK"
                row.update(status=status, result=result, metrics=_metrics(request, qrel, result), error=None)
            except (EvaluationError, OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
                row["error"] = str(exc)
                if commands.interrupted:
                    error = "capture interrupted; remaining fixed query slots retained without new children"
        rows.append(row)
    if error is None:
        try:
            if engine == "goldmine":
                native_readback(commands, values, plan.value["engines"][engine], after=True)
            after = _index(engine, commands, values, view, mapping)
            if after["stable_sha256"] != prepared["stable_sha256"]:
                raise EvaluationError("prepared document/vector body changed during query batch")
            verify_view(view)
            if engine_resources(engine, plan.value["engines"][engine], fixture=plan.bundle.fixture, arm=arm) != paths or implementation() != plan.value["implementation"]:
                raise EvaluationError("frozen engine/tool/model/evaluator changed during phase")
        except (EvaluationError, OSError, ValueError, TypeError, KeyError) as exc:
            error = str(exc)
            for row in rows:
                row.update(status="MISSING", error=error)
    raw_commands = commands.finish()
    return {"arm": arm, "view": view["name"], "owner": str(owner), "preparation": prepared, "prepared_snapshot": backup,
            "commands": raw_commands, "error": error, "rows": rows, "summary": summarize(rows),
            "status": "CAPTURED_DEV_RESULTS" if all(r["status"] == "OK" for r in rows) and error is None else "MISSING",
            "interrupted": commands.interrupted,
            "resource_preflight": preflight, "resource_observations": commands.resources,
            "resource_status": commands.resource_status(), "performance_status": "NOT_CAPTURED",
            "update_status": "NOT_CAPTURED", "position_status": "NOT_COMPARABLE_UNVERIFIED_ORIGINAL_COORDINATES"}


def _missing(plan, view, arm, reason):
    rows = [{**r, "ordinal": i, "status": "MISSING", "result": None, "metrics": None, "error": reason}
            for i, r in enumerate(plan.projection["requests"], 1)]
    return {"arm": arm, "view": view, "status": "MISSING", "error": reason, "rows": rows, "summary": summarize(rows), "commands": [],
            "resource_status": "MISSING", "resource_observations": []}


def capture(plan_path, plan_sha256, output, parent, *, fixture=False):
    plan = load_plan(plan_path, plan_sha256, fixture=fixture)
    owner = owned_output(output, parent, plan.bundle)
    protected = [Path(v["corpus"]) for v in plan.views.values()] + [plan.path, Path(plan.projection["reference"]["path"]).parent.parent]
    for paths in plan.engines.values():
        protected.extend(Path(p) for p in paths.values() if isinstance(p, str) and Path(p).is_absolute())
    if any(owner.is_relative_to(p) or p.is_relative_to(owner) for p in protected):
        raise EvaluationError("engine output overlaps source views or immutable engine snapshot")
    owner.mkdir()
    write_new(owner / "ownership.json", {"schema": 1, "suite_id": SUITE, "nonce": str(uuid.uuid4()), "owner": str(owner), "plan_sha256": plan.sha256})
    shutil.copyfile(plan.path, owner / "plan.json")
    # Bound every configured arm's exact context before any engine child, so a
    # large registered QMD inventory cannot fail only after other costly work.
    configured, setup_errors = {}, {}
    for view in ("native", "common"):
        for arm in plan.arms:
            if plan.missing.get(view) or plan.missing.get(arm):
                continue
            arm_owner = owner / "arms" / view / arm
            arm_owner.mkdir(parents=True)
            try:
                configured[(view, arm)] = configure_arm(plan, plan.views[view], arm, arm_owner)
            except (EvaluationError, OSError, ValueError, TypeError, KeyError) as exc:
                setup_errors[(view, arm)] = str(exc)
    rows, interrupted = [], False
    for view in ("native", "common"):
        for arm in plan.arms:
            reason = "capture interrupted; no subsequent engine started" if interrupted else plan.missing.get(view) or plan.missing.get(arm) or setup_errors.get((view, arm))
            if reason:
                rows.append(_missing(plan, view, arm, reason))
            else:
                arm_owner = owner / "arms" / view / arm
                captured = capture_arm(plan, plan.views[view], arm, arm_owner, configured=configured[(view, arm)])
                rows.append(captured)
                interrupted = captured["interrupted"]
    plan.bundle.verify()
    report = {"schema": 1, "suite_id": SUITE, "evidence_mode": "fixture" if fixture else "real", "owner": str(owner),
              "plan": {"path": str(plan.path), "sha256": plan.sha256}, "source_identity": plan.bundle.identity,
              "projection_identity": plan.projection["identity"], "implementation": implementation(), "registered_arms": list(plan.arms),
              "n_fixed_queries": len(plan.projection["requests"]), "source_count": len(plan.bundle.sources), "systems": rows,
              "status": "CAPTURED_DEV_RESULTS" if all(r["status"] == "CAPTURED_DEV_RESULTS" for r in rows) else "MISSING",
              "full_comparison_status": "MISSING_PERFORMANCE_UPDATE_RESOURCE_PHASES" if any(r["resource_status"] != "PASS" for r in rows) else "MISSING_PERFORMANCE_UPDATE_PHASES",
              "resource_status": "FAIL" if any(r["resource_status"] == "FAIL" for r in rows) else "PASS" if all(r["resource_status"] == "PASS" for r in rows) else "MISSING",
              "resource_setup_errors": [{"view": v, "arm": a, "error": e} for (v, a), e in setup_errors.items()],
              "quality_evidence": False, "release_eligible": False,
              "artifacts": _artifacts(owner)}
    write_new(owner / "report.json", report)
    return report


def verify_capture(report_path):
    report_path = Path(report_path).resolve()
    owner, report = report_path.parent, read(report_path)
    plan = load_plan(report["plan"]["path"], report["plan"]["sha256"], fixture=report["evidence_mode"] == "fixture")
    if (report["owner"] != str(owner) or report["suite_id"] != SUITE or report["implementation"] != implementation()
            or report["source_identity"] != plan.bundle.identity or report["projection_identity"] != plan.projection["identity"]
            or report["registered_arms"] != list(plan.arms) or report["n_fixed_queries"] != len(plan.projection["requests"])
            or report["source_count"] != len(plan.bundle.sources) or report["artifacts"] != _artifacts(owner)
            or report["quality_evidence"] is not False or report["release_eligible"] is not False
            or sha256_file(owner / "plan.json") != plan.sha256):
        raise EvaluationError("capture raw/source/code/config/query denominator identity changed", status="MISSING")
    ownership = read(owner / "ownership.json")
    if (ownership.get("schema") != 1 or ownership.get("suite_id") != SUITE or ownership.get("owner") != str(owner)
            or ownership.get("plan_sha256") != plan.sha256 or str(uuid.UUID(ownership.get("nonce", ""))) != ownership.get("nonce")):
        raise EvaluationError("owned capture identity changed")
    expected = [(v, a) for v in ("native", "common") for a in plan.arms]
    if [(r.get("view"), r.get("arm")) for r in report["systems"]] != expected:
        raise EvaluationError("native/common arm matrix was omitted/reordered")
    interrupted = False
    setup_rows = report["resource_setup_errors"]
    if (not isinstance(setup_rows, list) or any(not isinstance(r, dict) or set(r) != {"view", "arm", "error"}
            or (r["view"], r["arm"]) not in expected or not isinstance(r["error"], str) or not r["error"]
            or plan.missing.get(r["view"]) or plan.missing.get(r["arm"]) for r in setup_rows)):
        raise EvaluationError("resource preflight failure registry contains unregistered entries")
    setup_errors = {(r["view"], r["arm"]): r["error"] for r in setup_rows}
    if len(setup_errors) != len(setup_rows):
        raise EvaluationError("duplicate resource preflight failure entry")
    for (view, arm), saved in zip(expected, report["systems"]):
        reason = "capture interrupted; no subsequent engine started" if interrupted else plan.missing.get(view) or plan.missing.get(arm)
        if (view, arm) in setup_errors and not reason:
            try:
                configure_arm(plan, plan.views[view], arm, owner / "arms" / view / arm, replay=True)
            except (EvaluationError, OSError, ValueError, TypeError, KeyError) as exc:
                if setup_errors[(view, arm)] != str(exc):
                    raise EvaluationError("resource preflight failure differs from disk replay")
                reason = str(exc)
            else:
                raise EvaluationError("a ready arm was replaced by an invented preflight failure")
        actual = _missing(plan, view, arm, reason) if reason else capture_arm(plan, plan.views[view], arm, owner / "arms" / view / arm, saved=saved)
        if actual != saved:
            raise EvaluationError("retained engine rows/metrics differ from raw disk replay", status="MISSING")
        interrupted = interrupted or actual.get("interrupted", False)
    expected_status = "CAPTURED_DEV_RESULTS" if all(r["status"] == "CAPTURED_DEV_RESULTS" for r in report["systems"]) else "MISSING"
    resource_status = "FAIL" if any(r["resource_status"] == "FAIL" for r in report["systems"]) else "PASS" if all(r["resource_status"] == "PASS" for r in report["systems"]) else "MISSING"
    full = "MISSING_PERFORMANCE_UPDATE_PHASES" if resource_status == "PASS" else "MISSING_PERFORMANCE_UPDATE_RESOURCE_PHASES"
    if report["status"] != expected_status or report["resource_status"] != resource_status or report["full_comparison_status"] != full:
        raise EvaluationError("intermediate capture was relabeled as a complete comparison")
    return report
