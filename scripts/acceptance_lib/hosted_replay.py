"""Portable replay of downloaded raw source/JUnit/build/install artifacts.

No executable from an artifact is imported or run. Recorded runner paths are
compared lexically; the original receipt bytes are never rewritten.
"""
from pathlib import Path, PurePosixPath
import tarfile

from .evidence import InvalidEvidence, bound_path, read_command_run, read_json, sha256, verify_artifact
from .hosted_contract import JOBS, within
from .hosted_metadata import validate_context

MAX_WHEEL_ARCHIVE = 64 * 1024**2
MAX_WHEEL_EXPANDED = 128 * 1024**2
MAX_WHEEL_MEMBER = 8 * 1024**2
MAX_JUNIT_BYTES = 64 * 1024**2


def preflight_wheel(path):
    from .hosted_contract import safe_zip

    return safe_zip(path, max_archive=MAX_WHEEL_ARCHIVE, max_expanded=MAX_WHEEL_EXPANDED,
                    max_member=MAX_WHEEL_MEMBER, max_entries=10_000)


def preflight_junit(report_path):
    """Bound the actual parser input, irrespective of its extension or aliases."""
    report = read_json(report_path)
    for row in report["checks"]:
        if row["kind"] != "pytest":
            continue
        command_path = verify_artifact(report_path.parent, row["evidence"]["command"])
        command = read_json(command_path)
        receipt = read_json(verify_artifact(command_path.parent, command["receipt"]))
        ref = receipt["artifacts"]["junit"]
        if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
            raise InvalidEvidence("JUnit needs its actual bound artifact")
        path = bound_path(command_path.parent, ref["path"])
        if path.stat().st_size > MAX_JUNIT_BYTES:
            raise InvalidEvidence("JUnit exceeds the fixed64MiB parsing bound")


def successful(path, *, suite, check_id=None, identity=None, job=None):
    manifest, receipt, stdout = read_command_run(path, suite_id=suite, check_id=check_id)
    if identity is not None and manifest["identity"] != identity:
        raise InvalidEvidence("command is bound to a different source or artifact")
    if receipt["exit_code"] or receipt["timed_out"]:
        raise InvalidEvidence("required raw command failed or timed out")
    if job is not None:
        within(receipt["started_at"], receipt["finished_at"], job)
    return manifest, receipt, stdout


def pytest_report(root, extracted, selected, job, source):
    from .runner import validate

    scope = JOBS[job["name"]].profile
    relative = f"{scope}/ci-context.json" if scope in {"mcp"} else "ci-context.json"
    context_path = extracted / relative
    context = read_json(context_path)
    if set(context) != {"schema", "profile", "before", "after", "report", "hosted"} or context["schema"] != 1 or context["profile"] != scope:
        raise InvalidEvidence("raw CI profile/context schema differs")
    workspace = validate_context(context["hosted"], selected, job, source, scope)
    content = source["identity"]["content_sha256"]
    if context["before"] != content or context["after"] != content:
        raise InvalidEvidence("CI source snapshot differs from the current committed source")
    path = verify_artifact(context_path.parent, context["report"])
    preflight_junit(path)
    report = validate(root, path)
    profile = {"base": "offline", "native": "native", "mcp": "optional-mcp"}[scope]
    if report["requested_profile"] != profile or report["profile"] != profile or report["task_id"] is not None or report["inputs"] is not None:
        raise InvalidEvidence("CI report selected a different or incomplete source scope")
    if any(report[k] != v for k, v in source["identity"].items()) or report["platform"] != context["hosted"]["runner"]:
        raise InvalidEvidence("CI report checkout/actual platform differs from hosted context")
    within(report["started_at"], report["finished_at"], job)
    if report["status"] != "PASS":
        raise InvalidEvidence("required source/JUnit scope is failed or incomplete")
    for check in report["checks"]:
        if check["kind"] == "source":
            snap = read_json(verify_artifact(path.parent, check["evidence"]["snapshot"]))
            if snap != {"started": source["identity"], "finished": source["identity"]}:
                raise InvalidEvidence("CI checkout became dirty during the actual job")
        if check["kind"] != "pytest":
            continue
        for label in ("command", "collection"):
            command = verify_artifact(path.parent, check["evidence"][label])
            manifest, receipt, _ = successful(command, suite=f"pytest.{profile}" + (".collection" if label == "collection" else ""),
                identity=source["identity"], job=job)
            directory = PurePosixPath(workspace) / "reports/ci" / scope / "checks" / check["id"] / ("execution" if label == "command" else "collection")
            tail = ["-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "acceptance_lib.pytest_plugin", "--acceptance-profile", profile]
            if label == "collection":
                tail += ["--collect-only", "--acceptance-inventory", str(directory / "inventory.json"), "tests"]
            else:
                tail += ["--acceptance-inventory", str(directory / "inventory.json"), "--junitxml", str(directory / "junit.xml"), "tests"]
            if (receipt["argv"][1:] != tail or receipt["cwd"] != workspace or
                    receipt["env"].get("PYTHONPATH") != f"{workspace}/scripts:{workspace}"):
                raise InvalidEvidence("JUnit receipt did not execute the trusted full pytest scope")
    return {"profile": profile, "report_sha256": sha256(path), "platform": report["platform"]}


def _build(path, source, job, *, from_sdist):
    value = read_json(path)
    expected = {"schema", "kind", "identity", "source_snapshot", "from_sdist", "command", "assets"}
    if set(value) != expected or value["schema"] != 1 or value["kind"] != "goldmine.python-build" or value["identity"] != source["identity"] or value["from_sdist"] is not from_sdist:
        raise InvalidEvidence("build manifest source/schema differs")
    successful(verify_artifact(path.parent, value["command"]), suite="distribution.build.v1", identity=source["identity"], job=job)
    assets = {name: verify_artifact(path.parent, ref) for name, ref in value["assets"].items()}
    if set(assets) != {p.name for p in assets.values()}:
        raise InvalidEvidence("ambiguous build artifact names")
    wheels = [p for n, p in assets.items() if n.endswith(".whl")]
    sdists = [p for n, p in assets.items() if n.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != (0 if from_sdist else 1) or len(assets) != 1 + len(sdists):
        raise InvalidEvidence("build artifact inventory differs from wheel/sdist contract")
    return value, wheels[0], sdists


def _sdist(path, expected):
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        if len(members) > 100_000 or sum(r.size for r in members) > 256 * 1024**2:
            raise InvalidEvidence("sdist exceeds fixed bounded source size")
        names, content, roots = set(), {}, set()
        for row in members:
            parts = PurePosixPath(row.name).parts
            if not parts or row.name.startswith("/") or ".." in parts or "\\" in row.name or row.name in names or not (row.isfile() or row.isdir()):
                raise InvalidEvidence("unsafe/ambiguous sdist source member")
            names.add(row.name)
            roots.add(parts[0])
            if row.isfile() and len(parts) > 1:
                import hashlib
                content["/".join(parts[1:])] = hashlib.sha256(archive.extractfile(row).read()).hexdigest()
        if len(roots) != 1 or any(content.get(name) != pin for name, pin in expected.items()):
            raise InvalidEvidence("sdist did not preserve exact committed package source")


def _smoke(path, wheel, *, profile, workspace, job):
    result = read_json(path)
    expected = {"schema", "profile", "wheel", "provenance", "source_directory_imported", "offline_install", "commands", "native", "native_tar_sha256", "native_embedding_exercised"}
    if (set(result) != expected or result["schema"] != 1 or result["profile"] != profile or result["wheel"] != wheel or
            result["source_directory_imported"] is not False or result["offline_install"] is not True or
            result["native"] is not None or result["native_tar_sha256"] is not None or result["native_embedding_exercised"] is not False):
        raise InvalidEvidence("hosted install smoke identity differs")
    install_name = path.parent.name
    runner_out = PurePosixPath(workspace) / "reports/ci" / install_name
    outside, venv = runner_out / "outside-source", runner_out / "venv"
    cli, python = str(venv / "bin/goldmine"), str(venv / "bin/python")
    required = {"install", "provenance", "missing-native-tools", "version", "help", "mcp-help", "ingest-help", "imports", "ingest", "search", "status", "doctor", "missing-model"}
    required |= {"missing-sdk"} if profile == "base" else {"mcp-protocol", "incompatible-sdk"}
    rows = result["commands"]
    if len(rows) != len(required) or {r.get("name") for r in rows} != required:
        raise InvalidEvidence("hosted wheel smoke lacks required actual raw commands")
    stdout_by_name = {}
    for row in rows:
        name = row["name"]
        manifest, receipt, stdout = read_command_run(verify_artifact(path.parent, row["manifest"]), suite_id="distribution.v1", check_id="distribution." + name)
        if (manifest["identity"] != {"wheel_sha256": wheel["sha256"]} or receipt["timed_out"] or
                receipt["exit_code"] != {"doctor": 4, "missing-model": 2, "missing-sdk": 4, "incompatible-sdk": 4}.get(name, 0) or
                receipt["cwd"] != str(outside) or row.get("exit_code") != receipt["exit_code"]):
            raise InvalidEvidence("installed command failed or used another wheel/workspace")
        within(receipt["started_at"], receipt["finished_at"], job)
        argv = receipt["argv"]
        if argv[0] != (python if name in {"install", "provenance", "missing-native-tools", "imports", "mcp-protocol", "incompatible-sdk"} else cli):
            raise InvalidEvidence("installed probe did not use the isolated wheel executable")
        if name == "install" and (argv[1:6] != ["-m", "pip", "install", "--no-index", "--find-links"] or
                argv[6] != f"{workspace}/reports/ci/wheelhouse" or not argv[-1].endswith(".whl" + ("[mcp]" if profile == "mcp" else ""))):
            raise InvalidEvidence("installation was not performed from exact offline wheel artifacts")
        if name in {"version", "help", "mcp-help", "ingest-help"} and argv[1:] != {"version": ["--version"], "help": ["--help"], "mcp-help": ["mcp", "--help"], "ingest-help": ["ingest", "--help"]}[name]:
            raise InvalidEvidence("installed CLI help/version invocation differs")
        stdout_by_name[name] = stdout
    provenance = read_json(stdout_by_name["provenance"])
    installed_file = provenance.get("file", "")
    if (not isinstance(installed_file, str) or ".." in PurePosixPath(installed_file).parts or
            str(PurePosixPath(installed_file)) != installed_file or
            provenance != result["provenance"] or provenance.get("modules") != wheel["source_modules"] or
            provenance.get("version") != wheel["version"] or provenance.get("mcp") is not (profile == "mcp") or
            provenance.get("prefix") != str(venv) or not PurePosixPath(installed_file).is_relative_to(venv)):
        raise InvalidEvidence("installed bytes/provenance are absent or masked by a source checkout")
    absent = read_json(stdout_by_name["missing-native-tools"])
    if set(absent) != {"ffmpeg", "ffprobe", "pdftotext", "pdfinfo", "goldmine-ocr", "llama-server"} or any(absent.values()):
        raise InvalidEvidence("base installation had undeclared native tools")
    if read_json(stdout_by_name["ingest"]).get("files", {}).get("failed") != 0:
        raise InvalidEvidence("actual installed lexical ingest failed")
    hits = read_json(stdout_by_name["search"])
    if not isinstance(hits, list) or not any(h.get("path") == str(outside / "corpus/proof.txt") for h in hits):
        raise InvalidEvidence("actual installed search did not retrieve its owned proof")
    errors = {c["name"] for c in read_json(stdout_by_name["doctor"]).get("checks", []) if c.get("status") == "fail"}
    if not {"backend llama", "models: embedding"}.issubset(errors):
        raise InvalidEvidence("installed doctor did not explain missing dependencies")
    missing = read_json(stdout_by_name["missing-model"]).get("error", {})
    if missing.get("kind") != "backend_startup_failed" or not missing.get("fix"):
        raise InvalidEvidence("actual missing-model failure did not satisfy its contract")
    if profile == "mcp":
        if read_json(stdout_by_name["mcp-protocol"]) != {"handshake": True, "tools": 4, "invalid_path_refused": True} or read_json(stdout_by_name["incompatible-sdk"]).get("error", {}).get("kind") != "missing_capability":
            raise InvalidEvidence("actual SDK protocol/incompatible SDK proof is absent")


def package_report(root, extracted, selected, job, source):
    from distribution_lib import inspect_wheel

    context = read_json(extracted / "package-hosted.json")
    if set(context) != {"schema", "kind", "hosted", "before", "after", "commands", "build", "sdist_build", "smokes"} or context["schema"] != 1 or context["kind"] != "goldmine.hosted-package":
        raise InvalidEvidence("hosted package raw context missing")
    workspace = validate_context(context["hosted"], selected, job, source, "package")
    if context["before"] != source or context["after"] != source:
        raise InvalidEvidence("package checkout source changed during the hosted job")
    build_path = verify_artifact(extracted, context["build"])
    rebuilt_path = verify_artifact(extracted, context["sdist_build"])
    build, wheel_path, sdists = _build(build_path, source, job, from_sdist=False)
    rebuilt, rebuilt_path, _ = _build(rebuilt_path, source, job, from_sdist=True)
    expected = {str(p.relative_to(root)): sha256(p) for p in (root / "goldmine").rglob("*.py") if "__pycache__" not in p.parts}
    snapshot = {**expected, **{name: sha256(root / name) for name in ("pyproject.toml", "README.md", "LICENSE")}}
    if build["source_snapshot"] != snapshot or rebuilt["source_snapshot"] != {"sdist_sha256": sha256(sdists[0])}:
        raise InvalidEvidence("package source snapshot/sdist rebuild identity differs")
    _sdist(sdists[0], snapshot)
    preflight_wheel(wheel_path)
    preflight_wheel(rebuilt_path)
    first, second = inspect_wheel(wheel_path), inspect_wheel(rebuilt_path)
    if first["source_modules"] != expected or second["source_modules"] != expected or first["version"] != second["version"]:
        raise InvalidEvidence("wheel or sdist-rebuilt wheel differs from committed source")
    required = {"build", "sdist-build", "twine", "wheelhouse", "install-base", "install-mcp", "install-sdist"}
    rows = context["commands"]
    if set(rows) != required:
        raise InvalidEvidence("hosted package lacks required bound command receipts")
    remote = PurePosixPath(workspace)
    python = None
    for name, ref in rows.items():
        _, receipt, _ = successful(verify_artifact(extracted, ref), suite="ci.hosted.package.v1", check_id="ci.package." + name, identity=source["identity"], job=job)
        if receipt["cwd"] != workspace:
            raise InvalidEvidence("package wrapper ran outside the recorded source checkout")
        python = python or receipt["argv"][0]
        if receipt["argv"][0] != python:
            raise InvalidEvidence("package steps used mixed Python environments")
        fixed = {
            "build": [str(remote / "scripts/build_python.py"), "--python", python, "--out", str(remote / "reports/ci/python")],
            "sdist-build": [str(remote / "scripts/build_python.py"), "--python", python, "--sdist", str(remote / "reports/ci/python/dist" / sdists[0].name), "--out", str(remote / "reports/ci/sdist")],
            "twine": ["-m", "twine", "check", str(remote / "reports/ci/python/dist" / wheel_path.name), str(remote / "reports/ci/python/dist" / sdists[0].name), str(remote / "reports/ci/sdist/dist" / rebuilt_path.name)],
            "wheelhouse": ["-m", "pip", "download", "--only-binary=:all:", "--dest", str(remote / "reports/ci/wheelhouse"), "-r", str(remote / "requirements/mcp.lock")],
        }
        if name.startswith("install-"):
            selected_wheel = rebuilt_path if name == "install-sdist" else wheel_path
            directory = "sdist" if name == "install-sdist" else "python"
            fixed[name] = [str(remote / "scripts/smoke_install.py"), "--wheel", str(remote / "reports/ci" / directory / "dist" / selected_wheel.name), "--wheelhouse", str(remote / "reports/ci/wheelhouse"), "--profile", "mcp" if name == "install-mcp" else "base", "--out", str(remote / "reports/ci" / name)]
        if receipt["argv"][1:] != fixed[name]:
            raise InvalidEvidence("hosted package command did not execute the exact registered build/install scripts")
    if set(context["smokes"]) != {"install-base", "install-mcp", "install-sdist"}:
        raise InvalidEvidence("one of the three installed environments is absent")
    for name, ref in context["smokes"].items():
        _smoke(verify_artifact(extracted, ref), second if name == "install-sdist" else first,
               profile="mcp" if name == "install-mcp" else "base", workspace=workspace, job=job)
    return {"wheel_sha256": first["sha256"], "rebuilt_wheel_sha256": second["sha256"], "sdist_sha256": sha256(sdists[0])}
