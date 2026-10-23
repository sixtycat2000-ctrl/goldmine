"""Fixed non-evaluation compatibility recipes, never quality/performance jobs."""
from __future__ import annotations

from pathlib import Path
import sys

from acceptance_lib.evidence import artifact, capture_command, read_command_run
from answer_eval.protocol import EvaluationError, canonical_sha256, fields, sha256_file
from search_comparison.source_views.recipes import runtime_identity
from search_comparison.source_views.run import write_new
from .identity import implementation
from .protocol import SUITE, local_path, read, reference
from .recoll import DENIED_PROBE, body_argv, metadata_argv, parse_metadata, sandbox_profile, stored_body
from .settings import environment, setup

BODY = "SYNTHETIC NON-EVALUATION STORED-TEXT CANARY\n中文 second line $() `literal` not executed.\n"


def context(name, engine):
    omitted = "offline_canary" if name == "qmd" else "audit_canary"
    return {"engine": name, "engine_assets": {k: v for k, v in engine.items() if k != omitted},
            "implementation": implementation(), "runtime": runtime_identity(), "scope": "non-evaluation compatibility only"}


def new_owner(output, parent, paths):
    parent, output = local_path(str(parent)), local_path(str(output))
    if not parent.is_dir() or output == parent or not output.is_relative_to(parent) or output.exists():
        raise EvaluationError("compatibility output must be a fresh canonical child of the explicit parent")
    protected = [Path(v) for v in paths.values() if isinstance(v, str) and Path(v).is_absolute()]
    if any(output.is_relative_to(p) or p.is_relative_to(output) for p in protected):
        raise EvaluationError("compatibility output overlaps immutable engine assets")
    output.mkdir()
    return output


def _capture(owner, label, argv, env, identity):
    capture_command(owner / "raw" / label, "comparison.canary." + label, argv, cwd=owner / "cwd", env=env,
                    identity={**identity, "environment": env}, suite_id=SUITE, timeout=1800, owned_process_group=True)
    return {"path": str(owner / "raw" / label / "manifest.json"), "sha256": sha256_file(owner / "raw" / label / "manifest.json")}


def _raw(ref, owner, label, argv, env, identity):
    path = reference(ref)
    if path != owner / "raw" / label / "manifest.json":
        raise EvaluationError("canary raw capture path differs")
    manifest, receipt, stdout = read_command_run(path, check_id="comparison.canary." + label, suite_id=SUITE)
    if (receipt["argv"] != argv or receipt["cwd"] != str(owner / "cwd") or receipt["exit_code"] != 0 or receipt["timed_out"]
            or manifest["identity"] != {**identity, "environment": env}):
        raise EvaluationError("canary raw argv/env/source/exit identity differs", status="MISSING")
    return stdout.read_bytes()


def capture_qmd(engine, output, parent, *, fixture=False, mode="query"):
    from .plan import engine_resources
    if mode not in ("query", "vsearch"):
        raise EvaluationError("only fixed native semantic model-load canary modes exist")
    paths = engine_resources("qmd", engine, fixture=fixture, require_canary=False, arm="qmd." + mode)
    owner = new_owner(output, parent, paths)
    for n in ("cwd", "tmp", "xdg/cache", "xdg/data", "xdg/config", "qmd-config"):
        (owner / n).mkdir(parents=True)
    env = environment(owner, "qmd", paths)
    args = qmd_args(owner, paths)
    ref = _capture(owner, "qmd-load", args, env, context("qmd", engine))
    result = {"schema": 1, "suite_id": SUITE, "kind": "qmd-load", "owner": str(owner), "roles": paths["qmd_roles"].split(","), "identity": context("qmd", engine),
              "command": ref, "quality_evidence": False, "release_eligible": False}
    write_new(owner / "canary.json", result)
    if engine_resources("qmd", engine, fixture=fixture, require_canary=False, arm="qmd." + mode) != paths:
        raise EvaluationError("QMD immutable assets changed during model-load canary")
    validate_qmd({"path": str(owner / "canary.json"), "sha256": sha256_file(owner / "canary.json")}, engine, paths)
    return result


def qmd_args(owner, paths, roles=None):
    import json
    script = Path(__file__).with_name("_qmd_canary.mjs")
    roles = roles or paths["qmd_roles"].split(",")
    return [paths["node"], str(script), paths["qmd_package"], json.dumps({r: paths[r] for r in roles}, separators=(",", ":")), str(owner / "xdg/cache/qmd/models")]


def validate_qmd(ref, engine, paths):
    path = reference(ref)
    c = read(path)
    owner = local_path(c.get("owner"))
    fields(c, {"schema", "suite_id", "kind", "owner", "roles", "identity", "command", "quality_evidence", "release_eligible"}, "QMD canary")
    expected_context = context("qmd", engine)
    if (c["schema"] != 1 or c["suite_id"] != SUITE or c["kind"] != "qmd-load" or c["identity"] != expected_context
            or c["quality_evidence"] is not False or c["release_eligible"] is not False):
        raise EvaluationError("QMD compatibility context was relabeled", status="MISSING")
    roles = c["roles"]
    if roles not in (["embed", "generate"], ["embed", "generate", "rerank"]) or not set(paths["qmd_roles"].split(",")) <= set(roles):
        raise EvaluationError("QMD model-load canary omits a required actual role", status="MISSING")
    full_paths = dict(paths)
    for r in roles:
        full_paths[r] = str(reference({k: engine["model_roles"][r]["resource"][k] for k in ("path", "sha256")}))
    env = environment(owner, "qmd", full_paths)
    raw = _raw(c["command"], owner, "qmd-load", qmd_args(owner, full_paths, roles), env, expected_context)
    from answer_eval.protocol import parse_json
    value = parse_json(raw.decode("utf-8"), "QMD native model-load stdout")
    expected = {"schema": 1, "scope": "compatibility_only", "llama_directory_writable": False,
                "models": {r: {"path": full_paths[r], "loaded": True} for r in roles}}
    if value != expected:
        raise EvaluationError("QMD did not load every pinned role offline", status="MISSING")
    return c


def recoll_specs(owner, paths):
    corpus, config, profile = owner / "input", owner / "recoll-config", owner / "deny-source-read.sb"
    return {"recoll-index": [paths["recollindex"], "-c", str(config)],
            "recoll-fields": metadata_argv(paths["recollq"], config, corpus),
            "recoll-denied-read": [paths["sandbox"], "-f", str(profile), paths["python"], "-I", "-S", "-B", "-c", DENIED_PROBE, str(corpus / "canary.md")],
            "recoll-body": body_argv(paths["sandbox"], profile, paths["recollq"], config, corpus, 0)}


def capture_recoll(engine, output, parent, *, fixture=False):
    from .plan import engine_resources
    paths = engine_resources("recoll", engine, fixture=fixture, require_canary=False)
    owner = new_owner(output, parent, paths)
    corpus = owner / "input"
    corpus.mkdir()
    (corpus / "canary.md").write_text(BODY, encoding="utf-8")
    view = {"name": "common", "corpus": str(corpus)}
    _, env = setup(owner, "recoll", paths, view)
    identity = context("recoll", engine)
    commands = {label: _capture(owner, label, argv, env, identity) for label, argv in recoll_specs(owner, paths).items()}
    result = {"schema": 1, "suite_id": SUITE, "kind": "recoll-stored", "owner": str(owner), "identity": identity,
              "commands": commands, "quality_evidence": False, "release_eligible": False}
    write_new(owner / "canary.json", result)
    if engine_resources("recoll", engine, fixture=fixture, require_canary=False) != paths:
        raise EvaluationError("Recoll immutable assets changed during CLI canary")
    validate_recoll({"path": str(owner / "canary.json"), "sha256": sha256_file(owner / "canary.json")}, engine, paths)
    return result


def validate_recoll(ref, engine, paths):
    from search_comparison.adapters import DocumentMap
    path = reference(ref)
    c = read(path)
    fields(c, {"schema", "suite_id", "kind", "owner", "identity", "commands", "quality_evidence", "release_eligible"}, "Recoll canary")
    owner = local_path(c["owner"])
    expected_context = context("recoll", engine)
    if (c["schema"] != 1 or c["suite_id"] != SUITE or c["kind"] != "recoll-stored" or c["identity"] != expected_context
            or c["quality_evidence"] is not False or c["release_eligible"] is not False):
        raise EvaluationError("Recoll canary context differs", status="MISSING")
    corpus = owner / "input"
    setup(owner, "recoll", paths, {"name": "common", "corpus": str(corpus)}, replay=True)
    if (corpus / "canary.md").read_text() != BODY or (owner / "deny-source-read.sb").read_text() != sandbox_profile(corpus):
        raise EvaluationError("Recoll canary source/profile changed")
    specs, env = recoll_specs(owner, paths), environment(owner, "recoll", paths)
    fields(c["commands"], set(specs), "all Recoll canary commands")
    raw = {label: _raw(c["commands"][label], owner, label, argv, env, expected_context) for label, argv in specs.items()}
    mapping = DocumentMap(corpus, [{"source_path": "canary.md", "view_path": "canary.md"}])
    metadata = parse_metadata(raw["recoll-fields"].decode(), mapping)
    if (len(metadata) != 1 or metadata[0]["mtype"] != "text/plain" or metadata[0]["ipath"]
            or raw["recoll-denied-read"] != b"DENIED_SOURCE_READ\n" or stored_body(raw["recoll-body"], metadata[0]) != BODY.encode()):
        raise EvaluationError("Recoll stored-only CLI framing/access canary failed", status="MISSING")
    return c
