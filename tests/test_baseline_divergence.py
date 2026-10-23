"""Authorized baseline divergence: archive immutable, current inputs registered."""
from pathlib import Path
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from acceptance_lib.checks import history
from acceptance_lib.evidence import InvalidEvidence


def make_tree(tmp_path, *, current_override=None, divergence=None, archive_override=None):
    root = tmp_path
    (root / "evals/history/20261021").mkdir(parents=True)
    (root / "docs/execution-plan").mkdir(parents=True)
    pins = {}
    for name in ("evals/a.jsonl", "evals/b.json"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"v": 1}\n')
        archived = root / "evals/history/20261021" / Path(name).name
        archived.write_text('{"v": 1}\n')
        if archive_override and archive_override.get(name):
            archived.write_text(archive_override[name])
        pins[name] = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    (root / "docs/execution-plan/baseline-20261021.json").write_text(json.dumps({"sha256": pins}))
    if current_override:
        for name, text in current_override.items():
            (root / name).write_text(text)
    (root / "docs/execution-plan/baseline-divergence-20261023.json").write_text(
        json.dumps(divergence if divergence is not None else {
            "schema": 1, "kind": "baseline-authorized-divergence", "files": {}}))
    return root, pins


def registered_divergence(root, pins, name, reason="authorized removal"):
    import hashlib
    row = {"original_sha256": pins[name],
           "current_sha256": hashlib.sha256((root / name).read_bytes()).hexdigest(),
           "reason": reason}
    path = root / "docs/execution-plan/baseline-divergence-20261023.json"
    record = json.loads(path.read_text())
    record["files"][name] = row
    path.write_text(json.dumps(record))
    return row


def test_pinned_inputs_pass_without_any_divergence(tmp_path):
    root, pins = make_tree(tmp_path)
    result = history(root)
    assert result["verified"] == pins and result["authorized_divergence"] == {}


def test_registered_divergence_passes_and_reports_reason(tmp_path):
    root, pins = make_tree(tmp_path, current_override={"evals/a.jsonl": '{"v": 2}\n'})
    with pytest.raises(InvalidEvidence):
        history(root)
    registered_divergence(root, pins, "evals/a.jsonl")
    result = history(root)
    assert "evals/a.jsonl" in result["authorized_divergence"]
    assert result["verified"] == {"evals/b.json": pins["evals/b.json"]}


def test_unregistered_divergence_and_forged_original_are_rejected(tmp_path):
    root, pins = make_tree(tmp_path, current_override={"evals/a.jsonl": '{"v": 2}\n'})
    with pytest.raises(InvalidEvidence):
        history(root)  # divergence exists on disk but is not registered
    row = registered_divergence(root, pins, "evals/a.jsonl")
    row["original_sha256"] = "0" * 64  # forged original: must equal the pin
    path = root / "docs/execution-plan/baseline-divergence-20261023.json"
    record = json.loads(path.read_text())
    record["files"]["evals/a.jsonl"] = row
    path.write_text(json.dumps(record))
    with pytest.raises(InvalidEvidence):
        history(root)


def test_archive_bytes_must_forever_match_the_original_pin(tmp_path):
    root, pins = make_tree(tmp_path, current_override={"evals/a.jsonl": '{"v": 2}\n'},
                           archive_override={"evals/a.jsonl": '{"v": 9}\n'})
    registered_divergence(root, pins, "evals/a.jsonl")
    with pytest.raises(InvalidEvidence):
        history(root)  # current input may diverge; the archive may not


def test_divergence_record_cannot_cover_unknown_inputs(tmp_path):
    root, pins = make_tree(tmp_path)
    registered_divergence(root, pins, "evals/b.json")
    path = root / "docs/execution-plan/baseline-divergence-20261023.json"
    record = json.loads(path.read_text())
    record["files"]["evals/ghost.json"] = record["files"]["evals/b.json"]
    path.write_text(json.dumps(record))
    with pytest.raises(InvalidEvidence):
        history(root)
