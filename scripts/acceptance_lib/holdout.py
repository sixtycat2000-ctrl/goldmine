"""Disk-only replay of ONE completed source-bound final joint holdout audit."""
from pathlib import Path
import sqlite3
import sys

from .evidence import InvalidEvidence, external_ref, process_status, read_command_run, read_json, sha256, verify_artifact
from .ir_checks import _target_file


def check(root, inputs, targets):
    try:
        from answer_eval.protocol import EvaluationError, fields, timestamp
        from holdout_lib.ledger import verify_chain
        from holdout_lib.protocol import SUITE, files, load_freeze
        from holdout_lib.seal import check_seal
        from holdout_lib.validation import controls, report_bindings, validate_usage
        from product_eval.gates import check_targets, compare_reference
        from product_eval.runner import production_content

        root = Path(root).resolve()
        manifest_path = inputs.file("product.sealed_holdout_audit", manifest=True)
        manifest, receipt, stdout = read_command_run(manifest_path, suite_id=SUITE, check_id="release.holdout")
        status, reason = process_status(receipt)
        if status != "PASS":
            return status, {"reason": reason, "release_eligible": False,
                            "exposure_policy": "A failed/crashed audit remains spent; never reset the ledger."}
        argv = receipt["argv"]
        if (len(argv) != 9 or Path(argv[0]).resolve() != Path(sys.executable).resolve()
                or Path(argv[1]).resolve() != root / "scripts/eval_joint_holdout.py" or Path(receipt["cwd"]).resolve() != root):
            raise InvalidEvidence("joint receipt must invoke the fixed evaluator directly with the registered Python")
        freeze_path = Path(argv[3]).resolve()
        frozen, bundle, order = load_freeze(freeze_path, root)
        if argv != argv[:2] + ["--freeze", str(freeze_path), "--ledger", frozen["ledger"]["path"], "--out", str(manifest_path.parent / "results"), "--json"]:
            raise InvalidEvidence("joint receipt arguments/output differ from the fixed one-audit invocation")
        _target_file(root, targets)
        if (inputs.file("product.source_manifest").resolve() != bundle.root / "manifest.json"
                or inputs.file("product.queries").resolve() != bundle.root / bundle.manifest["queries"]["path"]
                or inputs.file("product.splits").resolve() != bundle.root / bundle.manifest["splits"]["path"]
                or inputs.file("models.embedding") != external_ref(freeze_path.parent, frozen["model"])
                or inputs.file("product.exposure_ledger") != Path(frozen["ledger"]["path"]) / "holdout_exposure.jsonl"):
            raise InvalidEvidence("joint audit source/question/fold/model/ledger differs from the registered release inputs")
        identity = {"evaluator_files": files(root), "freeze_sha256": sha256(freeze_path), "candidate_source": frozen["candidate_source"],
                    "model_sha256": frozen["model"]["sha256"], "server_sha256": frozen["server"]["sha256"],
                    "index_sha256": frozen["candidate"]["index"]["sha256"]}
        if manifest["identity"] != identity:
            raise InvalidEvidence("joint command lacks exact final freeze/evaluator/source/native/index bindings")
        if set(receipt["artifacts"]) != {"joint"}:
            raise InvalidEvidence("joint capture requires its actual completed raw audit artifact")
        audit_path = verify_artifact(manifest_path.parent, receipt["artifacts"]["joint"])
        if audit_path != manifest_path.parent / "results/joint.json":
            raise InvalidEvidence("joint audit artifact escaped the fixed output path")
        audit = read_json(audit_path)
        fields(audit, {"schema", "kind", "freeze_sha256", "production", "exposure", "arms"}, "joint raw audit")
        if (audit["schema"] != 1 or audit["kind"] != "goldmine.joint-holdout-audit/v1"
                or audit["freeze_sha256"] != sha256(freeze_path) or set(audit["arms"]) != set(order)
                or production_content(audit["production"]) != production_content(frozen["candidate_source"])):
            raise InvalidEvidence("joint raw audit source/arms/freeze differ")
        if read_json(stdout) != {"status": "COMPLETE", "audit_sha256": sha256(audit_path), "arms": order, "release_eligible": False}:
            raise InvalidEvidence("joint stdout is not the exact complete raw-artifact receipt")
        ledger = verify_chain(frozen, order, audit)
        if (timestamp(receipt["started_at"], "joint command start") > timestamp(ledger["exposure_at"], "joint exposure")
                or timestamp(receipt["finished_at"], "joint command finish") < timestamp(ledger["completed_at"], "joint completion")
                or timestamp(frozen["frozen_at"], "joint freeze") > timestamp(receipt["started_at"], "joint command start")):
            raise InvalidEvidence("joint freeze/exposure/query/completion command ordering differs")
        reference, reference_run_id = check_seal(frozen, freeze_path.parent, root, open_report=True)
        readbacks = {"reference": report_bindings(reference, frozen, bundle, arm="reference", root=root, exposure=None)}
        reports, usage = {}, {}
        for arm in order:
            refs = audit["arms"][arm]
            fields(refs, {"report", "usage"}, "joint arm artifacts")
            report_path = verify_artifact(audit_path.parent, refs["report"])
            usage_path = verify_artifact(audit_path.parent, refs["usage"])
            if report_path != audit_path.parent / (arm + ".json") or usage_path != audit_path.parent / (arm + "-usage.json"):
                raise InvalidEvidence("joint arm raw artifact paths differ from its registered names")
            report = read_json(report_path)
            readbacks[arm] = report_bindings(report, frozen, bundle, arm=arm, root=root, exposure=audit["exposure"])
            reports[arm] = report
            usage[arm] = validate_usage(read_json(usage_path), report, bundle, frozen["arms"][arm]["parameters"])
            if report["backend_identity"] != reference["backend_identity"]:
                raise InvalidEvidence("joint original/candidate real embedding identities differ")
        gate = check_targets(reports["fast"], root / "evals/acceptance_targets.json")
        failed, control_details = controls(reports, usage, frozen, targets)
        failures = gate["failed"] + failed
        return ("FAIL" if failures else "PASS"), {"scope": "one_final_joint_holdout_audit", "release_eligible": not failures,
            "failed": failures, "ledger": ledger, "readback_atoms": readbacks, "controls": control_details,
            "fast_targets": gate, "paired_original": compare_reference(reference, reports["fast"]),
            "capture_run_id": manifest["run_id"], "reference_seal_run_id": reference_run_id}
    except (InvalidEvidence, ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
        return "MISSING", {"reason": str(error), "release_eligible": False,
                           "exposure_policy": "Missing evidence never authorizes a new exposure or ledger reset."}
