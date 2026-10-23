"""One reviewed lifecycle boundary; disk replay checks the exact full context."""
from __future__ import annotations

from pathlib import Path

from acceptance_lib.evidence import capture_command, read_command_run
from answer_eval.protocol import EvaluationError, sha256_file, timestamp
from .protocol import LIMITS, SUITE, reference
from . import resources

VISIBLE_ENV = ("GOLDMINE_HOME", "GOLDMINE_CONFIG", "GOLDMINE_LIVE", "GOLDMINE_OCR_AUTOCOMPILE", "GOLDMINE_ACCEPTANCE_HERMETIC",
               "GOLDMINE_SERVE_PRESSURE_LEVEL", "GOLDMINE_MODEL", "GOLDMINE_LLAMA_SERVER", "GOLDMINE_SERVE", "PYTHONPATH", "PIP_NO_INDEX",
               "PIP_FIND_LINKS", "PYTHONNOUSERSITE")


class Commands:
    def __init__(self, owner, env, context, *, replay=None, fixture=False):
        self.owner, self.env, self.context = Path(owner), env, context
        self.replay, self.offset, self.rows = replay, 0, []
        self.interrupted = False
        self.fixture, self.resources, self.blocked = fixture, [], False
        self.counters = {"prepare": 0, "queries": 0, "audit": 0}

    def call(self, label, argv, *, phase="prepare"):
        if self.blocked:
            raise EvaluationError("resource validation failed or budget violated; no subsequent child in this owned arm", status="MISSING")
        if phase not in self.counters or not isinstance(argv, list) or any(not isinstance(v, str) or "\x00" in v for v in argv):
            raise EvaluationError("invalid registered phase/argv")
        self.counters[phase] += 1
        out = self.owner / phase / f"{self.counters[phase]:04d}"
        identity = {**self.context, "environment": self.env, "phase": phase, "label": label,
                    "resource_contract": "observer003-sampled-private-session-v1"}
        check = "comparison.command." + label
        ref = None
        disposition = {"expected_manifest": str(out / "manifest.json"), "phase": phase, "label": label}
        try:
            expected = resources.expected_policy(self.owner, phase, identity, fixture=self.fixture)
            timeout = expected["wall_seconds"]
            if self.replay is None:
                capture_command(out, check, argv, cwd=self.owner / "cwd", env=self.env, identity=identity,
                                suite_id=SUITE, timeout=timeout, owned_process_group=True, resource_policy=expected)
                ref = {"path": str(out / "manifest.json"), "sha256": sha256_file(out / "manifest.json")}
            else:
                if self.offset >= len(self.replay):
                    raise EvaluationError("raw command registry omitted an attempted phase", status="MISSING")
                ref = self.replay[self.offset]
            self.offset += 1
            self.rows.append(ref)
            path = reference(ref)
            if path != out / "manifest.json":
                raise EvaluationError("raw command path/order differs from generated ordinal")
            manifest, receipt, stdout = read_command_run(path, check_id=check, suite_id=SUITE)
            observed_identity = {k: v for k, v in manifest["identity"].items() if k != "resource_observer"}
            if (observed_identity != identity or receipt["cwd"] != str(self.owner / "cwd")
                    or receipt["env"] != {k: self.env[k] for k in VISIBLE_ENV if k in self.env}):
                raise EvaluationError("actual command argv/cwd/env/source context differs", status="MISSING")
            if timestamp(receipt["started_at"], "command start") < timestamp(self.context["registered_at"], "profile registration"):
                raise EvaluationError("engine command predates the frozen configuration")
            if receipt["exit_code"] in (130, 143, -2, -15):
                self.interrupted = True
            observation = resources.validate(path, expected, argv, identity)
        except (EvaluationError, OSError, ValueError, TypeError, KeyError) as exc:
            # No synthetic observer result or placeholder SHA: a missing raw
            # manifest is explicitly null. Every available raw ref is retained.
            self.resources.append({**disposition, "command": ref, "disposition": "resource_validation_error", "error": str(exc)})
            self.blocked = True
            raise EvaluationError("actual phase/resource validation failed: " + str(exc), status="MISSING") from exc
        self.resources.append({**disposition, "command": ref, "disposition": "validated", "observation": observation})
        self.blocked = observation.get("sampled_resource_status") == "FAIL"
        stderr = path.parent / receipt["stderr"]["path"]
        if stdout.stat().st_size > LIMITS["stream_bytes"] or stderr.stat().st_size > LIMITS["stream_bytes"]:
            raise EvaluationError("post-command accepted stream bound exceeded; this is not a live byte quota", status="MISSING")
        if receipt["exit_code"] != 0 or receipt["timed_out"]:
            raise EvaluationError("actual engine command failed/timed out; all raw streams retained", status="MISSING")
        if self.blocked:
            raise EvaluationError("observed resource budget violated; raw counters and outputs retained", status="MISSING")
        return stdout.read_bytes()

    def finish(self):
        if self.replay is not None and self.offset != len(self.replay):
            raise EvaluationError("unused/extra raw commands in fixed phase registry")
        actual = {str(path) for phase in self.counters for path in (self.owner / phase).glob("*/manifest.json")}
        declared = {r.get("path") for r in self.rows if isinstance(r, dict)}
        if actual != declared:
            raise EvaluationError("raw phase manifests omitted from the fixed attempted registry")
        return list(self.rows)

    def resource_status(self):
        return resources.status(self.resources, self.rows, attempted=sum(self.counters.values()))
