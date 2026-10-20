"""Inspect actual wheel/native contents and run installed-code smoke checks."""
from __future__ import annotations

import asyncio
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import sys
import tarfile
import venv
import zipfile

from acceptance_lib.evidence import artifact, capture_command, read_command_run, read_json, sha256, write_json
from native_build_lib.source import BuildError, system_dependencies
from native_build_lib.archive import open_native_archive

REQUIRED_MODULES = ("goldmine/__init__.py", "goldmine/application.py", "goldmine/contracts.py",
    "goldmine/mcp_server.py", "goldmine/commands/__init__.py", "goldmine/commands/search.py",
    "goldmine/commands/maintenance.py", "goldmine/lexical.py", "goldmine/storage_connections.py",
    "goldmine/quality.py", "goldmine/query_plan.py", "goldmine/rerank.py", "goldmine/admission.py",
    "goldmine/ingest/pdf_evidence.py", "goldmine/retrieval_evidence.py", "goldmine/citations.py",
    "goldmine/vector_cache.py", "goldmine/_vector_cache_files.py")


def inspect_wheel(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise BuildError("wheel contains duplicate entries")
        missing = set(REQUIRED_MODULES) - set(names)
        if missing:
            raise BuildError(f"wheel missing required modules: {sorted(missing)}")
        metadata_names = [n for n in names if n.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise BuildError("wheel needs one metadata file")
        metadata = Parser().parsestr(archive.read(metadata_names[0]).decode())
        dependencies = metadata.get_all("Requires-Dist", [])
        default = [d for d in dependencies if ";" not in d]
        if len(default) != 1 or not default[0].lower().startswith("numpy"):
            raise BuildError(f"unexpected base runtime dependencies: {default}")
        if metadata["Name"] != "goldmine" or not metadata["Version"]:
            raise BuildError("unexpected distribution identity")
        modules = {name: hashlib.sha256(archive.read(name)).hexdigest()
                   for name in names if name.startswith("goldmine/") and name.endswith(".py")}
        return {"version": metadata["Version"], "sha256": sha256(path), "source_modules": modules,
                "modules": list(REQUIRED_MODULES), "requires_dist": dependencies}


def extract_native(path: Path, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=False)
    with open_native_archive(path) as archive:
        members = archive.getmembers()
        allowed = {"llama-server", "goldmine-ocr", "LLAMA-LICENSE.txt", "native-manifest.json"}
        if {m.name for m in members} != allowed or len(members) != len(allowed) or any(not m.isfile() for m in members):
            raise BuildError("native release tar needs exactly the four regular declared files")
        archive.extractall(destination, filter="data")
    metadata = read_json(destination / "native-manifest.json")
    if metadata.get("release_eligible") is not True or metadata.get("architecture") != "arm64":
        raise BuildError("native tar is dev-only or wrong architecture")
    if set(metadata.get("files", {})) != allowed - {"native-manifest.json"}:
        raise BuildError("native manifest has incomplete file identity")
    for name, pin in metadata["files"].items():
        if sha256(destination / name) != pin:
            raise BuildError(f"native file SHA mismatch: {name}")
    return metadata


def smoke(wheel: Path, wheelhouse: Path, out: Path, profile: str, *,
          native_tar: Path | None = None, model: Path | None = None) -> dict:
    if out.exists() and any(out.iterdir()):
        raise BuildError("smoke output must be new or empty")
    out.mkdir(parents=True, exist_ok=True)
    wheel_info = inspect_wheel(wheel)
    if not wheelhouse.is_dir():
        raise BuildError("offline installation needs a prepared wheelhouse")
    isolated = out / "venv"
    venv.EnvBuilder(with_pip=True).create(isolated)
    python = isolated / "bin/python"
    cli = isolated / "bin/goldmine"
    cwd = out / "outside-source"
    cwd.mkdir()
    state = out / "state"
    state.mkdir()
    config = state / "config.toml"
    config.write_text('[llama]\nmodel = "missing-model.gguf"\nserver = "missing-server"\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GOLDMINE_", "DYLD_")) and k != "PYTHONPATH"}
    env.update({"GOLDMINE_HOME": str(state), "GOLDMINE_CONFIG": str(config),
                "GOLDMINE_OCR_AUTOCOMPILE": "0", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PYTHONNOUSERSITE": "1", "PATH": str(isolated / "bin")})
    commands = []

    def command(name: str, argv: list[str], allowed=(0,)) -> str:
        directory = out / "commands" / name
        capture_command(directory, f"distribution.{name}", argv, cwd=cwd, env=env,
                        identity={"wheel_sha256": wheel_info["sha256"]}, suite_id="distribution.v1", timeout=300)
        _, receipt, stdout = read_command_run(directory / "manifest.json")
        commands.append({"name": name, "exit_code": receipt["exit_code"],
                         "manifest": artifact(out, directory / "manifest.json")})
        if receipt["exit_code"] not in allowed:
            raise BuildError(f"installed command failed: {name}; logs: {directory}")
        if "Traceback (most recent call last)" in stdout.read_text() or "Traceback (most recent call last)" in (directory / "stderr.log").read_text():
            raise BuildError(f"installed command exposed traceback: {name}")
        return stdout.read_text()

    requirement = str(wheel.resolve()) + ("[mcp]" if profile == "mcp" else "")
    command("install", [str(python), "-m", "pip", "install", "--no-index", "--find-links", str(wheelhouse.resolve()), requirement])
    provenance = json.loads(command("provenance", [str(python), "-c",
        "import goldmine,hashlib,importlib.util,json,sys; from pathlib import Path; p=Path(goldmine.__file__).parent; "
        "print(json.dumps({'file':goldmine.__file__,'version':goldmine.__version__,'mcp':importlib.util.find_spec('mcp') is not None,'prefix':sys.prefix,"
        "'modules':{'goldmine/'+str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() for f in p.rglob('*.py')}}))"]))
    if not Path(provenance["file"]).resolve().is_relative_to(isolated.resolve()) or provenance["version"] != wheel_info["version"]:
        raise BuildError("source checkout masked the installed wheel or version mismatch")
    if provenance["mcp"] != (profile == "mcp"):
        raise BuildError("base/MCP environment dependency separation failed")
    if provenance["modules"] != wheel_info["source_modules"]:
        raise BuildError("installed module bytes differ from the exact wheel")
    absent = json.loads(command("missing-native-tools", [str(python), "-c",
        "import json,shutil; print(json.dumps({name:shutil.which(name) for name in "
        "['ffmpeg','ffprobe','pdftotext','pdfinfo','goldmine-ocr','llama-server']}))"]))
    if any(absent.values()):
        raise BuildError("base smoke unexpectedly found native tools")
    for name, args in (("version", ["--version"]), ("help", ["--help"]),
                       ("mcp-help", ["mcp", "--help"]), ("ingest-help", ["ingest", "--help"])):
        command(name, [str(cli), *args])
    modules = [name.removesuffix(".py").replace("/", ".").removesuffix(".__init__") for name in REQUIRED_MODULES]
    command("imports", [str(python), "-c", "import importlib; " + ";".join(f"importlib.import_module({name!r})" for name in modules)])
    corpus = cwd / "corpus"
    corpus.mkdir()
    document = corpus / "proof.txt"
    document.write_text("Zircon lantern evidence belongs to this local document.\n")
    db = state / "index.sqlite"
    ingest = json.loads(command("ingest", [str(cli), "ingest", str(corpus), "--db", str(db), "--lexical-only", "--json"]))
    if ingest.get("files", {}).get("failed") != 0:
        raise BuildError("installed lexical ingest failed")
    hits = json.loads(command("search", [str(cli), "search", "zircon lantern", "--db", str(db), "--lexical", "--json"]))
    if not isinstance(hits, list) or not any(h.get("path") == str(document) for h in hits):
        raise BuildError("installed search did not recover actual corpus document")
    command("status", [str(cli), "status", "--db", str(db), "--json"])
    doctor = json.loads(command("doctor", [str(cli), "doctor", "--db", str(db), "--json"], allowed=(4,)))
    failed_names = {check["name"] for check in doctor.get("checks", []) if check.get("status") == "fail"}
    if not {"backend llama", "models: embedding"}.issubset(failed_names):
        raise BuildError("doctor did not explain deliberately missing backend/model dependencies")
    # A lexical-only index can answer through words without loading a model.
    # Ingest without --lexical-only actually requires the deliberately absent model.
    missing = json.loads(command("missing-model", [str(cli), "ingest", str(corpus), "--db",
            str(state / "missing-model.sqlite"), "--json"], allowed=(2,)))
    if missing.get("error", {}).get("kind") != "backend_startup_failed" or not missing["error"].get("fix"):
        raise BuildError("missing backend did not preserve the existing startup-error contract")
    if profile == "base":
        command("missing-sdk", [str(cli), "mcp", "--db", str(db)], allowed=(4,))
    else:
        script = cwd / "mcp_smoke.py"
        script.write_text(_MCP_SMOKE)
        command("mcp-protocol", [str(python), str(script), str(cli), str(db)])
        incompatible = json.loads(command("incompatible-sdk", [str(python), "-c", _INCOMPATIBLE_SDK,
                                             str(db)], allowed=(4,)))
        if incompatible.get("error", {}).get("kind") != "missing_capability":
            raise BuildError("incompatible SDK version did not produce a readable prerequisite error")
    native_info = None
    if native_tar:
        native = cwd / "native"
        native_info = extract_native(native_tar, native)
        command("native-version", [str(native / "llama-server"), "--version"])
        command("ocr-version", [str(native / "goldmine-ocr"), "--version"])
        render = cwd / "render-ocr.swift"
        render.write_text(_OCR_FIXTURE)
        image = cwd / "ocr-proof.png"
        command("ocr-fixture", ["/usr/bin/swift", str(render), str(image)])
        recognized = json.loads(command("native-ocr", [str(native / "goldmine-ocr"), str(image)]))
        text = " ".join(line.get("text", "") for line in recognized.get("texts", []))
        if recognized.get("error") or "ZIRCON LANTERN" not in text.upper():
            raise BuildError("extracted OCR binary failed to read the generated proof image")
        for executable in ("llama-server", "goldmine-ocr"):
            system_dependencies(command(f"native-dependencies-{executable}", ["/usr/bin/otool", "-L", str(native / executable)]))
        if model:
            env.update({"GOLDMINE_MODEL": str(model.resolve()), "GOLDMINE_LLAMA_SERVER": str(native / "llama-server")})
            command("native-embedding", [str(python), "-c", _NATIVE_EMBED])
    result = {"schema": 1, "profile": profile, "wheel": wheel_info, "provenance": provenance,
              "source_directory_imported": False, "offline_install": True, "commands": commands,
              "native": native_info, "native_tar_sha256": sha256(native_tar) if native_tar else None,
              "native_embedding_exercised": bool(native_tar and model)}
    write_json(out / "smoke.json", result)
    return result


_MCP_SMOKE = '''import anyio, json, os, sys
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
async def main():
    params = StdioServerParameters(command=sys.argv[1], args=["mcp", "--db", sys.argv[2]], env=dict(os.environ))
    async with Client(stdio_client(params), mode="legacy", read_timeout_seconds=15) as client:
        tools = await client.list_tools()
        assert {t.name for t in tools.tools} == {"goldmine_search","goldmine_get","goldmine_context","goldmine_status"}
        status = await client.call_tool("goldmine_status", {})
        assert not status.is_error
        invalid = await client.call_tool("goldmine_get", {"citation_id":"/etc/passwd"})
        assert invalid.is_error
        print(json.dumps({"handshake":True,"tools":len(tools.tools),"invalid_path_refused":True}))
anyio.run(main)
'''

_INCOMPATIBLE_SDK = '''import importlib.metadata,sys
original=importlib.metadata.version
importlib.metadata.version=lambda name: '0.0.0' if name=='mcp' else original(name)
from goldmine.cli import main
raise SystemExit(main(['mcp','--db',sys.argv[1],'--json']))
'''

_NATIVE_EMBED = '''from goldmine.config import load
from goldmine.backends import create_backend
backend=create_backend(load(),"llama")
try:
    backend.startup()
    vectors=backend.embed_texts(["Isolated native installation proof."])
    assert vectors.shape == (1,768)
    print("native embedding dimension=768")
finally:
    backend.close()
'''

_OCR_FIXTURE = '''import AppKit
let image=NSImage(size:NSSize(width:1000,height:240))
image.lockFocus()
NSColor.white.setFill()
NSRect(x:0,y:0,width:1000,height:240).fill()
("ZIRCON LANTERN 2026" as NSString).draw(at:NSPoint(x:40,y:90),withAttributes:[.font:NSFont.systemFont(ofSize:64),.foregroundColor:NSColor.black])
image.unlockFocus()
let bitmap=NSBitmapImageRep(data:image.tiffRepresentation!)!
try bitmap.representation(using:.png,properties:[:])!.write(to:URL(fileURLWithPath:CommandLine.arguments[1]))
'''
