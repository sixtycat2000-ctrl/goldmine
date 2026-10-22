"""Trusted check registration and explicit test-profile migration.

Input manifests can reference results, never supply commands or required checks.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json


@dataclass(frozen=True)
class Check:
    id: str
    kind: str
    argument: str = ""
    remediation: str = "Run the registered check and retain its raw evidence."


NATIVE_MODULES = {
    "test_extract_pdf.py": "Real poppler extraction; tool absence verified separately.",
    "test_image_prep.py": "Real sips image preparation and EXIF.",
    "test_media.py": "Real ffmpeg/ffprobe window generation.",
    "test_pipeline.py": "Mixed ingest fixtures exercise real sips/poppler/ffmpeg.",
    "test_cli.py": "CLI ingest fixtures include real native media conversion.",
    "test_coverage.py": "Corpus builders exercise native media/PDF fixtures.",
    "test_eval_harness.py": "Real multiformat fixture extraction, mock embeddings.",
    "test_resource_observer_native.py": "Real Darwin ps/lsof/kqueue counters and owned tiny-process cleanup.",
}
TEST_PROFILES = {
    "tests/test_eval_real.py::test_real_jsonl_validates_against_manifest": "assets",
    "tests/test_eval_real.py::test_end_to_end_mock_subset": "assets",
    "tests/test_output.py::test_fzf_widget_file_exists_and_parses": "native",
    "tests/test_ocr.py::test_live_real_helper_handshake_and_ocr": "live",
    **{f"tests/test_asr.py::{name}": "native" for name in (
        "test_transcribe_canned_stub", "test_transcribe_timeout_raises",
        "test_transcribe_nonzero_rc_raises", "test_pipeline_asr_transcript_segments_with_vectors",
        "test_pipeline_whisper_missing_falls_back", "test_pipeline_asr_off_keeps_v1_path",
        "test_pipeline_asr_always_failure_is_failed_row", "test_pipeline_asr_timeout_is_failed_row",
        "test_pipeline_asr_always_no_speech_is_metadata", "test_pipeline_asr_max_minutes_partial")},
    **{f"tests/test_migrations.py::{name}": "native" for name in (
        "test_doctor_deep_rows", "test_doctor_deep_flags_staleness_and_cards_drift",
        "test_doctor_deep_without_index_is_a_warn_not_a_crash")},
}
LIVE_TESTS = {
    f"live-{suite}-{pressure}": (f"test_live_{suite}.py",)
    for suite in ("watch", "serve") for pressure in ("controlled", "real")
}
LIVE_TESTS["live-pack"] = ("test_live_pack.py",)


def classify(nodeid: str, markers=()) -> str:
    """Every original node has a declared owner; moved tests are never removed."""
    short = nodeid.split("[")[0]
    if short in TEST_PROFILES:
        return TEST_PROFILES[short]
    name = nodeid.split("::")[0].rsplit("/", 1)[-1]
    if name == "test_foresight_offline.py":
        return "optional-foresight"
    if name == "test_mcp.py" or "optional_mcp" in markers:
        return "optional-mcp"
    if name.startswith("test_live_") or "live" in markers:
        return "live"
    if name in NATIVE_MODULES or "native" in markers:
        return "native"
    if "assets" in markers:
        return "assets"
    return "offline"


TASK_TESTS = {
    "P0-01": ("test_eval_entrypoints.py",),
    "P0-02": ("test_eval_answer.py", "test_answer_calibration.py", "test_answer_grader_context.py",
              "test_answer_preparation.py"),
    "P0-03": ("test_answer_compare.py", "test_eval_answer.py", "test_answer_grader_context.py",
              "test_answer_preparation.py"),
    "P0-04": ("test_eval_real.py", "test_coverage.py", "test_eval_harness.py"),
    "P0-05": ("test_product_corpus.py", "test_eval_product.py", "test_product_alignment.py",
              "test_product_materialization.py", "test_product_reference_support.py", "test_product_statistics.py"),
    "P0-06": ("test_acceptance_runner.py", "test_ir_acceptance.py", "test_ir_original_readback.py",
              "test_public_raw.py", "test_joint_holdout.py", "test_joint_holdout_freeze.py"),
    "P1-01": ("test_application.py", "test_cli.py", "test_pack.py"),
    "P1-02": ("test_lexical_cjk.py", "test_lexical_migration.py", "test_lexical_backfill_traversal.py"),
    "P1-03": ("test_exact_lookup.py", "test_migrations.py", "test_store.py"),
    "P1-04": ("test_pdf_evidence.py", "test_extract_pdf.py", "test_pack.py",
              "test_chunker_paragraph_conservation.py", "test_chunker_utf8_boundaries.py",
              "test_chunker_char_conservation.py",
              "test_evidence_selection.py", "test_pack_allocation.py",
              "test_pack_contiguous_support.py", "test_pack_fold_provenance.py"),
    "P1-05": ("test_media_evidence.py", "test_asr.py", "test_ocr.py", "test_exif.py"),
    "P1-06": ("test_search_admission.py", "test_anchor_contract.py", "test_anchor_boundaries.py"),
    "P1-07": ("test_rerank.py",),
    "P1-08": ("test_query_plan.py",),
    "P1-09": ("test_mcp.py",),
    "P1-10": ("test_pack_contract.py", "test_agent_workflow.py", "test_pack.py",
              "test_pack_allocation.py",
              "test_pack_contiguous_support.py", "test_pack_fold_provenance.py", "test_answer_grader_context.py",
              "test_answer_preparation.py"),
    "P2-01": ("test_module_boundaries.py",),
    "P2-02": ("test_scale_bench.py", "test_scale_acceptance.py", "test_scale_protocol.py", "test_bench.py",
              "test_lexical_backfill_traversal.py"),
    "P2-03": ("test_pipeline.py", "test_watch.py", "test_serve.py", "test_v2_identity.py"),
    "P2-04": ("test_distill.py", "test_eval_distill.py", "test_distill_real_eval.py",
              "test_distill_preparation.py", "test_distill_evidence_boundaries.py", "test_distill_source_identity.py"),
    "P2-05": ("test_ci_contract.py", "test_hosted_ci.py", "test_acceptance_runner.py", "test_distribution.py"),
    "P2-06": ("test_distribution.py",),
    "P2-07": ("test_documentation.py",),
    "P2-08": ("test_compare_search.py", "test_comparison_source_views.py",
              "test_comparison_measurements.py",
              "test_measurement_protocol.py", "test_measurement_state.py",
              "test_measurement_execution.py", "test_measurement_updates.py",
              "test_measurement_slots.py",
              "test_comparison_engines.py", "test_comparison_report_framing.py",
              "test_comparison_engine_resources.py",
              "test_comparison_resource_dispositions.py", "test_comparison_native_resolvers.py",
              "test_resource_policy.py", "test_resource_observer_native.py"),
}

CHECKS = {c.id: c for c in (
    Check("history", "history", remediation="Restore original historical bytes; never regenerate baselines."),
    Check("plan", "plan", remediation="Reconcile tasks/contracts/targets without lowering quality targets."),
    Check("source.stable", "source", remediation="Freeze source changes while capturing authoritative acceptance evidence."),
    Check("pytest.offline", "pytest", "offline"),
    Check("pytest.assets", "pytest", "assets"),
    Check("pytest.native", "pytest", "native"),
    Check("pytest.optional_mcp", "pytest", "optional-mcp", "Install the locked MCP extra and run its real protocol tests."),
    Check("pytest.optional_foresight", "pytest", "optional-foresight", "Install the Foresight extra for the in-process SDK tests."),
    Check("assets.embedding", "file", "models.embedding", "Provide the pinned local embedding file and SHA."),
    Check("assets.public", "public_assets", "public.cache_manifest", "Prepare verified BEIR zips explicitly; do not download during acceptance."),
    Check("retrieval.legacy", "paired", "legacy"),
    Check("retrieval.full_media", "paired", "full_media"),
    Check("retrieval.product_dev", "ir", "product", "Provide complete product.v4 source-bound raw results; diagnostic seed is insufficient."),
    Check("retrieval.public", "ir", "public", "Capture separate real prepare/score receipts for both source arms; bind finalized indexes and all four raw rankings to pinned ZIP qrels."),
    Check("latency.fast", "latency", "evidence.latency"),
    Check("agent.answer", "ir", "answer", "Provide the complete source-bound v3 answer jobs and frozen preregistration."),
    Check("agent.pack", "pytest", "live-pack", "Run real tokenizer/budget/citation mechanics; answer quality remains a separate gate."),
    Check("latency.mcp", "mcp_latency", "evidence.mcp_latency", "Collect paired direct/stdio samples and verify equal results."),
    Check("capacity.scale", "scale", "evidence.scale", "Capture all12 synthetic native-schema cases with source-bound phase and actual cold/warm CLI+pack evidence; vector-only reports are incomplete."),
    Check("release.holdout", "holdout", "product.sealed_holdout_audit", "Freeze the exact final arm registry, keep original seal private, consume one prior0 exposure, and replay the complete joint raw audit."),
    Check("release.distribution", "distribution", "evidence.distribution", "Run wheel/sdist/native isolated installation and retain raw results."),
    Check("release.watch", "lifecycle", "watch", "Run true guard/duty/crash-recovery checks; no live skip."),
    Check("release.serve", "lifecycle", "serve", "Run resident/crash/resource checks with owned processes."),
    Check("release.distill", "distill", "evidence.distill", "Score real frozen-topic documents; mock purity is insufficient."),
    Check("release.docs", "documentation", "evidence.docs", "Run literal offline quickstart/installed helps and bind current docs/distribution evidence."),
    Check("release.ci_local", "ci_local", "evidence.ci_local", "Run CI's exact scripts and four failure-injection cases locally."),
    Check("release.ci_hosted", "ci_hosted", "evidence.ci_hosted", "Collect one explicit actual GitHub ci run, safe raw artifacts, and replay with live metadata revalidation against clean committed HEAD."),
)}
OFFLINE = ("history", "plan", "pytest.offline")
RETRIEVAL = OFFLINE + ("assets.embedding", "assets.public", "pytest.native",
    "retrieval.legacy", "retrieval.full_media", "retrieval.product_dev",
    "retrieval.public", "latency.fast")
AGENT = RETRIEVAL + ("pytest.optional_mcp", "agent.answer", "agent.pack", "latency.mcp")
LOCAL_RC = AGENT + ("capacity.scale", "release.holdout", "release.distribution", "release.watch",
    "release.serve", "release.distill", "release.docs", "release.ci_local")
PROFILES = {
    "offline": OFFLINE,
    "assets": ("assets.embedding", "assets.public", "pytest.assets"),
    "native": ("pytest.native",),
    "optional-mcp": ("pytest.optional_mcp",),
    "optional-foresight": ("pytest.optional_foresight",),
    "retrieval": RETRIEVAL, "agent": AGENT, "local_rc": LOCAL_RC,
    "hosted_release": LOCAL_RC + ("release.ci_hosted",),
    "task-check": (),
}
# Dev evidence requirements are explicit, not the full future release gate.
TASK_EVIDENCE = {
    "P0-04": ("retrieval.full_media",),
    "P0-05": ("retrieval.product_dev",),
    **{f"P1-{n:02}": ("retrieval.product_dev",) for n in range(2, 9)},
    "P1-10": ("agent.answer", "agent.pack"),
    "P2-02": ("capacity.scale",),
    "P2-03": ("release.watch", "release.serve"),
    "P2-04": ("release.distill",),
    "P2-05": ("release.ci_local",),
    "P2-06": ("release.distribution",),
    "P2-07": ("release.docs",),
}


def expand(profile: str, task: str | None = None) -> tuple[Check, ...]:
    canonical = "local_rc" if profile == "release" else profile
    if canonical not in PROFILES:
        raise ValueError(f"unknown profile: {profile}")
    if task is not None and task not in TASK_TESTS:
        raise ValueError(f"unknown task: {task}")
    if canonical == "task-check" and task is None:
        raise ValueError("task-check requires --task")
    checks = [CHECKS[c] for c in PROFILES[canonical]]
    if task:
        checks.append(Check(f"task.{task}", "pytest", task))
        checks.extend(CHECKS[c] for c in TASK_EVIDENCE.get(task, ()))
    checks.append(CHECKS["source.stable"])
    return tuple({c.id: c for c in checks}.values())


def fingerprint() -> str:
    state = {"profiles": PROFILES, "checks": [asdict(c) for c in CHECKS.values()],
             "task_tests": TASK_TESTS, "task_evidence": TASK_EVIDENCE,
             "native_modules": NATIVE_MODULES, "test_profiles": TEST_PROFILES,
             "live_tests": LIVE_TESTS}
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
