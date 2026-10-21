"""Exact composition with adopted observer003; sampled evidence is not lifetime proof."""
from __future__ import annotations

from pathlib import Path
import json

from acceptance_lib.evidence import InvalidEvidence
from acceptance_lib.resource_policy import policy
from acceptance_lib.resource_validation import validate_observation
from answer_eval.protocol import EvaluationError, canonical_sha256, sha256_file


def external_inputs(context):
    """Only declarations already validated by the fixed engine/source adapters.

    The observer does not certify these bytes or sandbox outside writes. Engine
    preflight/readback owns their actual identities; this binds that complete
    contract to each resource phase without inventing a second inventory format.
    """
    found = {}
    def add(path, kind, pin):
        row = {"path": path, "kind": kind, "sha256": pin}
        if path in found and found[path] != row:
            raise EvaluationError("contradictory immutable external identity")
        found[path] = row
    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("path"), str) and Path(value["path"]).is_absolute() and "sha256" in value:
                add(value["path"], "tree" if "files" in value else "file", value["sha256"])
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)
    walk(context.get("engine_resources", {}))
    walk(context.get("input_refs", {}))
    view = context.get("view_identity")
    if view:
        add(view["corpus"], "tree", view["sha256"])
    return [found[k] for k in sorted(found)]


def configuration(engine, values, view):
    """Bind actual fixed bytes or the exact independently read-back QMD config.

    QMD collection-add edits its YAML. Its final normalized value is declared
    before capture and validated against actual YAML/SQL by indexes.validate_qmd;
    the raw YAML byte SHA remains in that audit and the whole artifact inventory.
    No post-query mutable file is used as the expected registration at replay.
    """
    if engine == "qmd":
        value = {"models": {r: values[r] for r in ("embed", "generate", "rerank") if r in values},
                 "collections": {"sources": {"path": view["corpus"], "pattern": "**/*.md" if view["name"] == "common" else "**/*.{md,rst,py,c}"}}}
        return {"kind": "normalized-effective-yaml-and-sql", "path": str(Path(values["qmd_db"]).parents[3] / "qmd-config/comparison.yml"),
                "value": value, "sha256": canonical_sha256(value)}
    paths = [Path(values["goldmine_config"])] if engine == "goldmine" else [Path(values["recoll_config_dir"]) / "recoll.conf", Path(values["sandbox_profile"])]
    if engine == "recoll" and view["name"] == "common":
        paths.append(Path(values["recoll_config_dir"]) / "mimemap")
    return {"kind": "fixed-file-bytes", "files": {str(p): sha256_file(p) for p in paths}}


def expected_policy(owner, phase, identity, *, fixture=False):
    return policy("query" if phase == "queries" else "prepare", Path(owner), source=identity,
                  immutable_external=external_inputs(identity), fixture_limits={} if fixture else None)


def preflight(owner, context, env, *, fixture=False):
    """All finite fixed label shapes, before any model/index/query child starts."""
    labels = {"prepare": ("goldmine.python-runtime", "goldmine.native-runtime-before", "goldmine.ingest", "qmd.collection-add", "qmd.embed", "recoll.index"),
              "audit": ("goldmine.native-runtime-after", "qmd.audit", "recoll.metadata", "recoll.denied-source", "recoll.stored-body"),
              "queries": ("query." + context["arm"],)}
    sizes = {}
    for phase, names in labels.items():
        for label in names:
            identity = {**context, "environment": env, "phase": phase, "label": label,
                        "resource_contract": "observer003-sampled-private-session-v1"}
            size = len(json.dumps(identity, ensure_ascii=False, allow_nan=False).encode())
            expected_policy(owner, phase, identity, fixture=fixture)
            sizes[phase + ":" + label] = size
    return {"status": "INPUTS_BOUNDED", "limit_bytes": 1024 * 1024, "source_context_bytes": sizes,
            "maximum_source_context_bytes": max(sizes.values()), "method": "exact-json-source-before-any-child"}


def validate(path, expected, argv, identity):
    try:
        return validate_observation(path, expected, argv, identity)
    except (InvalidEvidence, OSError, ValueError, TypeError, KeyError) as exc:
        raise EvaluationError("invalid complete source-bound resource evidence: " + str(exc), status="MISSING") from exc


def status(rows, registry, *, attempted):
    """Require one same-order disposition for every attempt, retaining any FAIL."""
    if not isinstance(rows, list) or not isinstance(registry, list):
        return "MISSING"
    if any(isinstance(r, dict) and isinstance(r.get("observation"), dict) and r["observation"].get("sampled_resource_status") == "FAIL" for r in rows):
        return "FAIL"
    if type(attempted) is not int or attempted <= 0 or len(rows) != attempted or len(registry) != attempted:
        return "MISSING"
    if any(not isinstance(r, dict) or set(r) != {"path", "sha256"} or not isinstance(r["path"], str)
           or not Path(r["path"]).is_absolute() or not isinstance(r["sha256"], str)
           or len(r["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in r["sha256"]) for r in registry):
        return "MISSING"
    if len({r["path"] for r in registry}) != len(registry):
        return "MISSING"
    base = {"command", "expected_manifest", "phase", "label", "disposition"}
    for row, ref in zip(rows, registry):
        if (not isinstance(row, dict) or row.get("command") != ref or row.get("expected_manifest") != ref["path"]
                or row.get("phase") not in {"prepare", "queries", "audit"} or not isinstance(row.get("label"), str) or not row["label"]):
            return "MISSING"
        if row.get("disposition") != "validated" or set(row) != base | {"observation"}:
            return "MISSING"
        if not isinstance(row["observation"], dict) or row["observation"].get("sampled_resource_status") != "PASS":
            return "MISSING"
    if not rows:
        return "MISSING"
    return "PASS"
