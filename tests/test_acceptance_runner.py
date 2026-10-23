"""Acceptance must fail closed on incomplete, skipped, or edited evidence."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from acceptance_lib import checks, runner
from acceptance_lib.evidence import (InvalidEvidence, artifact, bound_path, candidate_content,
    capture_command, external_ref, read_command_run, read_json, sha256, write_json)
from acceptance_lib.registry import TASK_TESTS, classify, expand

REAL_CODE_IDENTITY = runner.code_identity


@pytest.fixture
def mini_root(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "scripts/acceptance_lib").symlink_to(SCRIPTS / "acceptance_lib", target_is_directory=True)
    contract = root / "docs/execution-plan/CONTRACTS.md"
    contract.parent.mkdir(parents=True)
    contract.write_text("Frozen miniature contract\n")
    write_json(root / "docs/execution-plan/tasks.json", {"tasks": [
        {"id": task, "depends_on": []} for task in TASK_TESTS]})
    (root / "evals/history/20261021").mkdir(parents=True)
    original = root / "evals/old.json"
    original.write_text('{"old": true}\n')
    shutil.copy2(original, root / "evals/history/20261021/old.json")
    write_json(root / "docs/execution-plan/baseline-20261021.json",
               {"sha256": {"evals/old.json": sha256(original)}})
    write_json(root / "docs/execution-plan/baseline-divergence-20261023.json",
               {"schema": 1, "kind": "baseline-authorized-divergence", "files": {}})
    write_json(root / "evals/acceptance_targets.json",
               {"schema": 1, "contract_sha256": sha256(contract)})
    write_json(root / "evals/acceptance_test_inventory.json", {"schema": 1, "tests": [
        {"nodeid": "tests/test_canary.py::test_one", "profile": "offline"}]})
    (root / "tests/test_canary.py").write_text("def test_one():\n    assert 1 + 1 == 2\n")
    monkeypatch.setattr(runner, "code_identity", lambda *args: {
        "git_commit": "1" * 40, "dirty_diff_sha256": "2" * 64,
        "untracked_sha256": {"synthetic_fixture": "3" * 64},
        "content_sha256": candidate_content(args[0])})
    return root


def test_real_offline_pass_and_repeat_validate(mini_root, tmp_path):
    out = tmp_path / "good"
    report = runner.run(mini_root, out, "offline")
    assert report["status"] == "PASS"
    assert report["dirty_diff_sha256"] is not None
    assert runner.validate(mini_root, out / "manifest.json") == report
    assert runner.validate(mini_root, out / "manifest.json") == report


@pytest.mark.parametrize(("body", "expected"), [
    ("print('PASS')\n    assert False", "FAIL"),
    ("import pytest\n    pytest.skip('missing optional tool')", "MISSING"),
])
def test_stdout_pass_and_skip_cannot_turn_green(mini_root, tmp_path, body, expected):
    (mini_root / "tests/test_canary.py").write_text("def test_one():\n    " + body + "\n")
    out = tmp_path / "bad"
    report = runner.run(mini_root, out, "offline")
    assert report["status"] == expected
    assert runner.validate(mini_root, out / "manifest.json")["status"] == expected


def test_zero_collection_is_missing(mini_root, tmp_path):
    (mini_root / "tests/test_canary.py").write_text("# no tests\n")
    out = tmp_path / "empty"
    report = runner.run(mini_root, out, "offline")
    assert report["status"] == "MISSING"
    assert runner.validate(mini_root, out / "manifest.json")["status"] == "MISSING"


def test_missing_task_file_and_no_holdout_side_effect(mini_root, tmp_path):
    out = tmp_path / "task"
    report = runner.run(mini_root, out, "task-check", task="P1-02")
    assert report["status"] == "MISSING"
    assert not any("holdout" in c for c in report["required_checks"])
    assert "retrieval.legacy" not in report["required_checks"]
    assert runner.validate(mini_root, out / "manifest.json")["status"] == "MISSING"


def test_full_profile_has_missing_real_dependencies(mini_root, tmp_path, monkeypatch):
    # Avoid an unrelated synthetic source suite; missing adapters may never PASS.
    monkeypatch.setattr(runner, "run_pytest", lambda *args: ("MISSING", {"reason": "fixture only"}, {}))
    report = runner.run(mini_root, tmp_path / "release", "local_rc")
    assert report["status"] == "MISSING"
    assert "release.holdout" in report["missing"]
    assert "release.ci_hosted" not in report["required_checks"]
    assert "release.ci_hosted" in [c.id for c in expand("hosted_release")]
    assert "tasks.json" not in json.dumps(report["checks"])


@pytest.mark.parametrize("mutation", ["status", "duplicate", "unknown", "missing", "targets"])
def test_edited_report_rejected(mini_root, tmp_path, mutation):
    out = tmp_path / "tamper"
    report = runner.run(mini_root, out, "offline")
    if mutation == "status":
        report["checks"][0]["status"] = "FAIL"
    elif mutation == "duplicate":
        report["checks"].append(report["checks"][0])
    elif mutation == "unknown":
        report["required_checks"].append("injected.pass")
    elif mutation == "missing":
        report["checks"].pop()
    else:
        report["targets_sha256"] = "0" * 64
    write_json(out / "manifest.json", report)
    with pytest.raises(InvalidEvidence):
        runner.validate(mini_root, out / "manifest.json")


def test_deleted_log_is_not_replaced_by_summary(mini_root, tmp_path):
    out = tmp_path / "deleted-log"
    runner.run(mini_root, out, "offline")
    (out / "checks/pytest.offline/execution/stdout.log").unlink()
    with pytest.raises(InvalidEvidence, match="summary"):
        runner.validate(mini_root, out / "manifest.json")


@pytest.mark.parametrize("name", ["goldmine/core.py", "scripts/validator.py", "tests/support.py",
                                  "requirements/runtime.lock", ".github/workflows/verify.yml", "pyproject.toml"])
def test_changed_candidate_content_rejects_previously_green_report(mini_root, tmp_path, name):
    target = mini_root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# original candidate content\n")
    out = tmp_path / "content-proof"
    assert runner.run(mini_root, out, "offline")["status"] == "PASS"
    target.write_text("# changed after the passing run\n")
    with pytest.raises(InvalidEvidence, match="source.stable"):
        runner.validate(mini_root, out / "manifest.json")


def test_commit_and_derived_progress_do_not_expire_unchanged_content(mini_root, tmp_path, monkeypatch):
    def git(*arguments):
        return subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *arguments],
                              cwd=mini_root, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "fixture@invalid.example")
    git("config", "user.name", "Fixture")
    git("add", ".")
    git("commit", "-qm", "initial miniature candidate")
    monkeypatch.setattr(runner, "code_identity", REAL_CODE_IDENTITY)
    out = tmp_path / "commit-proof"
    report = runner.run(mini_root, out, "offline")
    assert report["status"] == "PASS"
    git("commit", "--allow-empty", "-qm", "metadata-only commit")
    assert runner.code_identity(mini_root)["git_commit"] != report["git_commit"]
    tasks_path = mini_root / "docs/execution-plan/tasks.json"
    tasks = read_json(tasks_path)
    for task in tasks["tasks"]:
        task["state"] = "VERIFIED"
    write_json(tasks_path, tasks)
    (mini_root / "docs/execution-plan/PROGRESS.md").write_text("Derived from the successful raw checks.\n")
    (mini_root / "reports").mkdir()
    (mini_root / "reports/note.txt").write_text("Unrelated local report output.\n")
    assert runner.validate(mini_root, out / "manifest.json") == report


def test_capture_uses_argv_and_preserves_real_status(tmp_path):
    marker = "$(false); `false` with space"
    capture_command(tmp_path / "cmd", "test.command", [sys.executable, "-c",
        "import sys; print(sys.argv[1]); raise SystemExit(1)", marker], cwd=tmp_path)
    manifest, receipt, stdout = read_command_run(tmp_path / "cmd/manifest.json")
    assert stdout.read_text().strip() == marker
    assert receipt["exit_code"] == 1
    assert manifest["run_id"] == receipt["run_id"]


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}'])
def test_json_rejects_duplicate_keys_and_nonfinite(tmp_path, text):
    path = tmp_path / "invalid.json"
    path.write_text(text)
    with pytest.raises(InvalidEvidence):
        read_json(path)


def test_artifact_traversal_and_symlink_escape(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    for bad in ("../file", "escape/file", str(tmp_path / "file")):
        with pytest.raises(InvalidEvidence):
            bound_path(root, bad)


def test_external_asset_needs_actual_sha_and_explicit_path(tmp_path):
    path = tmp_path / "model.gguf"
    path.write_bytes(b"fixture")
    ref = {"path": path.name, "sha256": sha256(path)}
    assert external_ref(tmp_path, ref) == path
    path.write_bytes(b"corrupt")
    with pytest.raises(InvalidEvidence):
        external_ref(tmp_path, ref)
    with pytest.raises(InvalidEvidence):
        checks.Inputs({"schema": 2, "path_base": str(tmp_path), "commands": ["true"]})


def test_registry_does_not_use_state_or_require_future_checks_for_task():
    ids = [c.id for c in expand("task-check", "P1-02")]
    assert ids == ["task.P1-02", "retrieval.product_dev", "source.stable"]
    assert all("status" not in c.kind for c in expand("local_rc"))
    assert classify("tests/test_model.py::test_manifest_matches_files_on_disk") == "offline"
    assert checks.percentile(list(range(1, 31)), .95) == 29


def test_receipt_identity_and_hash_rejection(tmp_path):
    directory = tmp_path / "cmd"
    capture_command(directory, "test.identity", [sys.executable, "-c", "print('PASS')"], cwd=tmp_path)
    with pytest.raises(InvalidEvidence, match="suite"):
        read_command_run(directory / "manifest.json", suite_id="wrong.suite")
    (directory / "receipt.json").write_text('{"exit_code":0}')
    with pytest.raises(InvalidEvidence, match="changed artifact"):
        read_command_run(directory / "manifest.json")
