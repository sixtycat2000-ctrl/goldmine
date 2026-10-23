"""Frozen, offline engine assets. No resolver, install hook or download fallback."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re

from acceptance_lib.evidence import read_command_run
from acceptance_lib.resource_policy import runtime_identity as observer_runtime
from answer_eval.protocol import EvaluationError, canonical_sha256, fields, sha256_file, timestamp
from search_comparison.source_views.recipes import runtime_identity
from search_comparison.source_views.sources import load_sources
from .identity import implementation
from .projection import read_projection
from .protocol import LIMITS, SUITE, directory, expected_arms, local_path, read, reference, resource
from .views import load_views

QMD_URIS = {"embed": "hf:ggml-org/embeddinggemma-300M-GGUF/embeddinggemma-300M-Q8_0.gguf",
            "generate": "hf:tobil/qmd-query-expansion-1.7B-gguf/qmd-query-expansion-1.7B-q4_k_m.gguf",
            "rerank": "hf:ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF/qwen3-reranker-0.6b-q8_0.gguf"}
TOOLS = {"goldmine": {"python", "server", "ocr", "whisper", "ffmpeg", "ffprobe", "pdfinfo", "pdftotext", "pdftoppm", "sips", "imageio"},
         "qmd": {"node"}, "recoll": {"recollq", "recollindex", "python", "sandbox"}}
QMD_ROLES = {"qmd.search": (), "qmd.vsearch": ("embed", "generate"), "qmd.query": ("embed", "generate", "rerank")}


def _probe(ref, path, role, *, fixture):
    if fixture:
        return
    probe_path = reference(ref, bound=8 * 1024 * 1024)
    manifest, receipt, stdout = read_command_run(probe_path, check_id="comparison.engine.probe." + role, suite_id=SUITE)
    if (receipt["exit_code"] != 0 or receipt["timed_out"] or receipt["argv"][0] != str(path)
            or manifest["identity"] != {"role": role, "sha256": sha256_file(path)}
            or not stdout.read_bytes() and not (probe_path.parent / receipt["stderr"]["path"]).read_bytes()):
        raise EvaluationError("missing successful raw executable/runtime probe", status="MISSING")


def _tools(name, config, *, fixture):
    fields(config, TOOLS[name], name + " tool roles")
    paths = {}
    for role, ref in config.items():
        paths[role] = str(resource(ref, executable=True, fixture=fixture))
        _probe(ref["probe"], Path(paths[role]), name + "." + role, fixture=fixture)
    return paths


def engine_resources(name, value, *, fixture, require_canary=True, arm=None):
    """Return resolved paths only after all role/dependency/license pins pass."""
    if value is None:
        raise EvaluationError(name + " assets are unresolved", status="MISSING")
    required = {"version", "tools", "installation"}
    extra = {"goldmine": {"models", "model_roles", "python_environment"},
             "qmd": {"model_roles", "offline_canary"},
             "recoll": {"audit_canary", "dependencies", "thesaurus"}}[name]
    fields(value, required | extra, name + " fixed installation")
    if not isinstance(value["version"], str) or not value["version"]:
        raise EvaluationError("engine version must be explicit", status="MISSING")
    root = directory(value["installation"])
    paths = {**_tools(name, value["tools"], fixture=fixture), "installation": str(root)}
    if name == "goldmine":
        models, environment = directory(value["models"]), directory(value["python_environment"])
        paths.update(models=str(models), python_environment=str(environment))
        fields(value["model_roles"], {"embedding", "asr", "vad", "rerank"}, "Goldmine model roles")
        expected = {"embedding": "embeddinggemma-2-Q8_0.gguf", "asr": "ggml-large-v3-turbo-q5_0.bin",
                    "vad": "ggml-silero-v5.1.2.bin", "rerank": "Qwen3-Reranker-0.6B-Q8_0.gguf"}
        for role, ref in value["model_roles"].items():
            model = resource(ref, fixture=fixture)
            if model != models / expected[role]:
                raise EvaluationError("Goldmine fixed local model-manager path differs", status="MISSING")
            paths[role] = str(model)
            if not fixture:
                from goldmine.model_manifest import by_key
                keys = {"embedding": "embeddinggemma-2-Q8_0", "asr": "whisper-large-v3-turbo-q5_0", "vad": "silero-vad-v5.1.2", "rerank": "Qwen3-Reranker-0.6B-Q8_0"}
                entry = by_key(keys[role])
                if (ref["sha256"], ref["bytes"]) != (entry.sha256, entry.bytes):
                    raise EvaluationError("Goldmine default model family differs from pinned manifest")
        if not (root / "goldmine/__main__.py").is_file() or not (root / "pyproject.toml").is_file():
            raise EvaluationError("Goldmine immutable source snapshot is incomplete", status="MISSING")
        if not fixture:
            # A newly provisioned immutable snapshot contains only production
            # sources and package metadata, never repo/user model fallbacks.
            actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
            if any(n != "pyproject.toml" and not n.startswith("goldmine/") for n in actual) or any(n.startswith("goldmine/") and not n.endswith(".py") for n in actual):
                raise EvaluationError("Goldmine snapshot contains an undeclared import/model fallback")
            actual_root = Path(__file__).resolve().parents[3]
            expected_files = {p.relative_to(actual_root).as_posix(): sha256_file(p) for p in (actual_root / "goldmine").rglob("*.py")}
            expected_files["pyproject.toml"] = sha256_file(actual_root / "pyproject.toml")
            if {p.relative_to(root).as_posix(): sha256_file(p) for p in root.rglob("*") if p.is_file()} != expected_files:
                raise EvaluationError("Goldmine arm is not the frozen current candidate source")
    elif name == "qmd":
        if value["version"] != "2.8.3":
            raise EvaluationError("installed QMD grammar is pinned to2.8.3")
        pkg = root / "node_modules/@tobilu/qmd"
        if read(pkg / "package.json").get("version") != "2.8.3":
            raise EvaluationError("QMD package bytes/version differ")
        paths.update(qmd_launcher=str(pkg / "bin/qmd"), qmd_package=str(pkg),
                     llama_dir=str(root / "node_modules/node-llama-cpp/llama"))
        if not Path(paths["qmd_launcher"]).is_file() or not Path(paths["llama_dir"]).is_dir():
            raise EvaluationError("QMD compiled launcher/native dependency is missing", status="MISSING")
        roles = QMD_ROLES.get(arm, QMD_ROLES["qmd.query"])
        paths["qmd_roles"] = ",".join(roles)
        if roles and not fixture and os.access(paths["llama_dir"], os.W_OK):
            raise EvaluationError("writable node-llama-cpp llama directory permits automatic build/download", status="MISSING")
        if roles and not any(p.name == "llama-addon.node" for p in root.rglob("llama-addon.node")):
            raise EvaluationError("no provisioned native llama addon in frozen QMD snapshot", status="MISSING")
        fields(value["model_roles"], set(QMD_URIS), "QMD default model families")
        for role, model in value["model_roles"].items():
            if role not in roles:
                continue
            fields(model, {"uri", "revision", "resource"}, "QMD immutable model role")
            if model["uri"] != QMD_URIS[role] or not isinstance(model["revision"], str) or not re.fullmatch(r"[a-f0-9]{40}", model["revision"]):
                raise EvaluationError("QMD default model family/revision is unresolved", status="MISSING")
            path = resource(model["resource"], fixture=fixture)
            if not fixture and os.access(path, os.W_OK):
                raise EvaluationError("QMD model must be an owned immutable local asset", status="MISSING")
            paths[role] = str(path)
        # A primary-source recipe alone is not proof that this ABI/native/model
        # combination actually loads offline. This separate fixed canary must
        # be reviewed/provisioned before any real comparison run.
        if roles and not fixture and require_canary:
            from .canaries import validate_qmd
            validate_qmd(value["offline_canary"], value, paths)
    else:
        if value["version"] != "1.44.2":
            raise EvaluationError("installed Recoll grammar is pinned to1.44.2")
        paths["data_dir"] = str(root / "Contents/Resources")
        if not Path(paths["data_dir"]).is_dir():
            raise EvaluationError("Recoll full data/filter snapshot is absent", status="MISSING")
        paths["dependencies"] = str(directory(value["dependencies"]))
        if value["thesaurus"] is not None:
            paths["independent_thesaurus"] = str(resource(value["thesaurus"], fixture=fixture))
        if not fixture and require_canary:
            from .canaries import validate_recoll
            validate_recoll(value["audit_canary"], value, paths)
    return paths


@dataclass(frozen=True)
class Plan:
    path: Path
    sha256: str
    value: dict
    bundle: object
    projection: dict
    views: dict
    engines: dict
    missing: dict
    arms: tuple


def load_plan(path, pin, *, fixture=False):
    path = reference({"path": str(Path(path).absolute()), "sha256": pin})
    p = read(path)
    fields(p, {"schema", "suite_id", "state", "evidence_mode", "registered_at", "bundle", "projection", "views", "arms",
               "engines", "limits", "offline", "fallback", "runtime", "implementation", "resource_observer"}, "engine capture plan")
    if (type(p["schema"]) is not int or p["schema"] != 1 or p["suite_id"] != SUITE or p["state"] != "FROZEN"
            or p["evidence_mode"] != ("fixture" if fixture else "real") or p["offline"] is not True or p["fallback"] != "forbidden"
            or p["limits"] != LIMITS or p["runtime"] != runtime_identity() or p["implementation"] != implementation()
            or p["resource_observer"] != observer_runtime()):
        raise EvaluationError("unresolved/unfrozen/different engine capture profile", status="MISSING")
    registered = timestamp(p["registered_at"], "profile registration")
    if registered > datetime.now(timezone.utc):
        raise EvaluationError("profile registration cannot be in the future")
    fields(p["bundle"], {"path", "manifest_sha256"}, "source-only bundle")
    bundle = load_sources(p["bundle"]["path"], p["bundle"]["manifest_sha256"], fixture=fixture)
    projection = read_projection(p["projection"], bundle)
    if timestamp(projection["completed_at"], "dev export completion") > registered:
        raise EvaluationError("fixed dev projection must precede the frozen engine plan")
    arms = expected_arms(p["arms"], fixture=fixture)
    views, missing = load_views(p["views"], bundle)
    fields(p["engines"], {"goldmine", "qmd", "recoll"}, "all registered engines")
    engines, cache = {}, {}
    for arm in arms:
        name = arm.split(".")[0]
        key = (name, arm if name == "qmd" else "shared")
        try:
            if key not in cache:
                cache[key] = engine_resources(name, p["engines"][name], fixture=fixture, arm=arm)
            engines[arm] = cache[key]
        except (EvaluationError, OSError, TypeError, ValueError, KeyError) as exc:
            missing[arm] = str(exc)
    if "recoll.synonyms" in arms and "independent_thesaurus" not in engines.get("recoll.synonyms", {}):
        missing["recoll.synonyms"] = "independently licensed frozen thesaurus is absent"
    return Plan(path, pin, p, bundle, projection, views, engines, missing, arms)
