"""Same-script local CI evidence, independently of any hosted run claim."""
from pathlib import Path

from .evidence import InvalidEvidence, candidate_content, external_ref, read_command_run, read_json, sha256, verify_artifact
from .pytest_checks import junit_results, validate_pytest


def source_files(root):
    return candidate_content(root)


def verify(path):
    from .runner import validate

    root = Path(__file__).resolve().parents[2]
    bundle = read_json(path)
    if (set(bundle) != {"schema", "kind", "evidence", "counterexamples", "actionlint"} or
            bundle["schema"] != 1 or bundle["kind"] != "goldmine.ci-local"):
        raise InvalidEvidence("local CI needs fixed schema1 evidence, not a claimed hosted status")
    required = {"base": "offline", "native": "native", "mcp": "optional-mcp"}
    if set(bundle["evidence"]) != set(required):
        raise InvalidEvidence("local CI lacks one of base/native/MCP scopes")
    current = source_files(root)
    reports = {}
    for scope, profile in required.items():
        context_path = external_ref(path.parent, bundle["evidence"][scope], manifest=True)
        context = read_json(context_path)
        if context.get("profile") != scope or context.get("before") != current or context.get("after") != current:
            raise InvalidEvidence("local CI source changed or differs from candidate")
        report_path = verify_artifact(context_path.parent, context["report"])
        report = validate(root, report_path)
        if report["profile"] != profile or report["task_id"] is not None:
            raise InvalidEvidence("local CI report uses a different scope")
        if report["status"] != "PASS":
            return report["status"], {"scope": scope, "reason": "required scoped CI checks are not complete"}
        reports[scope] = {"profile": profile, "report_sha256": sha256(report_path)}
    counter = bundle["counterexamples"]
    execution = external_ref(path.parent, counter["command"], manifest=True)
    collection = external_ref(path.parent, counter["collection"], manifest=True)
    status, details = validate_pytest(root, execution, collection, "P2-05")
    if status != "PASS":
        return status, {"counterexamples": details}
    _, receipt, _ = read_command_run(execution)
    outcomes = junit_results(verify_artifact(execution.parent, receipt["artifacts"]["junit"]))
    names = {name.split("[")[0] for name in outcomes}
    negative = {
        "tests/test_acceptance_runner.py::test_stdout_pass_and_skip_cannot_turn_green",
        "tests/test_acceptance_runner.py::test_zero_collection_is_missing",
        "tests/test_distribution.py::test_wheel_missing_command_is_rejected_before_source_can_mask_it",
        "tests/test_ci_contract.py::test_live_ci_needs_existing_inputs_and_creates_no_green_report",
    }
    if not negative.issubset(names):
        raise InvalidEvidence("one of four CI failure-injection cases disappeared")
    lint_path = external_ref(path.parent, bundle["actionlint"], manifest=True)
    _, lint, _ = read_command_run(lint_path, suite_id="ci.actionlint.v1")
    expected = {str((root / ".github/workflows" / name).resolve()) for name in ("ci.yml", "live-validation.yml", "build-release.yml")}
    if lint["exit_code"] or lint["timed_out"] or not expected.issubset(set(lint["argv"])):
        raise InvalidEvidence("registered workflows did not pass actual actionlint")
    return "PASS", {"local_scopes": reports, "counterexamples": details["n"], "hosted_verification": "MISSING"}


def check(inputs, targets):
    return verify(inputs.file("evidence.ci_local", manifest=True))
