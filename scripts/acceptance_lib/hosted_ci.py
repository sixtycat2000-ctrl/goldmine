"""Source-bound collection/replay for an explicitly named GitHub Actions run."""
from __future__ import annotations

import base64
from pathlib import Path
import tempfile

from .evidence import InvalidEvidence, artifact, is_sha, now, read_json, sha256, verify_artifact, write_json
from .hosted_contract import API_VERSION, JOBS, WORKFLOW, committed_source, extract_zip, selection, timestamp
from .hosted_metadata import validate_metadata
from .hosted_replay import package_report, pytest_report
from .hosted_transport import GitHubReadonly, snapshot

_NAMES = {"repository": "repository", "run": "run", "attempt": "attempt", "workflow": "workflow", "jobs": "jobs", "artifacts": "artifacts", "workflow_source": "workflow-source"}


def _saved_get(base, ref, *, url, status, blob=False):
    path = verify_artifact(base, ref)
    receipt = read_json(path)
    required = {"schema", "kind", "method", "api_version", "url", "started_at", "finished_at", "status", "headers", "authenticated", "error", "response_sha256", "bytes", "response",
                "worker_argv", "worker_sha256", "exit_code", "timed_out", "worker_cwd", "worker_environment"}
    if blob:
        required.add("signed_url_sha256")
    if not isinstance(receipt, dict) or set(receipt) != required or receipt["schema"] != 1 or receipt["kind"] != "goldmine.github-get" or receipt["method"] != "GET" or receipt["api_version"] != API_VERSION:
        raise InvalidEvidence("GitHub response needs the fixed GET transport receipt")
    if receipt["url"] != url or receipt["status"] != status or receipt["error"] is not None or receipt["exit_code"] != 0 or receipt["timed_out"] is not False:
        raise InvalidEvidence("saved GitHub API receipt is failed or from an unexpected endpoint")
    if (type(receipt["authenticated"]) is not bool or type(receipt["bytes"]) is not int or receipt["bytes"] < 0 or
            not isinstance(receipt["headers"], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in receipt["headers"].items()) or
            (blob and not is_sha(receipt["signed_url_sha256"]))):
        raise InvalidEvidence("saved HTTP receipt fields differ from the fixed schema")
    argv = receipt["worker_argv"]
    worker = Path(__file__).with_name("hosted_http.py")
    if (not isinstance(argv, list) or len(argv) != 5 or argv[1:4] != ["-I", "-S", "-B"] or
            not isinstance(argv[0], str) or not Path(argv[0]).is_absolute() or not isinstance(argv[4], str) or
            not Path(argv[4]).is_absolute() or Path(argv[4]).name != worker.name or receipt["worker_sha256"] != sha256(worker) or
            receipt["worker_cwd"] != str(Path(argv[4]).parents[2]) or
            receipt["worker_environment"] != {"python_isolated": True, "proxy_environment_used": False, "credential_via_stdin": bool(receipt["authenticated"]) if not blob else False}):
        raise InvalidEvidence("raw GitHub response did not use the fixed source-bound isolated HTTP worker")
    if timestamp(receipt["started_at"]) > timestamp(receipt["finished_at"]):
        raise InvalidEvidence("GitHub GET receipt chronology differs")
    body = verify_artifact(path.parent, receipt["response"])
    if sha256(body) != receipt["response_sha256"] or body.stat().st_size != receipt["bytes"]:
        raise InvalidEvidence("GitHub response bytes differ from its raw receipt")
    return receipt, body


def _saved_snapshot(base, refs, selected, prefix):
    repo, run, attempt = (selected[k] for k in ("repository", "run_id", "run_attempt"))
    api = f"https://api.github.com/repos/{repo}"
    endpoints = {"repository": api, "run": f"{api}/actions/runs/{run}",
        "attempt": f"{api}/actions/runs/{run}/attempts/{attempt}",
        "jobs": f"{api}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100&page=1",
        "artifacts": f"{api}/actions/runs/{run}/artifacts?per_page=100&page=1",
        "workflow_source": f"{api}/contents/{WORKFLOW}?ref={selected['head_sha']}"}
    result = {}
    for key in ("repository", "run", "attempt", "workflow", "jobs", "artifacts", "workflow_source"):
        if key == "workflow":
            endpoints[key] = f"{api}/actions/workflows/{result['run'].get('workflow_id')}"
        _, body = _saved_get(base, refs[prefix + _NAMES[key]], url=endpoints[key], status=200)
        result[key] = read_json(body)
    return result


def _stable(before, after):
    for name in ("run", "attempt", "workflow", "jobs", "artifacts", "workflow_source"):
        if before[name] != after[name]:
            raise InvalidEvidence("run/jobs/artifacts changed during collection or replay; recapture the completed run")
    if (before["repository"].get("id"), before["repository"].get("full_name")) != (after["repository"].get("id"), after["repository"].get("full_name")):
        raise InvalidEvidence("repository identity changed during collection or replay")


def collect(root, selected, out, *, token_env="GH_TOKEN"):
    """No discovery/default latest run, writes, dispatch, Git remote, or workflow run."""
    selected = selection(selected)
    source = committed_source(root)
    if source["identity"]["git_commit"] != selected["head_sha"]:
        raise InvalidEvidence("explicit run SHA must equal current clean committed HEAD")
    out = Path(out).resolve()
    if out.exists() and any(out.iterdir()):
        raise InvalidEvidence("hosted collection output must be new or empty")
    out.mkdir(parents=True, exist_ok=True)
    started = now()
    client = GitHubReadonly(out / "transport", token_env=token_env)
    try:
        before = snapshot(client, selected, prefix="before-")
        jobs, artifacts = validate_metadata(selected, before, (Path(root) / WORKFLOW).read_bytes())
        downloads = {}
        for name in JOBS:
            row = artifacts[name]
            path = client.download(f"/repos/{selected['repository']}/actions/artifacts/{row['id']}/zip", f"artifact-{row['id']}")
            if sha256(path) != row["digest"][7:] or path.stat().st_size != row["size_in_bytes"]:
                raise InvalidEvidence("downloaded artifact ZIP differs from GitHub's digest/size")
            from .hosted_contract import safe_zip
            safe_zip(path)
            downloads[name] = {"artifact_id": row["id"], "zip": artifact(out, path)}
        after = snapshot(client, selected, prefix="after-")
        validate_metadata(selected, after, (Path(root) / WORKFLOW).read_bytes())
        _stable(before, after)
        if committed_source(root) != source:
            raise InvalidEvidence("committed source changed during collection")
        bundle = {"schema": 1, "kind": "goldmine.ci-hosted", "selection": selected, "source": source,
                  "started_at": started, "finished_at": now(),
                  "transport": {k: artifact(out, verify_artifact(client.out, v)) for k, v in client.records.items()},
                  "downloads": downloads}
        write_json(out / "manifest.json", bundle)
        # Merely collecting bytes does not claim actual hosted success.
        return out / "manifest.json"
    except (InvalidEvidence, OSError, ValueError, KeyError, TypeError) as exc:
        write_json(out / "failure.json", {"schema": 1, "status": "MISSING", "selection": selected,
            "started_at": started, "finished_at": now(), "reason": str(exc),
            "transport": {k: artifact(out, verify_artifact(client.out, v)) for k, v in client.records.items()}})
        raise


def _verify(root, path, *, selected=None, online=True, token_env="GH_TOKEN"):
    """Offline replay can reject evidence but can never issue hosted PASS."""
    source = committed_source(root)
    path = Path(path)
    bundle = read_json(path)
    expected = {"schema", "kind", "selection", "source", "started_at", "finished_at", "transport", "downloads"}
    if not isinstance(bundle, dict) or set(bundle) != expected or bundle["schema"] != 1 or bundle["kind"] != "goldmine.ci-hosted":
        raise InvalidEvidence("local reports, URLs or summaries are not a hosted evidence bundle")
    actual = selection(bundle["selection"])
    if selected is not None and actual != selection(selected):
        raise InvalidEvidence("bundle differs from explicitly selected repository/run/attempt/head")
    if bundle["source"] != source or actual["head_sha"] != source["identity"]["git_commit"]:
        raise InvalidEvidence("hosted bundle does not cover the current clean committed source")
    if timestamp(bundle["started_at"]) > timestamp(bundle["finished_at"]):
        raise InvalidEvidence("collection chronology differs")
    before = _saved_snapshot(path.parent, bundle["transport"], actual, "before-")
    after = _saved_snapshot(path.parent, bundle["transport"], actual, "after-")
    _stable(before, after)
    jobs, artifacts = validate_metadata(actual, before, (Path(root) / WORKFLOW).read_bytes())
    expected_transport = {prefix + name for prefix in ("before-", "after-") for name in _NAMES.values()}
    expected_transport |= {f"artifact-{row['id']}-{suffix}" for row in artifacts.values() for suffix in ("redirect", "blob")}
    if set(bundle["transport"]) != expected_transport or set(bundle["downloads"]) != set(JOBS):
        raise InvalidEvidence("unexpected, missing or mixed raw transport/artifact identities")
    for ref in bundle["transport"].values():
        receipt = read_json(verify_artifact(path.parent, ref))
        if not timestamp(bundle["started_at"]) <= timestamp(receipt["started_at"]) <= timestamp(receipt["finished_at"]) <= timestamp(bundle["finished_at"]):
            raise InvalidEvidence("raw GET receipt falls outside this declared collection interval")
    details = {}
    # Extract only safe data in a private temporary directory, never import it.
    with tempfile.TemporaryDirectory(prefix="goldmine-hosted-replay-") as temp:
        for name in JOBS:
            row, ref = artifacts[name], bundle["downloads"][name]
            if not isinstance(ref, dict) or set(ref) != {"artifact_id", "zip"} or ref["artifact_id"] != row["id"]:
                raise InvalidEvidence("download is not bound to this actual artifact ID")
            archive = verify_artifact(path.parent, ref["zip"])
            if sha256(archive) != row["digest"][7:] or archive.stat().st_size != row["size_in_bytes"]:
                raise InvalidEvidence("artifact ZIP digest/size differs from the same-run API metadata")
            endpoint = f"https://api.github.com/repos/{actual['repository']}/actions/artifacts/{row['id']}/zip"
            _saved_get(path.parent, bundle["transport"][f"artifact-{row['id']}-redirect"], url=endpoint, status=302)
            blob_receipt = read_json(verify_artifact(path.parent, bundle["transport"][f"artifact-{row['id']}-blob"]))
            from .hosted_transport import blob_url
            blob_url(blob_receipt["url"].replace("<redacted>", "redacted"))
            _saved_get(path.parent, bundle["transport"][f"artifact-{row['id']}-blob"], url=blob_receipt["url"], status=200, blob=True)
            if (blob_receipt.get("kind") != "goldmine.github-get" or blob_receipt.get("method") != "GET" or
                    blob_receipt.get("status") != 200 or blob_receipt.get("error") is not None or
                    blob_receipt.get("authenticated") is not False or blob_receipt.get("response_sha256") != sha256(archive) or
                    verify_artifact(verify_artifact(path.parent, bundle["transport"][f"artifact-{row['id']}-blob"]).parent, blob_receipt["response"]) != archive):
                raise InvalidEvidence("artifact download lacks its unmodified credential-free raw transfer receipt")
            destination = Path(temp) / name
            extract_zip(archive, destination)
            details[name] = package_report(root, destination, actual, jobs[name], source) if name == "package-install" else pytest_report(root, destination, actual, jobs[name], source)
    if committed_source(root) != source:
        raise InvalidEvidence("source changed during hosted evidence replay")
    summary = {"repository": actual["repository"], "run_id": actual["run_id"], "run_attempt": actual["run_attempt"],
               "head_sha": actual["head_sha"], "run_url": before["run"]["html_url"], "jobs": details,
               "artifacts": {name: row["digest"] for name, row in artifacts.items()}, "bundle_sha256": sha256(path)}
    if not online:
        return "MISSING", {**summary, "reason": "raw replay validated; actual GitHub TLS revalidation was not performed"}
    # No injectable API snapshot, mock flag or cached green summary enters here.
    current = snapshot(GitHubReadonly(token_env=token_env), actual)
    validate_metadata(actual, current, (Path(root) / WORKFLOW).read_bytes())
    _stable(before, current)
    if committed_source(root) != source:
        raise InvalidEvidence("source changed during actual GitHub revalidation")
    return "PASS", summary


def verify(root, path, *, selected=None, online=True, token_env="GH_TOKEN"):
    import tarfile
    import zipfile
    import zlib
    from native_build_lib.source import BuildError

    try:
        return _verify(root, path, selected=selected, online=online, token_env=token_env)
    except (KeyError, TypeError, IndexError, AttributeError, zipfile.BadZipFile, tarfile.TarError, zlib.error, EOFError, BuildError) as exc:
        raise InvalidEvidence(f"malformed or incomplete hosted raw evidence: {type(exc).__name__}") from exc


def check(root, inputs, targets):
    return verify(root, inputs.file("evidence.ci_hosted", manifest=True), selected=inputs.get("hosted"), online=True)
