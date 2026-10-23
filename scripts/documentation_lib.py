"""Fixed documentation checks and literal quickstart execution in disposable state."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tomllib
from urllib.parse import unquote, urlsplit

from acceptance_lib.evidence import (InvalidEvidence, artifact, capture_command, read_command_run,
                                     read_json, sha256, verify_artifact, write_json)

DOCUMENTS = ("README.md", "SPEC.md", "ARCHITECTURE.md", "docs/USER_GUIDE.md", "docs/ROADMAP.md",
             "docs/MODULE_MAP.md", "docs/TEST_PLAN.md", "docs/FEATURE_STATUS.md", "docs/CI.md",
             "docs/BUILDING.md", "docs/MCP.md", "docs/PUBLIC_EVALUATION.md", "docs/HOLDOUT_AUDIT.md", "docs/CAPACITY.md")
SUITE = "goldmine.documentation.v1"
QUICKSTART = """python3 -m venv .venv
.venv/bin/python -m pip install .
mkdir -p sample
printf '中文知识库：退款期限为四十五天。\\n' > sample/notes.md
.venv/bin/goldmine ingest sample --db sample.sqlite --lexical-only
.venv/bin/goldmine search '退款期限' --db sample.sqlite --lexical --pack"""
HELPS = ("ingest", "search", "status", "backup", "reindex", "serve", "watch", "mcp", "model", "init", "distill")
MCP_INSTALL = "python3 -m venv .mcp-venv\n.mcp-venv/bin/python -m pip install '.[mcp]'"
PROVENANCE = ("import goldmine,hashlib,importlib.util,json,sys; from pathlib import Path; p=Path(goldmine.__file__).parent; "
              "print(json.dumps({'file':goldmine.__file__,'prefix':sys.prefix,'version':goldmine.__version__,"
              "'mcp':importlib.util.find_spec('mcp') is not None,"
              "'modules':{'goldmine/'+f.relative_to(p).as_posix():hashlib.sha256(f.read_bytes()).hexdigest() "
              "for f in p.rglob('*.py')}}))")


def _without_fences(text):
    return re.sub(r"(?m)^(```|~~~)[^\n]*\n.*?^\1[^\n]*$", "", text, flags=re.S)


def _anchors(text):
    used, result = {}, set()
    for title in re.findall(r"(?m)^#{1,6}\s+(.+?)\s*#*\s*$", _without_fences(text)):
        slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        count = used.get(slug, 0)
        result.add(slug + ("-" + str(count) if count else ""))
        used[slug] = count + 1
    return result


def document_state(root):
    root = Path(root).resolve()
    links = 0
    for name in DOCUMENTS:
        path = root / name
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", _without_fences(text)):
            destination = match[1].split(" ", 1)[0].strip("<>")
            url = urlsplit(destination)
            if url.scheme or url.netloc:
                continue  # External URL uptime is not asserted by an offline check.
            target = (path.parent / unquote(url.path)).resolve() if url.path else path
            if not target.is_relative_to(root) or not target.exists():
                raise InvalidEvidence(f"broken/out-of-repository documentation link: {name}: {destination}")
            if url.fragment and target.suffix == ".md" and unquote(url.fragment) not in _anchors(target.read_text()):
                raise InvalidEvidence(f"broken documentation anchor: {name}: {destination}")
            links += 1
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assignments = ast.parse((root / "goldmine/__init__.py").read_text()).body
    versions = [ast.literal_eval(node.value) for node in assignments if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)]
    if versions != [project]:
        raise InvalidEvidence("documented package/runtime versions differ")
    blocks = re.findall(r"```(?:sh|bash)\n(.*?)\n```", (root / "README.md").read_text(), flags=re.S)
    if not blocks or blocks[0] != QUICKSTART:
        raise InvalidEvidence("README lexical quickstart changed; review the fixed command registry before executing it")
    mcp_blocks = re.findall(r"```sh\n(.*?)\n```", (root / "docs/MCP.md").read_text(), flags=re.S)
    if not mcp_blocks or mcp_blocks[0] != MCP_INSTALL:
        raise InvalidEvidence("MCP checkout install changed; review the fixed command registry before executing it")
    from search_bench.session import package_identity

    return {"documents_sha256": {name: sha256(root / name) for name in DOCUMENTS}, "local_links": links,
            "version": project, "modules_sha256": package_identity(root), "pyproject_sha256": sha256(root / "pyproject.toml"),
            "tools_sha256": {name: sha256(root / name) for name in ("scripts/documentation_lib.py", "scripts/check_documentation.py", "scripts/distribution_lib.py")}}


def commands(out, python):
    out = Path(out).resolve()
    source, outside = out / "source", out / "outside-source"
    cli, installed_python = source / ".venv/bin/goldmine", source / ".venv/bin/python"
    result = {"quickstart": (["/bin/sh", "-eu", "-c", QUICKSTART], source),
              "mcp-install": (["/bin/sh", "-eu", "-c", MCP_INSTALL], source),
              "provenance": ([str(installed_python), "-c", PROVENANCE], outside),
              "provenance-mcp": ([str(source / ".mcp-venv/bin/python"), "-c", PROVENANCE], outside),
              "mcp-protocol": ([str(source / ".mcp-venv/bin/python"), str(out / "outside-source/mcp_smoke.py"), str(source / ".mcp-venv/bin/goldmine"), str(source / "sample.sqlite")], outside),
              "version": ([str(cli), "--version"], outside),
              "help": ([str(cli), "--help"], outside),
              "search-json": ([str(cli), "search", "退款期限", "--db", str(source / "sample.sqlite"), "--lexical", "--json"], outside),
              "status": ([str(cli), "status", "--db", str(source / "sample.sqlite"), "--json"], outside),
              "backup": ([str(cli), "backup", "--db", str(source / "sample.sqlite"), "--out", str(out / "backup.sqlite")], outside),
              "reindex": ([str(cli), "reindex", "lexical", "--db", str(source / "sample.sqlite"), "--json"], outside)}
    result.update({"help-" + command: ([str(cli), command, "--help"], outside) for command in HELPS})
    return result


def capture(root, out, wheelhouse, distribution, *, python=None):
    from build_python import snapshot
    from acceptance_lib.delivery import verify_bundle

    root, out, wheelhouse = Path(root).resolve(), Path(out).resolve(), Path(wheelhouse).resolve()
    before = document_state(root)
    delivery = verify_bundle(distribution)
    if out.exists():
        raise InvalidEvidence("documentation output must be new")
    if not wheelhouse.is_dir():
        raise InvalidEvidence("documentation quickstart requires a prepared offline wheelhouse including setuptools")
    out.mkdir(parents=True)
    source = out / "source"
    copied = snapshot(root, source)
    (out / "outside-source").mkdir()
    from distribution_lib import _MCP_SMOKE

    (out / "outside-source/mcp_smoke.py").write_text(_MCP_SMOKE)
    state = out / "isolated-home"
    state.mkdir()
    # The shell text is a constant and must equal the reviewed README block.
    # No unreviewed Markdown, substitutions or arbitrary command input executes.
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GOLDMINE_", "DYLD_")) and key not in ("PYTHONPATH", "PYTHONHOME")}
    chosen_python = Path(python or sys.executable).absolute()
    if not (chosen_python.parent / "python3").is_file() or (chosen_python.parent / "python3").resolve() != chosen_python.resolve():
        raise InvalidEvidence("literal docs need python3 beside the explicitly selected Python interpreter")
    env.update({"GOLDMINE_HOME": str(state), "GOLDMINE_CONFIG": str(state / "config.toml"),
                "GOLDMINE_OCR_AUTOCOMPILE": "0", "PIP_NO_INDEX": "1", "PIP_FIND_LINKS": str(wheelhouse),
                "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONNOUSERSITE": "1",
                "PATH": os.pathsep.join([str(chosen_python.parent), "/usr/bin", "/bin"])})
    rows = {}
    identity = {"documentation": before, "distribution": delivery}
    for name, (argv, cwd) in commands(out, chosen_python).items():
        directory = out / "commands" / name
        capture_command(directory, "docs." + name, argv, cwd=cwd, env=env, identity=identity, suite_id=SUITE, timeout=180)
        rows[name] = artifact(out, directory / "manifest.json")
        _, receipt, _ = read_command_run(directory / "manifest.json")
        if receipt["exit_code"] or receipt["timed_out"]:
            raise InvalidEvidence(f"documentation command did not pass: {name}; preserve {directory}")
    manifest = {"schema": 1, "kind": SUITE, "before": before, "after": document_state(root),
                "distribution": {"manifest_path": str(Path(distribution).resolve()), "sha256": sha256(distribution)},
                "distribution_identity": delivery, "source_snapshot": copied,
                "python": str(chosen_python), "wheelhouse": str(wheelhouse), "commands": rows,
                "artifacts": {name: artifact(out, out / name) for name in ("source/sample/notes.md", "source/sample.sqlite", "backup.sqlite", "outside-source/mcp_smoke.py")}}
    write_json(out / "manifest.json", manifest)
    return verify(root, out / "manifest.json")


def verify(root, path):
    from contextlib import closing
    from acceptance_lib.delivery import verify_bundle
    from acceptance_lib.evidence import external_ref

    path, root = Path(path).resolve(), Path(root).resolve()
    data = read_json(path)
    keys = {"schema", "kind", "before", "after", "distribution", "distribution_identity", "source_snapshot", "python", "wheelhouse", "commands", "artifacts"}
    if not isinstance(data, dict) or set(data) != keys or data["schema"] != 1 or data["kind"] != SUITE:
        raise InvalidEvidence("documentation evidence must have the fixed raw manifest schema")
    current = document_state(root)
    if data["before"] != data["after"] or data["after"] != current:
        raise InvalidEvidence("documentation/version/runtime source changed after command capture")
    delivery = verify_bundle(external_ref(path.parent, data["distribution"], manifest=True))
    if delivery != data["distribution_identity"]:
        raise InvalidEvidence("documentation installed distribution identity differs")
    expected_snapshot = {**current["modules_sha256"], **{name: sha256(root / name) for name in ("pyproject.toml", "README.md", "LICENSE")}}
    if data["source_snapshot"] != expected_snapshot:
        raise InvalidEvidence("documentation source-install snapshot differs from current package bytes")
    expected_artifacts = {"source/sample/notes.md", "source/sample.sqlite", "backup.sqlite", "outside-source/mcp_smoke.py"}
    if not isinstance(data["artifacts"], dict) or set(data["artifacts"]) != expected_artifacts:
        raise InvalidEvidence("documentation sample/index/backup/protocol artifacts are missing")
    for name, ref in data["artifacts"].items():
        if verify_artifact(path.parent, ref) != path.parent / name:
            raise InvalidEvidence("documentation artifact identity differs: " + name)
    required = commands(path.parent, data["python"])
    if not isinstance(data["commands"], dict) or set(data["commands"]) != set(required):
        raise InvalidEvidence("documentation evidence lacks a fixed required command")
    raw = {}
    identity = {"documentation": current, "distribution": delivery}
    for name, (argv, cwd) in required.items():
        manifest, receipt, stdout = read_command_run(verify_artifact(path.parent, data["commands"][name]), check_id="docs." + name, suite_id=SUITE)
        if (manifest["identity"] != identity or receipt["argv"] != argv or Path(receipt["cwd"]).resolve() != cwd
                or receipt["exit_code"] or receipt["timed_out"]):
            raise InvalidEvidence("documentation command/identity/exit status differs: " + name)
        if (receipt["env"].get("GOLDMINE_HOME") != str(path.parent / "isolated-home")
                or receipt["env"].get("GOLDMINE_CONFIG") != str(path.parent / "isolated-home/config.toml")
                or "PYTHONPATH" in receipt["env"] or receipt["env"].get("PYTHONNOUSERSITE") != "1"):
            raise InvalidEvidence("documentation command lacks isolated actual home/import settings")
        if name in ("quickstart", "mcp-install") and (receipt["env"].get("PIP_NO_INDEX") != "1"
                or receipt["env"].get("PIP_FIND_LINKS") != data["wheelhouse"]
                or receipt["env"].get("PYTHONNOUSERSITE") != "1"):
            raise InvalidEvidence("documentation literal installation lacks its actual offline environment")
        raw[name] = stdout
    for name, environment, mcp in (("provenance", ".venv", False), ("provenance-mcp", ".mcp-venv", True)):
        provenance, venv = read_json(raw[name]), path.parent / "source" / environment
        if (provenance.get("modules") != current["modules_sha256"] or provenance.get("version") != current["version"]
                or provenance.get("mcp") is not mcp or Path(provenance.get("prefix", "")).resolve() != venv
                or not Path(provenance.get("file", "")).resolve().is_relative_to(venv)):
            raise InvalidEvidence("documentation quickstart imported checkout code or installed different modules/version/extra")
    from distribution_lib import _MCP_SMOKE

    if (path.parent / "outside-source/mcp_smoke.py").read_text() != _MCP_SMOKE or read_json(raw["mcp-protocol"]) != {"handshake": True, "tools": 4, "invalid_path_refused": True}:
        raise InvalidEvidence("documentation MCP example lacks actual fixed SDK protocol proof")
    if raw["version"].read_text().strip() != "goldmine " + current["version"]:
        raise InvalidEvidence("installed documented version output differs")
    for name in ("help", *("help-" + command for command in HELPS)):
        if "usage:" not in raw[name].read_text().lower():
            raise InvalidEvidence("documented command has no actual installed CLI help")
    hits = read_json(raw["search-json"])
    document = path.parent / "source/sample/notes.md"
    if not isinstance(hits, list) or not any(Path(row.get("path", "")).resolve() == document for row in hits):
        raise InvalidEvidence("literal README CJK query did not recover its actual sample document")
    if document.read_text() != "中文知识库：退款期限为四十五天。\n":
        raise InvalidEvidence("README quickstart did not produce its literal sample content")
    original, backup = path.parent / "source/sample.sqlite", path.parent / "backup.sqlite"
    for database in (original, backup):
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] != 1:
                raise InvalidEvidence("documented backup/reindex index contents are incomplete")
    return {"scope": "literal_offline_base_and_mcp_installs_plus_sdk_helps_backup_reindex", "documents": len(DOCUMENTS),
            "local_links": current["local_links"], "external_links_checked": False, "version": current["version"],
            "commands": len(required), "distribution": delivery, "optional_live_and_quality": "checked separately by local_rc"}
