"""Owned source-view capture and raw-command replay; no engine search/index API."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unicodedata
import uuid

from acceptance_lib import evidence
from answer_eval.protocol import EvaluationError, canonical_sha256, fields, parse_json, sha256_file
from product_eval import bundle as product_bundle, materialization
import goldmine

from .commands import Commands
from .convert import convert
from .coverage import render, validate_coverage
from .recipes import validate_config, SUITE
from .sources import load_sources, read_bounded


def implementation_identity():
    import acceptance_lib
    import acceptance_lib.owned_process as owned
    import answer_eval
    import answer_eval.protocol as protocol
    import product_eval

    root = Path(__file__).resolve().parents[3]
    files = {path.relative_to(root).as_posix(): sha256_file(path)
             for path in sorted(Path(__file__).parent.glob("*.py"))}
    # These packages have eager initializers and local transitive imports. Bind
    # their complete local Python inventories, including later-added modules.
    for directory, package in (("scripts/product_eval", product_eval), ("scripts/answer_eval", answer_eval), ("goldmine", goldmine)):
        if Path(package.__file__).resolve() != root / directory / "__init__.py":
            raise EvaluationError("converter imported an installed/shadow source package", status="MISSING")
        for path in sorted((root / directory).rglob("*")):
            if path.is_symlink():
                raise EvaluationError("local converter source closure contains a symlink", status="MISSING")
            if path.is_file() and path.suffix == ".py":
                files[path.relative_to(root).as_posix()] = sha256_file(path)
    for name, path in {"scripts/acceptance_lib/__init__.py": acceptance_lib.__file__,
                       "scripts/acceptance_lib/evidence.py": evidence.__file__,
                       "scripts/acceptance_lib/owned_process.py": owned.__file__,
                       "scripts/answer_eval/protocol.py": protocol.__file__,
                       "scripts/product_eval/bundle.py": product_bundle.__file__,
                       "scripts/product_eval/materialization.py": materialization.__file__}.items():
        if Path(path).resolve() != root / name:
            raise EvaluationError("converter imported an installed/shadow source dependency", status="MISSING")
        files[name] = sha256_file(Path(path))
    files["scripts/capture_comparison_sources.py"] = sha256_file(root / "scripts/capture_comparison_sources.py")
    return {"files": files, "sha256": canonical_sha256(files)}


def write_new(path, value):
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _no_links(path, stop):
    for item in [path, *path.parents]:
        if item.is_symlink():
            raise EvaluationError("owned output path contains a symlink")
        if item == stop:
            return
    raise EvaluationError("owned output path does not descend from the allowed parent")


def owned_output(output, allowed_parent, bundle):
    parent = Path(allowed_parent).absolute()
    if not parent.is_dir():
        raise EvaluationError("explicit output parent must already exist", status="MISSING")
    if parent != parent.resolve():
        raise EvaluationError("explicit output parent must be canonical without symlink components")
    _no_links(parent, parent)
    output = Path(output).absolute()
    if not output.is_relative_to(parent) or output == parent or output.is_relative_to(bundle.root):
        raise EvaluationError("new output must be inside the declared owner parent and outside source bundle")
    _no_links(output, parent)
    if output.exists():
        raise EvaluationError("output already exists; captures never overwrite")
    return output.resolve()


def common_names(bundle):
    names = {path: path + ".md" for path in bundle.sources}
    portable = [unicodedata.normalize("NFKC", name).casefold() for name in names.values()]
    if len(set(portable)) != len(portable) or any(p.as_posix() in portable for name in portable for p in Path(name).parents if p.as_posix() != "."):
        raise EvaluationError("common-view output mapping has a portable file/directory collision")
    return names


def _artifacts(owner):
    files = {}
    for path in sorted(owner.rglob("*")):
        if path.is_symlink():
            raise EvaluationError("capture contains an unregistered symlink")
        if path.is_file() and path != owner / "report.json":
            files[path.relative_to(owner).as_posix()] = sha256_file(path)
    return files


def _corpus_files(root):
    files = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise EvaluationError("materialized corpus contains a symlink")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = sha256_file(path)
    return files


def one_source(bundle, source, owner, case, resources, config, config_ref, implementation, *, saved=None):
    native = owner / "native/corpus" / source["path"]
    if sha256_file(native) != source["asset"]["sha256"]:
        raise EvaluationError("native input bytes changed before conversion", status="MISSING")
    commands = Commands(owner, case, resources,
                        {"source_path": source["path"], "source_sha256": source["asset"]["sha256"],
                         "source_identity": bundle.identity, "config": config_ref,
                         "evidence_mode": "fixture" if bundle.fixture else "real",
                         "implementation_sha256": implementation["sha256"], "process_ownership": "new-session/v1"},
                        timeout=config["limits"]["command_timeout_s"], max_output=config["limits"]["max_output_bytes"],
                        replay=saved["commands"] if saved else None)
    expected, units, error = None, [], None
    try:
        expected, units = convert(source, native, commands, config["limits"])
        complete = validate_coverage(source["kind"], expected, units)
    except (EvaluationError, OSError, ValueError) as exc:
        complete, error = False, str(exc)
    raw_commands = commands.finish()
    text, mapping = render(source, units)
    row = {"source_path": source["path"], "source_kind": source["kind"], "source_group": source["source_group"],
           "source_sha256": source["asset"]["sha256"], "status": "COMPLETE_CONVERSION" if complete else "MISSING",
           "expected_coverage": expected, "units": units, "error": error,
           "commands": raw_commands, "mapping": mapping, "common_text_sha256": hashlib.sha256(text.encode()).hexdigest()}
    return row, text


def capture_sources(bundle_path, manifest_sha256, config_path, output, allowed_parent, *, fixture=False):
    bundle = load_sources(bundle_path, manifest_sha256, fixture=fixture)
    implementation = implementation_identity()
    config, resources, config_ref = validate_config(config_path, bundle, implementation["sha256"])
    names = common_names(bundle)
    owner = owned_output(output, allowed_parent, bundle)
    owner.mkdir(parents=False)
    write_new(owner / "ownership.json", {"schema": 1, "nonce": str(uuid.uuid4()), "owner": str(owner), "suite_id": SUITE,
                                         "evidence_mode": "fixture" if fixture else "real"})
    write_new(owner / "config.json", config)
    native = product_bundle.build_corpus(bundle, owner / "native")
    materialization.validate(owner / "native/corpus", bundle.sources, native["materialization"])
    (owner / "work").mkdir()
    (owner / "common").mkdir()
    (owner / "common/corpus").mkdir()
    rows = []
    for ordinal, source in enumerate(bundle.sources.values(), 1):
        case = owner / "work" / f"{ordinal:04d}"
        case.mkdir()
        row, text = one_source(bundle, source, owner, case, resources, config, config_ref, implementation)
        row["common_path"] = names[source["path"]]
        destination = owner / "common/corpus" / row["common_path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        write_new(case / "source.json", row)
        rows.append(row)
    bundle.verify()
    if implementation_identity() != implementation or validate_config(config_path, bundle, implementation["sha256"])[2] != config_ref:
        raise EvaluationError("converter source/config/resources changed during capture", status="MISSING")
    native_after = evidence.read_json(owner / "native/build.json")
    materialization.validate(owner / "native/corpus", bundle.sources, native_after["materialization"])
    report = {"schema": 1, "suite_id": SUITE, "evidence_mode": "fixture" if fixture else "real", "owner": str(owner),
              "source_identity": bundle.identity, "source_count": len(bundle.sources), "config": config_ref,
              "implementation": implementation, "native_build_sha256": sha256_file(owner / "native/build.json"),
              "sources": rows, "status": ("MECHANICAL_CAPTURE_COMPLETE" if fixture else "CAPTURED_FULL_SOURCES") if all(r["status"] == "COMPLETE_CONVERSION" for r in rows) else "MISSING",
              "quality_evidence": False, "release_eligible": False, "artifacts": _artifacts(owner)}
    write_new(owner / "report.json", report)
    return report


def verify_capture(report_path, bundle_path, manifest_sha256, config_path, *, fixture=False):
    report_path = Path(report_path).resolve()
    owner = report_path.parent
    report = parse_json(read_bounded(report_path, 64 * 1024 * 1024).decode(), "source-view report")
    fields(report, {"schema", "suite_id", "evidence_mode", "owner", "source_identity", "source_count", "config", "implementation",
                    "native_build_sha256", "sources", "status", "quality_evidence", "release_eligible", "artifacts"}, "source-view report")
    bundle = load_sources(bundle_path, manifest_sha256, fixture=fixture)
    code = implementation_identity()
    config, resources, config_ref = validate_config(config_path, bundle, code["sha256"])
    if (type(report["schema"]) is not int or report["schema"] != 1 or report["suite_id"] != SUITE or report["evidence_mode"] != ("fixture" if fixture else "real")
            or report["owner"] != str(owner) or report["source_identity"] != bundle.identity or report["source_count"] != len(bundle.sources)
            or report["implementation"] != code or report["config"] != config_ref or report["quality_evidence"] is not False
            or report["release_eligible"] is not False or report["artifacts"] != _artifacts(owner)):
        raise EvaluationError("source-view raw/source/config/code/ownership identity changed", status="MISSING")
    if evidence.read_json(owner / "config.json") != config:
        raise EvaluationError("saved converter config differs from its pinned actual input")
    ownership = evidence.read_json(owner / "ownership.json")
    fields(ownership, {"schema", "nonce", "owner", "suite_id", "evidence_mode"}, "source ownership")
    try:
        valid_nonce = str(uuid.UUID(ownership["nonce"])) == ownership["nonce"]
    except (ValueError, TypeError, AttributeError):
        valid_nonce = False
    if (type(ownership["schema"]) is not int or ownership["schema"] != 1 or not valid_nonce
            or ownership["owner"] != str(owner) or ownership["suite_id"] != SUITE
            or ownership["evidence_mode"] != ("fixture" if fixture else "real")):
        raise EvaluationError("source capture ownership receipt differs")
    native = evidence.read_json(owner / "native/build.json")
    expected_files = {p: s["asset"]["sha256"] for p, s in bundle.sources.items()}
    fields(native, {"schema", "suite_id", "identity", "files", "materialization", "diagnostic_only"}, "native build")
    if (type(native["schema"]) is not int or native["schema"] != 2 or native["suite_id"] != "product.v4"
            or native["diagnostic_only"] is not fixture
            or sha256_file(owner / "native/build.json") != report["native_build_sha256"] or native["identity"] != bundle.identity
            or native["files"] != expected_files):
        raise EvaluationError("native input build identity differs")
    if _corpus_files(owner / "native/corpus") != expected_files:
        raise EvaluationError("native corpus contains missing/extra/changed source assets")
    materialization.validate(owner / "native/corpus", bundle.sources, native["materialization"])
    if not isinstance(report["sources"], list) or any(not isinstance(r, dict) for r in report["sources"]) or [r.get("source_path") for r in report["sources"]] != list(bundle.sources):
        raise EvaluationError("source-view denominator/order differs")
    names = common_names(bundle)
    if set(_corpus_files(owner / "common/corpus")) != set(names.values()):
        raise EvaluationError("common corpus contains missing/extra source identities")
    for ordinal, (source, saved) in enumerate(zip(bundle.sources.values(), report["sources"]), 1):
        case = owner / "work" / f"{ordinal:04d}"
        if evidence.read_json(case / "source.json") != saved:
            raise EvaluationError("durable per-source receipt differs from its aggregate row")
        actual, text = one_source(bundle, source, owner, case, resources, config, config_ref, code, saved=saved)
        actual["common_path"] = names[source["path"]]
        if actual != saved or (owner / "common/corpus" / actual["common_path"]).read_bytes() != text.encode("utf-8"):
            raise EvaluationError("common body/coverage/map differs from replayed original-byte conversion")
    expected_status = ("MECHANICAL_CAPTURE_COMPLETE" if fixture else "CAPTURED_FULL_SOURCES") if all(r["status"] == "COMPLETE_CONVERSION" for r in report["sources"]) else "MISSING"
    if report["status"] != expected_status:
        raise EvaluationError("source capture status was relabeled")
    bundle.verify()
    return report
