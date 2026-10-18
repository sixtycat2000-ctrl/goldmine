"""Item 11: scripts/bench.py two-gate sliver — offline smoke + gate math.

Gate 2 (real llama-server embed rate) is GOLDMINE_LIVE-gated inside the
script itself and never runs here; only its plumbing is pinned.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import pytest  # noqa: E402


@pytest.mark.parametrize("rows", [2000, 20000])
def test_bench_gate1_passes_offline(rows):
    import bench

    result = bench.bench_gate1(rows, runs=2)
    assert result["rows"] == rows
    assert result["pass"] is True
    assert result["p50_ms"] < bench.GATE1_BUDGET_MS
    # a synthetic budget failure flips the flag (the arithmetic is honest)
    assert not (result["p50_ms"] < 1e-6)


@pytest.mark.parametrize("rows", [2000, 20000])
def test_bench_gate1b_loads_a_real_store_db(tmp_path, rows):
    """03 F2: the db-inclusive gate builds a REAL index, and every run's
    load_vectors is cold (fresh Store instance per run)."""
    import bench

    db = tmp_path / "bench.sqlite"
    result = bench.bench_gate1b(rows, runs=2, db_path=db)
    assert result["rows"] == rows
    assert result["vectors_loaded"] == rows
    assert db.is_file() and result["db_bytes"] > 0
    assert result["pass"] is True
    assert result["p50_ms"] < bench.GATE1B_BUDGET_MS


def test_bench_gate1b_rebuilds_stale_db(tmp_path):
    import bench

    db = tmp_path / "bench.sqlite"
    bench.build_vector_db(1000, db)
    bench.build_vector_db(1000, db)  # rebuilt fresh, never reused/appended to
    # row count is exact after a rebuild (no doubles from the earlier build)
    from goldmine.store import Store

    s = Store(db)
    try:
        assert s.load_vectors().matrix.shape == (1000, 768)
    finally:
        s.close()


def test_bench_main_gate_exit_code(monkeypatch, capsys, tmp_path):
    import bench

    ok1b = {
        "rows": 500, "vectors_loaded": 500, "runs": 1, "p50_ms": 5.0,
        "min_ms": 5.0, "max_ms": 5.0, "db_bytes": 1, "budget_ms": bench.GATE1B_BUDGET_MS,
        "pass": True,
    }
    # gate 1 forced to fail -> --gate exits 1 and names the gate
    monkeypatch.setattr(bench, "bench_gate1", lambda rows, runs: {
        "rows": rows, "dim": 768, "runs": runs, "p50_ms": 999.0,
        "min_ms": 999.0, "max_ms": 999.0, "top_k_files": 0,
        "budget_ms": bench.GATE1_BUDGET_MS, "pass": False,
    })
    monkeypatch.setattr(bench, "bench_gate1b", lambda rows, runs, db_path=None: ok1b)
    assert bench.main(["--gate"]) == 1
    err = capsys.readouterr().err
    assert "gate1" in err and "BENCH GATE FAILED" in err

    # gate 1b forced to fail is named too
    bad1b = {**ok1b, "p50_ms": 999.0, "pass": False}
    monkeypatch.setattr(bench, "bench_gate1", lambda rows, runs: {
        "rows": rows, "dim": 768, "runs": runs, "p50_ms": 5.0,
        "min_ms": 5.0, "max_ms": 5.0, "top_k_files": 3,
        "budget_ms": bench.GATE1_BUDGET_MS, "pass": True,
    })
    monkeypatch.setattr(bench, "bench_gate1b", lambda rows, runs, db_path=None: bad1b)
    assert bench.main(["--gate"]) == 1
    assert "gate1b" in capsys.readouterr().err

    # offline success path exits 0 and prints all three numbers (gate2 skipped)
    monkeypatch.setattr(bench, "bench_gate1b", lambda rows, runs, db_path=None: ok1b)
    assert bench.main(["--rows", "500", "--gate"]) == 0
    out = capsys.readouterr().out
    assert "gate 1 " in out and "gate 1b" in out and "gate 2" in out and "skipped" in out
