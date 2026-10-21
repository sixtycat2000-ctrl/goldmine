"""GET-only GitHub TLS transport; no command dispatch and no caller-defined API host."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.parse

from .evidence import InvalidEvidence, artifact, now, read_json, sha256, write_json
from .hosted_contract import API_VERSION, MAX_ZIP_BYTES, selection

HTTP_DEADLINE_SECONDS = 120


def _execute(argv, request, timeout):
    # subprocess.run kills and reaps its owned child on timeout, including while
    # DNS, TLS headers or a partial read are blocked. No artifact executable runs.
    return subprocess.run(argv, input=json.dumps(request).encode(), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout, check=False,
                          cwd=Path(__file__).resolve().parents[2],
                          env={k: v for k, v in os.environ.items() if k not in {"GH_TOKEN", "GITHUB_TOKEN"}})


def blob_url(value):
    parsed = urllib.parse.urlsplit(value)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment or
            not (host.endswith(".blob.core.windows.net") or host.endswith(".actions.githubusercontent.com") or
                 host in {"objects.githubusercontent.com", "github.com"})):
        raise InvalidEvidence("artifact redirect is not an allowed HTTPS GitHub artifact host")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "<redacted>" if parsed.query else "", ""))


class GitHubReadonly:
    """Credentials stay in memory and are never forwarded to blob redirects.

    A saved receipt is consistency evidence, not a cryptographic attestation.
    Public hosted PASS additionally refreshes metadata through this transport.
    """
    def __init__(self, out: Path | None = None, *, token_env="GH_TOKEN"):
        if token_env not in {"GH_TOKEN", "GITHUB_TOKEN"}:
            raise InvalidEvidence("only an explicitly named conventional token environment variable is supported")
        self.out, self.token = out, os.environ.get(token_env)
        self.records = {}

    def _get(self, url, *, name, limit, api=True, redirect=False):
        parsed = urllib.parse.urlsplit(url)
        if api and (parsed.scheme != "https" or parsed.hostname != "api.github.com" or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment or not parsed.path.startswith("/repos/")):
            raise InvalidEvidence("only fixed github.com repository GET endpoints are supported")
        if not api:
            blob_url(url)
        directory = self.out / name if self.out is not None else None
        temporary = tempfile.TemporaryDirectory(prefix="goldmine-github-get-") if directory is None else None
        directory = Path(temporary.name) if temporary else directory
        if not temporary:
            directory.mkdir(parents=True, exist_ok=False)
        body = directory / "body.bin"
        body.touch(exist_ok=False)
        worker = Path(__file__).with_name("hosted_http.py").resolve()
        argv = [sys.executable, "-I", "-S", "-B", str(worker)]
        request = {"url": url, "api": api, "token": self.token if api else None,
                   "api_version": API_VERSION, "redirect": redirect, "limit": limit, "body": str(body)}
        started = now()
        result, timed_out, exit_code = {}, False, None
        try:
            process = _execute(argv, request, HTTP_DEADLINE_SECONDS)
            exit_code = process.returncode
            if len(process.stdout) > 1024 * 1024 or process.stderr:
                raise InvalidEvidence("isolated HTTP worker produced invalid protocol output")
            result = json.loads(process.stdout)
            if set(result) != {"status", "headers", "location", "error"}:
                raise InvalidEvidence("isolated HTTP worker protocol differs")
        except subprocess.TimeoutExpired:
            timed_out, exit_code = True, 124
            result = {"status": None, "headers": {}, "location": None, "error": "absolute HTTP deadline exceeded; child killed and reaped"}
        except (OSError, InvalidEvidence, ValueError, TypeError) as exc:
            result = {"status": None, "headers": {}, "location": None, "error": type(exc).__name__}
        receipt = {"schema": 1, "kind": "goldmine.github-get", "method": "GET", "api_version": API_VERSION,
                   "url": url if api else blob_url(url), "started_at": started, "finished_at": now(),
                   "status": result["status"], "headers": result["headers"], "authenticated": bool(self.token) if api else False,
                   "error": result["error"], "response_sha256": sha256(body), "bytes": body.stat().st_size,
                   "worker_argv": argv, "worker_sha256": sha256(worker), "exit_code": exit_code, "timed_out": timed_out,
                   "worker_cwd": str(worker.parents[2]), "worker_environment": {"python_isolated": True, "proxy_environment_used": False, "credential_via_stdin": bool(api and self.token)}}
        if not api:
            receipt["signed_url_sha256"] = hashlib.sha256(url.encode()).hexdigest()
        if self.out is not None:
            receipt["response"] = artifact(directory, body)
            write_json(directory / "receipt.json", receipt)
            self.records[name] = artifact(self.out, directory / "receipt.json")
        failed = result["error"] or exit_code != 0 or result["status"] != (302 if redirect else 200) or body.stat().st_size > limit
        raw = body.read_bytes() if api and not failed else b""
        if temporary:
            temporary.cleanup()
        if failed:
            raise InvalidEvidence(f"GitHub GET unavailable/unsuccessful: {result['status']}; {result['error'] or name}")
        return raw, result["location"]

    def json(self, endpoint, name):
        raw, _ = self._get("https://api.github.com" + endpoint, name=name, limit=16 * 1024**2)
        if self.out is not None:
            return read_json(self.out / name / "body.bin")
        from .evidence import _object, finite
        try:
            value = json.loads(raw, object_pairs_hook=_object)
            finite(value)
            return value
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise InvalidEvidence("invalid GitHub JSON response") from exc

    def download(self, endpoint, name):
        if self.out is None:
            raise InvalidEvidence("artifact collection requires an explicit new output directory")
        _, location = self._get("https://api.github.com" + endpoint, name=name + "-redirect",
                                limit=1024 * 1024, redirect=True)
        if not location:
            raise InvalidEvidence("artifact API did not return its documented 302 redirect")
        blob_url(location)
        raw, _ = self._get(location, name=name + "-blob", limit=MAX_ZIP_BYTES, api=False)
        return self.out / (name + "-blob") / "body.bin"


def snapshot(client, selected, *, prefix=""):
    """Fixed bounded endpoints; totals above100 cannot match the fixed8-job contract."""
    selected = selection(selected)
    repository, run, attempt = (selected[key] for key in ("repository", "run_id", "run_attempt"))
    base = f"/repos/{repository}"
    run_path = f"{base}/actions/runs/{run}"
    result = {"repository": client.json(base, prefix + "repository"),
              "run": client.json(run_path, prefix + "run"),
              "attempt": client.json(f"{run_path}/attempts/{attempt}", prefix + "attempt")}
    workflow_id = result["run"].get("workflow_id")
    if type(workflow_id) is not int or workflow_id < 1:
        raise InvalidEvidence("workflow ID missing from actual run metadata")
    result.update({"workflow": client.json(f"{base}/actions/workflows/{workflow_id}", prefix + "workflow"),
        "jobs": client.json(f"{run_path}/attempts/{attempt}/jobs?per_page=100&page=1", prefix + "jobs"),
        "artifacts": client.json(f"{run_path}/artifacts?per_page=100&page=1", prefix + "artifacts"),
        "workflow_source": client.json(f"{base}/contents/.github/workflows/ci.yml?ref={selected['head_sha']}", prefix + "workflow-source")})
    return result
