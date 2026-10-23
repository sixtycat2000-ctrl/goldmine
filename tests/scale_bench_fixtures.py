"""A real tiny two-version capture reused by adversarial evidence tests."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from scale_bench.protocol import sha, write
from scale_bench.validation import validate


def original_source(directory):
    revision = json.loads((ROOT / "docs/execution-plan/baseline-20261021.json").read_text())["source_commit"]
    raw = subprocess.check_output(["git", "archive", "--format=tar", revision, "goldmine"], cwd=ROOT)
    directory.mkdir()
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        archive.extractall(directory, filter="data")
    return directory


@pytest.fixture(scope="session")
def scale_capture(tmp_path_factory):
    directory = tmp_path_factory.mktemp("full-capacity-mechanical")
    reference = original_source(directory / "original")
    out = directory / "capture"
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/bench_scale.py"), "--full-cli",
        "--reference-root", str(reference), "--candidate-root", str(ROOT), "--out", str(out),
        "--scales", "121", "--distributions", "10", "--keep-fixtures"],
        cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return out


@pytest.fixture(scope="module", params=["reference", "candidate"])
def scale_human_capture(tmp_path_factory, request):
    from scale_bench.execute import command
    from scale_bench.output import structured
    from scale_bench.protocol import cli_args, isolated_env, query

    directory = tmp_path_factory.mktemp("small-human-pack")
    source = original_source(directory / "original") if request.param == "reference" else ROOT
    db, output = directory / "fixture.sqlite", directory / "fixture.json"
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/bench_scale.py"), "--full-cli", "--worker", "build",
        "--source-root", str(source), "--db", str(db), "--rows", "41", "--per-file", "10", "--result", str(output)],
        capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stderr
    env, parsed, human = isolated_env(source, directory / "state"), {}, None
    for form in ("search", "pack_json", "pack_text"):
        log = directory / form
        receipt = command([sys.executable, "-m", "goldmine.cli", *cli_args(db, 3, form)], log,
                          cwd=directory, env=env, timeout=15)
        assert receipt["exit_code"] == 0, (log / "stderr.log").read_text()
        raw = (log / "stdout.log").read_bytes()
        if form == "pack_text":
            human = raw
        else:
            parsed[form] = structured(raw, form, 41, 10, query(3))
    return human, parsed["pack_json"], json.loads(output.read_text()), len(parsed["search"]["rows"])


class Capture:
    def __init__(self, root):
        self.root, self.originals = root, {}
        self.manifest_path = root / "manifest.json"
        self.manifest = self.get(self.manifest_path)
        self.manifest.pop("summary", None)
        self.case_path = root / self.manifest["cases"][0]["path"]
        self.case = self.get(self.case_path)

    def get(self, path):
        return json.loads(path.read_text())

    def put(self, path, value):
        if path not in self.originals:
            self.originals[path] = path.read_bytes()
        write(path, value)

    def bind_case(self):
        self.put(self.case_path, self.case)
        self.manifest["cases"][0]["sha256"] = sha(self.case_path)
        self.put(self.manifest_path, self.manifest)

    def selected(self):
        sample = next(s for s in self.case["samples"] if s["index"] == 3 and
                      s["arm"] == "candidate" and s["temperature"] == "cold" and s["form"] == "search")
        audit_path = self.case_path.parent / sample["audit"]["path"]
        audit = self.get(audit_path)
        receipt_path = audit_path.parent / audit["command"]["path"]
        return sample, audit_path, audit, receipt_path, self.get(receipt_path)

    def bind_selected(self, selected):
        sample, audit_path, audit, receipt_path, receipt = selected
        self.put(receipt_path, receipt)
        audit["command"]["sha256"] = sha(receipt_path)
        self.put(audit_path, audit)
        sample["audit"]["sha256"] = sha(audit_path)
        self.bind_case()

    def tiny_targets(self):
        # Only test-local target substitution; production acceptance always reads
        # the unchanged12-case registry. No tiny fixture is release evidence.
        value = json.loads((ROOT / "evals/acceptance_targets.json").read_text())["scale"]
        return {**value, "sizes": [121], "segments_per_file": [10]}

    def replay(self):
        self.bind_case()
        return validate(self.manifest_path, current_project=ROOT, targets=self.tiny_targets(), require_all=True)

    def restore(self):
        for path, raw in self.originals.items():
            path.write_bytes(raw)


@contextmanager
def mutate(root):
    capture = Capture(root)
    try:
        yield capture
    finally:
        capture.restore()
