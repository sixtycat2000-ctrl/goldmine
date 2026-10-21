"""Full synthetic capacity suite; execute one case at a time to bound disk use."""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

from .execute import command
from .fixture import logical_identity
from .output import json_value
from .protocol import (ARMS, CACHE, CLI_RUNS, DIM, DISTRIBUTIONS, PARAMETERS, SEED, SIZES,
                       WARMUPS, db_snapshot, hardware, isolated_env, ref, sha, source_identity,
                       tool_identity, write)
from .sampling import cli_samples, worker_args


def build_one(project, source, db, directory, rows, per_file, timeout):
    output = directory / "fixture.json"
    directory.mkdir(parents=True)
    env = isolated_env(source, directory / "state")
    argv = worker_args(project, "build", source) + ["--db", str(db), "--rows", str(rows),
        "--per-file", str(per_file), "--result", str(output)]
    receipt = command(argv, directory / "build", cwd=directory, env=env, timeout=timeout)
    if receipt["exit_code"] != 0 or receipt["timed_out"]:
        raise ValueError(f"native fixture build failed: {directory / 'build/stderr.log'}")
    return json_value(output.read_bytes())


def cleanup(case_dir: Path, databases: dict, cache_result: dict | None):
    """Remove only the exact disposable fixture paths this suite generated."""
    cache_root = None
    paths = []
    for arm in ARMS:
        db = databases[arm]
        if db != case_dir / arm / "fixture.sqlite":
            raise ValueError("refuse cleanup of a non-owned fixture path")
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(str(db) + suffix)
            if path.is_symlink():
                raise ValueError("refuse cleanup through a fixture symlink")
            paths.append(path)
    if cache_result is not None:
        cache_root = Path(cache_result["cache_root"])
        expected = databases["candidate"].with_name("fixture.sqlite.vectors")
        if cache_root.is_symlink() or cache_root.resolve() != expected.resolve():
            raise ValueError("refuse cleanup outside the exact owned derived-cache directory")
    for path in paths:
        path.unlink(missing_ok=True)
    if cache_root is not None:
        shutil.rmtree(cache_root)


def one_case(args, project: Path, rows: int, per_file: int, sources: dict) -> dict:
    from bench_scale import paired_measurements

    case_dir = args.out / f"{rows}-{per_file}"
    case_dir.mkdir()
    databases = {arm: case_dir / arm / "fixture.sqlite" for arm in ARMS}
    fixtures = {arm: build_one(project, sources[arm], databases[arm], case_dir / arm, rows, per_file,
                                args.build_timeout) for arm in ARMS}
    if any(fixtures[arm][key] != fixtures["reference"][key] for arm in ARMS
           for key in ("logical_sha256", "vector_logical_sha256", "counts", "embedding_fingerprint")):
        raise ValueError("native-schema fixtures differ in common FTS/vector content or mock fingerprint")
    cache, cache_receipt = None, None
    if args.cache_experiment:
        directory = case_dir / "candidate"
        output = directory / "cache.json"
        argv = worker_args(project, "cache", sources["candidate"]) + ["--db", str(databases["candidate"]), "--result", str(output)]
        receipt = command(argv, directory / "cache-build", cwd=directory,
            env=isolated_env(sources["candidate"], directory / "state"), timeout=args.build_timeout)
        if receipt["exit_code"] != 0 or receipt["timed_out"]:
            raise ValueError("explicit derived-cache build failed")
        cache = ref(case_dir, output)
        cache_receipt = ref(case_dir, directory / "cache-build/receipt.json")
    # The two phase workers have independent lifetimes and alternate the same
    # vector control; neither inherits generator RSS nor a preceding case.
    stage = case_dir / "phases"
    stage.mkdir()
    phase_args = SimpleNamespace(out=stage, runs=args.runs, reference_root=sources["reference"],
        candidate_root=sources["candidate"], reference_db=databases["reference"], candidate_db=databases["candidate"])
    paired_measurements(phase_args, databases["reference"], "vector")
    before = {arm: logical_identity(databases[arm]) for arm in ARMS}
    environment = hardware()
    samples = cli_samples(project, case_dir, sources, databases, rows=rows, per_file=per_file,
                          runs=args.cli_runs, timeout=args.sample_timeout)
    after = {arm: logical_identity(databases[arm]) for arm in ARMS}
    if before != after:
        raise ValueError("CLI samples changed common native-fixture content")
    result = {"schema": 2, "kind": "goldmine.scale.case/v2", "rows": rows, "segments_per_file": per_file,
              "fixtures": {arm: ref(case_dir, case_dir / arm / "fixture.json") for arm in ARMS},
              "build_receipts": {arm: ref(case_dir, case_dir / arm / "build/receipt.json") for arm in ARMS},
              "phase_reports": {arm: ref(case_dir, stage / f"vector-{arm}.json") for arm in ARMS},
              "phase_artifacts": {arm: {name: ref(case_dir, stage / f"vector-{arm}.{suffix}")
                  for name, suffix in (("receipt", "receipt.json"), ("stdout", "stdout"), ("stderr", "stderr"))}
                  for arm in ARMS},
              "phase_order": ref(case_dir, stage / "vector-order.json"),
              "cache_build": cache, "cache_receipt": cache_receipt, "logical_before": before, "logical_after": after,
              "final_snapshots": {arm: db_snapshot(databases[arm]) for arm in ARMS},
              "environment": environment, "environment_after": hardware(), "samples": samples,
              "warm_workers": {arm: ref(case_dir, case_dir / arm / "warm-worker/process.json") for arm in ARMS},
              "fixtures_retained": args.keep_fixtures}
    write(case_dir / "case.json", result)
    if not args.keep_fixtures:
        cleanup(case_dir, databases, json_value((case_dir / cache["path"]).read_bytes()) if cache else None)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--reference-root", type=Path)
    parser.add_argument("--candidate-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--scales", nargs="+", type=int, default=list(SIZES))
    parser.add_argument("--distributions", nargs="+", type=int, default=list(DISTRIBUTIONS))
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--cli-runs", type=int, default=CLI_RUNS)
    parser.add_argument("--keep-fixtures", action="store_true")
    parser.add_argument("--cache-experiment", action="store_true")
    parser.add_argument("--sample-timeout", type=float, default=120)
    parser.add_argument("--build-timeout", type=float, default=1800)
    parser.add_argument("--worker", choices=("build", "warm", "cache"), help=argparse.SUPPRESS)
    parser.add_argument("--source-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--db", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--rows", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--per-file", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--result", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.worker:
            from .worker import run

            return run(args)
        project = Path(__file__).resolve().parents[2]
        if args.out is None or args.reference_root is None:
            parser.error("--out and --reference-root are required")
        if args.runs < 10 or args.cli_runs < CLI_RUNS:
            parser.error("at least10 phase and30 CLI runs are required, plus3 warmups")
        if any(x <= 0 for x in args.scales + args.distributions) or len(set(args.scales)) != len(args.scales) or len(set(args.distributions)) != len(args.distributions):
            parser.error("scale/distribution values must be positive and unique")
        args.out = args.out.resolve()
        if args.out.exists() and any(args.out.iterdir()):
            parser.error("--out must be absent or empty")
        args.out.mkdir(parents=True, exist_ok=True)
        sources = {"reference": args.reference_root.resolve(), "candidate": args.candidate_root.resolve()}
        if sources["reference"] == sources["candidate"]:
            parser.error("independent source directories are required")
        sources.update({arm + "_identity": source_identity(source) for arm, source in list(sources.items())})
        report = {"schema": 2, "kind": "goldmine.scale.full/v2", "suite_id": "scale.full.v2",
                  "started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "status": "MISSING",
                  "source_roots": {arm: str(sources[arm]) for arm in ARMS},
                  "sources": {arm: sources[arm + "_identity"] for arm in ARMS}, "tools": tool_identity(project),
                  "python": {"path": sys.executable, "sha256": sha(Path(sys.executable))},
                  "parameters": PARAMETERS, "seed": SEED, "dim": DIM, "warmups": WARMUPS,
                  "phase_runs": args.runs, "cli_runs": args.cli_runs, "quantile": "nearest-rank",
                  "cache_definition": CACHE, "cache_experiment": args.cache_experiment,
                  "scope": "synthetic capacity only; no model/quality/real-tokenizer claim", "quality_claim": False,
                  "environment": hardware(), "cases": []}
        write(args.out / "manifest.json", report)
        for rows in args.scales:
            for per_file in args.distributions:
                one_case(args, project, rows, per_file, sources)
                report["cases"].append(ref(args.out, args.out / f"{rows}-{per_file}/case.json"))
                write(args.out / "manifest.json", report)
                print(f"capacity CLI+pack {rows}/{per_file}: captured", flush=True)
        report["source_after"] = {arm: source_identity(sources[arm]) for arm in ARMS}
        report["tools_after"] = tool_identity(project)
        report["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        write(args.out / "manifest.json", report)
        # A subset run is useful mechanically but can never satisfy the12-case gate.
        from .validation import validate

        details = validate(args.out / "manifest.json", current_project=project, require_all=False)
        report["summary"], report["status"] = details, details["status"]
        write(args.out / "manifest.json", report)
        if details["cache_benefit_status"] == "MISSING":
            return 2
        return 0 if details["consistent"] and details["cache_benefit_status"] in ("PASS", "not_required") and not any(
            item["cli"]["failures"] for item in details["cases"]) else 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
