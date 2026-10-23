#!/usr/bin/env python3
"""Collect interleaved CLI latency and paired MCP overhead on a frozen source tree."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from acceptance_lib.evidence import artifact, capture_command, read_command_run, read_json, sha256, write_json
from search_bench.metrics import fast_metrics, mcp_metrics
from search_bench.session import Session, hardware, package_identity, pressure


def query_list(path):
    data = read_json(path)
    if not isinstance(data, list) or not data or any(set(r) != {"id", "query"} or
            not all(isinstance(r[k], str) and r[k].strip() for k in ("id", "query")) for r in data):
        raise ValueError("queries must be a nonempty JSON list of {id,query}; supply dev queries only")
    if len({r["id"] for r in data}) != len(data):
        raise ValueError("duplicate query ID")
    return data


def benchmark(args):
    out, source = args.out.resolve(), args.source_root.resolve()
    queries = query_list(args.queries)
    session = Session(out, source, args.model, args.server, sys.executable, args.profile)
    before = hardware()
    result = {"schema": 2, "kind": "goldmine.search-latency", "quantile": "nearest-rank",
              "profile": args.profile, "identity": session.identity, "environment": before,
              "queries_sha256": sha256(args.queries), "cache_state": "3warmups; new Python subprocess per CLI sample",
              "samples": [], "mcp_pairs": []}
    try:
        session.prepare(args.db.resolve(), upgrade=not args.cli_only)
        for i in range(3):
            receipt, valid, _, _, _ = session.command(f"warmup-{i}", ["search", queries[i % len(queries)]["query"], "--db", str(session.db), "--json"])
            if receipt["exit_code"] != 0 or not valid:
                raise ValueError("warmup did not produce valid CLI JSON")
        initial = session.status()
        if not initial.get("resident"):
            raise ValueError("owned model did not become resident")
        for i in range(args.samples):
            query = queries[i % len(queries)]
            for arm in (("warm", "cold") if i % 2 == 0 else ("cold", "warm")):
                arguments = ["search", query["query"], "--db", str(session.db), "--limit", "5", "--json"]
                if arm == "cold":
                    arguments.append("--no-serve")
                receipt, valid, _, stderr, ref = session.command(f"{i:03}-{arm}", arguments)
                result["samples"].append({"arm": arm, "query_id": query["id"], "query": query["query"],
                    "wall_ms": receipt["wall_ms"], "exit_code": receipt["exit_code"], "json_valid": valid,
                    "degraded": (f"via: {'serve' if arm == 'warm' else 'cold'}" not in stderr),
                    "pressure": pressure(), "command": ref})
        if not args.cli_only:
            import anyio
            from search_bench.protocol import paired

            result["mcp_pairs"] = anyio.run(paired, session, queries, args.samples)
        final = session.status()
        result["guard_before"], result["guard_after"] = initial, final
        result["fast_metrics"] = fast_metrics(result["samples"])
        result["mcp_metrics"] = mcp_metrics(result["mcp_pairs"]) if not args.cli_only else None
        fm, mm = result["fast_metrics"], result["mcp_metrics"]
        missing = (fm["incomplete"] or (mm and mm["failures"]) or before["pressure"]["level"] is None or
                   initial["guard"]["unloads"] != final["guard"]["unloads"] or
                   package_identity(source) != session.identity["source_files"])
        passed = (fm["warm_p50_ms"] <= 300 and fm["warm_p95_ms"] <= 500 and
                  fm["warm_p50_ms"] <= fm["cold_p50_ms"] / 3 and (mm is None or mm["paired_extra_p95_ms"] <= 100))
        result["status"] = "MISSING" if missing else "PASS" if passed else "FAIL"
    except Exception as exc:
        result.update({"status": "MISSING", "reason": f"{type(exc).__name__}: {exc}"})
    finally:
        session.close()
        result["source_after"] = package_identity(source)
        result["environment_after"] = hardware()
        result["daemon_logs"] = {name: artifact(out, out / name) for name in ("daemon.stdout", "daemon.stderr") if (out / name).is_file()}
        write_json(out / "samples.json", result)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return {"PASS": 0, "FAIL": 1, "MISSING": 2}[result["status"]]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("out", "source-root", "db", "model", "server", "queries"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--profile", choices=("controlled", "real"), required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--cli-only", action="store_true", help="historical reference without MCP support; cannot satisfy MCP acceptance")
    args = parser.parse_args(argv)
    if args.samples < 30:
        parser.error("at least30 samples per arm are required")
    if args.worker:
        return benchmark(args)
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        parser.error("output must be new or empty")
    out.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env["PYTHONPATH"] = str(args.source_root.resolve())
    worker_argv = [str(args.python.absolute()), str(Path(__file__).resolve()), "--worker",
                   "--profile", args.profile, "--samples", str(args.samples)]
    if args.cli_only:
        worker_argv.append("--cli-only")
    for key in ("out", "source_root", "db", "model", "server", "queries"):
        worker_argv.extend(["--" + key.replace("_", "-"), str(getattr(args, key).resolve())])
    capture_command(out / "execution", "latency.search", worker_argv, cwd=out, env=env,
                    suite_id="latency.fast.v2", identity={"source_files": package_identity(args.source_root.resolve())},
                    timeout=3600)
    _, receipt, _ = read_command_run(out / "execution/manifest.json")
    print(f"search benchmark exit{receipt['exit_code']}; raw manifest: {out / 'execution/manifest.json'}")
    return receipt["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
