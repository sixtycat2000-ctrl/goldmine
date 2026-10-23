"""Verify a genuine original command seal without exposing its report early."""
from pathlib import Path
import stat

from acceptance_lib.evidence import external_ref, process_status, read_json, sha256
from acceptance_lib.ir_checks import _command, evaluator_files
from answer_eval.protocol import EvaluationError, timestamp


def check_seal(frozen, base, root, *, open_report=False):
    original = frozen["reference"]
    manifest_path = external_ref(base, original["seal_capture"], manifest=True)
    manifest, receipt, stdout = _command(manifest_path, root, kind="product", suite="product.v4", check_id="product.holdout_reference")
    status, reason = process_status(receipt)
    if status != "PASS":
        raise EvaluationError("original holdout sealing command incomplete: " + reason, status="MISSING")
    argv = receipt["argv"]
    if len(argv) != 25:
        raise EvaluationError("original seal needs the exact fixed direct command")
    parameters, output = Path(argv[17]).resolve(), manifest_path.parent / "sealed"
    expected = argv[:2] + ["--bundle", str(Path(frozen["bundle"]).resolve()), "--build", str(Path(original["build"]).resolve()),
        "--db", str(external_ref(base, original["index"])), "--production-root", str(Path(original["production_root"]).resolve()),
        "--backend", "llama", "--model", str(external_ref(base, frozen["model"])), "--server", str(external_ref(base, frozen["server"])),
        "--parameters", str(parameters), "--split", "holdout", "--reference-code", "--seal-reference", "--out", str(output), "--json"]
    if argv != expected or read_json(parameters) != original["parameters"]:
        raise EvaluationError("original seal command source/assets/parameters/output differ")
    sealed = external_ref(base, original["sealed_report"])
    if (sealed != (output / "run.json").resolve() or stat.S_IMODE(sealed.stat().st_mode) != 0o600
            or stat.S_IMODE(output.stat().st_mode) != 0o700):
        raise EvaluationError("original report must remain in its private sealed output directory")
    expected_seal = {"status": "SEALED_REFERENCE", "sha256": sha256(sealed), "bundle_identity": frozen["bundle_identity"],
                     "production_commit": original["production"]["git_commit"], "release_eligible": False, "metrics_exposed": False}
    if read_json(stdout) != expected_seal:
        raise EvaluationError("original stdout is not the exact non-metric seal of its private report")
    identity = {"evaluator_files": evaluator_files(root, "product"), "production_source": original["production"],
                "index_sha256": original["index"]["sha256"], "server_sha256": frozen["server"]["sha256"],
                "model_sha256": frozen["model"]["sha256"], "parameters_sha256": original["parameters_sha256"],
                "bundle_identity": frozen["bundle_identity"]}
    if manifest["identity"] != identity:
        raise EvaluationError("original seal lacks complete source/native/index/parameter bindings")
    if timestamp(receipt["finished_at"], "original seal completion") > timestamp(frozen["frozen_at"], "final freeze"):
        raise EvaluationError("original sealing must complete before the final joint freeze")
    return (read_json(sealed) if open_report else None), manifest["run_id"]
