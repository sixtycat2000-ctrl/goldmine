"""Replay resource artifacts, not summaries. Consistency is not job authentication."""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

from .evidence import InvalidEvidence, _object, is_sha, read_command_run, read_json, sha256, verify_artifact
from .resource_policy import OUTPUTS, bridge_argv, digest, runtime_identity, validate_policy
from .resource_tools import PS


def _number(value, name, *, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or value < 0 or value > 2**63 or not math.isfinite(value):
        raise InvalidEvidence(f"invalid nonnegative resource counter: {name}")
    return value


def _stamp(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is None:
            raise ValueError("timezone missing")
        return stamp
    except (TypeError, ValueError) as exc:
        raise InvalidEvidence("invalid resource UTC interval") from exc


def _disk(value, *, final=False):
    keys = {"logical_bytes", "allocated_bytes", "entries", "raced_entries", "elapsed_seconds"}
    if not final:
        keys |= {"stdout_bytes", "stderr_bytes"}
    if not isinstance(value, dict) or set(value) != keys:
        raise InvalidEvidence("resource output counter schema mismatch")
    for key in keys:
        _number(value[key], key, integer=key != "elapsed_seconds")
    return value


def _processes(value, root_pid):
    if not isinstance(value, list) or len(value) > 4096:
        raise InvalidEvidence("invalid sampled process array")
    ids = set()
    for row in value:
        if not isinstance(row, dict) or set(row) != {"pid", "ppid", "pgid", "rss_bytes", "birth", "state", "fd_count"}:
            raise InvalidEvidence("resource process counter schema mismatch")
        for key in ("pid", "ppid", "pgid", "rss_bytes"):
            _number(row[key], key, integer=True)
        if row["pgid"] != root_pid or row["pid"] in ids:
            raise InvalidEvidence("foreign or duplicate process counter")
        ids.add(row["pid"])
        if not all(isinstance(row[key], str) and 0 < len(row[key]) <= 128 for key in ("birth", "state")):
            raise InvalidEvidence("invalid resource process lifetime/state")
        if row["fd_count"] is not None:
            _number(row["fd_count"], "descriptor count", integer=True)
    return value


def _tools(value, processes):
    keys = {"argv", "elapsed_seconds", "exit_code", "stdout_bytes", "stderr_bytes", "stdout_sha256", "stderr_sha256"}
    if not isinstance(value, list) or not value or len(value) > len(processes) + 1:
        raise InvalidEvidence("native resource tool records missing/ambiguous")
    pids = {str(row["pid"]) for row in processes}
    linked = set()
    for index, tool in enumerate(value):
        if not isinstance(tool, dict) or set(tool) != keys or not isinstance(tool["argv"], list):
            raise InvalidEvidence("native resource tool receipt schema mismatch")
        argv = tool["argv"]
        if index == 0:
            valid = argv == PS
        else:
            valid = (len(argv) == 7 and argv[:5] == ["/usr/sbin/lsof", "-nP", "-w", "-a", "-p"]
                     and argv[5] in pids and argv[6] == "-Fpf")
        if not valid or type(tool["exit_code"]) is not int or tool["exit_code"] != 0 or tool["stderr_bytes"] != 0:
            raise InvalidEvidence("noncanonical/failed/foreign native resource tool")
        if index:
            if argv[5] in linked:
                raise InvalidEvidence("duplicate per-process descriptor receipt")
            linked.add(argv[5])
        _number(tool["elapsed_seconds"], "native tool duration")
        for key in ("stdout_bytes", "stderr_bytes"):
            if _number(tool[key], key, integer=True) > 256 * 1024:
                raise InvalidEvidence("native resource tool byte bound exceeded")
        if not is_sha(tool["stdout_sha256"]) or not is_sha(tool["stderr_sha256"]):
            raise InvalidEvidence("native resource tool response hash missing")
    for process in processes:
        pid, zombie = str(process["pid"]), process["state"].startswith("Z")
        if zombie:
            if process["fd_count"] != 0 or pid in linked:
                raise InvalidEvidence("zombie descriptor counter/receipt mismatch")
        elif process["fd_count"] is not None and pid not in linked:
            raise InvalidEvidence("live descriptor count has no matching lsof receipt")


def _records(path):
    if path.stat().st_size > 64 * 1024**2:
        raise InvalidEvidence("resource sample log exceeds64MiB")
    with path.open(encoding="utf-8") as stream:
        for count, line in enumerate(stream):
            if count >= 20000 or len(line.encode()) > 256 * 1024:
                raise InvalidEvidence("resource record/count bound exceeded")
            try:
                record = json.loads(line, object_pairs_hook=_object)
            except (ValueError, UnicodeError) as exc:
                raise InvalidEvidence("invalid resource sample JSONL") from exc
            if not isinstance(record, dict) or record.get("schema") != 1:
                raise InvalidEvidence("invalid resource sample schema")
            yield record



def _resource_statuses(receipt, gaps, violations):
    """Separate sampled measurements from the one known lifetime caveat.

    No other gap is waived. This does not authenticate an engine job or claim a
    lifetime-complete tree, and it never relaxes measured resource violations.
    """
    def result(pending):
        return ("FAIL" if violations or receipt["exit_code"] not in {0, 2, 124, 127, 130, 143}
                else "MISSING" if pending or receipt["exit_code"] else "PASS")
    sampled_gaps = set(gaps) - {"fork-descendant-lifetimes-not-atomically-tracked"}
    return result(gaps), result(sampled_gaps), sampled_gaps

def validate_observation(manifest_path: Path, expected_policy: dict, expected_inner_argv: list[str],
                         expected_source: dict) -> dict:
    """Require exact complete context, current modules/tools, raw counters and bridge.

    Original status retains every gap. sampled_resource_status excludes only
    the exact unsupported fork-lifetime caveat; all other gaps/failures remain.
    PASS concerns the declared sampled budgets, not lifetime completeness.
    It NEVER means a kernel byte ceiling, a lifetime-complete tree, a quality
    result, or proof that a fabricated command actually ran. Fixture mode and
    unresolved forks are MISSING regardless of plausible numeric measurements.
    """
    manifest_path = Path(manifest_path).absolute()
    validate_policy(expected_policy)
    if expected_policy["source"] != expected_source or not isinstance(expected_source, dict):
        raise InvalidEvidence("resource expected source context must match the entire registered object")
    manifest, receipt, _ = read_command_run(Path(manifest_path))
    binding = manifest["identity"].get("resource_observer") if isinstance(manifest["identity"], dict) else None
    if not isinstance(binding, dict) or set(binding) != {"schema", "policy", "runtime", "inner_argv", "outer_argv"}:
        return {"status": "MISSING", "sampled_resource_status": "MISSING", "reason": "complete opt-in resource binding absent", "lifetime_complete": False, "full_tree_ceiling_status": "MISSING"}
    if type(binding["schema"]) is not int or binding["schema"] != 1 or binding["policy"] != expected_policy or binding["policy"]["source"] != expected_source:
        raise InvalidEvidence("resource policy/full source context differs from registration")
    if binding["inner_argv"] != expected_inner_argv or binding["runtime"] != runtime_identity():
        raise InvalidEvidence("resource inner command or current source/tool/runtime identity changed")
    outer = binding["outer_argv"]
    if outer is None:
        return {"status": "MISSING", "sampled_resource_status": "MISSING", "reason": "resource bridge was not spawned", "lifetime_complete": False, "full_tree_ceiling_status": "MISSING"}
    if (not isinstance(outer, list) or len(outer) != 12 + len(expected_inner_argv)
            or outer[7] != "--start-fd" or outer[9] != "--ready-fd"):
        raise InvalidEvidence("noncanonical resource exec bridge")
    try:
        start_fd, ready_fd = int(outer[8]), int(outer[10])
    except (ValueError, TypeError) as exc:
        raise InvalidEvidence("invalid bridge descriptor arguments") from exc
    if min(start_fd, ready_fd) <= 2 or start_fd == ready_fd or str(start_fd) != outer[8] or str(ready_fd) != outer[10]:
        raise InvalidEvidence("invalid bridge descriptor identity")
    expected_outer = bridge_argv(Path(manifest_path).parent / "resource-bridge-policy.json", start_fd, ready_fd,
                                 expected_inner_argv, runtime=binding["runtime"])
    if outer != expected_outer or receipt["argv"] != outer:
        raise InvalidEvidence("executed outer bridge differs from registered inner command")
    artifacts = receipt["artifacts"]
    if not all(key in artifacts and artifacts[key]["path"] == name for key, name in OUTPUTS.items()):
        return {"status": "MISSING", "sampled_resource_status": "MISSING", "reason": "raw resource artifacts absent", "lifetime_complete": False, "full_tree_ceiling_status": "MISSING"}
    obs = read_json(verify_artifact(Path(manifest_path).parent, artifacts["resource_observation"]))
    samples = verify_artifact(Path(manifest_path).parent, artifacts["resource_samples"])
    keys = {"schema", "kind", "binding_sha256", "started_at", "finished_at", "root_pid", "bridge",
            "bridge_released_after_seconds", "exit_code", "timed_out", "error", "samples_sha256", "sample_count",
            "fork_events", "coverage_gaps", "peak", "violations", "observer_callback_wall_seconds",
            "lifetime_complete", "kernel_tree_peak", "model_child_roles", "execution_seconds",
            "cleanup_seconds", "wall_deadline_overshoot_seconds", "target_exec_observed_after_seconds"}
    if not isinstance(obs, dict) or set(obs) != keys or type(obs["schema"]) is not int or obs["schema"] != 1 or obs["kind"] != "goldmine.resource-observation":
        raise InvalidEvidence("resource observation schema mismatch")
    if (type(obs["exit_code"]) is not int or type(obs["timed_out"]) is not bool
            or (obs["error"] is not None and not isinstance(obs["error"], str))):
        raise InvalidEvidence("resource observation process/error types mismatch")
    _number(obs["observer_callback_wall_seconds"], "observer callback wall")
    if obs["root_pid"] is not None:
        _number(obs["root_pid"], "owned root pid", integer=True)
    if obs["bridge_released_after_seconds"] is not None:
        _number(obs["bridge_released_after_seconds"], "bridge release time")
    if obs["binding_sha256"] != digest(binding) or obs["samples_sha256"] != sha256(samples):
        raise InvalidEvidence("resource raw binding/hash mismatch")
    if obs["lifetime_complete"] is not False or obs["kernel_tree_peak"] is not False:
        raise InvalidEvidence("unsupported complete-lifetime or kernel-tree peak claim")
    if obs["exit_code"] != receipt["exit_code"] or obs["timed_out"] != receipt["timed_out"]:
        raise InvalidEvidence("resource process status differs from raw command")
    if not (_stamp(receipt["started_at"]) <= _stamp(obs["started_at"]) <= _stamp(obs["finished_at"]) <= _stamp(receipt["finished_at"])):
        raise InvalidEvidence("resource observation outside actual capture interval")
    spec_path = Path(manifest_path).parent / "resource-bridge-policy.json"
    if spec_path.is_symlink():
        raise InvalidEvidence("resource bridge specification must not be a symlink")
    spec = read_json(spec_path)
    bridge = obs["bridge"]
    if bridge is not None:
        expected_bridge = {"schema": 1, "pid": obs["root_pid"], "pgid": obs["root_pid"], "sid": obs["root_pid"],
                           "nofile": [expected_policy["limits"]["nofile"]] * 2,
                           "spec_sha256": sha256(spec_path), "nonce": spec.get("nonce") if isinstance(spec, dict) else None}
        if (not isinstance(spec, dict) or set(spec) != {"schema", "nofile", "inner_argv", "nonce"}
                or spec["schema"] != 1 or spec["nofile"] != expected_policy["limits"]["nofile"]
                or spec["inner_argv"] != expected_inner_argv or bridge != expected_bridge):
            raise InvalidEvidence("bridge FD/session handshake or specification changed")
    peak = {key: 0 for key in ("sampled_tree_rss_bytes", "sampled_root_rss_bytes", "sampled_children_rss_bytes",
                               "sampled_per_process_fds", "owned_output_bytes", "stdout_bytes", "stderr_bytes")}
    count, forks, last_elapsed, finalized, sample_gaps, last_sample = 0, 0, -1, False, set(), None
    exec_observed = None
    intervals, callback_seconds, target_count, target_tree_peak, target_live = [], 0.0, 0, 0, False
    for row in _records(samples):
        elapsed = _number(row.get("elapsed_seconds"), "sample elapsed")
        _stamp(row.get("utc"))
        if finalized or elapsed < last_elapsed:
            raise InvalidEvidence("resource chronology/final record mismatch")
        last_elapsed = elapsed
        if row.get("kind") == "lifecycle":
            if set(row) != {"schema", "kind", "elapsed_seconds", "utc", "events", "after_release"} or not isinstance(row["events"], list):
                raise InvalidEvidence("invalid lifecycle record")
            released = obs["bridge_released_after_seconds"]
            if type(row["after_release"]) is not bool or row["after_release"] != (released is not None and elapsed >= released):
                raise InvalidEvidence("lifecycle event release chronology mismatch")
            for event in row["events"]:
                if not isinstance(event, dict) or set(event) != {"pid", "flags"}:
                    raise InvalidEvidence("invalid owned lifecycle event")
                _number(event["pid"], "event pid", integer=True)
                flags = _number(event["flags"], "event flags", integer=True)
                if event["pid"] == obs["root_pid"] and flags & 0x20000000 and row["after_release"] and exec_observed is None:
                    exec_observed = elapsed
                forks += bool(flags & 0x40000000)  # public Darwin NOTE_FORK constant
            continue
        if row.get("kind") == "final":
            if set(row) != {"schema", "kind", "elapsed_seconds", "utc", "output", "stdout_bytes", "stderr_bytes",
                            "execution_finished_seconds", "cleanup_finished_seconds"}:
                raise InvalidEvidence("invalid final resource record")
            finalized = True
            if row["stdout_bytes"] is not None and row["stderr_bytes"] is not None:
                if (row["stdout_bytes"] != verify_artifact(Path(manifest_path).parent, receipt["stdout"]).stat().st_size
                        or row["stderr_bytes"] != verify_artifact(Path(manifest_path).parent, receipt["stderr"]).stat().st_size):
                    raise InvalidEvidence("resource final stream counters differ from retained raw byte lengths")
            execution = _number(row["execution_finished_seconds"], "execution finish")
            cleanup = _number(row["cleanup_finished_seconds"], "cleanup finish")
            if not execution <= cleanup <= elapsed or obs["execution_seconds"] != execution or obs["cleanup_seconds"] != cleanup - execution:
                raise InvalidEvidence("resource execution/cleanup times differ from raw final record")
            if obs["wall_deadline_overshoot_seconds"] != max(0, execution - expected_policy["wall_seconds"]):
                raise InvalidEvidence("resource deadline overshoot hidden")
            output = row["output"]
            if output is not None:
                _disk(output, final=True)
                if output["raced_entries"]:
                    sample_gaps.add("owned-output-census-race")
            if output is None:
                sample_gaps.add("final-output-census-incomplete")
            else:
                peak["owned_output_bytes"] = max(peak["owned_output_bytes"], _number(output["logical_bytes"], "final disk", integer=True),
                                                  _number(output["allocated_bytes"], "final allocated", integer=True))
                for key in ("stdout_bytes", "stderr_bytes"):
                    peak[key] = max(peak[key], _number(row[key], key, integer=True))
            continue
        required = {"schema", "kind", "sequence", "elapsed_seconds", "utc", "observation_seconds", "processes",
                    "tree_rss_bytes", "root_rss_bytes", "children_rss_bytes", "output", "tools", "gaps", "violations", "stage", "started_elapsed_seconds"}
        if row.get("kind") != "sample" or set(row) != required or row["sequence"] != count:
            raise InvalidEvidence("resource sample fields/sequence mismatch")
        count += 1
        callback_seconds += _number(row["observation_seconds"], "observer duration")
        start = _number(row["started_elapsed_seconds"], "sample start")
        proven = exec_observed is not None and exec_observed <= start
        if start > elapsed or row["stage"] != ("target" if proven else "bridge"):
            raise InvalidEvidence("target sample lacks prior owned root NOTE_EXEC proof")
        if last_sample is not None:
            intervals.append(elapsed - last_sample)
        last_sample = elapsed
        processes = _processes(row["processes"], obs["root_pid"])
        for process in processes:
            if process["fd_count"] is not None:
                peak["sampled_per_process_fds"] = max(peak["sampled_per_process_fds"], process["fd_count"])
            else:
                sample_gaps.add("missing-fd-counter")
        tree = sum(p["rss_bytes"] for p in processes)
        direct = sum(p["rss_bytes"] for p in processes if p["pid"] == obs["root_pid"])
        if (row["tree_rss_bytes"], row["root_rss_bytes"], row["children_rss_bytes"]) != (tree, direct, tree - direct):
            raise InvalidEvidence("reported tree/root/child RSS is not raw snapshot sum")
        if row["stage"] == "target":
            target_count += 1
            target_tree_peak = max(target_tree_peak, tree)
            target_live |= any(p["pid"] == obs["root_pid"] and not p["state"].startswith("Z") and p["rss_bytes"] > 0 for p in processes)
        for key, val in (("sampled_tree_rss_bytes", tree), ("sampled_root_rss_bytes", direct), ("sampled_children_rss_bytes", tree - direct)):
            peak[key] = max(peak[key], val)
        output = _disk(row["output"])
        if output["raced_entries"]:
            sample_gaps.add("owned-output-census-race")
        peak["owned_output_bytes"] = max(peak["owned_output_bytes"], output["logical_bytes"], output["allocated_bytes"])
        for key in ("stdout_bytes", "stderr_bytes"):
            peak[key] = max(peak[key], output[key])
        if not isinstance(row["gaps"], list) or not all(isinstance(gap, str) for gap in row["gaps"]):
            raise InvalidEvidence("invalid resource sample gaps")
        sample_gaps.update(row["gaps"])
        _tools(row["tools"], processes)
    if type(obs["sample_count"]) is not int or type(obs["fork_events"]) is not int or obs["sample_count"] != count or obs["fork_events"] != forks or obs["peak"] != peak:
        raise InvalidEvidence("resource summary differs from raw samples")
    if obs["target_exec_observed_after_seconds"] != exec_observed:
        raise InvalidEvidence("target exec summary differs from raw lifecycle proof")
    if obs["observer_callback_wall_seconds"] != callback_seconds:
        raise InvalidEvidence("reported observer callback time differs from raw sample intervals")
    if not finalized:
        raise InvalidEvidence("resource final output census absent")
    from .resource_publication import validate as validate_publication
    publication = validate_publication(Path(manifest_path).parent, manifest, receipt, binding)
    _stamp(publication["observed_at"])
    if _stamp(publication["observed_at"]) < _stamp(receipt["finished_at"]):
        raise InvalidEvidence("final publication census precedes complete capture metadata")
    peak["owned_output_bytes"] = max(peak["owned_output_bytes"], publication["owned"]["logical_bytes"], publication["owned"]["allocated_bytes"])
    if publication["owned"]["raced_entries"]:
        sample_gaps.add("owned-output-census-race")
    limits = expected_policy["limits"]
    violations = []
    for metric, key in (("owned_bytes", "owned_output_bytes"), ("stdout_bytes", "stdout_bytes"),
                        ("stderr_bytes", "stderr_bytes"), ("nofile", "sampled_per_process_fds")):
        if peak[key] > limits[metric]:
            violations.append({"metric": metric, "observed": peak[key], "limit": limits[metric], "overshoot": peak[key] - limits[metric]})
    if not isinstance(obs["coverage_gaps"], list) or not all(isinstance(gap, str) for gap in obs["coverage_gaps"]) or not sample_gaps <= set(obs["coverage_gaps"]) | {"missing-fd-counter"}:
        raise InvalidEvidence("resource coverage gaps hidden")
    gaps = set(obs["coverage_gaps"]) | sample_gaps
    if forks:
        gaps.add("fork-descendant-lifetimes-not-atomically-tracked")
    if receipt["timed_out"] or obs["wall_deadline_overshoot_seconds"] > 0:
        gaps.add("wall-deadline-exceeded")
    if bridge is None or count == 0 or obs["bridge_released_after_seconds"] is None:
        gaps.add("incomplete-bridge-or-sampling")
    if not target_live:
        gaps.add("no-live-target-rss-sample")
    if expected_policy["mode"] != "comparison":
        gaps.add("fixture-only-policy")
    status, sampled_status, sampled_gaps = _resource_statuses(receipt, gaps, violations)
    return {"status": status, "sampled_resource_status": sampled_status, "sampled_coverage_gaps": sorted(sampled_gaps),
            "violations": violations, "coverage_gaps": sorted(gaps), "peak": peak,
            "sample_count": count, "target_sample_count": target_count, "sampled_target_tree_rss_bytes": target_tree_peak,
            "fork_events": forks, "largest_sample_gap_seconds": max(intervals, default=None),
            "observer_callback_wall_seconds": obs["observer_callback_wall_seconds"],
            "execution_seconds": obs["execution_seconds"], "cleanup_seconds": obs["cleanup_seconds"],
            "publication_seconds": publication["elapsed_seconds"], "retained_owned_output": publication["owned"],
            "wall_deadline_overshoot_seconds": obs["wall_deadline_overshoot_seconds"],
            "lifetime_complete": False, "kernel_tree_peak": False, "full_tree_ceiling_status": "MISSING",
            "reason": "sampled owned counters; no lifetime-complete or hard byte-ceiling claim"}
