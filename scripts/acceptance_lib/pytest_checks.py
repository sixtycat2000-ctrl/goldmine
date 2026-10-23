"""Collect and validate pytest execution, including setup/teardown and skips."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

from .evidence import (InvalidEvidence, artifact, capture_command, process_status,
                       read_command_run, read_json, verify_artifact)
from .registry import LIVE_TESTS, TASK_TESTS, classify


def junit_results(path: Path) -> dict[str, str]:
    try:
        xml = ET.parse(path)
    except (OSError, ET.ParseError) as exc:
        raise InvalidEvidence(f"invalid JUnit: {exc}") from exc
    outcomes = {}
    for case in xml.iter("testcase"):
        identities = [p.get("value") for p in case.findall("./properties/property")
                      if p.get("name") == "acceptance_nodeid"]
        if len(identities) != 1 or identities[0] in outcomes:
            raise InvalidEvidence("JUnit needs one unique original nodeid per testcase")
        status = "FAIL" if case.find("failure") is not None or case.find("error") is not None else "MISSING" if case.find("skipped") is not None else "PASS"
        outcomes[identities[0]] = status
    if not outcomes:
        raise InvalidEvidence("zero JUnit tests is not success")
    return outcomes


def _expected(root: Path, profile: str) -> set[str]:
    manifest = read_json(root / "evals/acceptance_test_inventory.json")
    inventory = manifest.get("tests")
    if not isinstance(inventory, list) or not inventory:
        raise InvalidEvidence("original test inventory missing or empty")
    expected, all_ids = set(), set()
    for entry in inventory:
        nodeid = entry.get("nodeid")
        if not isinstance(nodeid, str) or nodeid in all_ids:
            raise InvalidEvidence("original inventory has invalid/duplicate nodeids")
        all_ids.add(nodeid)
        if entry.get("profile") != classify(nodeid, entry.get("markers", [])):
            raise InvalidEvidence("test classification changed without inventory migration")
        if profile in LIVE_TESTS:
            selected = Path(nodeid.split("::")[0]).name in LIVE_TESTS[profile] and entry["profile"] == "live"
        else:
            selected = (Path(nodeid.split("::")[0]).name in TASK_TESTS[profile] and entry["profile"] != "live") if profile in TASK_TESTS else entry["profile"] == profile
        if selected:
            expected.add(nodeid)
    return expected


def validate_pytest(root: Path, command_manifest: Path, collection_manifest: Path,
                    profile: str, *, identity: dict | None = None) -> tuple[str, dict]:
    collection, collect_receipt, _ = read_command_run(collection_manifest, check_id=f"pytest.{profile}.collection", suite_id=f"pytest.{profile}.collection")
    if identity is not None and collection["identity"] != identity:
        raise InvalidEvidence("pytest collection belongs to a different candidate")
    if collect_receipt["exit_code"] != 0:
        return "MISSING", {"reason": "pytest collection did not complete"}
    selected_path = verify_artifact(collection_manifest.parent,
                                    collect_receipt["artifacts"].get("inventory"))
    collected = read_json(selected_path)
    selected = collected.get("selected")
    if not isinstance(selected, list) or not selected or len(selected) != len(set(selected)):
        raise InvalidEvidence("empty/duplicate collection")
    if collected.get("collection_skips"):
        raise InvalidEvidence("collection skipped required module(s)")
    missing_original = _expected(root, profile) - set(selected)
    if missing_original:
        raise InvalidEvidence(f"required original nodeids disappeared: {sorted(missing_original)[:5]}")
    execution, receipt, _ = read_command_run(command_manifest, check_id=f"pytest.{profile}", suite_id=f"pytest.{profile}")
    if identity is not None and execution["identity"] != identity:
        raise InvalidEvidence("pytest execution belongs to a different candidate")
    status, reason = process_status(receipt)
    # pytest code5 is no tests, not a completed failing check.
    if receipt["exit_code"] in (2, 3, 4, 5, 127) or receipt["timed_out"]:
        return "MISSING", {"reason": reason, "exit_code": receipt["exit_code"]}
    junit = verify_artifact(command_manifest.parent, receipt["artifacts"].get("junit"))
    run_inventory = read_json(verify_artifact(command_manifest.parent,
                                            receipt["artifacts"].get("inventory")))
    outcomes = junit_results(junit)
    if set(outcomes) != set(selected) or set(run_inventory.get("selected", [])) != set(selected):
        raise InvalidEvidence("collected and executed nodeids differ")
    if run_inventory.get("collection_skips"):
        raise InvalidEvidence("required module skipped during actual run")
    failures = [node for node, outcome in outcomes.items() if outcome == "FAIL"]
    skipped = [node for node, outcome in outcomes.items() if outcome == "MISSING"]
    if failures or status == "FAIL":
        return "FAIL", {"n": len(outcomes), "failed": failures, "skipped": skipped,
                        "exit_code": receipt["exit_code"]}
    return ("MISSING" if skipped else "PASS"), {"n": len(outcomes), "failed": [],
                                               "skipped": skipped, "exit_code": receipt["exit_code"]}


def preflight_pytest(root: Path, profile: str) -> None:
    if profile in LIVE_TESTS and any(not (root / "tests" / name).is_file() for name in LIVE_TESTS[profile]):
        raise InvalidEvidence("required live test module missing")
    if profile in TASK_TESTS:
        absent = [name for name in TASK_TESTS[profile] if not (root / "tests" / name).is_file()]
        if absent:
            raise InvalidEvidence(f"required task test files not implemented: {absent}")
    if profile == "optional-mcp" and not (root / "tests/test_mcp.py").is_file():
        raise InvalidEvidence("MCP protocol tests not implemented")


def run_pytest(root: Path, out: Path, profile: str, identity: dict, *,
               python: str | None = None, capability_env: dict | None = None) -> tuple[str, dict, dict]:
    preflight_pytest(root, profile)
    if profile in TASK_TESTS:
        paths = [str(root / "tests" / name) for name in TASK_TESTS[profile]]
    elif profile in LIVE_TESTS:
        paths = [str(root / "tests" / name) for name in LIVE_TESTS[profile]]
    else:
        paths = ["tests"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GOLDMINE_")}
    home = out / "state"
    home.mkdir(parents=True)
    env.update({"GOLDMINE_HOME": str(home), "GOLDMINE_CONFIG": str(home / "config.toml"),
                "GOLDMINE_OCR_AUTOCOMPILE": "0",
                "PYTHONPATH": os.pathsep.join([str(root / "scripts"), str(root)])})
    if profile == "offline":
        env["GOLDMINE_ACCEPTANCE_HERMETIC"] = "1"
    if profile in LIVE_TESTS:
        env["GOLDMINE_LIVE"] = "1"
        if profile != "live-pack":
            env["GOLDMINE_LIVE_PRESSURE_PROFILE"] = profile.rsplit("-", 1)[-1]
        env.update(capability_env or {})
    prefix = [python or sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
              "-p", "acceptance_lib.pytest_plugin", "--acceptance-profile", profile]
    collection = out / "collection"
    capture_command(collection, f"pytest.{profile}.collection", prefix + ["--collect-only",
        "--acceptance-inventory", str(collection / "inventory.json"), *paths],
        cwd=root, env=env, identity=identity, suite_id=f"pytest.{profile}.collection",
        outputs={"inventory": "inventory.json"}, timeout=180)
    command = out / "execution"
    capture_command(command, f"pytest.{profile}", prefix + ["--acceptance-inventory",
        str(command / "inventory.json"), "--junitxml", str(command / "junit.xml"), *paths],
        cwd=root, env=env, identity=identity, suite_id=f"pytest.{profile}",
        outputs={"inventory": "inventory.json", "junit": "junit.xml"}, timeout=1800 if profile in LIVE_TESTS else 900)
    status, details = validate_pytest(root, command / "manifest.json", collection / "manifest.json", profile, identity=identity)
    refs = {"command": artifact(out, command / "manifest.json"),
            "collection": artifact(out, collection / "manifest.json")}
    return status, details, refs
