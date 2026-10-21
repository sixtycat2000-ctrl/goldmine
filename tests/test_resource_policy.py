"""Offline policy, receipt and ownership negatives; no native counter required."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import sys
import time
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from acceptance_lib.evidence import InvalidEvidence, capture_command, read_command_run
from acceptance_lib.resource_policy import LIMITS, policy, validate_policy
from acceptance_lib.resource_validation import validate_observation
from acceptance_lib import resource_tools
SOURCE = {"fixture": True, "implementation": {"revision": "fixture-v1"},
          "packages": [], "models": [], "view": "tiny", "arm": "python", "projection_sha256": "1" * 64}

def test_formal_contract_fixed_caps_and_deadlines(tmp_path):
    for phase, seconds in (("prepare", 1800), ("query", 600)):
        value = policy(phase, tmp_path, source=SOURCE)
        assert value["limits"] == LIMITS and value["wall_seconds"] == seconds
        assert value["mode"] == "comparison"


@pytest.mark.parametrize("field,value", [("owned_root", []), ("source", []), ("limits", []),
                                       ("phase", []), ("mode", []), ("wall_seconds", True),
                                       ("interval_seconds", float("nan"))])
def test_malformed_policy_rejected_before_spawn(tmp_path, field, value):
    registered = policy("query", tmp_path, source=SOURCE)
    registered[field] = value
    with pytest.raises(InvalidEvidence):
        validate_policy(registered)


def test_formal_cannot_weaken_registered_limits(tmp_path):
    value = policy("query", tmp_path, source=SOURCE)
    value["limits"]["nofile"] = 1024
    with pytest.raises(InvalidEvidence):
        validate_policy(value)
    value = policy("query", tmp_path, source=SOURCE)
    value["wall_seconds"] = 599
    with pytest.raises(InvalidEvidence):
        validate_policy(value)


@pytest.mark.parametrize("node", ["symlink", "hardlink", "fifo"])
def test_owned_output_special_or_external_alias_is_missing(tmp_path, node):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "external"
    outside.write_bytes(b"private-fixture")
    if node == "symlink":
        (root / "alias").symlink_to(outside)
    elif node == "hardlink":
        os.link(outside, root / "alias")
    else:
        os.mkfifo(root / "alias")
    info = root.stat()
    with pytest.raises(OSError, match="symlink/special|hardlink"):
        resource_tools.disk_usage(root, (info.st_dev, info.st_ino), time.monotonic() + 1)


def test_default_capture_does_not_import_observer_or_claim_resources(tmp_path):
    code = "from pathlib import Path;from acceptance_lib.evidence import capture_command;import sys;capture_command(Path(sys.argv[1]),'default',[sys.executable,'-c','print(7)'],cwd=Path.cwd(),owned_process_group=True);assert not any('resource_' in name for name in sys.modules if name.startswith('acceptance_lib.'))"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "scripts"), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([sys.executable, "-c", code, str(tmp_path / "default")], env=env, capture_output=True, timeout=3)
    assert proc.returncode == 0, proc.stderr
    manifest, receipt, _ = read_command_run(tmp_path / "default" / "manifest.json")
    assert "resource_observer" not in manifest["identity"] and receipt["artifacts"] == {}


def test_tool_partial_output_obeys_absolute_deadline(tmp_path, monkeypatch):
    actual, children = resource_tools.subprocess.Popen, []
    def fixture_tool(_argv, **kwargs):
        child = actual([sys.executable, "-I", "-S", "-c", "import os,time;os.write(1,b'{');time.sleep(.3)"], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(resource_tools.subprocess, "Popen", fixture_tool)
    start = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            resource_tools.tool(resource_tools.PS, start + .05)
        assert time.monotonic() - start < .2 and children[0].returncode is not None
    finally:
        for child in children:
            if child.returncode is None:
                child.kill()
                child.wait(timeout=1)


@pytest.mark.parametrize("leader_unreaped", [False, True])
def test_zombie_exception_requires_exact_protected_direct_leader(monkeypatch, leader_unreaped):
    def vanished(_pid):
        raise ProcessLookupError(3, "fixture exited process")
    monkeypatch.setattr(resource_tools.os, "getpgid", vanished)
    monkeypatch.setattr(resource_tools.os, "getsid", vanished)
    row = {"pid": 100, "ppid": 1, "pgid": 100, "rss_bytes": 0, "birth": "fixture", "state": "Z"}
    selected, gaps = resource_tools.owned_rows([row], 100, {}, leader_unreaped=leader_unreaped)
    assert selected == ([row] if leader_unreaped else [])
    assert bool(gaps) is (not leader_unreaped)


def test_reused_pid_or_escaped_group_never_enters_owned_counters(monkeypatch):
    rows = [
        {"pid": 100, "ppid": 1, "pgid": 100, "rss_bytes": 10, "birth": "root", "state": "R"},
        {"pid": 200, "ppid": 100, "pgid": 200, "rss_bytes": 20, "birth": "child", "state": "R"},
        {"pid": 300, "ppid": 1, "pgid": 400, "rss_bytes": 30, "birth": "new-birth", "state": "R"},
    ]
    monkeypatch.setattr(resource_tools.os, "getpgid", lambda pid: {100: 100, 200: 200, 300: 400}[pid])
    monkeypatch.setattr(resource_tools.os, "getsid", lambda pid: 100 if pid != 300 else 400)
    selected, gaps = resource_tools.owned_rows(rows, 100, {300: "old-birth"}, leader_unreaped=True)
    assert [row["pid"] for row in selected] == [100]
    assert gaps == ["observed-owned-descendant-escaped-private-group"]


def test_missing_unreaped_leader_is_not_a_complete_snapshot():
    with pytest.raises(OSError, match="leader absent"):
        resource_tools.owned_rows([], 100, {}, leader_unreaped=True)


@pytest.mark.parametrize("schema", ["process", "disk", "tool"])
def test_malformed_raw_counter_shapes_raise_evidence_error(schema):
    from acceptance_lib.resource_validation import _disk, _processes, _tools
    with pytest.raises(InvalidEvidence):
        if schema == "process":
            _processes([1], 100)
        elif schema == "disk":
            _disk([1])
        else:
            _tools([1], [])


def test_plain_receipt_is_missing_and_never_promoted_to_observed_pass(tmp_path):
    registered = policy("query", tmp_path, source=SOURCE)
    argv = [str(Path(sys.executable).resolve()), "-c", "print('PASS')"]
    capture_command(tmp_path / "plain", "fixture", argv, cwd=tmp_path)
    result = validate_observation(tmp_path / "plain" / "manifest.json", registered, argv, SOURCE)
    assert result["status"] == "MISSING" and result["full_tree_ceiling_status"] == "MISSING"


def test_release_without_exec_never_classifies_bridge_rss_as_target(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from acceptance_lib.resource_observer import Observer
    obj = Observer.__new__(Observer)
    obj.proc = SimpleNamespace(pid=1000, returncode=None)
    obj.started, obj.deadline = time.monotonic() - 1, time.monotonic() + 1
    obj.ready_at, obj.target_exec_seen, obj.target_exec_at = None, False, None
    obj.policy, obj.root, obj.root_identity = {"limits": LIMITS}, tmp_path, (0, 0)
    obj.previous, obj.gaps, obj.violations = {}, set(), []
    obj.samples = obj.callback_seconds = 0
    for name in ("tree_peak", "root_peak", "child_peak", "fd_peak", "disk_peak", "stdout_peak", "stderr_peak"):
        setattr(obj, name, 0)
    records = []
    obj._record, obj._watch, obj._events = records.append, lambda _pid: None, lambda _timeout=0: []
    row = {"pid": 1000, "ppid": 1, "pgid": 1000, "rss_bytes": 10, "birth": "fixture", "state": "R"}
    monkeypatch.setattr(resource_tools, "census", lambda *_a: ([dict(row)], {"fixture": True}))
    monkeypatch.setattr(resource_tools, "owned_rows", lambda *_a, **_k: ([dict(row)], []))
    monkeypatch.setattr(resource_tools, "descriptors", lambda *_a: (3, None, None))
    monkeypatch.setattr(resource_tools.os, "getpgid", lambda _pid: 1000)
    monkeypatch.setattr(resource_tools.os, "getsid", lambda _pid: 1000)
    monkeypatch.setattr(resource_tools, "disk_usage", lambda *_a: {"logical_bytes": 0, "allocated_bytes": 0,
                        "entries": 0, "raced_entries": 0, "elapsed_seconds": 0})
    with (tmp_path / "out").open("wb") as out, (tmp_path / "err").open("wb") as err:
        obj.stdout, obj.stderr = out, err
        obj.sample()
        obj.ready_at = .5  # Parent release is not proof that exec has happened.
        obj.sample()
    assert [row["stage"] for row in records] == ["bridge", "bridge"]


@pytest.mark.parametrize("case", ["missing", "duplicate", "zombie", "live", "unknown"])
def test_fd_receipts_link_one_to_one_with_live_counts(case):
    from acceptance_lib.resource_validation import _tools
    def tool(argv):
        return {"argv": argv, "elapsed_seconds": 0.001, "exit_code": 0, "stdout_bytes": 1,
                "stderr_bytes": 0, "stdout_sha256": "1" * 64, "stderr_sha256": "2" * 64}
    process = {"pid": 123, "state": "R", "fd_count": 5}
    lsof = tool(["/usr/sbin/lsof", "-nP", "-w", "-a", "-p", "123", "-Fpf"])
    tools, rows = [tool(resource_tools.PS)], [process]
    if case == "duplicate":
        rows.append({"pid": 124, "state": "R", "fd_count": 5})
        tools += [lsof, dict(lsof)]
    elif case == "zombie":
        process.update(state="Z", fd_count=0)
    elif case == "live":
        tools.append(lsof)
    elif case == "unknown":
        process["fd_count"] = None
    if case in {"missing", "duplicate"}:
        with pytest.raises(InvalidEvidence, match="descriptor"):
            _tools(tools, rows)
    else:
        _tools(tools, rows)  # Structural control only; no actual-job claim.


@pytest.mark.parametrize("gap", ["observed-owned-descendant-escaped-private-group", "ownership-lost-during-counter-read",
    "fd-census-unavailable-or-process-exited", "owned-output-census-race", "no-live-target-rss-sample",
    "fixture-only-policy", "wall-deadline-exceeded", "fork-descendant-lifetimes-not-atomically-tracked-extra"])
def test_sampled_status_does_not_launder_other_coverage_gaps(gap):
    from acceptance_lib.resource_validation import _resource_statuses
    caveat = "fork-descendant-lifetimes-not-atomically-tracked"
    original, sampled, remaining = _resource_statuses({"exit_code": 0}, {caveat, gap}, [])
    assert original == sampled == "MISSING" and remaining == {gap}


@pytest.mark.parametrize("exit_code,violations", [(0, [{"metric": "owned_bytes"}]), (1, []), (127, [])])
def test_sampled_status_never_waives_failure_or_incomplete_command(exit_code, violations):
    from acceptance_lib.resource_validation import _resource_statuses
    original, sampled, _ = _resource_statuses({"exit_code": exit_code},
        {"fork-descendant-lifetimes-not-atomically-tracked"}, violations)
    assert original == sampled == ("MISSING" if exit_code == 127 else "FAIL")

