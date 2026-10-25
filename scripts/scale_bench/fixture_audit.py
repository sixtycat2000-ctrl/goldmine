"""Bind exited native builders and frozen common fixture content."""
from __future__ import annotations

import math
from pathlib import Path
import re
from uuid import UUID

from .audit import check_rss, check_snapshot, finite, hashed, load
from .fixture import logical_identity
from .protocol import ARMS, BLOCK, DIM, SEED, configuration, db_snapshot


def _schema_target() -> int:
    """The LIVE schema for the candidate arm (lazy: bench subprocesses may
    not have the repo root on sys.path at import time)."""
    import sys
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)
    from goldmine.migrations import TARGET

    return TARGET


def validate(case_dir: Path, case: dict, run: dict, project: Path) -> dict:
    rows, per_file, fixtures = case["rows"], case["segments_per_file"], {}
    schema_target = _schema_target()
    for arm in ARMS:
        fixture = load(case_dir, case["fixtures"][arm])
        keys = {"schema", "kind", "rows", "segments_per_file", "dim", "seed", "generation_block_rows",
                "generation_dtype", "stored_dtype", "vector_logical_sha256", "logical_sha256", "counts",
                "schema_version", "snapshot", "fixture_peak_rss", "embedding_fingerprint", "index_uuid", "quality_claim"}
        if set(fixture) != keys or fixture["schema"] != 2 or fixture["kind"] != "synthetic-capacity-text/v2":
            raise ValueError("vector-only or unknown fixtures cannot satisfy full CLI/pack capacity")
        expected = {"rows": rows, "segments_per_file": per_file, "dim": DIM, "seed": SEED,
                    "generation_block_rows": BLOCK, "generation_dtype": "float32", "stored_dtype": "<f2",
                    # candidate arm = the LIVE schema (was 6; additive bumps
                    # like P1-3 v7 stay valid candidates)
                    "schema_version": 4 if arm == "reference" else schema_target,
                    "quality_claim": False}
        if any(fixture.get(key) != value for key, value in expected.items()):
            raise ValueError("native fixture format differs from the fixed capacity protocol")
        uuid = fixture["index_uuid"]
        if (arm == "reference" and uuid is not None or arm == "candidate" and
                (not isinstance(uuid, str) or str(UUID(uuid)) != uuid)):
            raise ValueError("native fixture lacks its actual citation identity")
        counts = {"files": math.ceil(rows / per_file), "segments": rows, "fts": rows, "vectors": rows}
        if fixture["counts"] != counts:
            raise ValueError("a small/empty/FTS-less fixture cannot stand in for the required scale")
        for key in ("vector_logical_sha256", "logical_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(fixture[key])):
                raise ValueError("fixture content digest missing")
        fp = fixture["embedding_fingerprint"]
        if fp.get("backend") != "mock" or fp.get("dim") != DIM or fp.get("model_sha256") != "n/a":
            raise ValueError("full capacity fixture must disclose its mock control, never a real-model claim")
        check_snapshot(fixture["snapshot"])
        check_rss(fixture["fixture_peak_rss"], scope="fixture-builder-only")
        directory = case_dir / arm
        receipt = load(case_dir, case["build_receipts"][arm])
        if case["build_receipts"][arm]["path"] != f"{arm}/build/receipt.json":
            raise ValueError("native fixture receipt is outside its owned arm")
        result_path = hashed(case_dir, case["fixtures"][arm])
        argv = [run["python"]["path"], str(project / "scripts/bench_scale.py"), "--full-cli", "--worker", "build",
                "--source-root", run["source_roots"][arm], "--db", str(directory / "fixture.sqlite"),
                "--rows", str(rows), "--per-file", str(per_file), "--result", str(result_path)]
        env = configuration(Path(run["source_roots"][arm]), directory / "state")
        if (directory / "state/config.toml").read_bytes() != b"":
            raise ValueError("capacity config must remain the explicitly empty isolated config")
        if (receipt.get("kind") != "goldmine.scale.command" or receipt.get("argv") != argv or
                receipt.get("cwd") != str(directory) or receipt.get("env") != env or
                receipt.get("exit_code") != 0 or receipt.get("timed_out") is not False or
                not finite(receipt.get("wall_ms"), positive=True)):
            raise ValueError("native builder command failed or is not bound to exact source/input")
        for stream in ("stdout", "stderr"):
            if hashed(directory / "build", receipt[stream]).read_bytes():
                raise ValueError("native builder emitted unexpected diagnostics")
        check_rss(receipt["rss"], scope="fresh-process")
        expected_logical = {key: fixture[key] for key in ("logical_sha256", "counts", "schema_version")}
        if case["logical_before"][arm] != expected_logical or case["logical_after"][arm] != expected_logical:
            raise ValueError("common fixture content changed during preparation or measurement")
        final = case["final_snapshots"][arm]
        check_snapshot(final)
        if not run["cache_experiment"] or arm == "reference":
            if final["sqlite"] != fixture["snapshot"]["sqlite"]:
                raise ValueError("native fixture bytes changed outside an explicit cache preparation")
        db = directory / "fixture.sqlite"
        if db.exists():
            if db_snapshot(db)["sqlite"] != final["sqlite"] or logical_identity(db) != expected_logical:
                raise ValueError("retained fixture is stale or corrupted")
        elif case["fixtures_retained"]:
            raise ValueError("fixture declared retained but missing")
        fixtures[arm] = fixture
    for key in ("counts", "logical_sha256", "vector_logical_sha256", "embedding_fingerprint"):
        if fixtures["reference"][key] != fixtures["candidate"][key]:
            raise ValueError("reference/candidate native fixtures have unequal common FTS/vector content")
    return fixtures
