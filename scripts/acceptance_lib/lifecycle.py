"""Replay required controlled and real-pressure lifecycle tests from raw JUnit."""
from pathlib import Path

from .evidence import InvalidEvidence, read_command_run, read_json, sha256, verify_artifact
from .pytest_checks import validate_pytest
from .runner import code_identity


def check(inputs, suite):
    root = Path(__file__).resolve().parents[2]
    actual = code_identity(root)
    details, statuses = {}, []
    for profile, key in (("controlled", f"evidence.{suite}"), ("real", f"evidence.{suite}_real")):
        path = inputs.file(key, manifest=True)
        data = read_json(path)
        expected = f"live-{suite}-{profile}"
        if data.get("schema") != 1 or data.get("kind") != "goldmine.live-lifecycle" or data.get("profile") != expected:
            raise InvalidEvidence("lifecycle report has wrong registered profile/schema")
        identity = data.get("identity", {})
        if identity.get("content_sha256") != actual["content_sha256"] or identity.get("pressure_profile") != profile:
            raise InvalidEvidence("lifecycle report uses stale source or wrong pressure override")
        if identity.get("model_sha256") != sha256(inputs.file("models.embedding")):
            raise InvalidEvidence("lifecycle model differs from pinned embedding asset")
        for name in ("environment", "environment_after"):
            if data.get(name, {}).get("pressure", {}).get("level") is None:
                raise InvalidEvidence("lifecycle did not measure actual kernel pressure")
        evidence = data.get("evidence", {})
        execution = verify_artifact(path.parent, evidence["command"])
        collection = verify_artifact(path.parent, evidence["collection"])
        status, result = validate_pytest(root, execution, collection, expected)
        statuses.append(status)
        details[profile] = result
    if suite == "watch":
        from search_bench.session import package_identity

        raw = inputs.file("evidence.ingest", manifest=True)
        _, receipt, stdout = read_command_run(raw, suite_id="resources.ingest.v1")
        measured = read_json(stdout)
        if receipt["exit_code"] not in (0, 1) or measured.get("kind") != "goldmine.repeated-ingest":
            raise InvalidEvidence("repeated-ingest real resource run incomplete")
        if measured.get("source_files") != package_identity(root) or measured.get("evidence_mode") != "real":
            raise InvalidEvidence("repeated-ingest report uses different source or synthetic evidence")
        phases = measured.get("phases", [])
        if [r.get("phase") for r in phases] != ["first", "unchanged", "modified_one", "injected_embedding_failure", "recovery", "deleted_one"]:
            raise InvalidEvidence("repeated-ingest lifecycle stages missing")
        initial, unchanged = phases[:2]
        if not measured.get("media_and_cards_exercised") or any(initial["measurements"]["calls"].get(k, 0) <= 0 for k in ("embed_texts", "ocr", "asr")):
            raise InvalidEvidence("real OCR/ASR/embed/cards were not all exercised")
        expensive = ("embed_texts", "embed_image", "embed_audio_wav", "ocr", "asr", "extract")
        required_gates = {"unchanged_no_expensive_calls", "unchanged_exact_stored_content", "initial_success", "modified_exactly_one",
                          "controlled_failure_occurred", "atomic_failure_policy", "recovery_success", "deleted_exactly_one"}
        if set(measured.get("gates", {})) != required_gates:
            raise InvalidEvidence("repeated-ingest result omitted one of the fixed acceptance gates")
        passed = (all(unchanged["measurements"]["calls"].get(k, 0) == 0 for k in expensive) and
                  unchanged["before"] == unchanged["after"] and all(value is True for value in measured["gates"].values()))
        statuses.append("PASS" if passed else "FAIL")
        details["repeated_ingest"] = {"unchanged_calls": unchanged["measurements"]["calls"], "gates": measured["gates"]}
    return ("FAIL" if "FAIL" in statuses else "MISSING" if "MISSING" in statuses else "PASS"), details
