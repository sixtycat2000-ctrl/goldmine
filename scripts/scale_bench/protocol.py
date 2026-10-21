"""Fixed, source-bound inputs for the full capacity protocol."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import sys

DIM, SEED, BLOCK, WARMUPS, CLI_RUNS = 768, 20261021, 4096, 3, 30
SIZES, DISTRIBUTIONS = (10000, 100000, 250000, 500000), (1, 10, 100)
FORMS, TEMPERATURES, ARMS = ("search", "pack_json", "pack_text"), ("cold", "warm"), ("reference", "candidate")
PARAMETERS = {"backend": "mock", "scorer": "semantic", "z_gate": 0,
              "limit": 10, "context": 1, "budget": 4000}
CACHE = {"cold": "fresh Python process, fresh Store and in-process mock backend",
         "warm": "one pre-imported CLI process per case/arm; fresh Store/backend per request",
         "os": "untouched; neither temperature claims a cold filesystem cache",
         "model": "no external model; deterministic mock capacity control only"}


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def ref(root: Path, path: Path) -> dict:
    return {"path": str(path.resolve().relative_to(root.resolve())), "sha256": sha(path)}


def source_identity(source: Path) -> dict:
    files = {str(p.relative_to(source)): sha(p) for p in sorted((source / "goldmine").rglob("*.py"))}
    if "goldmine/cli.py" not in files or "goldmine/store.py" not in files:
        raise ValueError("source root must contain the actual Goldmine CLI and Store")
    return {"files": files, "source_sha256": digest(files)}


def tool_identity(project: Path) -> dict:
    paths = [project / "scripts/bench_scale.py", *sorted((project / "scripts/scale_bench").glob("*.py"))]
    return {str(p.relative_to(project)): sha(p) for p in paths}


def query(index: int) -> str:
    # Repeated controls make distributions comparable; these are not quality labels.
    return ("capacity measurement record", "stable vector storage boundary",
            "synthetic document accounting", "measurement file segment mapping")[index % 4]


def body(sid: int) -> str:
    return (f"synthetic capacity record {sid:08d}. this stored fts body exercises real "
            "detail and context reads. random vectors and mock queries carry no relevance claim.")


def file_id(sid: int, rows: int, per_file: int) -> int:
    return (sid - 1) % math.ceil(rows / per_file) + 1


def file_path(fid: int) -> str:
    return f"/goldmine-scale/{fid}.txt"


def cli_args(db: Path, index: int, form: str) -> list[str]:
    if form not in FORMS:
        raise ValueError("unknown capacity CLI form")
    args = ["search", query(index), "--db", str(db.resolve()), "--backend", "mock",
            "--no-serve", "--scorer", "semantic", "--z-gate", "0", "--limit", "10", "--context", "1"]
    if form != "search":
        args += ["--pack", "--budget", "4000"]
    if form != "pack_text":
        args += ["--json"]
    return args


def isolated_env(source: Path, state: Path) -> dict:
    state.mkdir(parents=True, exist_ok=True)
    config = state / "config.toml"
    if not config.exists():
        config.write_text("")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GOLDMINE_") and k not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(configuration(source, state))
    return env


def configuration(source: Path, state: Path) -> dict:
    return {"GOLDMINE_HOME": str(state.resolve()), "GOLDMINE_CONFIG": str((state / "config.toml").resolve()),
            "GOLDMINE_OCR_AUTOCOMPILE": "0", "PYTHONPATH": str(source.resolve()),
            "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"}


def visible_env(env: dict) -> dict:
    return {k: v for k, v in env.items() if k.startswith("GOLDMINE_") or
            k in ("PYTHONPATH", "PYTHONHASHSEED", "PYTHONDONTWRITEBYTECODE")}


def peak(usage=None, *, scope="fresh-process") -> dict:
    raw = (usage or resource.getrusage(resource.RUSAGE_SELF)).ru_maxrss
    unit = "bytes" if sys.platform == "darwin" else "KiB"
    return {"raw": raw, "raw_unit": unit, "bytes": int(raw if unit == "bytes" else raw * 1024), "scope": scope}


def quantile(values, fraction):
    if not values or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("finite nonnegative samples required")
    return sorted(values)[math.ceil(len(values) * fraction) - 1]


def hardware() -> dict:
    from search_bench.session import pressure

    return {"os": platform.system(), "release": platform.release(), "arch": platform.machine(),
            "python": platform.python_version(), "cpu_count": os.cpu_count(), "pressure": pressure()}


def db_snapshot(db: Path) -> dict:
    # A full byte hash implies unchanged logical data, without trusting row counts.
    result = {}
    for name, suffix in (("sqlite", ""), ("wal", "-wal"), ("shm", "-shm"), ("journal", "-journal")):
        path = Path(str(db) + suffix)
        result[name] = {"bytes": path.stat().st_size, "sha256": sha(path)} if path.is_file() else None
    return result


def unchanged(before: dict, after: dict) -> bool:
    # SQLite read marks can change -shm. They do not contain database pages.
    return (before["sqlite"] == after["sqlite"] and all(
        state[key] is None or state[key]["bytes"] == 0
        for state in (before, after) for key in ("wal", "journal")))
