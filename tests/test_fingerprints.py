"""Tier-1: fingerprints — canonical identity, diff fields, build-string exclusion."""

from __future__ import annotations

import json

from goldmine import fingerprints
from goldmine.backends.mock import MockBackend


def test_id_stable_across_key_order():
    fp = {"backend": "mock", "model_id": "m", "dim": 768}
    shuffled = {"dim": 768, "model_id": "m", "backend": "mock"}
    assert fingerprints.fingerprint_id(fp) == fingerprints.fingerprint_id(shuffled)


def test_canonical_is_sorted_no_whitespace():
    blob = fingerprints.canonical({"b": 1, "a": 2})
    assert blob == b'{"a":2,"b":1}'


def test_build_covers_required_fields():
    backend = MockBackend(dim=768)
    fp = fingerprints.build(backend)
    for key in ("backend", "model_id", "model_sha256", "dim", "prompts",
                "pooling", "normalization", "preprocessing"):
        assert key in fp
    assert fp["backend"] == "mock"
    assert fp["dim"] == 768
    assert "task: search result | query:" in fp["prompts"]
    assert "title:" in fp["prompts"]


def test_build_string_is_not_part_of_fingerprint():
    """D2 refinement: the llama.cpp build string is meta provenance, never identity."""
    backend = MockBackend(dim=768)
    fp = fingerprints.build(backend)
    assert "llama_build" not in fp
    # even if a stored dict carried one, it must not affect the live identity
    with_build = dict(fp, llama_build="b11146")
    assert fingerprints.fingerprint_id(fp) == fingerprints.fingerprint_id(
        {k: v for k, v in with_build.items() if k != "llama_build"}
    )


def test_check_ok_when_ids_match():
    backend = MockBackend(dim=768)
    fp = fingerprints.build(backend)
    report = fingerprints.check(
        fingerprints.fingerprint_id(fp), fingerprints.canonical(fp).decode(), fp
    )
    assert report.ok and report.diff_fields == []


def test_check_names_diff_fields():
    stored = {"backend": "mock", "model_id": "mock-v1", "dim": 256,
              "prompts": "p", "pooling": "x", "normalization": "l2",
              "preprocessing": "pipeline.v1", "model_sha256": "n/a"}
    live = dict(stored, dim=768)
    report = fingerprints.check(
        fingerprints.fingerprint_id(stored),
        json.dumps(stored),
        live,
    )
    assert not report.ok
    assert report.diff_fields == ["dim"]
    assert "reindex" in report.remediation.lower()
    assert "256" in report.message and "768" in report.message


def test_check_backend_family_change_detected():
    stored = {"backend": "llama", "model_id": "x", "dim": 768, "prompts": "p",
              "pooling": "y", "normalization": "l2", "preprocessing": "v",
              "model_sha256": "h"}
    live = dict(stored, backend="mock")
    report = fingerprints.check("deadbeef", json.dumps(stored), live)
    assert report.diff_fields == ["backend"]


def test_check_handles_unparsable_stored_json():
    live = fingerprints.build(MockBackend())
    report = fingerprints.check("whatever", "{not json", live)
    assert not report.ok
    assert report.diff_fields


def test_dim_configurable_mock_changes_fingerprint():
    fp768 = fingerprints.build(MockBackend(dim=768))
    fp256 = fingerprints.build(MockBackend(dim=256))
    assert fp768["dim"] == 768 and fp256["dim"] == 256
    assert fingerprints.fingerprint_id(fp768) != fingerprints.fingerprint_id(fp256)
