"""Recompute CLI/pack consistency and every cold/warm sample from raw evidence."""
from __future__ import annotations

from pathlib import Path
import re

from .audit import (check_command, check_proofs, check_readonly, check_rss, finite, hashed, load)
from .output import equivalent, human_pack, json_value, structured
from .protocol import (ARMS, FORMS, TEMPERATURES, WARMUPS, cli_args, configuration, quantile, query)


def _warm(case_dir, case, run, project):
    workers = {}
    for arm in ARMS:
        directory = case_dir / arm / "warm-worker"
        process = load(case_dir, case["warm_workers"][arm])
        expected_argv = [run["python"]["path"], str(project / "scripts/bench_scale.py"), "--full-cli",
                         "--worker", "warm", "--source-root", run["source_roots"][arm]]
        fields = {"schema", "argv", "cwd", "env", "process_id", "exit_code", "started_ns", "finished_ns",
                  "ready", "stdout", "stderr", "failure", "timed_out", "reaped"}
        ready = process.get("ready", {})
        if (set(process) != fields or process["schema"] != 1 or process["argv"] != expected_argv or
                process["exit_code"] != 0 or process["failure"] is not None or process["timed_out"] is not False or
                process["reaped"] is not True or process["cwd"] != str(case_dir / arm) or
                process["env"] != configuration(Path(run["source_roots"][arm]), case_dir / arm / "state") or
                ready.get("kind") != "ready" or ready.get("process_id") != process["process_id"] or
                ready.get("source") != run["sources"][arm] or not finite(ready.get("import_ms"), positive=True)):
            raise ValueError("warm CLI process lacks source-bound imports/independent lifecycle")
        check_rss(ready["rss"], scope="one-arm-warm-worker")
        stdout = hashed(directory, process["stdout"]).read_bytes().splitlines()
        if not stdout or json_value(stdout[0]) != ready or hashed(directory, process["stderr"]).read_bytes():
            raise ValueError("warm worker raw lifecycle logs do not match captured metadata")
        completions = [json_value(line) for line in stdout[1:]]
        expected = [{"kind": "completed", "directory": str(case_dir / arm / "commands" / f"{i:03d}-warm-{form}")}
                    for i in range(run["cli_runs"] + WARMUPS) for form in FORMS]
        if completions != expected:
            raise ValueError("warm process did not actually complete every required CLI invocation")
        workers[arm] = process
    if workers["reference"]["process_id"] == workers["candidate"]["process_id"]:
        raise ValueError("warm reference/candidate must not share a Python process or cumulative RSS")
    return workers


def validate(case_dir: Path, case: dict, run: dict, project: Path) -> dict:
    workers = _warm(case_dir, case, run, project)
    fixtures = {arm: load(case_dir, case["fixtures"][arm]) for arm in ARMS}
    expected = [(i, temperature, form, arm) for i in range(run["cli_runs"] + WARMUPS)
                for temperature in TEMPERATURES for form in FORMS
                for arm in (ARMS if i % 2 == 0 else tuple(reversed(ARMS)))]
    samples = case.get("samples")
    if not isinstance(samples, list) or len(samples) != len(expected):
        raise ValueError("each case requires30 actual calls per arm/temperature/form, plus3 warmups")
    parsed, text, previous_end, failures, cold_pids = {}, {}, 0, [], set()
    metrics = {f"{arm}.{temperature}.{form}": {"wall_ms": [], "rss_bytes": []}
               for arm in ARMS for temperature in TEMPERATURES for form in FORMS}
    vectors = {arm: {} for arm in ARMS}
    for sample, (index, temperature, form, arm) in zip(samples, expected):
        keys = {"index", "query", "query_id", "warmup", "arm", "temperature", "form", "audit"}
        if (not isinstance(sample, dict) or set(sample) != keys or sample["index"] != index or
                sample["query"] != query(index) or sample["query_id"] != f"control-{index % 4}" or
                type(sample["warmup"]) is not bool or sample["warmup"] != (index < WARMUPS) or
                (sample["temperature"], sample["form"], sample["arm"]) != (temperature, form, arm)):
            raise ValueError("samples are missing/duplicated/reordered or use different paired queries")
        relative = f"{arm}/commands/{index:03d}-{temperature}-{form}/audit.json"
        if sample["audit"]["path"] != relative:
            raise ValueError("a required attempt was replaced by a repeated favorable output")
        audit = load(case_dir, sample["audit"])
        if (set(audit) != {"schema", "command", "database_before", "database_after", "readback", "error"} or
                audit["schema"] != 1 or audit["command"]["path"] != "receipt.json"):
            raise ValueError("incomplete immutable per-attempt database audit")
        directory = case_dir / Path(relative).parent
        receipt = load(directory, audit["command"])
        raw, stderr = check_command(directory, receipt,
            [run["python"]["path"], "-m", "goldmine.cli", *cli_args(case_dir / arm / "fixture.sqlite", index, form)],
            warm=temperature == "warm", env=configuration(Path(run["source_roots"][arm]), case_dir / arm / "state"))
        check_rss(receipt["rss"], unit="bytes" if run["environment"]["os"] == "Darwin" else "KiB")
        if receipt["started_ns"] < previous_end:
            raise ValueError("CLI arms ran in separate batches/overlapped rather than alternating the same query")
        previous_end = receipt["finished_ns"]
        if temperature == "warm":
            process = workers[arm]
            if (receipt["process_id"] != process["process_id"] or receipt["started_ns"] < process["started_ns"] or
                    receipt["finished_ns"] > process["finished_ns"]):
                raise ValueError("warm sample RSS/lifetime does not belong to its declared fresh arm worker")
        else:
            pid = receipt["process_id"]
            if pid in cold_pids or pid in [worker["process_id"] for worker in workers.values()]:
                raise ValueError("cold capacity calls must use actual fresh subprocesses")
            cold_pids.add(pid)
        check_readonly(audit, case["final_snapshots"][arm])
        if index >= WARMUPS:
            bucket = metrics[f"{arm}.{temperature}.{form}"]
            bucket["wall_ms"].append(receipt["wall_ms"])
            bucket["rss_bytes"].append(receipt["rss"]["bytes"])
        if (receipt["exit_code"] != 0 or receipt["timed_out"] or audit["error"] is not None or
                not re.fullmatch(r"via: cold \([0-9]+\.[0-9]+s\)\n", stderr)):
            failures.append({"index": index, "arm": arm, "temperature": temperature, "form": form,
                             "exit_code": receipt["exit_code"], "timed_out": receipt["timed_out"], "reason": audit["error"]})
            continue
        key = (index, temperature, arm)
        if form == "pack_text":
            text[key] = raw
            check_proofs(load(directory, audit["readback"]), [], case["rows"], case["segments_per_file"])
        else:
            result = structured(raw, form, case["rows"], case["segments_per_file"], query(index))
            proofs = load(directory, audit["readback"])
            check_proofs(proofs, result["locations"], case["rows"], case["segments_per_file"])
            for proof in proofs:
                sid, value = proof["segment_id"], proof["vector_sha256"]
                if sid in vectors[arm] and vectors[arm][sid] != value:
                    raise ValueError("repeated actual SQLite vector readback changed during a case")
                vectors[arm][sid] = value
            parsed[key + (form,)] = result
    consistent = True
    for index in range(run["cli_runs"] + WARMUPS):
        for temperature in TEMPERATURES:
            for arm in ARMS:
                key = (index, temperature, arm)
                if key in text and key + ("pack_json",) in parsed and key + ("search",) in parsed:
                    human_pack(text[key], parsed[key + ("pack_json",)], fixture=fixtures[arm],
                               n_hits=len(parsed[key + ("search",)]["rows"]))
            for form in ("search", "pack_json"):
                left, right = ((index, temperature, arm, form) for arm in ARMS)
                if left in parsed and right in parsed:
                    consistent &= equivalent(parsed[left], parsed[right])
            for arm in ARMS:
                for form in ("search", "pack_json"):
                    cold, warm = ((index, t, arm, form) for t in TEMPERATURES)
                    if cold in parsed and warm in parsed:
                        consistent &= equivalent(parsed[cold], parsed[warm])
                search, pack = ((index, temperature, arm, form) for form in ("search", "pack_json"))
                if search in parsed and pack in parsed:
                    consistent &= equivalent(parsed[search], parsed[pack])
    for sid in set(vectors["reference"]) & set(vectors["candidate"]):
        if vectors["reference"][sid] != vectors["candidate"][sid]:
            raise ValueError("native reference/candidate vector bytes differ at an emitted source row")
    summary = {key: {"attempts": len(value["wall_ms"]), "samples_ms": value["wall_ms"],
                     "p50_ms": quantile(value["wall_ms"], .5), "p95_ms": quantile(value["wall_ms"], .95),
                     "process_peak_rss_bytes": max(value["rss_bytes"])} for key, value in metrics.items()}
    return {"metrics": summary, "failures": failures, "consistent": bool(consistent),
            "imports_ms": {arm: workers[arm]["ready"]["import_ms"] for arm in ARMS}}
