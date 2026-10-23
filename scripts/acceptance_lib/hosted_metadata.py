"""Interpret only required primary API fields; no summary or URL can certify CI."""
from __future__ import annotations

import base64
import hashlib
from pathlib import PurePosixPath

from .evidence import InvalidEvidence, is_sha
from .hosted_contract import JOBS, WORKFLOW, selection, timestamp, within


def validate_metadata(selected, data, workflow_bytes):
    selected = selection(selected)
    repo, run_id, attempt, head = (selected[k] for k in ("repository", "run_id", "run_attempt", "head_sha"))
    base = f"https://api.github.com/repos/{repo}"
    repository = data["repository"]
    if repository.get("full_name") != repo or type(repository.get("id")) is not int:
        raise InvalidEvidence("API repository differs from the explicit repository")
    for run in (data["run"], data["attempt"]):
        if (run.get("id") != run_id or run.get("run_attempt") != attempt or run.get("head_sha") != head or
                run.get("repository", {}).get("id") != repository["id"] or
                run.get("repository", {}).get("full_name") != repo or
                run.get("head_repository", {}).get("id") != repository["id"] or
                run.get("head_repository", {}).get("full_name") != repo or
                run.get("url") != f"{base}/actions/runs/{run_id}" or
                run.get("html_url") != f"https://github.com/{repo}/actions/runs/{run_id}" or
                run.get("event") not in {"push", "workflow_dispatch"} or
                run.get("path", "").split("@", 1)[0] != WORKFLOW or run.get("name") != "ci"):
            raise InvalidEvidence("run/repository/attempt/source/workflow identity differs")
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            raise InvalidEvidence("hosted run is failed or incomplete")
    workflow = data["workflow"]
    if (workflow.get("id") != data["run"].get("workflow_id") or workflow.get("id") != data["attempt"].get("workflow_id") or
            workflow.get("path") != WORKFLOW or workflow.get("name") != "ci" or workflow.get("state") != "active"):
        raise InvalidEvidence("run does not identify the committed ci workflow")
    source = data["workflow_source"]
    try:
        decoded = base64.b64decode(source["content"].replace("\n", ""), validate=True)
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidEvidence("workflow contents API needs an actual base64 file response") from exc
    git_blob = hashlib.sha1(b"blob " + str(len(decoded)).encode() + b"\0" + decoded).hexdigest()
    if (source.get("type") != "file" or source.get("path") != WORKFLOW or source.get("encoding") != "base64" or
            source.get("sha") != git_blob or decoded != workflow_bytes):
        raise InvalidEvidence("downloaded workflow bytes differ from the current committed workflow")
    jobs = data["jobs"]
    rows = jobs.get("jobs")
    if (jobs.get("total_count") != len(JOBS) or not isinstance(rows, list) or len(rows) != len(JOBS) or
            {r.get("name") for r in rows} != set(JOBS) or len({r.get("id") for r in rows}) != len(rows)):
        raise InvalidEvidence("missing, duplicate or unexpected hosted job matrix")
    by_job = {}
    for job in rows:
        name = job["name"]
        if (type(job.get("id")) is not int or job["id"] < 1 or job.get("run_id") != run_id or
                job.get("head_sha") != head or job.get("run_url") != f"{base}/actions/runs/{run_id}" or
                job.get("status") != "completed" or job.get("conclusion") != "success" or
                JOBS[name].label not in job.get("labels", []) or "self-hosted" in job.get("labels", [])):
            raise InvalidEvidence("job failed, incomplete, from another run or on an unexpected runner")
        timestamp(job.get("started_at")); timestamp(job.get("completed_at"))
        within(job["started_at"], job["completed_at"], job)
        steps = job.get("steps", [])
        for required in (JOBS[name].step, "Upload raw proof"):
            found = [s for s in steps if s.get("name") == required]
            if len(found) != 1 or found[0].get("status") != "completed" or found[0].get("conclusion") != "success":
                raise InvalidEvidence("a required actual job step is absent, skipped or unsuccessful")
        if any(s.get("conclusion") not in {"success", "skipped"} or s.get("status") != "completed" for s in steps):
            raise InvalidEvidence("job has failed or unfinished raw steps")
        by_job[name] = job
    artifacts = data["artifacts"]
    rows = artifacts.get("artifacts")
    expected = {job.artifact for job in JOBS.values()}
    if (artifacts.get("total_count") != len(expected) or not isinstance(rows, list) or len(rows) != len(expected) or
            {r.get("name") for r in rows} != expected or len({r.get("id") for r in rows}) != len(rows)):
        raise InvalidEvidence("missing, ambiguous or unexpected raw artifacts")
    by_artifact = {}
    for row in rows:
        related = row.get("workflow_run", {})
        name = row["name"]
        if (type(row.get("id")) is not int or row["id"] < 1 or row.get("expired") is not False or
                related.get("id") != run_id or related.get("repository_id") != repository["id"] or
                related.get("head_repository_id") != repository["id"] or related.get("head_sha") != head or
                row.get("url") != f"{base}/actions/artifacts/{row['id']}" or
                row.get("archive_download_url") != f"{base}/actions/artifacts/{row['id']}/zip" or
                not isinstance(row.get("digest"), str) or not row["digest"].startswith("sha256:") or not is_sha(row["digest"][7:]) or
                type(row.get("size_in_bytes")) is not int or row["size_in_bytes"] < 1):
            raise InvalidEvidence("artifact lacks unexpired same-run SHA-bound provenance")
        # v4 names are immutable; reject old attempts that retained earlier artifacts.
        within(row["created_at"], row["updated_at"], by_job[name])
        by_artifact[name] = row
    return by_job, by_artifact


def validate_context(value, selected, job, source, profile):
    expected = {"schema", "repository", "run_id", "run_attempt", "head_sha", "job_key", "github_job", "workspace", "runner", "source"}
    if not isinstance(value, dict) or set(value) != expected or value.get("schema") != 1:
        raise InvalidEvidence("raw output lacks its strict hosted context")
    name = job["name"]
    expected_job = "base" if name.startswith("base-") else "optional" if name.startswith("optional-") else "native" if name.startswith("native-") else "package"
    if (any(value.get(k) != v for k, v in selected.items()) or value["job_key"] != name or
            value["github_job"] != expected_job or value["source"] != source or JOBS[name].profile != profile):
        raise InvalidEvidence("raw report is mixed from another source/run/attempt/job")
    workspace = value["workspace"]
    if not isinstance(workspace, str) or not workspace.startswith("/") or str(PurePosixPath(workspace)) != workspace or ".." in PurePosixPath(workspace).parts:
        raise InvalidEvidence("recorded workspace must be one canonical absolute runner path")
    runner = value["runner"]
    if not isinstance(runner, dict) or set(runner) != {"os", "arch", "python"}:
        raise InvalidEvidence("actual runner environment missing")
    expected = JOBS[name]
    if (runner["os"] != expected.os or not isinstance(runner["python"], str) or not runner["python"].startswith(expected.python + ".") or
            runner["arch"].lower() not in ({"arm64", "aarch64"} if expected.os == "Darwin" else {"amd64", "x86_64"})):
        raise InvalidEvidence("raw report runner does not satisfy its actual matrix cell")
    return workspace
