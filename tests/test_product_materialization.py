"""Screenshot identity must survive transport and cannot be silently forged."""
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from answer_eval.protocol import EvaluationError
from product_eval.bundle import build_corpus, load_bundle
from product_eval import materialization as native
from product_eval.runner import check_build


def test_real_seed_build_rejects_extra_oracle_and_old_byte_only_receipt(tmp_path):
    bundle = load_bundle(ROOT / "evals/product_v4", require_complete=False)
    build = tmp_path / "build"
    build_corpus(bundle, build, allow_draft=True)
    assert check_build(bundle, build) == build / "corpus"
    injected = build / "corpus/expected-answers.txt"
    injected.write_text("not a registered source")
    with pytest.raises(EvaluationError, match="unregistered"):
        check_build(bundle, build)
    injected.unlink()
    receipt = json.loads((build / "build.json").read_text())
    receipt["schema"] = 1
    receipt.pop("materialization")
    (build / "build.json").write_text(json.dumps(receipt))
    with pytest.raises(EvaluationError, match="rebuild inputs") as error:
        check_build(bundle, build)
    assert error.value.status == "MISSING"


def test_capture_materialization_only_uses_declared_kind_and_exact_native_value(tmp_path):
    shot, photo = tmp_path / "capture.png", tmp_path / "photograph.png"
    shot.write_bytes(b"unmodified capture bytes")
    photo.write_bytes(b"unmodified photograph bytes")
    sources = {"capture.png": {"kind": "screenshot"}, "photograph.png": {"kind": "image"}}
    expected = native.expected_flags(sources)
    assert expected == {"capture.png": native.CAPTURE_VALUE.hex(), "photograph.png": None}
    if sys.platform != "darwin":
        with pytest.raises(EvaluationError, match="requires macOS"):
            native.materialize(shot, sources["capture.png"])
        return
    assert native.materialize(shot, sources["capture.png"]) == expected["capture.png"]
    assert native.materialize(photo, sources["photograph.png"]) is None
    assert shot.read_bytes() == b"unmodified capture bytes"
    receipt = {"policy": native.POLICY, "expected": expected, "observed": dict(expected)}
    native.validate(tmp_path, sources, receipt)
    # A stripped flag cannot be hidden by the still-valid asset SHA or receipt.
    shot.unlink()
    shot.write_bytes(b"unmodified capture bytes")
    with pytest.raises(EvaluationError, match="lost/changed"):
        native.validate(tmp_path, sources, receipt)
    native.materialize(shot, sources["capture.png"])
    # Nor can an ordinary photo acquire a screenshot hint without detection.
    native.materialize(photo, {"kind": "screenshot"})
    with pytest.raises(EvaluationError, match="lost/changed"):
        native.validate(tmp_path, sources, receipt)


def test_screenshot_unavailable_platform_cannot_be_claimed_materialized(tmp_path, monkeypatch):
    shot = tmp_path / "capture.png"
    shot.write_bytes(b"source bytes")
    sources = {shot.name: {"kind": "screenshot"}}
    expected = native.expected_flags(sources)
    monkeypatch.setattr(native.sys, "platform", "linux")
    with pytest.raises(EvaluationError, match="requires macOS") as error:
        native.validate(tmp_path, sources, {"policy": native.POLICY, "expected": expected, "observed": expected})
    assert error.value.status == "MISSING"


def test_rehashed_marker_claim_cannot_change_the_source_type(tmp_path):
    sources = {"photo.png": {"kind": "image"}}
    forged = {"photo.png": native.CAPTURE_VALUE.hex()}
    with pytest.raises(EvaluationError, match="frozen source kinds"):
        native.validate(tmp_path, sources, {"policy": native.POLICY, "expected": forged, "observed": forged})


@pytest.mark.parametrize("node_kind", ["fifo", "dangling_link", "directory_link", "empty_directory"])
def test_ignored_filesystem_nodes_cannot_enter_the_ingest_universe(tmp_path, node_kind):
    bundle = load_bundle(ROOT / "evals/product_v4", require_complete=False)
    build = tmp_path / "build"
    build_corpus(bundle, build, allow_draft=True)
    extra = build / "corpus/unregistered-answer-names"
    if node_kind == "fifo":
        os.mkfifo(extra)
    elif node_kind == "dangling_link":
        extra.symlink_to(tmp_path / "missing")
    elif node_kind == "directory_link":
        extra.symlink_to(tmp_path, target_is_directory=True)
    else:
        extra.mkdir()
    with pytest.raises(EvaluationError, match="unregistered.*source nodes"):
        check_build(bundle, build)
