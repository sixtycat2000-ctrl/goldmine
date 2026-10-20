"""Help must work before any assets or services have been installed."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["eval_real", "eval_answer", "eval_public", "eval_distill", "bench"])
def test_evaluation_help_without_assets(name, tmp_path):
    env = {**os.environ, "GOLDMINE_HOME": str(tmp_path),
           "GOLDMINE_LLAMA_MODEL": str(tmp_path / "missing.gguf"),
           "GOLDMINE_LLAMA_SERVER": str(tmp_path / "missing-server")}
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / f"{name}.py"), "--help"],
                            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert "Traceback" not in result.stderr
    assert list(tmp_path.iterdir()) == []


def _capture_module():
    spec = importlib.util.spec_from_file_location("capture_baseline", ROOT / "scripts/capture_baseline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_capture_preserves_bytes_and_rejects_nonempty(tmp_path):
    module = _capture_module()
    root = tmp_path / "source"
    root.mkdir()
    data = root / "baseline.json"
    data.write_bytes(b'{"frozen": true}\r\n')
    spec = root / "spec.json"
    spec.write_text(json.dumps({"source_commit": "historical", "sha256": {
        "baseline.json": module.sha256(data)}}))
    out = tmp_path / "capture"
    report = module.capture(out, root=root, spec=spec, collect=False)
    assert report["status"] == "PASS" and report["include_live"] is False
    assert (out / "history/baseline.json").read_bytes() == data.read_bytes()
    with pytest.raises(ValueError, match="absent or empty"):
        module.capture(out, root=root, spec=spec, collect=False)


def test_capture_drift_is_reported_without_rewriting_input(tmp_path):
    module = _capture_module()
    source = tmp_path / "source"
    source.mkdir()
    (source / "baseline.json").write_bytes(b"changed")
    spec = source / "spec.json"
    spec.write_text(json.dumps({"source_commit": "old", "sha256": {"baseline.json": "0" * 64}}))
    out = tmp_path / "report"
    report = module.capture(out, root=source, spec=spec, collect=False)
    assert report["status"] == "NOT_COMPARABLE"
    assert report["drift"] == ["baseline.json"]
    assert (source / "baseline.json").read_bytes() == b"changed"
