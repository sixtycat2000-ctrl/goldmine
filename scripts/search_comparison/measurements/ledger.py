"""One adopted Commands owner; explicit events retain failures and all clocks."""
from contextlib import contextmanager
from pathlib import Path
import sys

from acceptance_lib.evidence import read_command_run
from answer_eval.protocol import EvaluationError, parse_json, sha256_file
from search_comparison.engine_capture.commands import Commands
from search_comparison.engine_capture.protocol import SUITE, reference, read
from search_comparison.source_views.run import write_new
from .filesystem import SUITE as STATE_SUITE


class Ledger:
    def __init__(self, owner, env, context, kind, *, replay=None, fixture=False):
        self.owner, self.kind, self.replay = Path(owner), kind, replay
        self.commands = Commands(owner, env, context, replay=replay["commands"] if replay else None, fixture=fixture)
        self.events, self.requests = [], []

    @contextmanager
    def bound(self, value):
        prior = self.commands.context
        self.commands.context = {**prior, "measurement_operation": value}
        try:
            yield
        finally:
            self.commands.context = prior

    def call(self, label, argv, *, phase="audit", binding=None):
        before = len(self.commands.resources)
        raw, error = None, None
        try:
            with self.bound(binding):
                raw = self.commands.call(label, argv, phase=phase)
        except (EvaluationError, OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            error = str(exc)
        disposition = self.commands.resources[-1] if len(self.commands.resources) > before else None
        ref = disposition.get("command") if disposition else None
        clock = None
        if ref is not None:
            try:
                _, receipt, _ = read_command_run(reference(ref), check_id="comparison.command." + label, suite_id=SUITE)
                observation = disposition.get("observation", {})
                clock = {"wall_ms": receipt["wall_ms"], "exit_code": receipt["exit_code"], "timed_out": receipt["timed_out"],
                         "started_at": receipt["started_at"], "finished_at": receipt["finished_at"],
                         "resource_execution_seconds": observation.get("execution_seconds"),
                         "resource_observation": observation}
            except (EvaluationError, OSError, ValueError, TypeError, KeyError) as exc:
                error = "raw receipt unavailable: " + str(exc)
        event = {"ordinal": len(self.events) + 1, "label": label, "phase": phase, "binding": binding,
                 "command": ref, "disposition": disposition, "clock": clock, "error": error}
        self.events.append(event)
        if error is not None:
            raise EvaluationError(error, status="MISSING")
        return raw

    def state(self, operation, arguments, *, binding=None):
        value = {"schema": 1, "suite_id": STATE_SUITE, "owner": str(self.owner),
                 "operation": operation, "kind": self.kind, "arguments": arguments}
        path = self.owner / "requests" / f"{len(self.requests) + 1:05d}.json"
        if self.replay is None:
            path.parent.mkdir(exist_ok=True)
            write_new(path, value)
        elif read(path) != value:
            raise EvaluationError("typed state request differs from reconstructed fixed operation")
        ref = {"path": str(path), "sha256": sha256_file(path)}
        self.requests.append(ref)
        argv = [str(Path(sys.executable).absolute()), "-I", "-S", "-B", str(Path(__file__).with_name("_state_worker.py")), str(path), ref["sha256"]]
        raw = self.call("measurement.state." + operation, argv, binding={"typed_request": ref, "purpose": binding})
        result = parse_json(raw.decode("utf-8"), "typed state worker stdout")
        if (set(result) != {"schema", "request_sha256", "operation", "result"} or result["schema"] != 1
                or result["request_sha256"] != ref["sha256"] or result["operation"] != operation):
            raise EvaluationError("state worker response/request identity differs")
        return result["result"]

    def finish(self):
        commands = self.commands.finish()
        if {str(p) for p in (self.owner / "requests").glob("*.json")} != {r["path"] for r in self.requests}:
            raise EvaluationError("typed request registry omits/unregisters files")
        value = {"commands": commands, "resource_observations": self.commands.resources,
                 "resource_status": self.commands.resource_status(), "events": self.events, "requests": self.requests,
                 "interrupted": self.commands.interrupted}
        if self.replay is not None and value != {k: self.replay[k] for k in value}:
            raise EvaluationError("attempts, clocks, failures or resource dispositions differ from raw replay")
        return value
