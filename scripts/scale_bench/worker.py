"""Subprocess-only builders and actual warmed CLI calls."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import gc
import json
from pathlib import Path
import sys
import time
import traceback

from .protocol import peak, ref, source_identity, visible_env, write


def warm(source: Path):
    import os

    began = time.monotonic_ns()
    from goldmine.cli import main

    ready = {"kind": "ready", "process_id": os.getpid(), "import_ms": (time.monotonic_ns() - began) / 1e6,
             "source": source_identity(source), "rss": peak(scope="one-arm-warm-worker")}
    print(json.dumps(ready), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        args, directory = request["args"], Path(request["directory"])
        directory.mkdir(parents=True, exist_ok=False)
        gc.collect()
        with (directory / "stdout.log").open("w") as stdout, (directory / "stderr.log").open("w") as stderr:
            started = time.monotonic_ns()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    code = main(args)
                except SystemExit as exc:
                    code = exc.code if type(exc.code) is int else 1
                except Exception:
                    traceback.print_exc()
                    code = 1
            finished = time.monotonic_ns()
        receipt = {"schema": 1, "kind": "goldmine.scale.warm-command",
                   "argv": [sys.executable, "-m", "goldmine.cli", *args],
                   "invocation": "goldmine.cli.main(argv)", "process_id": os.getpid(),
                   "cwd": str(Path.cwd()), "env": visible_env(dict(os.environ)),
                   "exit_code": code, "timed_out": False, "started_ns": started, "finished_ns": finished,
                   "wall_ms": (finished - started) / 1e6, "rss": peak(scope="one-arm-warm-worker"),
                   "stdout": ref(directory, directory / "stdout.log"),
                   "stderr": ref(directory, directory / "stderr.log")}
        write(directory / "receipt.json", receipt)
        print(json.dumps({"kind": "completed", "directory": str(directory.resolve())}), flush=True)


def run(args) -> int:
    sys.path.insert(0, str(args.source_root.resolve()))
    if args.worker == "warm":
        warm(args.source_root)
        return 0
    if args.worker == "build":
        from .fixture import build

        result = build(args.db, args.rows, args.per_file)
    elif args.worker == "cache":
        import shutil
        from goldmine.store import Store
        from goldmine.vector_cache import build_vector_cache, set_vector_cache_enabled

        store = Store(args.db)
        try:
            result = build_vector_cache(store)
            set_vector_cache_enabled(store, True)
            result["enabled_after"] = store.meta("vector_cache_enabled") == "1"
            copy = args.result.with_name("cache-manifest.json")
            shutil.copyfile(Path(result["cache_root"]) / "manifest.json", copy)
            result["manifest_copy"] = ref(args.result.parent, copy)
        finally:
            store.close()
        result["builder_peak_rss"] = peak(scope="cache-builder-only")
    else:
        raise ValueError("unknown capacity worker action")
    write(args.result, result)
    return 0
