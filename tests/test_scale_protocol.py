"""Adversarial child lifecycle, complete-line deadlines and source attribution."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from scale_bench.execute import Warm
from scale_bench.output import human_pack
from scale_bench.pipe_protocol import MAX_LINE_BYTES
from scale_bench.validation import _cache, cache_gate
from scale_bench_fixtures import scale_human_capture


def failed_worker(tmp_path, code, *, timeout=.12):
    worker = Warm.__new__(Warm)
    began = time.monotonic()
    with pytest.raises((TimeoutError, ValueError)):
        worker.__init__([sys.executable, "-c", code], tmp_path / "worker", cwd=tmp_path,
                        env=dict(os.environ), timeout=timeout)
    assert time.monotonic() - began < 1
    assert worker.proc.returncode is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(worker.proc.pid, os.WNOHANG)
    assert all(stream.closed for stream in (worker.proc.stdin, worker.proc.stdout, worker.log, worker.raw))
    receipt = json.loads((tmp_path / "worker/process.json").read_text())
    assert receipt["reaped"] is True and receipt["failure"]["phase"] == "startup"
    original = (tmp_path / "worker/process.json").read_bytes()
    worker.close()
    assert (tmp_path / "worker/process.json").read_bytes() == original
    return receipt


def test_warm_startup_timeout_reaps_owned_child_and_records_failure(tmp_path):
    receipt = failed_worker(tmp_path, "import time;time.sleep(2)")
    assert receipt["timed_out"] is True and receipt["failure"]["type"] == "TimeoutError"


def test_partial_startup_line_cannot_extend_deadline(tmp_path):
    receipt = failed_worker(tmp_path, "import os,time;os.write(1,b'{');time.sleep(2);os.write(1,b'}\\n')")
    assert receipt["timed_out"] is True
    assert (tmp_path / "worker/worker.stdout").read_bytes() == b"{"


def test_invalid_or_oversized_startup_line_also_reaps_child(tmp_path):
    invalid = tmp_path / "invalid"
    invalid.mkdir()
    receipt = failed_worker(invalid, "import os,time;os.write(1,b'invalid\\n');time.sleep(2)")
    assert receipt["timed_out"] is False
    oversized = tmp_path / "oversized"
    oversized.mkdir()
    receipt = failed_worker(oversized, f"import os,time;os.write(1,b'x'*{MAX_LINE_BYTES + 100});time.sleep(2)", timeout=.5)
    assert receipt["failure"]["type"] == "ValueError"
    assert (oversized / "worker/worker.stdout").stat().st_size == MAX_LINE_BYTES


def test_spawn_failure_closes_streams_and_has_explicit_receipt(tmp_path):
    worker = Warm.__new__(Warm)
    with pytest.raises(FileNotFoundError):
        worker.__init__([str(tmp_path / "absent-program")], tmp_path / "worker", cwd=tmp_path,
                        env=dict(os.environ), timeout=.1)
    assert worker.proc is None and worker.log.closed and worker.raw.closed
    receipt = json.loads((tmp_path / "worker/process.json").read_text())
    assert receipt["process_id"] is None and receipt["exit_code"] == 127
    assert receipt["failure"]["phase"] == "spawn" and receipt["failure"]["type"] == "FileNotFoundError"


def test_partial_request_response_is_deadline_bound_and_failure_is_retained(tmp_path):
    code = ("import os,sys,json,time;print(json.dumps({'kind':'ready','process_id':os.getpid()}),flush=True);"
            "sys.stdin.readline();os.write(1,b'{');time.sleep(2)")
    worker = Warm([sys.executable, "-c", code], tmp_path / "worker", cwd=tmp_path,
                  env=dict(os.environ), timeout=1)
    worker.timeout = .08
    began = time.monotonic()
    with pytest.raises(TimeoutError):
        worker.call(["search", "fixture"], tmp_path / "request")
    assert time.monotonic() - began < 1
    assert worker.proc.returncode is not None and worker.raw.closed
    receipt = json.loads((tmp_path / "worker/process.json").read_text())
    assert receipt["timed_out"] is True and receipt["failure"]["phase"] == "request" and receipt["reaped"] is True
    assert (tmp_path / "worker/worker.stdout").read_bytes().endswith(b"{")


def test_actual_human_pack_has_exact_source_body_location_order_and_citations(scale_human_capture):
    raw, pack, fixture, n_hits = scale_human_capture
    human_pack(raw, pack, fixture=fixture, n_hits=n_hits)
    corrupt = [raw.replace(b"goldmine-scale", b"counterfeit-xx")]
    footer = raw.index(b"# sources\n")
    corrupt.append(raw[:footer] + raw[footer:].replace(b"goldmine-scale", b"counterfeit-xx", 1))
    lines = raw.splitlines(keepends=True)
    head = next(i for i, line in enumerate(lines) if line.startswith(b"[1] "))
    changed = list(lines)
    changed[head] = re.sub(rb" L([0-9]+) ", lambda m: b" L" +
        (b"2" if m[1][:1] != b"2" else b"3") + m[1][1:] + b" ", changed[head], count=1)
    corrupt.append(b"".join(changed))
    start2 = next(i for i, line in enumerate(lines) if line.startswith(b"[2] "))
    corrupt.append(b"".join(lines[:head] + lines[start2:footer_line(lines)] + lines[head:start2] + lines[footer_line(lines):]))
    if fixture["schema_version"] == 6:
        match = re.search(rb"gm1:[0-9a-f-]+:[0-9]+:([0-9a-f]{16})", raw)
        assert match is not None
        position = match.end() - 1
        corrupt.append(raw[:position] + (b"0" if raw[position:position + 1] != b"0" else b"1") + raw[position + 1:])
        indices = [i for i, line in enumerate(lines) if b" citation: gm1:" in line]
        changed = list(lines)
        changed[indices[0]], changed[indices[1]] = changed[indices[1]], changed[indices[0]]
        corrupt.append(b"".join(changed))
    for value in corrupt:
        assert len(value) == len(raw)
        with pytest.raises(ValueError, match="human pack"):
            human_pack(value, pack, fixture=fixture, n_hits=n_hits)


def footer_line(lines):
    return next(i for i, line in enumerate(lines) if line == b"# sources\n")


def test_human_score_rounding_does_not_invent_four_decimal_precision():
    from scale_bench.human import _score_matches

    assert _score_matches("1.23", 1.235) and _score_matches("1.24", 1.235)
    assert not _score_matches("1.22", 1.235)


def test_trigger_without_cache_measurement_is_missing_and_not_a_benefit_pass(tmp_path):
    thresholds = json.loads((ROOT / "evals/acceptance_targets.json").read_text())["scale"]
    reports = {arm: {"phases_ms": {"cold_load": {"p50": 1000}},
                     "query_process_peak_rss": {"bytes": 2000000000}} for arm in ("reference", "candidate")}
    case = {"rows": 500000, "cache_build": None, "cache_receipt": None}
    result = _cache(tmp_path, case, {"cache_experiment": False}, reports, thresholds)
    assert result["status"] == "not_evaluated" and result["measured"] is False and result["triggered"] is True
    assert result["meets_required_benefit"] is None
    assert cache_gate([{"cache": result}]) == {"status": "MISSING", "passed": None}
    case["rows"] = 10000
    result = _cache(tmp_path, case, {"cache_experiment": False}, reports, thresholds)
    assert cache_gate([{"cache": result}]) == {"status": "not_required", "passed": None}
    measured = {"triggered": True, "measured": True, "meets_required_benefit": True}
    assert cache_gate([{"cache": measured}]) == {"status": "PASS", "passed": True}
    failed = {**measured, "meets_required_benefit": False}
    assert cache_gate([{"cache": measured}, {"cache": failed}]) == {"status": "FAIL", "passed": False}
