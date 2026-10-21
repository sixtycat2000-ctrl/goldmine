"""Fixed resolver provenance checks; no actual native tool/model is executed."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import pytest

from acceptance_lib.evidence import capture_command, read_command_run
from answer_eval.protocol import EvaluationError, sha256_file
from search_comparison.engine_capture import native
from search_comparison.engine_capture.plan import TOOLS
from search_comparison.engine_capture.settings import environment


def fixture(root):
    root = root.resolve()
    tools = root / "tools"
    tools.mkdir()
    paths = {"installation": str(Path(__file__).resolve().parents[1]), "python": str(Path(sys.executable).resolve())}
    for role in sorted(TOOLS["goldmine"] - {"python", "imageio"}):
        path = tools / role
        path.write_text("#!/bin/sh\nexit 99\n")
        path.chmod(0o755)
        paths[role] = str(path)
    paths["imageio"] = "/usr/bin/osascript"
    paths.update(models=str(root / "not-real-models"), embedding=str(root / "not-real-models/never-opened.gguf"), python_environment="/owned-unused-test-prefix")
    cfg = root / "goldmine.toml"
    cfg.write_text('[core]\nhome = ' + json.dumps(str(root / "home")) + '\n[llama]\nserver = ' + json.dumps(paths["server"]) + '\nmodel = ' + json.dumps(paths["embedding"]) + '\n[ingest]\nasr_whisper = ' + json.dumps(paths["whisper"]) + '\n')
    paths["goldmine_config"] = str(cfg)
    engine = {"tools": {r: {"sha256": sha256_file(Path(paths[r])), "bytes": Path(paths[r]).stat().st_size} for r in TOOLS["goldmine"]}}
    return paths, engine


def payload(paths, engine):
    modules = {"goldmine": "__init__.py", "goldmine.config": "config.py", "goldmine.ocr": "ocr.py", "goldmine.backends.llama": "backends/llama.py",
               "goldmine.ingest.asr": "ingest/asr.py", "goldmine.ingest.exif": "ingest/exif.py", "goldmine.ingest.extract_pdf": "ingest/extract_pdf.py", "goldmine.ingest.media": "ingest/media.py"}
    return {"schema": 1, "scope": "source-only-effective-resolvers-no-tool-or-model-execution", "installation": paths["installation"], "configuration": paths["goldmine_config"],
            "server_source": "env/config", "modules": {k: str(Path(paths["installation"]) / "goldmine" / n) for k, n in modules.items()},
            "tools": {r: {"selected": paths[r], "resolved": paths[r], "sha256": engine["tools"][r]["sha256"], "bytes": engine["tools"][r]["bytes"]} for r in TOOLS["goldmine"]}}


@pytest.mark.parametrize("change", ["path", "selected", "bytes", "sha", "missing", "module", "fallback"])
def test_effective_tool_must_equal_pinned_dependency(tmp_path, change):
    # No platform tool bytes needed for parser-level counterexamples.
    paths = {r: "/owned/tools/" + r for r in TOOLS["goldmine"]}
    paths.update(installation="/owned/installation", goldmine_config="/owned/config.toml")
    engine = {"tools": {r: {"sha256": "a" * 64, "bytes": 32} for r in TOOLS["goldmine"]}}
    value = payload(paths, engine)
    assert native.verify(value, engine, paths, paths["goldmine_config"]) == value
    if change == "path": value["tools"]["pdfinfo"]["resolved"] = "/opt/homebrew/other/pdfinfo"
    if change == "selected": value["tools"]["pdfinfo"]["selected"] = "/unrelated/pdfinfo"
    if change == "bytes": value["tools"]["sips"]["bytes"] = 31
    if change == "sha": value["tools"]["imageio"]["sha256"] = "b" * 64
    if change == "missing": value["tools"]["pdftoppm"] = None
    if change == "module": value["modules"]["goldmine.ingest.extract_pdf"] = "/global/shadow/extract_pdf.py"
    if change == "fallback": value["server_source"] = "brew"
    with pytest.raises(EvaluationError):
        native.verify(value, engine, paths, paths["goldmine_config"])


def test_all_native_roles_are_explicit_and_owned_path_wins(tmp_path):
    assert {"pdfinfo", "pdftotext", "pdftoppm", "sips", "imageio"} <= TOOLS["goldmine"]
    paths = {r: "/owned/pinned/" + r for r in TOOLS["goldmine"]}
    paths.update(installation="/owned/installation", models="/owned/models")
    env = environment(tmp_path.resolve(), "goldmine", paths)
    assert env["PATH"].split(":")[0] == "/owned/pinned"
    assert env["GOLDMINE_OCR_AUTOCOMPILE"] == "0"
    paths["pdfinfo"] = "/owned/pinned/hidden-different-name"
    with pytest.raises(EvaluationError, match="basename"):
        environment(tmp_path.resolve(), "goldmine", paths)


@pytest.mark.native
def test_actual_source_resolvers_read_bytes_without_executing_tools(tmp_path):
    owner = tmp_path.resolve()
    paths, engine = fixture(owner)
    env = environment(owner, "goldmine", paths)
    script = Path(native.__file__).with_name("_native_runtime.py")
    argv = [sys.executable, "-I", "-B", str(script), paths["installation"], paths["goldmine_config"]]
    capture_command(owner / "raw", "comparison.native-resolver.fixture", argv, cwd=owner, env=env,
                    identity={"scope": "actual resolver-only declared controlled executable stubs", "models_or_native_tools_executed": False}, timeout=10, owned_process_group=True)
    _, receipt, stdout = read_command_run(owner / "raw/manifest.json")
    assert receipt["exit_code"] == 0
    value = json.loads(stdout.read_text())
    assert native.verify(value, engine, paths, paths["goldmine_config"])["tools"]["pdfinfo"]["resolved"] == paths["pdfinfo"]
    assert not Path(paths["embedding"]).exists()


def test_before_readback_happens_before_ingest(monkeypatch):
    from search_comparison.engine_capture import run
    seen = []
    values = {"python": "/owned/python", "python_environment": "/owned", "goldmine_db": "/owned/index", "goldmine_config": "/owned/config", "selected_corpus": "/owned/corpus"}
    class Calls:
        def call(self, label, argv, **kwargs):
            seen.append(label)
            return json.dumps({"python": values["python"], "numpy": {"path": "/owned/numpy/__init__.py"}}).encode()
    monkeypatch.setattr(run, "native_readback", lambda *a, **k: seen.append("actual-effective-resolvers"))
    run._prepare("goldmine", "goldmine.fast", Calls(), values, {}, {"scope": "fixture"})
    assert seen == ["goldmine.python-runtime", "actual-effective-resolvers", "goldmine.ingest"]
