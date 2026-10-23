"""Replay unrounded vector phases and their actual interleaved worker receipts."""
from __future__ import annotations

from pathlib import Path

from .audit import check_rss, finite, hashed, load
from .output import json_value
from .protocol import ARMS, WARMUPS, quantile, sha

PHASES = ("open", "cold_load", "hot_cache", "cosine", "zscore", "aggregate", "admission_sort", "details")


def validate(case_dir: Path, case: dict, run: dict, project: Path):
    runs, reports, pids = run["phase_runs"], {}, []
    order = load(case_dir, case["phase_order"])
    if not isinstance(order, list) or len(order) != 2 * (runs + WARMUPS):
        raise ValueError("every phase warmup/measurement must have an interleaved handshake")
    last_end = 0
    for index in range(runs + WARMUPS):
        expected = ARMS if index % 2 == 0 else tuple(reversed(ARMS))
        for row, arm in zip(order[index * 2:index * 2 + 2], expected):
            if (set(row) != {"sample", "arm", "query_index", "started_s", "finished_s"} or
                    row["sample"] != index or row["query_index"] != index or row["arm"] != arm or
                    not finite(row["started_s"]) or not finite(row["finished_s"], positive=True) or
                    row["started_s"] < last_end or row["finished_s"] < row["started_s"]):
                raise ValueError("phase workers were not alternated for identical queries")
            last_end = row["finished_s"]
    for arm in ARMS:
        report_path = hashed(case_dir, case["phase_reports"][arm])
        result = json_value(report_path.read_bytes())
        artifacts = case["phase_artifacts"][arm]
        receipt = load(case_dir, artifacts["receipt"])
        stdout, stderr = (hashed(case_dir, artifacts[name]) for name in ("stdout", "stderr"))
        fields = {"argv", "exit_code", "elapsed_s", "stdout_sha256", "result_sha256", "interleaved_samples", "process_id"}
        expected_argv = [run["python"]["path"], str(project / "scripts/bench_scale.py"), "--worker", "measure", "--stream",
            "--source-root", run["source_roots"][arm], "--db", str(case_dir / arm / "fixture.sqlite"),
            "--out", str(report_path), "--runs", str(runs)]
        if (set(receipt) != fields or receipt["argv"] != expected_argv or receipt["exit_code"] != 0 or
                not finite(receipt["elapsed_s"], positive=True) or receipt["interleaved_samples"] != runs + WARMUPS or
                receipt["stdout_sha256"] != sha(stdout) or receipt["result_sha256"] != sha(report_path) or
                type(receipt["process_id"]) is not int or receipt["process_id"] <= 0):
            raise ValueError("phase process receipt/source/output is incomplete or changed")
        lines = [json_value(line) for line in stdout.read_bytes().splitlines()]
        if lines != [{"sample": i, "completed": True} for i in range(runs + WARMUPS)] or stderr.read_bytes():
            raise ValueError("phase raw stdout/stderr does not prove every completed measurement")
        if (result.get("schema") != 1 or result.get("runs") != runs or result.get("warmups") != WARMUPS or
                not finite(result.get("imports_ms"), positive=True) or
                result.get("model_process_rss") != {"status": "not_used", "bytes": 0}):
            raise ValueError("phase report lacks independent imports/count/model-residency evidence")
        check_rss(result["query_process_peak_rss"], unit="bytes" if run["environment"]["os"] == "Darwin" else "KiB")
        values = result.get("phases_ms", {})
        if set(values) != set(PHASES):
            raise ValueError("all eight scale phases are required")
        for name, series in values.items():
            samples = series.get("samples")
            if (not isinstance(samples, list) or len(samples) != runs or any(not finite(v) for v in samples) or
                    series.get("p50") != quantile(samples, .5) or series.get("p95") != quantile(samples, .95)):
                raise ValueError(f"phase {name} summary differs from actual raw samples")
        predictions = result.get("top_files_and_z")
        if not isinstance(predictions, list) or len(predictions) != runs:
            raise ValueError("unrounded vector predictions missing")
        for ranking in predictions:
            if not isinstance(ranking, list) or len(ranking) > 50:
                raise ValueError("invalid unrounded vector ranking")
            for entry in ranking:
                if (not isinstance(entry, list) or len(entry) != 2 or type(entry[0]) is not int or
                        entry[0] <= 0 or not finite(entry[1]) or entry[1] < 3):
                    raise ValueError("invalid unrounded admitted file/score")
            if len({pair[0] for pair in ranking}) != len(ranking) or ranking != sorted(ranking, key=lambda p: (-p[1], p[0])):
                raise ValueError("unrounded vector ranking lacks stable file-ID ordering")
        reports[arm] = result
        pids.append(receipt["process_id"])
    if pids[0] == pids[1]:
        raise ValueError("reference/candidate phase RSS cannot share one process")
    a, b = (reports[arm]["top_files_and_z"] for arm in ARMS)
    same = all(len(x) == len(y) and all(left[0] == right[0] and abs(left[1] - right[1]) <= 1e-5
        for left, right in zip(x, y)) for x, y in zip(a, b))
    return reports, same, pids
