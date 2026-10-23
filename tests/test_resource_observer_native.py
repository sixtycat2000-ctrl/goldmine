"""Tiny real POSIX fixtures; none is a real-engine or full-comparison result."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from acceptance_lib.evidence import InvalidEvidence, artifact, capture_command, read_command_run, read_json, sha256, write_json
from acceptance_lib.resource_policy import policy
from acceptance_lib.resource_validation import validate_observation
from acceptance_lib import owned_process

@pytest.fixture(autouse=True)
def require_declared_native_platform():
    if sys.platform != "darwin":
        pytest.fail("this declared native profile requires real Darwin ps/lsof/kqueue; no skip-success")
SOURCE = {"fixture": True, "implementation": {"revision": "fixture-v1"},
          "packages": [], "models": [], "view": "tiny", "arm": "python", "projection_sha256": "1" * 64}


def execute(tmp_path, body="print('literal')", *, limits=None, monkeypatch=None):
    owner = tmp_path / "owner"
    owner.mkdir(exist_ok=True)
    value = policy("query", owner, source=SOURCE,
                   fixture_limits={"wall_seconds": 3, "interval_seconds": .03, **(limits or {})})
    inner = [str(Path(sys.executable).resolve()), "-I", "-S", "-c", body]
    run = owner / "phase"
    manifest = capture_command(run, "mechanical.resources", inner, cwd=tmp_path,
                               timeout=value["wall_seconds"], owned_process_group=True, resource_policy=value)
    return run, value, inner, manifest


def replay(run, value, inner):
    return validate_observation(run / "manifest.json", value, inner, SOURCE)


def rebind(run, *, observation=None, records=None, binding=None, receipt_change=None):
    """Coordinated hashes prevent tests from passing merely on a stale hash."""
    manifest, receipt, _ = read_command_run(run / "manifest.json")
    if records is not None:
        raw = "".join(json.dumps(row) + "\n" for row in records)
        (run / "resource-samples.jsonl").write_text(raw)
    if observation is None:
        observation = read_json(run / "resource-observation.json")
    observation["samples_sha256"] = sha256(run / "resource-samples.jsonl")
    if binding is not None:
        from acceptance_lib.resource_policy import digest
        manifest["identity"]["resource_observer"] = binding
        observation["binding_sha256"] = digest(binding)
    write_json(run / "resource-observation.json", observation)
    for key, name in (("resource_observation", "resource-observation.json"), ("resource_samples", "resource-samples.jsonl")):
        receipt["artifacts"][key] = artifact(run, run / name)
    if receipt_change:
        receipt_change(receipt)
    write_json(run / "receipt.json", receipt)
    manifest["receipt"] = artifact(run, run / "receipt.json")
    write_json(run / "manifest.json", manifest)








def test_actual_simple_command_raw_counters_and_hard_fd_handshake(tmp_path):
    before = resource.getrlimit(resource.RLIMIT_NOFILE)
    # 2s instead of the historical 0.08s: the observer must sample the
    # short-lived process and read its descriptors before it exits; shorter
    # sleeps raced on loaded CI runners ("process-disappeared-before-
    # ownership-check" / "ownership-lost-during-counter-read").
    run, value, inner, manifest = execute(tmp_path, "import resource,time; print(resource.getrlimit(resource.RLIMIT_NOFILE));time.sleep(2)")
    result = replay(run, value, inner)
    assert result["status"] == "MISSING" and result["coverage_gaps"] == ["fixture-only-policy"]
    assert result["sample_count"] >= 2 and result["peak"]["sampled_root_rss_bytes"] > 0
    assert "(512, 512)" in (run / "stdout.log").read_text()
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == before
    assert result["lifetime_complete"] is False and result["kernel_tree_peak"] is False


def test_actual_parent_child_rss_is_snapshot_sum_and_fork_is_missing(tmp_path):
    code = "import subprocess,sys,time; parent=bytearray(65536); p=subprocess.Popen([sys.executable,'-I','-S','-c','import time; child=bytearray(65536);time.sleep(.4)']);time.sleep(.3)"
    run, value, inner, _ = execute(tmp_path, code)
    result = replay(run, value, inner)
    assert result["status"] == "MISSING" and result["fork_events"] >= 1
    assert result["peak"]["sampled_children_rss_bytes"] > 0
    rows = [json.loads(line) for line in (run / "resource-samples.jsonl").read_text().splitlines()]
    for row in rows:
        if row["kind"] == "sample":
            assert row["tree_rss_bytes"] == sum(p["rss_bytes"] for p in row["processes"])
    assert not any("command" in str(key).lower() for row in rows for key in row)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_actual_stream_budget_fails_and_preserves_all_bytes(tmp_path, stream):
    fd = 1 if stream == "stdout" else 2
    run, value, inner, _ = execute(tmp_path, f"import os,time;os.write({fd},b'x'*8192);time.sleep(1)",
                                  limits={f"{stream}_bytes": 1024})
    result = replay(run, value, inner)
    assert result["status"] == "FAIL"
    assert any(v["metric"] == f"{stream}_bytes" and v["overshoot"] >= 7168 for v in result["violations"])
    assert (run / f"{stream}.log").read_bytes().startswith(b"x" * 8192)


def test_actual_owned_disk_budget_fails_without_deleting_output(tmp_path):
    path = tmp_path / "owner" / "payload.bin"
    run, value, inner, _ = execute(tmp_path, f"from pathlib import Path;import time;Path({str(path)!r}).write_bytes(b'x'*163840);time.sleep(1)",
                                  limits={"owned_bytes": 131072})
    result = replay(run, value, inner)
    assert result["status"] == "FAIL" and path.stat().st_size == 163840
    assert any(v["metric"] == "owned_bytes" for v in result["violations"])


def test_actual_fd_kernel_cap_does_not_change_parent(tmp_path):
    before = resource.getrlimit(resource.RLIMIT_NOFILE)
    body = "import os,resource,time;files=[]\ntry:\n while True:files.append(os.open('/dev/null',os.O_RDONLY))\nexcept OSError as exc:print('EMFILE',exc.errno,resource.getrlimit(resource.RLIMIT_NOFILE),flush=True)\ntime.sleep(.15)"
    run, value, inner, _ = execute(tmp_path, body, limits={"nofile": 32})
    result = replay(run, value, inner)
    assert "EMFILE 24 (32, 32)" in (run / "stdout.log").read_text()
    assert result["peak"]["sampled_per_process_fds"] == 32
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == before


def test_actual_timeout_retains_raw_and_reaps_only_owned_leader(tmp_path):
    unowned = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(5)"])
    try:
        run, value, inner, _ = execute(tmp_path, "import time;time.sleep(5)", limits={"wall_seconds": .3})
        _, receipt, _ = read_command_run(run / "manifest.json")
        result = replay(run, value, inner)
        assert receipt["timed_out"] is True and receipt["exit_code"] == 124
        assert result["status"] == "MISSING" and "wall-deadline-exceeded" in result["coverage_gaps"]
        assert unowned.poll() is None
        rows = [json.loads(line) for line in (run / "resource-samples.jsonl").read_text().splitlines()]
        assert unowned.pid not in {p["pid"] for row in rows if row["kind"] == "sample" for p in row["processes"]}
        obs = read_json(run / "resource-observation.json")
        with pytest.raises(ChildProcessError):
            os.waitpid(obs["root_pid"], os.WNOHANG)
    finally:
        unowned.terminate()
        unowned.wait(timeout=1)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_actual_cancellation_cleanup_and_receipt(tmp_path, signum):
    run, value, inner, _ = execute(tmp_path, f"import os,signal;os.kill(os.getppid(),{int(signum)})")
    _, receipt, _ = read_command_run(run / "manifest.json")
    assert receipt["exit_code"] == 128 + signum
    assert replay(run, value, inner)["status"] == "MISSING"


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_actual_cancel_at_popen_handle_handoff_reaps_bridge(tmp_path, monkeypatch, signum):
    actual, owned = owned_process.subprocess.Popen, []
    def handoff(*args, **kwargs):
        child = actual(*args, **kwargs)
        if kwargs.get("start_new_session") is True:
            owned.append(child)
            os.kill(os.getpid(), signum)
        return child
    monkeypatch.setattr(owned_process.subprocess, "Popen", handoff)
    try:
        run, value, inner, _ = execute(tmp_path, "import time;time.sleep(5)")
        assert len(owned) == 1 and owned[0].returncode is not None
        _, receipt, _ = read_command_run(run / "manifest.json")
        assert receipt["exit_code"] == 128 + signum
        assert replay(run, value, inner)["status"] == "MISSING"
    finally:
        for child in owned:
            if child.returncode is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=1)


def test_observed_escaped_descendant_is_missing_not_foreign_signal(tmp_path):
    # Escaped fixture self-exits shortly; observer never acquires authority to
    # kill its now-independent group. Do not create an indefinitely live daemon.
    code = "import subprocess,sys,time;p=subprocess.Popen([sys.executable,'-I','-S','-c','import os,time;os.setsid();time.sleep(.4)']);time.sleep(.2)"
    run, value, inner, _ = execute(tmp_path, code)
    result = replay(run, value, inner)
    assert result["status"] == "MISSING"
    assert result["fork_events"] > 0
    time.sleep(.25)


@pytest.mark.parametrize("change", ["source", "wrapper", "rss", "peak", "kernel", "missing-log", "status", "stream", "process-shape", "disk-shape", "tool-shape", "exec-stage"])
def test_coordinated_tamper_cannot_promote_resource_evidence(tmp_path, change):
    run, value, inner, _ = execute(tmp_path)
    manifest = read_json(run / "manifest.json")
    obs = read_json(run / "resource-observation.json")
    records = [json.loads(line) for line in (run / "resource-samples.jsonl").read_text().splitlines()]
    if change == "source":
        expected = copy.deepcopy(SOURCE)
        expected["packages"] = [{"injected": "other-package"}]
        with pytest.raises(InvalidEvidence, match="entire"):
            validate_observation(run / "manifest.json", value, inner, expected)
        return
    if change == "wrapper":
        binding = manifest["identity"]["resource_observer"]
        binding["outer_argv"] = ["/usr/bin/env", *binding["outer_argv"]]
        rebind(run, binding=binding, receipt_change=lambda receipt: receipt.update(argv=binding["outer_argv"]))
    elif change == "rss":
        next(row for row in records if row["kind"] == "sample")["tree_rss_bytes"] += 1
        rebind(run, records=records)
    elif change == "peak":
        obs["peak"]["sampled_tree_rss_bytes"] += 1
        rebind(run, observation=obs)
    elif change == "kernel":
        obs["kernel_tree_peak"] = True
        rebind(run, observation=obs)
    elif change == "missing-log":
        (run / "resource-samples.jsonl").unlink()
    elif change == "status":
        rebind(run, receipt_change=lambda receipt: receipt.update(exit_code=8))
    elif change == "stream":
        # Keep all JSON summaries and receipt hashes internally coordinated,
        # but actual retained stdout has additional bytes not in final counter.
        def change_bytes(receipt):
            (run / "stdout.log").write_bytes((run / "stdout.log").read_bytes() + b"extra")
            receipt["stdout"] = artifact(run, run / "stdout.log")
        rebind(run, receipt_change=change_bytes)
    elif change in {"process-shape", "disk-shape", "tool-shape"}:
        first = next(row for row in records if row["kind"] == "sample")
        first[{"process-shape": "processes", "disk-shape": "output", "tool-shape": "tools"}[change]] = [1]
        rebind(run, records=records)
    elif change == "exec-stage":
        first = next(row for row in records if row["kind"] == "sample")
        assert first["stage"] == "bridge"
        first["stage"] = "target"
        rebind(run, records=records)
    with pytest.raises(InvalidEvidence):
        replay(run, value, inner)








def test_observer_callback_failure_cannot_skip_exact_owned_cleanup(tmp_path, monkeypatch):
    from acceptance_lib.resource_observer import Observer
    actual, children = owned_process.subprocess.Popen, []
    def child(*args, **kwargs):
        proc = actual(*args, **kwargs)
        if kwargs.get("start_new_session") is True:
            children.append(proc)
        return proc
    def broken(*_args):
        raise OSError(5, "fixture observer callback failed")
    monkeypatch.setattr(owned_process.subprocess, "Popen", child)
    monkeypatch.setattr(Observer, "before_cleanup", broken)
    try:
        run, value, inner, _ = execute(tmp_path)
        assert children and children[0].returncode is not None
        _, receipt, _ = read_command_run(run / "manifest.json")
        assert receipt["exit_code"] == 127 and replay(run, value, inner)["status"] == "MISSING"
    finally:
        for proc in children:
            if proc.returncode is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=1)


def test_runtime_module_mutation_invalidates_real_capture_in_isolated_copy(tmp_path):
    # Mutate an actual executed local dependency in a separate source tree;
    # checking only a manifest's list of expected filenames would miss this.
    import shutil
    scripts = tmp_path / "copy" / "scripts"
    shutil.copytree(Path(__file__).resolve().parents[1] / "scripts" / "acceptance_lib", scripts / "acceptance_lib")
    work = tmp_path / "work"
    work.mkdir()
    program = """from pathlib import Path
import sys,json
from acceptance_lib.evidence import capture_command,write_json
from acceptance_lib.resource_policy import policy
owner=Path(sys.argv[1]);owner.mkdir();src={'fixture':True}
p=policy('query',owner,source=src,fixture_limits={'wall_seconds':2})
argv=[str(Path(sys.executable).resolve()),'-c','print(1)']
capture_command(owner/'phase','fixture',argv,cwd=owner,owned_process_group=True,resource_policy=p,timeout=2)
write_json(owner/'args.json',{'policy':p,'argv':argv,'source':src})
"""
    env = {**os.environ, "PYTHONPATH": str(scripts), "PYTHONDONTWRITEBYTECODE": "1"}
    owner = work / "owner"
    proc = subprocess.run([sys.executable, "-c", program, str(owner)], env=env, capture_output=True, timeout=4)
    assert proc.returncode == 0, proc.stderr
    bridge = scripts / "acceptance_lib" / "resource_bridge.py"
    bridge.write_bytes(bridge.read_bytes() + b"\n# isolated mutation\n")
    verifier = """from pathlib import Path
import sys
from acceptance_lib.evidence import read_json,InvalidEvidence
from acceptance_lib.resource_validation import validate_observation
p=Path(sys.argv[1]);a=read_json(p/'args.json')
try:validate_observation(p/'phase'/'manifest.json',a['policy'],a['argv'],a['source'])
except InvalidEvidence as exc:assert 'identity changed' in str(exc)
else:raise AssertionError('mutated executed bridge source accepted')
"""
    checked = subprocess.run([sys.executable, "-c", verifier, str(owner)], env=env, capture_output=True, timeout=2)
    assert checked.returncode == 0, checked.stderr


def test_unsupported_sigchld_fails_before_spawn_and_retains_raw(tmp_path, monkeypatch):
    original = owned_process.signal.getsignal
    monkeypatch.setattr(owned_process.signal, "getsignal", lambda sig: signal.SIG_IGN if sig == signal.SIGCHLD else original(sig))
    monkeypatch.setattr(owned_process.subprocess, "Popen", lambda *_a, **_k: pytest.fail("must fail before spawn"))
    run, value, inner, _ = execute(tmp_path)
    _, receipt, _ = read_command_run(run / "manifest.json")
    assert receipt["exit_code"] == 127 and (run / "resource-observation.json").is_file()
    assert replay(run, value, inner)["status"] == "MISSING"


def test_large_registered_context_counts_complete_final_metadata(tmp_path):
    owner = tmp_path / "owner"
    owner.mkdir()
    source = {"fixture": True, "large": "x" * 200000}
    value = policy("query", owner, source=source,
                   fixture_limits={"wall_seconds": 3, "interval_seconds": .02, "owned_bytes": 131072})
    inner = [str(Path(sys.executable).resolve()), "-I", "-S", "-c", "import time;time.sleep(.3)"]
    run = owner / "phase"
    capture_command(run, "mechanical.publication", inner, cwd=owner, timeout=3,
                    resource_policy=value, owned_process_group=True)
    result = validate_observation(run / "manifest.json", value, inner, source)
    from acceptance_lib.resource_tools import disk_usage
    info = owner.stat()
    actual = disk_usage(owner, (info.st_dev, info.st_ino), time.monotonic() + 1)
    assert (run / "manifest.json").stat().st_size > 200000
    assert result["status"] == "FAIL" and result["retained_owned_output"]["logical_bytes"] == actual["logical_bytes"]
    assert result["retained_owned_output"]["allocated_bytes"] == actual["allocated_bytes"]
    assert any(row["metric"] == "owned_bytes" and row["observed"] >= 200000 for row in result["violations"])


def test_semantically_identical_changed_manifest_size_invalidates_final_census(tmp_path):
    run, value, inner, _ = execute(tmp_path)
    path = run / "manifest.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(InvalidEvidence, match="retained.*bytes"):
        replay(run, value, inner)


def test_publication_failure_retains_raw_and_never_gives_clean_resource_status(tmp_path, monkeypatch):
    from acceptance_lib import resource_publication
    def fail(*_args):
        raise OSError(5, "fixture final publication unavailable")
    monkeypatch.setattr(resource_publication, "disk_usage", fail)
    run, value, inner, _ = execute(tmp_path, "print('preserved')")
    assert (run / "stdout.log").read_bytes() == b"preserved\n"
    assert read_json(run / "resource-publication-failure.json")["status"] == "MISSING"
    with pytest.raises(InvalidEvidence):
        replay(run, value, inner)


@pytest.mark.parametrize("case", ["sampled-child", "escaped-child", "missing-fd"])
def test_sampled_status_separates_only_ordinary_fork_lifetime_caveat(tmp_path, monkeypatch, case):
    # Tiny Python fixture under the exact formal resource caps; never an engine
    # or quality result. Source context explicitly identifies the fixture.
    owner = tmp_path / "owner"
    owner.mkdir()
    value = policy("query", owner, source=SOURCE)
    if case == "escaped-child":
        child = "import os,time;os.setsid();time.sleep(.6)"
        pause = .45
    else:
        child = "import time;live=bytearray(65536);time.sleep(1)"
        pause = .55
    body = f"import subprocess,sys,time;p=subprocess.Popen([sys.executable,'-I','-S','-c',{child!r}]);time.sleep({pause})"
    inner = [str(Path(sys.executable).resolve()), "-I", "-S", "-c", body]
    if case == "missing-fd":
        from acceptance_lib import resource_tools
        actual = resource_tools.descriptors
        def unavailable(row, deadline):
            return actual(row, deadline) if row["state"].startswith("Z") else (None, None, "fd-census-unavailable-or-process-exited")
        monkeypatch.setattr(resource_tools, "descriptors", unavailable)
    run = owner / "phase"
    capture_command(run, "mechanical.sampled-status", inner, cwd=owner, timeout=600,
                    owned_process_group=True, resource_policy=value)
    result = validate_observation(run / "manifest.json", value, inner, SOURCE)
    assert result["status"] == "MISSING" and result["lifetime_complete"] is False
    assert result["fork_events"] > 0 and "fork-descendant-lifetimes-not-atomically-tracked" in result["coverage_gaps"]
    assert result["full_tree_ceiling_status"] == "MISSING"
    if case == "sampled-child":
        assert result["sampled_resource_status"] == "PASS" and result["sampled_coverage_gaps"] == []
        assert result["peak"]["sampled_children_rss_bytes"] > 0
    else:
        assert result["sampled_resource_status"] == "MISSING"
        expected = "observed-owned-descendant-escaped-private-group" if case == "escaped-child" else "fd-census-unavailable-or-process-exited"
        assert expected in result["sampled_coverage_gaps"]
