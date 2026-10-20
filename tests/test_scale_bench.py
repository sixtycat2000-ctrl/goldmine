"""Capacity fixtures stay deterministic and measurements run in another process."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("scale_bench", ROOT / "scripts/bench_scale.py")
scale = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scale)


def test_nearest_rank_and_invalid_samples():
    assert scale.quantile(list(range(1, 31)), .95) == 29
    assert scale.quantile(list(range(1, 31)), .5) == 15
    with pytest.raises(ValueError):
        scale.quantile([float("nan")], .5)


def test_chunked_fixture_has_same_logical_identity_in_two_locations(tmp_path):
    a = scale.build_fixture(tmp_path / "a.sqlite", 121, 10)
    b = scale.build_fixture(tmp_path / "b.sqlite", 121, 10)
    assert a["logical_sha256"] == b["logical_sha256"]
    assert a["generation_dtype"] == "float32" and a["stored_dtype"] == "<f2"
    assert a["files"] == 13 and a["rows"] == 121
    with pytest.raises(ValueError, match="existing"):
        scale.build_fixture(tmp_path / "a.sqlite", 121, 10)


def test_search_worker_is_separate_and_retains_all_timing_samples(tmp_path):
    db = tmp_path / "fixture.sqlite"
    scale.build_fixture(db, 100, 10)
    out = tmp_path / "measure.json"
    argv = [sys.executable, str(ROOT / "scripts/bench_scale.py"), "--worker", "measure",
            "--source-root", str(ROOT), "--db", str(db), "--out", str(out), "--runs", "10"]
    result = subprocess.run(argv, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(out.read_text())
    assert all(len(phase["samples"]) == 10 for phase in report["phases_ms"].values())
    assert report["query_process_peak_rss"]["bytes"] > 0
    assert report["full_cli"]["status"] == "MISSING"


def test_real_workers_alternate_same_query_and_have_independent_peak_rss(tmp_path):
    from types import SimpleNamespace

    db = tmp_path / "fixture.sqlite"
    scale.build_fixture(db, 100, 10)
    arms = scale.paired_measurements(SimpleNamespace(out=tmp_path, runs=10,
        reference_root=ROOT, candidate_root=ROOT), db, "paired")
    order = json.loads((tmp_path / "paired-order.json").read_text())
    assert len(order) == 26
    for sample in range(13):
        pair = order[sample*2:sample*2+2]
        assert [r["query_index"] for r in pair] == [sample, sample]
        assert [r["arm"] for r in pair] == (["reference", "candidate"] if sample % 2 == 0 else ["candidate", "reference"])
        assert pair[0]["finished_s"] <= pair[1]["started_s"]
    receipts = [json.loads((tmp_path / f"paired-{arm}.receipt.json").read_text()) for arm in arms]
    assert receipts[0]["process_id"] != receipts[1]["process_id"]
    assert arms["reference"]["top_files_and_z"] == arms["candidate"]["top_files_and_z"]
