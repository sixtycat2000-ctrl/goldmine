"""Performance evidence counterexamples, without timing product internals."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from acceptance_lib.evidence import InvalidEvidence, artifact, capture_command, read_command_run
from search_bench.metrics import fast_metrics, mcp_metrics, percentile, verify_fast_samples


def test_nearest_rank_uses_the_observed_tail():
    assert percentile(list(range(1, 31)), .95) == 29
    assert percentile(list(range(1, 31)), .5) == 15


def _samples():
    return [{"arm": arm, "query_id": str(i), "wall_ms": 100 if arm == "warm" else 500,
             "exit_code": 0, "degraded": False, "json_valid": True}
            for i in range(30) for arm in ("warm", "cold")]


def test_fast_requires_thirty_interleaved_actual_pairs():
    samples = _samples()
    assert fast_metrics(samples)["warm_p95_ms"] == 100
    with pytest.raises(InvalidEvidence, match="at least30"):
        fast_metrics(samples[:44])
    with pytest.raises(InvalidEvidence, match="interleave"):
        fast_metrics(sorted(samples, key=lambda sample: sample["arm"]))
    samples[17]["exit_code"] = 1
    assert fast_metrics(samples)["incomplete"] == 1


def test_mcp_quantile_is_of_paired_differences_not_difference_of_quantiles():
    rows = [{"direct_ms": 100+i*100, "stdio_ms": 190+i*100 if i % 2 else 100+i*100,
             "equal": True, "via": "serve"} for i in range(30)]
    assert mcp_metrics(rows)["paired_extra_p95_ms"] == 90
    rows[0]["equal"] = False
    assert mcp_metrics(rows)["failures"] == 1


def test_fast_rejects_fabricated_sample_latency_against_actual_process(tmp_path):
    command = tmp_path / "actual"
    identity = {"index_sha256": "a"*64}
    capture_command(command, "latency.test", [sys.executable, "-c", "print('[]')", "search", "proof"],
                    cwd=tmp_path, identity=identity, suite_id="latency.sample.v1")
    _, receipt, _ = read_command_run(command / "manifest.json")
    row = {"command": artifact(tmp_path, command / "manifest.json"), "arm": "warm", "query": "proof",
           "wall_ms": receipt["wall_ms"], "exit_code": 0, "json_valid": True, "degraded": True}
    verify_fast_samples(tmp_path, [row], identity)
    row["wall_ms"] = 0.001
    with pytest.raises(InvalidEvidence, match="time/status"):
        verify_fast_samples(tmp_path, [row], identity)


def test_source_snapshot_ignores_models_and_private_build_inputs(tmp_path):
    from build_python import snapshot

    root = tmp_path / "source"
    (root / "goldmine/commands").mkdir(parents=True)
    for name in ("pyproject.toml", "README.md", "LICENSE", "goldmine/__init__.py", "goldmine/commands/new.py"):
        (root / name).write_text(name)
    (root / "models").mkdir()
    (root / "models/private.gguf").write_text("private")
    destination = tmp_path / "frozen"
    copied = snapshot(root, destination)
    assert "goldmine/commands/new.py" in copied
    assert not (destination / "models").exists()
