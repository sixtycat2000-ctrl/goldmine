"""Capacity fixtures stay deterministic and measurements run in another process."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from scale_bench_fixtures import scale_capture

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


def test_actual_original_and_current_cli_read_equivalent_native_schemas(scale_capture):
    from scale_bench.validation import validate
    from scale_bench_fixtures import mutate

    manifest = json.loads((scale_capture / "manifest.json").read_text())
    case_dir = scale_capture / "121-10"
    reference = json.loads((case_dir / "reference/fixture.json").read_text())
    candidate = json.loads((case_dir / "candidate/fixture.json").read_text())
    from goldmine.migrations import TARGET
    assert reference["schema_version"] == 4 and candidate["schema_version"] == TARGET
    assert reference["logical_sha256"] == candidate["logical_sha256"]
    assert reference["counts"] == {"files": 13, "segments": 121, "fts": 121, "vectors": 121}
    with mutate(scale_capture) as capture:
        result = capture.replay()
        assert result["status"] == "PASS" and result["consistent"]
        cli = result["cases"][0]["cli"]
        assert not cli["failures"] and len(cli["metrics"]) == 12
        assert all(bucket["attempts"] == 30 and bucket["process_peak_rss_bytes"] > 0
                   for bucket in cli["metrics"].values())
    assert manifest["status"] == "MISSING" and manifest["quality_claim"] is False


def test_sqlite_read_marks_are_distinguished_from_content_writes():
    from copy import deepcopy
    from scale_bench.protocol import unchanged

    before = {"sqlite": {"bytes": 100, "sha256": "a" * 64}, "wal": None, "shm": None, "journal": None}
    after = deepcopy(before)
    after["shm"] = {"bytes": 32768, "sha256": "b" * 64}
    assert unchanged(before, after)
    after["wal"] = {"bytes": 32, "sha256": "c" * 64}
    assert not unchanged(before, after)
    after = deepcopy(before)
    after["sqlite"]["sha256"] = "d" * 64
    assert not unchanged(before, after)


def test_cleanup_rejects_cache_parent_before_deleting_anything(tmp_path):
    from scale_bench.suite import cleanup

    databases = {arm: tmp_path / arm / "fixture.sqlite" for arm in ("reference", "candidate")}
    for db in databases.values():
        db.parent.mkdir()
        db.write_bytes(b"owned fixture")
    retained = tmp_path / "case.json"
    retained.write_bytes(b"raw receipt")
    with pytest.raises(ValueError, match="exact owned"):
        cleanup(tmp_path, databases, {"cache_root": str(tmp_path)})
    assert all(db.exists() for db in databases.values())
    cache = databases["candidate"].with_name("fixture.sqlite.vectors")
    cache.mkdir()
    (cache / "generation.npy").write_bytes(b"derived")
    cleanup(tmp_path, databases, {"cache_root": str(cache)})
    assert not cache.exists() and not any(db.exists() for db in databases.values())
    assert retained.read_bytes() == b"raw receipt"


def test_explicit_cache_case_keeps_audits_after_owned_cleanup(tmp_path, scale_capture):
    from types import SimpleNamespace
    from scale_bench import fixture_audit, phases, cli_replay
    from scale_bench.protocol import source_identity
    from scale_bench.suite import one_case
    from scale_bench.validation import _cache

    run = json.loads((scale_capture / "manifest.json").read_text())
    sources = {arm: Path(run["source_roots"][arm]) for arm in ("reference", "candidate")}
    sources.update({arm + "_identity": source_identity(source) for arm, source in list(sources.items())})
    args = SimpleNamespace(out=tmp_path, cache_experiment=True, keep_fixtures=False,
                           runs=10, cli_runs=0, build_timeout=60, sample_timeout=20)
    # Just the3 warmups exercise the optional build/load/cleanup mechanics. They
    # are explicitly insufficient for a complete CLI acceptance denominator.
    case = one_case(args, ROOT, 41, 10, sources)
    directory = tmp_path / "41-10"
    run["cache_experiment"] = True
    fixture_audit.validate(directory, case, run, ROOT)
    reports, same, _ = phases.validate(directory, case, run, ROOT)
    thresholds = json.loads((ROOT / "evals/acceptance_targets.json").read_text())["scale"]
    assert _cache(directory, case, run, reports, thresholds)["triggered"] is False
    assert same and (directory / "candidate/cache-manifest.json").is_file()
    assert not list(directory.rglob("fixture.sqlite"))
    assert not (directory / "candidate/fixture.sqlite.vectors").exists()
    assert (directory / "case.json").is_file() and (directory / "samples.json").is_file()
    with pytest.raises(ValueError, match="did not actually complete"):
        cli_replay.validate(directory, case, run, ROOT)
