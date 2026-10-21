"""Alternate every same-query CLI invocation; retain every attempted sample."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import re
import sys

from .execute import Warm, command
from .fixture import readback
from .output import structured
from .protocol import (ARMS, FORMS, TEMPERATURES, WARMUPS, cli_args, db_snapshot,
                       isolated_env, query, ref, unchanged, write)


def worker_args(project: Path, action: str, source: Path) -> list[str]:
    return [sys.executable, str(project / "scripts/bench_scale.py"), "--full-cli", "--worker", action,
            "--source-root", str(source.resolve())]


def attempt(project: Path, directory: Path, db: Path, source: Path, index: int, form: str,
            temperature: str, worker: Warm, *, rows: int, per_file: int, timeout: float) -> dict:
    args = cli_args(db, index, form)
    before = db_snapshot(db)
    env = isolated_env(source, directory.parent.parent / "state")
    if temperature == "cold":
        result = command([sys.executable, "-m", "goldmine.cli", *args], directory,
                         cwd=directory.parent.parent, env=env, timeout=timeout)
    else:
        try:
            result = worker.call(args, directory)
        except (OSError, ValueError, TimeoutError) as exc:
            worker.close()
            directory.mkdir(parents=True, exist_ok=True)
            # Preserve a genuinely incomplete attempt; never substitute an empty
            # success JSON or retry until a favorable sample appears.
            write(directory / "failure.json", {"exception": type(exc).__name__, "reason": str(exc), "args": args})
            raise RuntimeError(f"warm CLI attempt incomplete: {directory}") from exc
    after = db_snapshot(db)
    error, proofs = None, []
    try:
        if result["exit_code"] != 0 or result["timed_out"]:
            raise ValueError("actual CLI process failed or timed out")
        stderr = (directory / "stderr.log").read_text()
        if not re.fullmatch(r"via: cold \([0-9]+\.[0-9]+s\)\n", stderr):
            raise ValueError("unexpected CLI diagnostics/degradation; retain the complete attempt")
        if not unchanged(before, after):
            raise ValueError("search wrote SQLite content or left a nonempty write journal")
        if form != "pack_text":
            parsed = structured((directory / "stdout.log").read_bytes(), form, rows, per_file, query(index))
            proofs = readback(db, [tuple(pair) for pair in parsed["locations"]], rows, per_file)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        error = str(exc)
    write(directory / "readback.json", proofs)
    audit = {"schema": 1, "command": ref(directory, directory / "receipt.json"),
             "database_before": before, "database_after": after,
             "readback": ref(directory, directory / "readback.json"), "error": error}
    write(directory / "audit.json", audit)
    return audit


def cli_samples(project: Path, case_dir: Path, sources: dict, databases: dict, *,
                rows: int, per_file: int, runs: int, timeout: float) -> list[dict]:
    samples, workers = [], {}
    with ExitStack() as stack:
        for arm in ARMS:
            state = case_dir / arm
            env = isolated_env(sources[arm], state / "state")
            worker = Warm(worker_args(project, "warm", sources[arm]), state / "warm-worker",
                          cwd=state, env=env, timeout=timeout)
            stack.callback(worker.close)
            if worker.ready["source"] != sources[arm + "_identity"]:
                raise ValueError("warm worker imported the wrong source tree")
            workers[arm] = worker
        for index in range(runs + WARMUPS):
            for temperature in TEMPERATURES:
                for form in FORMS:
                    order = ARMS if index % 2 == 0 else tuple(reversed(ARMS))
                    for arm in order:
                        directory = case_dir / arm / "commands" / f"{index:03d}-{temperature}-{form}"
                        samples.append({"index": index, "query": query(index), "query_id": f"control-{index % 4}",
                                        "warmup": index < WARMUPS, "arm": arm, "temperature": temperature,
                                        "form": form, "audit": {"path": str((directory / "audit.json").relative_to(case_dir)), "sha256": None}})
                        write(case_dir / "samples.json", samples)
                        try:
                            attempt(project, directory, databases[arm], sources[arm], index, form, temperature,
                                    workers[arm], rows=rows, per_file=per_file, timeout=timeout)
                        finally:
                            if (directory / "audit.json").is_file():
                                samples[-1]["audit"] = ref(case_dir, directory / "audit.json")
                            write(case_dir / "samples.json", samples)
                        # Persist progress so interruption/failed/degraded attempts
                        # remain visible even when the complete suite is MISSING.
    return samples
