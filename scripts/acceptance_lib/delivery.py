"""Recheck the fixed installed-distribution proof from bound commands and assets."""
from __future__ import annotations

from pathlib import Path

from .evidence import InvalidEvidence, external_ref, read_command_run, read_json, sha256, verify_artifact


def _ref(root, mapping, key):
    return external_ref(root, mapping[key], manifest=True)


def _command(root, row, *, suite, identity):
    path = verify_artifact(root, row["manifest"])
    manifest, receipt, stdout = read_command_run(path, suite_id=suite)
    if any(manifest["identity"].get(key) != value for key, value in identity.items()):
        raise InvalidEvidence("delivery command identity differs from pinned artifacts")
    if receipt["timed_out"]:
        raise InvalidEvidence("delivery command timed out")
    return manifest, receipt, stdout


def _smoke(path, wheel, native_pin, *, profile, native_required):
    result = read_json(path)
    root = path.parent
    if result.get("profile") != profile or result.get("wheel") != wheel:
        raise InvalidEvidence("installed smoke uses a different wheel/profile")
    if (result.get("offline_install") is not True or result.get("source_directory_imported") is not False or
            result.get("provenance", {}).get("modules") != wheel["source_modules"]):
        raise InvalidEvidence("installed source provenance is absent or differs from wheel bytes")
    commands = result.get("commands", [])
    by_name = {row["name"]: row for row in commands}
    required = {"install", "provenance", "missing-native-tools", "version", "help", "mcp-help", "ingest-help",
                "imports", "ingest", "search", "status", "doctor", "missing-model",
                "missing-sdk" if profile == "base" else "mcp-protocol"}
    if profile == "mcp":
        required.add("incompatible-sdk")
    if native_required:
        required.update({"native-version", "ocr-version", "ocr-fixture", "native-ocr", "native-embedding",
                         "native-dependencies-llama-server", "native-dependencies-goldmine-ocr"})
    if len(by_name) != len(commands) or set(by_name) != required:
        raise InvalidEvidence("installed smoke lacks fixed required command identities")
    for name, row in by_name.items():
        _, receipt, stdout = _command(root, row, suite="distribution.v1", identity={"wheel_sha256": wheel["sha256"]})
        expected = {"doctor": 4, "missing-model": 2, "missing-sdk": 4, "incompatible-sdk": 4}.get(name, 0)
        if receipt["exit_code"] != expected:
            raise InvalidEvidence(f"installed smoke command failed: {name}")
        if name == "install" and "--no-index" not in receipt["argv"]:
            raise InvalidEvidence("wheel smoke did not install offline")
        if name == "provenance":
            provenance = read_json(stdout)
            if provenance != result["provenance"] or provenance["mcp"] != (profile == "mcp"):
                raise InvalidEvidence("installed provenance differs from raw Python output")
            if not Path(provenance["file"]).resolve().is_relative_to((root / "venv").resolve()):
                raise InvalidEvidence("checkout masked installed package")
        if name == "missing-native-tools" and any(read_json(stdout).values()):
            raise InvalidEvidence("base smoke had undeclared native tools")
        if name == "native-embedding" and stdout.read_text().strip() != "native embedding dimension=768":
            raise InvalidEvidence("actual native embedding proof missing")
        if name.startswith("native-dependencies-"):
            from native_build_lib.source import system_dependencies

            system_dependencies(stdout.read_text())
        if name == "native-ocr":
            ocr = read_json(stdout)
            if ocr.get("error") or "ZIRCON LANTERN" not in " ".join(r.get("text", "") for r in ocr.get("texts", [])).upper():
                raise InvalidEvidence("raw OCR did not recognize the generated proof image")
    if native_required and (not result.get("native_embedding_exercised") or result.get("native_tar_sha256") != native_pin):
        raise InvalidEvidence("native smoke did not exercise the exact built native asset")


def verify_bundle(path: Path):
    from distribution_lib import inspect_wheel
    from native_build_lib.source import PATCH_SHA256, REVISION
    from search_bench.session import package_identity

    bundle = read_json(path)
    if set(bundle) != {"schema", "kind", "evidence"} or bundle["schema"] != 1 or bundle["kind"] != "goldmine.distribution-bundle":
        raise InvalidEvidence("distribution bundle needs the fixed schema1 identity")
    refs = bundle["evidence"]
    required = {"build", "sdist_build", "base", "mcp", "rebuilt", "upgrade", "native", "twine"}
    if set(refs) != required:
        raise InvalidEvidence("distribution bundle lacks fixed required evidence")
    root = Path(__file__).resolve().parents[2]
    build_path, rebuilt_path = (_ref(path.parent, refs, key) for key in ("build", "sdist_build"))
    build, rebuilt = read_json(build_path), read_json(rebuilt_path)
    snapshot = build.get("source_snapshot", {})
    if {name: pin for name, pin in snapshot.items() if name.startswith("goldmine/")} != package_identity(root):
        raise InvalidEvidence("Python artifact was built from an earlier candidate source")
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        if snapshot.get(name) != sha256(root / name):
            raise InvalidEvidence(f"Python artifact metadata/source changed: {name}")
    assets = build.get("assets", {})
    wheels = [verify_artifact(build_path.parent, ref) for name, ref in assets.items() if name.endswith(".whl")]
    sdists = [verify_artifact(build_path.parent, ref) for name, ref in assets.items() if name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1 or rebuilt.get("from_sdist") is not True or rebuilt.get("source_snapshot") != {"sdist_sha256": sha256(sdists[0])}:
        raise InvalidEvidence("wheel/sdist identity or exact sdist rebuild missing")
    rebuilt_wheels = [verify_artifact(rebuilt_path.parent, ref) for name, ref in rebuilt.get("assets", {}).items() if name.endswith(".whl")]
    if len(rebuilt_wheels) != 1:
        raise InvalidEvidence("sdist rebuild must produce exactly one wheel")
    _, twine, _ = read_command_run(_ref(path.parent, refs, "twine"), suite_id="distribution.twine.v1")
    expected_assets = {str(p.resolve()) for p in (*wheels, *sdists, *rebuilt_wheels)}
    if (twine["exit_code"] or twine["timed_out"] or twine["argv"][1:4] != ["-m", "twine", "check"] or
            set(twine["argv"][4:]) != expected_assets):
        raise InvalidEvidence("twine did not successfully inspect these exact Python artifacts")
    wheel, rebuilt_wheel = inspect_wheel(wheels[0]), inspect_wheel(rebuilt_wheels[0])
    if wheel["source_modules"] != rebuilt_wheel["source_modules"] or wheel["version"] != rebuilt_wheel["version"]:
        raise InvalidEvidence("sdist rebuilt different installed source or version")
    for manifest_path, data in ((build_path, build), (rebuilt_path, rebuilt)):
        _, receipt, _ = read_command_run(verify_artifact(manifest_path.parent, data["command"]), suite_id="distribution.build.v1")
        if receipt["exit_code"] or receipt["timed_out"]:
            raise InvalidEvidence("Python build command failed")
    native_path = _ref(path.parent, refs, "native")
    native = read_json(native_path)
    native_tar = verify_artifact(native_path.parent, native["asset"])
    if (native.get("release_eligible") is not True or native.get("revision") != REVISION or
            native.get("patch_sha256") != PATCH_SHA256 or native.get("patched_diff_sha256") != PATCH_SHA256 or
            native.get("runtime_dyld_overrides") is not False or native.get("architecture") != "arm64"):
        raise InvalidEvidence("native source/patch/architecture/dependency proof incomplete")
    if native.get("ocr_source_sha256") != sha256(root / "tools/ocr/ocr_helper.swift"):
        raise InvalidEvidence("native OCR source changed since build")
    from .evidence import bound_path
    from native_build_lib.source import system_dependencies

    native_logs = bound_path(native_path.parent, native["command_logs"])
    log_manifests = sorted(native_logs.glob("*/manifest.json"))
    counts = {"cmake": 0, "swiftc": 0, "lipo": 0, "otool": 0, "verify": 0}
    for raw_path in log_manifests:
        manifest, receipt, stdout = read_command_run(raw_path, suite_id="native.build.v1")
        if receipt["exit_code"] or receipt["timed_out"] or any(manifest["identity"].get(k) != native[k] for k in ("revision", "patch_sha256", "patched_diff_sha256")):
            raise InvalidEvidence("native raw build command failed or uses another source")
        tool = Path(receipt["argv"][0]).name
        if tool in counts:
            counts[tool] += 1
        if tool == "lipo" and stdout.read_text().strip() != "arm64":
            raise InvalidEvidence("native executable is not measured arm64")
        if tool == "otool":
            system_dependencies(stdout.read_text())
        if tool == "codesign" and "--verify" in receipt["argv"]:
            counts["verify"] += 1
    if any(counts[key] < minimum for key, minimum in {"cmake": 2, "swiftc": 1, "lipo": 2, "otool": 2, "verify": 2}.items()):
        raise InvalidEvidence("native command inventory lacks actual build/architecture/link/signature checks")
    for profile in ("base", "mcp", "rebuilt"):
        _smoke(_ref(path.parent, refs, profile), rebuilt_wheel if profile == "rebuilt" else wheel,
               sha256(native_tar), profile="mcp" if profile == "mcp" else "base", native_required=profile == "base")
    upgrade_path = _ref(path.parent, refs, "upgrade")
    upgrade = read_json(upgrade_path)
    if (upgrade.get("kind") != "goldmine.installed-upgrade" or upgrade.get("identity", {}).get("wheel_sha256") != wheel["sha256"] or
            not all(upgrade.get(key) is True for key in ("readonly_legacy", "future_schema_refused", "stale_citation_refused"))):
        raise InvalidEvidence("installed v4/v6/backup/rollback evidence incomplete")
    required_upgrade = {"install-old", "provenance-old", "install-new", "provenance-new", "v4-ingest", "v4-readonly", "v4-backup", "v6-reindex",
                        "citation-create", "citation-read", "v6-modify", "citation-stale", "future-schema", "rollback-read"}
    rows = upgrade.get("commands", [])
    if len(rows) != len(required_upgrade) or {row["name"] for row in rows} != required_upgrade:
        raise InvalidEvidence("upgrade command set is incomplete")
    for row in rows:
        _, receipt, _ = _command(upgrade_path.parent, row, suite="distribution.upgrade.v1", identity=upgrade["identity"])
        if receipt["exit_code"] != (1 if row["name"] == "future-schema" else 0):
            raise InvalidEvidence("installed upgrade command failed")
    return {"wheel_sha256": wheel["sha256"], "sdist_sha256": sha256(sdists[0]),
            "native_tar_sha256": sha256(native_tar), "version": wheel["version"]}


def distribution(inputs, targets):
    result = verify_bundle(inputs.file("evidence.distribution", manifest=True))
    for key, output in (("wheel", "wheel_sha256"), ("sdist", "sdist_sha256"), ("native_tar", "native_tar_sha256")):
        if sha256(inputs.file(f"assets.{key}")) != result[output]:
            raise InvalidEvidence("distribution artifact differs from explicitly pinned release assets")
    return "PASS", result
