"""ERC-01: a successful prefix never substitutes for every attempted phase."""
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

from answer_eval.protocol import EvaluationError
from search_comparison.engine_capture import resources
from search_comparison.engine_capture.commands import Commands


def rows():
    refs = [{"path": f"/owned/prepare/{i:04d}/manifest.json", "sha256": str(i) * 64} for i in (1, 2)]
    dispositions = [{"command": r, "expected_manifest": r["path"], "phase": "prepare", "label": "synthetic-" + str(i),
                     "disposition": "validated", "observation": {"sampled_resource_status": "PASS", "status": "MISSING", "full_tree_ceiling_status": "MISSING"}}
                    for i, r in enumerate(refs, 1)]
    return refs, dispositions


@pytest.mark.parametrize("change", ["missing", "reordered", "duplicate", "raw_missing", "raw_duplicate", "count_shrink", "wrong_path", "no_disposition", "validation_error"])
def test_complete_same_order_unique_disposition_registry_required(change):
    refs, dispositions = rows()
    assert resources.status(dispositions, refs, attempted=2) == "PASS"
    count = 2
    if change == "missing": dispositions.pop()
    if change == "reordered": dispositions.reverse()
    if change == "duplicate": dispositions[1] = dispositions[0]
    if change == "raw_missing": refs.pop()
    if change == "raw_duplicate": refs[1] = refs[0]
    if change == "count_shrink": count = 1
    if change == "wrong_path": dispositions[1]["expected_manifest"] = "/different/manifest.json"
    if change == "no_disposition": dispositions[1].pop("disposition")
    if change == "validation_error": dispositions[1] = {**{k: v for k, v in dispositions[1].items() if k != "observation"}, "disposition": "resource_validation_error", "error": "actual strict validation failed"}
    assert resources.status(dispositions, refs, attempted=count) == "MISSING"


def test_known_measured_fail_dominates_later_missing_record():
    refs, dispositions = rows()
    dispositions[0]["observation"]["sampled_resource_status"] = "FAIL"
    assert resources.status(dispositions[:1], refs, attempted=2) == "FAIL"
    assert resources.status(dispositions, refs[:1], attempted=2) == "FAIL"


@pytest.mark.native
def test_actual_previous_pass_next_invalid_publication_is_missing_and_sticky(tmp_path, monkeypatch):
    owner = tmp_path.resolve()
    (owner / "cwd").mkdir()
    value = {"registered_at": datetime.now(timezone.utc).isoformat(), "scope": "DECLARED_SYNTHETIC_ERC01_ONLY", "quality_evidence": False}
    # This test needs an actually complete positive prefix before corrupting
    # the second publication. Finish the controlled child only after one live
    # target sample completes, avoiding an accidental exit during ps/lsof.
    # Resource counters/validation stay real; an incomplete sample still fails.
    from acceptance_lib.resource_observer import Observer
    from acceptance_lib.owned_process import _wait_unreaped
    release = owner / "release"
    release.mkdir()
    original_sample = Observer.sample
    def sample_then_release(observed):
        original_sample(observed)
        flag = release / observed.run_dir.name
        if observed.target_exec_seen and observed.samples >= 2 and not observed.gaps and not flag.exists():
            flag.write_text("completed live target sample\n")
            _wait_unreaped(observed.proc, .5)  # Keep the exact owned PID unreaped.
    monkeypatch.setattr(Observer, "sample", sample_then_release)
    def command_for(number):
        body = ("import time;from pathlib import Path;flag=Path(" + repr(str(release / number)) + ");"
                "print('synthetic actual child',flush=True);end=time.monotonic()+3\n"
                "while not flag.exists() and time.monotonic()<end:time.sleep(.01)\n"
                "assert flag.exists(),'controlled sample handshake missing'\n")
        return [str(Path(sys.executable).resolve()), "-I", "-S", "-B", "-c", body]
    calls = Commands(owner, {"PATH": "/usr/bin:/bin"}, value)
    assert calls.call("first", command_for("0001")) == b"synthetic actual child\n"
    assert calls.resources[0]["observation"]["sampled_resource_status"] == "PASS"
    original = resources.validate
    def corrupt_publication(path, expected, argv, identity):
        publication = path.parent / "resource-publication.json"
        publication.write_bytes(publication.read_bytes() + b"\n")
        return original(path, expected, argv, identity)
    monkeypatch.setattr(resources, "validate", corrupt_publication)
    with pytest.raises(EvaluationError, match="resource validation failed"):
        calls.call("second", command_for("0002"))
    registry = calls.finish()
    assert len(registry) == len(calls.resources) == 2
    assert calls.resources[1]["command"] == registry[1]
    assert calls.resources[1]["disposition"] == "resource_validation_error"
    assert "observation" not in calls.resources[1]
    assert calls.resource_status() == "MISSING" and calls.blocked
    assert resources.status(calls.resources[:1], registry, attempted=2) == "MISSING"
    assert resources.status(list(reversed(calls.resources)), registry, attempted=2) == "MISSING"
    with pytest.raises(EvaluationError, match="no subsequent child"):
        calls.call("third-must-not-run", command_for("0003"))
    assert len(calls.finish()) == 2 and not (owner / "prepare/0003").exists()
    # Deleting a failed ref/phase from the published registry cannot hide the
    # retained actual raw manifest on disk, even when aggregate output is edited.
    calls.rows.pop()
    with pytest.raises(EvaluationError, match="manifests omitted"):
        calls.finish()


def test_capture_without_raw_manifest_is_explicit_not_a_placeholder_hash(tmp_path, monkeypatch):
    owner = tmp_path.resolve()
    (owner / "cwd").mkdir()
    value = {"registered_at": datetime.now(timezone.utc).isoformat(), "scope": "declared capture-failure fixture"}
    calls = Commands(owner, {"PATH": "/usr/bin:/bin"}, value, fixture=True)
    def failed(*args, **kwargs):
        raise OSError("declared fixture write failure before manifest publication")
    monkeypatch.setattr("search_comparison.engine_capture.commands.capture_command", failed)
    with pytest.raises(EvaluationError):
        calls.call("no-manifest", ["/owned/not-executed"])
    assert calls.finish() == []
    assert len(calls.resources) == 1 and calls.resources[0]["command"] is None
    assert calls.resources[0]["disposition"] == "resource_validation_error" and calls.blocked
    assert calls.resource_status() == "MISSING"
