"""Mechanical synthetic counterexamples; none is an actual hosted CI success."""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import platform
import time
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from acceptance_lib import hosted_ci, hosted_contract, hosted_metadata, hosted_transport
from acceptance_lib.evidence import InvalidEvidence, artifact, now, read_json, sha256, write_json
from acceptance_lib.hosted_contract import JOBS, WORKFLOW, committed_source, extract_zip, selection


def _git(root, *args):
    return subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                          cwd=root, check=True, capture_output=True).stdout.decode().strip()


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "synthetic-source"
    root.mkdir()
    for name, text in {WORKFLOW: "name: ci\n# synthetic source fixture only\n", "goldmine/__init__.py": "__version__='0.0.0'\n",
                       "evals/acceptance_targets.json": "{}\n", "evals/acceptance_test_inventory.json": "{}\n",
                       "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
                       ".gitignore": "reports/\n__pycache__/\n.pytest_cache/\n"}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(root, "init", "-q")
    _git(root, "-c", "user.email=fixture@invalid.example", "-c", "user.name=Synthetic fixture", "add", ".")
    _git(root, "-c", "user.email=fixture@invalid.example", "-c", "user.name=Synthetic fixture", "commit", "-qm", "mechanical fixture, no hosted claim")
    return root


def metadata(root):
    source = committed_source(root)
    selected = {"repository": "fixture-owner/mechanical-only", "run_id": 123, "run_attempt": 1, "head_sha": source["identity"]["git_commit"]}
    repo, head = selected["repository"], selected["head_sha"]
    api = f"https://api.github.com/repos/{repo}"
    repository = {"id": 10, "full_name": repo}
    run = {"id": 123, "run_attempt": 1, "head_sha": head, "repository": repository, "head_repository": repository,
           "url": api + "/actions/runs/123", "html_url": f"https://github.com/{repo}/actions/runs/123", "event": "push",
           "path": WORKFLOW + "@main", "name": "ci", "status": "completed", "conclusion": "success", "workflow_id": 11}
    body = (root / WORKFLOW).read_bytes()
    encoded = {"type": "file", "path": WORKFLOW, "encoding": "base64", "content": base64.b64encode(body).decode(),
               "sha": hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()}
    jobs, artifacts = [], []
    for i, (name, spec) in enumerate(JOBS.items(), 1):
        job = {"id": 100 + i, "name": name, "run_id": 123, "head_sha": head, "run_url": api + "/actions/runs/123",
            "status": "completed", "conclusion": "success", "labels": [spec.label],
            "started_at": "2026-10-20T00:00:00Z", "completed_at": "2026-10-20T00:01:00Z",
            "steps": [{"name": n, "status": "completed", "conclusion": "success"} for n in (spec.step, "Upload raw proof")]}
        jobs.append(job)
        artifacts.append({"id": 200 + i, "name": name, "expired": False,
            "workflow_run": {"id": 123, "repository_id": 10, "head_repository_id": 10, "head_sha": head},
            "url": f"{api}/actions/artifacts/{200+i}", "archive_download_url": f"{api}/actions/artifacts/{200+i}/zip",
            "created_at": "2026-10-20T00:00:55Z", "updated_at": "2026-10-20T00:00:55Z",
            "digest": "sha256:" + "1" * 64, "size_in_bytes": 128})
    data = {"repository": repository, "run": run, "attempt": deepcopy(run),
        "workflow": {"id": 11, "path": WORKFLOW, "name": "ci", "state": "active"},
        "jobs": {"total_count": len(JOBS), "jobs": jobs}, "artifacts": {"total_count": len(JOBS), "artifacts": artifacts}, "workflow_source": encoded}
    return selected, source, data


@pytest.mark.parametrize("value", [None, {}, {"repository": "x/y", "run_id": True, "run_attempt": 1, "head_sha": "1"*40},
    {"repository": "https://github.com/x/y", "run_id": 1, "run_attempt": 1, "head_sha": "1"*40},
    {"repository": "x/y", "run_id": 1, "run_attempt": 1, "head_sha": "main"},
    {"repository": "x/y", "run_id": 1, "run_attempt": 1, "head_sha": "1"*40, "summary": "PASS"}])
def test_selection_is_explicit_and_cannot_supply_status(value):
    with pytest.raises(InvalidEvidence):
        selection(value)


def test_only_actual_clean_committed_source_can_be_selected(source):
    assert committed_source(source)["identity"]["dirty_diff_sha256"] is None
    (source / "goldmine/__init__.py").write_text("# changed after hosted run\n")
    with pytest.raises(InvalidEvidence, match="dirty"):
        committed_source(source)


def test_ignored_executable_file_cannot_bypass_committed_source(source):
    with (source / ".git/info/exclude").open("a") as stream:
        stream.write("goldmine/ignored.py\n")
    (source / "goldmine/ignored.py").write_text("print('PASS')\n")
    with pytest.raises(InvalidEvidence, match="uncommitted/ignored"):
        committed_source(source)


def test_uncommitted_test_source_is_not_covered_by_head(source):
    (source / "tests").mkdir()
    (source / "tests/uncommitted.py").write_text("print('PASS')\n")
    with pytest.raises(InvalidEvidence, match="clean committed"):
        committed_source(source)


def test_named_other_commit_rejected_before_network(source, tmp_path, monkeypatch):
    monkeypatch.setattr(hosted_ci, "GitHubReadonly", lambda *a, **kw: pytest.fail("must not contact GitHub for a different source"))
    selected, _, _ = metadata(source)
    selected["head_sha"] = "0" * 40
    with pytest.raises(InvalidEvidence, match="HEAD"):
        hosted_ci.collect(source, selected, tmp_path / "out")


def test_synthetic_metadata_parses_but_does_not_certify_hosted_pass(source):
    selected, _, data = metadata(source)
    jobs, assets = hosted_metadata.validate_metadata(selected, data, (source / WORKFLOW).read_bytes())
    assert set(jobs) == set(assets) == set(JOBS)
    assert not isinstance(jobs, str)  # no PASS result from a mocked API object


@pytest.mark.parametrize("mutation", ["other_run", "other_head", "other_attempt", "failed", "incomplete", "fork", "wrong_workflow", "changed_workflow", "missing_job", "extra_job", "duplicate_job", "skipped_step", "failed_step", "self_hosted", "duplicate_artifact", "extra_artifact", "expired", "no_digest", "other_artifact_run", "old_attempt_artifact"])
def test_matrix_and_artifacts_reject_mixed_failed_ambiguous_or_stale_data(source, mutation):
    selected, _, data = metadata(source)
    first = data["jobs"]["jobs"][0]
    row = data["artifacts"]["artifacts"][0]
    if mutation == "other_run": first["run_id"] = 456
    elif mutation == "other_head": first["head_sha"] = "0" * 40
    elif mutation == "other_attempt": data["run"]["run_attempt"] = 2
    elif mutation == "failed": first["conclusion"] = "failure"
    elif mutation == "incomplete": first["status"] = "in_progress"
    elif mutation == "fork": data["attempt"]["head_repository"] = {"id": 99, "full_name": "x/fork"}
    elif mutation == "wrong_workflow": data["workflow"]["path"] = ".github/workflows/live-validation.yml"
    elif mutation == "changed_workflow": data["workflow_source"]["content"] = base64.b64encode(b"print('PASS')").decode()
    elif mutation == "missing_job": data["jobs"]["jobs"].pop()
    elif mutation == "extra_job": data["jobs"]["jobs"].append(deepcopy(first))
    elif mutation == "duplicate_job": data["jobs"]["jobs"][-1]["name"] = first["name"]
    elif mutation == "skipped_step": first["steps"][0]["conclusion"] = "skipped"
    elif mutation == "failed_step": first["steps"][0]["status"] = "in_progress"
    elif mutation == "self_hosted": first["labels"].append("self-hosted")
    elif mutation == "duplicate_artifact": data["artifacts"]["artifacts"][-1]["name"] = row["name"]
    elif mutation == "extra_artifact": data["artifacts"]["artifacts"].append(deepcopy(row))
    elif mutation == "expired": row["expired"] = True
    elif mutation == "no_digest": row["digest"] = None
    elif mutation == "other_artifact_run": row["workflow_run"]["id"] = 456
    elif mutation == "old_attempt_artifact": row["created_at"] = row["updated_at"] = "2026-10-19T00:00:00Z"
    with pytest.raises(InvalidEvidence):
        hosted_metadata.validate_metadata(selected, data, (source / WORKFLOW).read_bytes())


def test_raw_context_refuses_same_commit_cross_run_and_false_platform(source):
    selected, pin, data = metadata(source)
    job = data["jobs"]["jobs"][2]
    value = {"schema": 1, **selected, "job_key": job["name"], "github_job": "base", "workspace": "/runner/repository",
        "runner": {"os": "Darwin", "arch": "arm64", "python": "3.12.99"}, "source": pin}
    assert hosted_metadata.validate_context(value, selected, job, pin, "base") == "/runner/repository"
    value["run_id"] += 1
    with pytest.raises(InvalidEvidence, match="mixed"):
        hosted_metadata.validate_context(value, selected, job, pin, "base")
    value["run_id"] -= 1
    value["runner"]["arch"] = "x86_64"
    with pytest.raises(InvalidEvidence, match="matrix"):
        hosted_metadata.validate_context(value, selected, job, pin, "base")


@pytest.mark.parametrize("names", [["../escape"], ["/absolute"], ["a\\b"], ["./ambiguous"], ["a//b"], ["C:/drive"], ["foo", "foo"], ["Folder/a", "folder/A"], ["A/file", "a/other"], ["a", "a/b"], ["trailing./file"], ["cafe\u0301/file"]])
def test_zip_path_and_duplicate_attacks_fail_before_extraction(tmp_path, names):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as target:
        for name in names:
            target.writestr(name, b"synthetic")
    output = tmp_path / "extracted"
    with pytest.raises(InvalidEvidence):
        extract_zip(archive, output)
    assert not output.exists()
    assert not (tmp_path / "escape").exists()


def test_zip_symlink_cannot_escape_even_with_safe_name(tmp_path):
    archive = tmp_path / "symlink.zip"
    with zipfile.ZipFile(archive, "w") as target:
        link = zipfile.ZipInfo("normal/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        target.writestr(link, "../../escape")
    with pytest.raises(InvalidEvidence, match="symlinks"):
        extract_zip(archive, tmp_path / "out")


def test_raw_zip_nul_name_is_rejected_before_standard_library_truncation(tmp_path):
    archive = tmp_path / "raw-nul.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("safeXsuffix", b"synthetic payload")
    raw = archive.read_bytes()
    assert raw.count(b"safeXsuffix") == 2  # matching local and central names
    archive.write_bytes(raw.replace(b"safeXsuffix", b"safe\x00suffix"))
    with zipfile.ZipFile(archive) as target:
        row = target.infolist()[0]
        assert row.orig_filename == "safe\x00suffix" and row.filename == "safe"
    output = tmp_path / "extracted"
    with pytest.raises(InvalidEvidence, match="original filename"):
        extract_zip(archive, output)
    assert not output.exists()


def test_bounded_zip_stream_preserves_exact_bytes(tmp_path):
    archive = tmp_path / "safe.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("source/junit.xml", b"<testsuites/>\n")
        target.writestr("raw/stdout.log", b"print('PASS') is just raw output\n")
    extract_zip(archive, tmp_path / "out")
    assert (tmp_path / "out/source/junit.xml").read_bytes() == b"<testsuites/>\n"


def synthetic_bundle(root, out):
    """Intentionally fabricated transport receipts, solely for negative replay tests."""
    selected, source, data = metadata(root)
    started = now()
    out.mkdir()
    downloads, refs = {}, {}
    for row in data["artifacts"]["artifacts"]:
        target = out / f"zip-{row['id']}.zip"
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("SYNTHETIC-ONLY.txt", "not an actual GitHub artifact")
        row["digest"], row["size_in_bytes"] = "sha256:" + sha256(target), target.stat().st_size
        downloads[row["name"]] = {"artifact_id": row["id"], "zip": artifact(out, target)}
    repo, run, attempt = selected["repository"], selected["run_id"], selected["run_attempt"]
    base = f"https://api.github.com/repos/{repo}"
    endpoints = {"repository": base, "run": base + "/actions/runs/123", "attempt": base + "/actions/runs/123/attempts/1",
        "jobs": base + "/actions/runs/123/attempts/1/jobs?per_page=100&page=1", "artifacts": base + "/actions/runs/123/artifacts?per_page=100&page=1",
        "workflow": base + "/actions/workflows/11", "workflow_source": base + f"/contents/{WORKFLOW}?ref={selected['head_sha']}"}

    def get(name, url, raw, status, *, body_path=None, blob=False):
        directory = out / name
        directory.mkdir()
        body = body_path or directory / "body.bin"
        if body_path is None: body.write_bytes(raw)
        receipt = {"schema": 1, "kind": "goldmine.github-get", "method": "GET", "api_version": hosted_contract.API_VERSION,
            "url": url, "started_at": now(), "finished_at": now(), "status": status, "headers": {}, "authenticated": False,
            "error": None, "response_sha256": sha256(body), "bytes": body.stat().st_size,
            "response": artifact(directory, body) if body.is_relative_to(directory) else {"path": "../" + body.name, "sha256": sha256(body)},
            "worker_argv": [sys.executable, "-I", "-S", "-B", str(Path(hosted_transport.__file__).with_name("hosted_http.py"))],
            "worker_sha256": sha256(Path(hosted_transport.__file__).with_name("hosted_http.py")), "exit_code": 0, "timed_out": False,
            "worker_cwd": str(Path(hosted_transport.__file__).resolve().parents[2]),
            "worker_environment": {"python_isolated": True, "proxy_environment_used": False, "credential_via_stdin": False}}
        if blob:
            # Keep the canonical ZIP next to its raw receipt without unsafe refs.
            actual = directory / "body.bin"
            shutil.copyfile(body, actual)
            receipt["response"] = artifact(directory, actual)
            receipt["signed_url_sha256"] = "0" * 64
            downloads[next(k for k,v in downloads.items() if v["artifact_id"] == int(name.split("-")[1]))]["zip"] = artifact(out, actual)
        write_json(directory / "receipt.json", receipt)
        refs[name] = artifact(out, directory / "receipt.json")

    for prefix in ("before-", "after-"):
        for key, value in data.items():
            get(prefix + hosted_ci._NAMES[key], endpoints[key], json.dumps(value).encode(), 200)
    for row in data["artifacts"]["artifacts"]:
        get(f"artifact-{row['id']}-redirect", row["archive_download_url"], b"", 302)
        get(f"artifact-{row['id']}-blob", "https://fixture.blob.core.windows.net/proof?<redacted>", b"", 200,
            body_path=out / f"zip-{row['id']}.zip", blob=True)
    write_json(out / "manifest.json", {"schema": 1, "kind": "goldmine.ci-hosted", "selection": selected, "source": source,
        "started_at": started, "finished_at": now(), "transport": refs, "downloads": downloads})
    return out / "manifest.json"


def test_offline_mock_api_and_fake_green_summaries_cannot_produce_hosted_pass(source, tmp_path, monkeypatch):
    path = synthetic_bundle(source, tmp_path / "SYNTHETIC-raw")
    # Isolate the authenticity boundary from raw-scope validation in this one test.
    monkeypatch.setattr(hosted_ci, "pytest_report", lambda *args: {"synthetic_scope_only": True})
    monkeypatch.setattr(hosted_ci, "package_report", lambda *args: {"synthetic_scope_only": True})
    monkeypatch.setattr(hosted_ci, "GitHubReadonly", lambda *a, **kw: pytest.fail("offline mode must not contact GitHub"))
    status, details = hosted_ci.verify(source, path, online=False)
    assert status == "MISSING" and "actual GitHub TLS" in details["reason"]
    raw = read_json(path)
    raw["status"] = "PASS"
    write_json(path, raw)
    with pytest.raises(InvalidEvidence, match="summaries"):
        hosted_ci.verify(source, path, online=False)


def test_missing_actual_github_cannot_be_replaced_by_saved_mock_api(source, tmp_path, monkeypatch):
    path = synthetic_bundle(source, tmp_path / "SYNTHETIC-raw")
    monkeypatch.setattr(hosted_ci, "pytest_report", lambda *args: {"synthetic_scope_only": True})
    monkeypatch.setattr(hosted_ci, "package_report", lambda *args: {"synthetic_scope_only": True})
    def missing(*a, **kw): raise InvalidEvidence("actual GitHub unavailable")
    monkeypatch.setattr(hosted_ci, "snapshot", missing)
    with pytest.raises(InvalidEvidence, match="unavailable"):
        hosted_ci.verify(source, path, online=True)


def test_bound_but_fake_junit_and_print_pass_are_not_scope_evidence(source, tmp_path):
    path = synthetic_bundle(source, tmp_path / "SYNTHETIC-raw")
    with pytest.raises(InvalidEvidence, match="ci-context"):
        hosted_ci.verify(source, path, online=False)


def test_artifact_bytes_cannot_be_replaced_even_after_summary_is_green(source, tmp_path):
    path = synthetic_bundle(source, tmp_path / "SYNTHETIC-raw")
    bundle = read_json(path)
    ref = next(iter(bundle["downloads"].values()))["zip"]
    (path.parent / ref["path"]).write_bytes(b"print('PASS')")
    with pytest.raises(InvalidEvidence, match="changed artifact"):
        hosted_ci.verify(source, path, online=False)


@pytest.mark.parametrize("value", ["http://fixture.blob.core.windows.net/x", "https://127.0.0.1/x", "https://api.github.com.evil.example/x", "https://user:secret@github.com/x", "https://github.com:444/x", "https://example.com/x"])
def test_artifact_redirects_cannot_target_arbitrary_hosts_or_leak_credentials(value):
    with pytest.raises(InvalidEvidence):
        hosted_transport.blob_url(value)


def test_network_failure_keeps_a_bounded_raw_failure_receipt(tmp_path, monkeypatch):
    client = hosted_transport.GitHubReadonly(tmp_path / "transport")
    def unavailable(*a, **kw): raise OSError("synthetic offline transport")
    monkeypatch.setattr(hosted_transport, "_execute", unavailable)
    with pytest.raises(InvalidEvidence, match="unavailable"):
        client.json("/repos/fixture-owner/mechanical-only", "failed")
    receipt = read_json(tmp_path / "transport/failed/receipt.json")
    assert receipt["status"] is None and receipt["error"]
    assert (tmp_path / "transport/failed/body.bin").read_bytes() == b""


def test_absolute_worker_deadline_reaps_own_partial_line_process(tmp_path):
    pid_file = tmp_path / "owned.pid"
    script = "import os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));sys.stdout.write('{');sys.stdout.flush();time.sleep(1)"
    argv = [sys.executable, "-I", "-S", "-B", "-c", script, str(pid_file)]
    started, clock = now(), time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        hosted_transport._execute(argv, {}, .1)
    elapsed = time.monotonic() - clock
    assert elapsed < .7
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    write_json(tmp_path / "owned-timeout-proof.json", {"classification": "synthetic mechanical only", "argv": argv,
        "started_at": started, "finished_at": now(), "elapsed_seconds": elapsed, "deadline_seconds": .1,
        "child_pid": pid, "reaped": True, "script_sha256": hashlib.sha256(script.encode()).hexdigest()})


def test_timeout_preserves_partial_response_and_failed_get_receipt(tmp_path, monkeypatch):
    def timeout(argv, request, duration):
        Path(request["body"]).write_bytes(b"{")
        raise subprocess.TimeoutExpired(argv, duration)
    monkeypatch.setattr(hosted_transport, "_execute", timeout)
    client = hosted_transport.GitHubReadonly(tmp_path / "transport")
    with pytest.raises(InvalidEvidence, match="deadline"):
        client.json("/repos/fixture-owner/mechanical-only", "timeout")
    receipt = read_json(tmp_path / "transport/timeout/receipt.json")
    assert receipt["timed_out"] is True and receipt["exit_code"] == 124 and receipt["bytes"] == 1
    assert (tmp_path / "transport/timeout/body.bin").read_bytes() == b"{"


def test_tokens_and_signed_urls_are_never_in_worker_argv_or_persisted_receipts(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "SYNTHETIC-SECRET-NEVER-REAL")
    received = []
    def response(argv, request, duration):
        received.append((argv, request))
        Path(request["body"]).write_bytes(b"zip-bytes")
        value = {"status": 302 if request["api"] else 200, "headers": {}, "error": None,
                 "location": "https://fixture.blob.core.windows.net/archive?SYNTHETIC-SIGNED-SECRET" if request["api"] else None}
        return subprocess.CompletedProcess(argv, 0, json.dumps(value).encode(), b"")
    monkeypatch.setattr(hosted_transport, "_execute", response)
    client = hosted_transport.GitHubReadonly(tmp_path / "transport")
    client.download("/repos/fixture-owner/mechanical-only/actions/artifacts/1/zip", "fixture")
    assert received[0][1]["token"] == "SYNTHETIC-SECRET-NEVER-REAL" and received[1][1]["token"] is None
    for path in (tmp_path / "transport").rglob("receipt.json"):
        raw = path.read_text()
        assert "SYNTHETIC-SECRET" not in raw and "SYNTHETIC-SIGNED-SECRET" not in raw
    assert all("SYNTHETIC" not in " ".join(argv) for argv, _ in received)


def test_nested_wheel_is_bounded_before_any_metadata_module_read(tmp_path, monkeypatch):
    from acceptance_lib import hosted_replay
    archive = tmp_path / "synthetic-inner.whl"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as target:
        target.writestr("goldmine/large.py", b"x" * 1024)
    monkeypatch.setattr(hosted_replay, "MAX_WHEEL_MEMBER", 512)
    with pytest.raises(InvalidEvidence, match="expansion"):
        hosted_replay.preflight_wheel(archive)


def test_malformed_zip_and_bad_crc_are_missing_evidence(tmp_path):
    archive = tmp_path / "not-a-zip"
    archive.write_bytes(b"print('PASS')")
    with pytest.raises(InvalidEvidence, match="malformed"):
        extract_zip(archive, tmp_path / "no-output")
    assert not (tmp_path / "no-output").exists()
    archive = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as target:
        target.writestr("proof.txt", b"SYNTHETIC-UNIQUE-PAYLOAD")
    archive.write_bytes(archive.read_bytes().replace(b"SYNTHETIC-UNIQUE-PAYLOAD", b"SYNTHETIC-UNIQUE-COUNTER"))
    with pytest.raises(InvalidEvidence, match="CRC"):
        extract_zip(archive, tmp_path / "corrupt-output")


def test_corrupt_download_retains_collection_failure_receipt(source, tmp_path, monkeypatch):
    selected, _, data = metadata(source)
    archive = tmp_path / "bad.download"
    archive.write_bytes(b"not a ZIP")
    data["artifacts"]["artifacts"][0]["digest"] = "sha256:" + sha256(archive)
    data["artifacts"]["artifacts"][0]["size_in_bytes"] = archive.stat().st_size
    class SyntheticTransport:
        def __init__(self, out, **kw): self.out, self.records = out, {}
        def download(self, *args): return archive
    monkeypatch.setattr(hosted_ci, "GitHubReadonly", SyntheticTransport)
    monkeypatch.setattr(hosted_ci, "snapshot", lambda *args, **kw: deepcopy(data))
    out = tmp_path / "SYNTHETIC-failed-collection"
    with pytest.raises(InvalidEvidence, match="ZIP"):
        hosted_ci.collect(source, selected, out)
    assert read_json(out / "failure.json")["status"] == "MISSING"
    assert not (out / "manifest.json").exists()


def test_junit_bound_applies_to_actual_ref_even_when_extension_is_bin(tmp_path, monkeypatch):
    from acceptance_lib import hosted_replay
    from acceptance_lib.evidence import capture_command

    command = tmp_path / "raw"
    capture_command(command, "synthetic.junit", [sys.executable, "-c", "from pathlib import Path;Path('oversized.bin').write_bytes(b'x'*129)"],
        cwd=tmp_path, outputs={}, suite_id="synthetic.mechanical.v1")
    # Pin the actual alias inside the receipt, not a guessed *.xml pathname.
    shutil.move(tmp_path / "oversized.bin", command / "oversized.bin")
    manifest = read_json(command / "manifest.json")
    receipt = read_json(command / "receipt.json")
    receipt["artifacts"] = {"junit": artifact(command, command / "oversized.bin")}
    write_json(command / "receipt.json", receipt)
    manifest["receipt"] = artifact(command, command / "receipt.json")
    write_json(command / "manifest.json", manifest)
    report = tmp_path / "report.json"
    write_json(report, {"checks": [{"kind": "pytest", "evidence": {"command": artifact(tmp_path, command / "manifest.json")}}]})
    monkeypatch.setattr(hosted_replay, "MAX_JUNIT_BYTES", 128)
    with pytest.raises(InvalidEvidence, match="JUnit exceeds"):
        hosted_replay.preflight_junit(report)


def test_real_tiny_junit_replays_and_print_pass_command_substitution_fails(source, monkeypatch):
    """Real two-process pytest receipts, synthetic hosted envelope, never hosted PASS."""
    from acceptance_lib import runner
    from acceptance_lib.hosted_replay import pytest_report
    from acceptance_lib.registry import TASK_TESTS

    # A physical copy keeps plugin imports independent of the live checkout.
    scripts = source / "scripts/acceptance_lib"
    shutil.copytree(Path(hosted_ci.__file__).parent, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    test = source / "tests/test_canary.py"
    test.parent.mkdir()
    test.write_text("def test_arithmetic_canary():\n    assert 11 * 13 == 143\n")
    contract = source / "docs/execution-plan/CONTRACTS.md"
    contract.parent.mkdir(parents=True)
    contract.write_text("Synthetic protocol fixture; no quality or hosted assertion.\n")
    write_json(source / "docs/execution-plan/tasks.json", {"tasks": [{"id": key, "depends_on": []} for key in TASK_TESTS]})
    old = source / "evals/old.json"
    old.write_text("{}\n")
    history = source / "evals/history/20261021/old.json"
    history.parent.mkdir(parents=True)
    shutil.copyfile(old, history)
    write_json(source / "docs/execution-plan/baseline-20261021.json", {"sha256": {"evals/old.json": sha256(old)}})
    write_json(source / "evals/acceptance_targets.json", {"contract_sha256": sha256(contract)})
    write_json(source / "evals/acceptance_test_inventory.json", {"tests": [
        {"nodeid": "tests/test_canary.py::test_arithmetic_canary", "profile": "offline"}]})
    _git(source, "add", ".")
    _git(source, "-c", "user.email=fixture@invalid.example", "-c", "user.name=Synthetic fixture", "commit", "-qm", "tiny actual pytest scope")
    pin = committed_source(source)
    selected = {"repository": "fixture-owner/mechanical-only", "run_id": 123, "run_attempt": 1, "head_sha": pin["identity"]["git_commit"]}
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    os_label = "macos-14" if platform.system() == "Darwin" else "ubuntu-latest"
    job_name = f"base-{os_label}-{python}"
    assert job_name in JOBS, "run this protocol on a declared Python3.12/3.13 platform"
    out = source / "reports/ci/base"
    started = now()
    raw = runner.run(source, out, "offline")
    assert raw["status"] == "PASS"  # tiny local canary only, not hosted verification
    job = {"name": job_name, "started_at": started, "completed_at": now()}
    envelope = {"schema": 1, **selected, "job_key": job_name, "github_job": "base", "workspace": str(source), "runner": raw["platform"], "source": pin}
    context = {"schema": 1, "profile": "base", "before": pin["identity"]["content_sha256"], "after": pin["identity"]["content_sha256"],
               "report": artifact(out, out / "manifest.json"), "hosted": envelope}
    write_json(out / "ci-context.json", context)
    details = pytest_report(source, out, selected, job, pin)
    assert details["profile"] == "offline"
    # Rehash every affected local reference; hashes alone cannot legitimize -c.
    report = read_json(out / "manifest.json")
    row = next(r for r in report["checks"] if r["kind"] == "pytest")
    command_path = out / row["evidence"]["command"]["path"]
    command = read_json(command_path)
    receipt_path = command_path.parent / command["receipt"]["path"]
    receipt = read_json(receipt_path)
    receipt["argv"] = [sys.executable, "-c", "print('PASS')"]
    write_json(receipt_path, receipt)
    command["receipt"] = artifact(command_path.parent, receipt_path)
    write_json(command_path, command)
    row["evidence"]["command"] = artifact(out, command_path)
    write_json(out / "manifest.json", report)
    context["report"] = artifact(out, out / "manifest.json")
    write_json(out / "ci-context.json", context)
    with pytest.raises(InvalidEvidence, match="trusted full pytest"):
        pytest_report(source, out, selected, job, pin)
