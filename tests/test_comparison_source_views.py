"""Synthetic conversion receipts/coverage denial tests; no actual media or model."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from answer_eval.protocol import EvaluationError, sha256_file
from search_comparison.source_views import sources as source_api
from search_comparison.source_views.commands import Commands, _owned_path
from search_comparison.source_views.coverage import file_window, render, unit, validate_coverage
from search_comparison.source_views.recipes import RECIPES, compile_recipe, validate_config
from search_comparison.source_views.run import owned_output, _artifacts, capture_sources, implementation_identity, verify_capture
from comparison_source_fixtures import dump, make_fixture, synthetic_native_policy


@contextmanager
def changed(path, raw):
    before = path.read_bytes()
    path.write_bytes(raw)
    try:
        yield
    finally:
        path.write_bytes(before)


class SourceViews(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != "darwin":
            raise unittest.SkipTest(
                "synthetic capture fixtures materialize macOS screen-capture metadata")
        parent = os.environ.get("SOURCE_VIEW_MECHANICAL_ARTIFACTS")
        cls.temporary = None if parent else tempfile.TemporaryDirectory(prefix="comparison-source-mechanics-")
        cls.root = (Path(parent) if parent else Path(cls.temporary.name)).resolve()
        cls.root.mkdir(parents=True, exist_ok=True)
        cls.base = make_fixture(cls.root / "base")
        cls.out = cls.base["root"] / "owned '空 格-$(literal)'"
        cls.native_context = synthetic_native_policy()
        cls.flags = cls.native_context.__enter__()
        # A setUpClass failure must never leak the global materialize/read_flag
        # patches into later test modules; class cleanups run even then.
        cls.addClassCleanup(cls.native_context.__exit__, None, None, None)
        original = Path.open
        cls.opened = []
        def guarded(path, *args, **kwargs):
            cls.opened.append(str(path))
            if Path(path).name.startswith("NEVER-OPEN-"):
                raise AssertionError("query/split/audit/reference environment was opened")
            return original(path, *args, **kwargs)
        cls.guard = patch.object(Path, "open", guarded)
        cls.guard.start()
        cls.addClassCleanup(cls.guard.stop)
        if cls.temporary:
            cls.addClassCleanup(cls.temporary.cleanup)
        cls.report = capture_sources(cls.base["bundle"], cls.base["pin"], cls.base["config"], cls.out, cls.base["root"], fixture=True)

    def verify(self, fixture=None, output=None):
        f = fixture or self.base
        return verify_capture((output or self.out) / "report.json", f["bundle"], f["pin"], f["config"], fixture=True)

    def test_full_synthetic_capture_and_replay_are_explicitly_mechanical(self):
        self.assertEqual(self.report["status"], "MECHANICAL_CAPTURE_COMPLETE")
        self.assertFalse(self.report["quality_evidence"])
        self.assertFalse(self.report["release_eligible"])
        self.assertEqual(self.verify(), self.report)

    def test_no_hidden_label_file_open_and_no_oracle_body_as_conversion(self):
        self.assertFalse(any(Path(p).name.startswith("NEVER-OPEN-") for p in self.opened))
        pdf = (self.out / "common/corpus/pdf/a.pdf.md").read_text()
        self.assertIn("FULL FIRST PAGE", pdf)
        self.assertIn("FULL IMAGE OCR NOT AN ORACLE", pdf)
        self.assertNotIn("ANNOTATED_ONLY_NOT_FULL_SOURCE", pdf)
        self.assertEqual(self.report["source_count"], 6)

    def test_all_pages_including_ocr_empty_page_and_all_source_mappings(self):
        row = next(r for r in self.report["sources"] if r["source_kind"] == "pdf")
        self.assertEqual(row["expected_coverage"], 2)
        self.assertEqual([u["start"] for u in row["units"]], [1, 2])
        self.assertEqual(row["units"][1]["method"], "vision-full-raster-page/v1")
        # The common OCR profile preserves every actual low-confidence block.
        self.assertEqual(row["units"][1]["details"]["blocks"][0]["confidence"], .1)
        for row in self.report["sources"]:
            body = (self.out / "common/corpus" / row["common_path"]).read_text()
            for mapping, value in zip(row["mapping"], row["units"]):
                self.assertEqual(body[mapping["output_start_char"]:mapping["output_end_char"]], value["text"])
                self.assertEqual(mapping["source_sha256"], row["source_sha256"])

    def test_audio_full_decode_duration_and_unfiltered_transcript(self):
        row = next(r for r in self.report["sources"] if r["source_kind"] == "audio")
        value = row["units"][0]
        self.assertEqual((value["start"], value["end"]), (0, 1000))
        self.assertEqual(value["details"]["decoded_duration_ms"], 1000)
        self.assertIn("WHOLE CLIP FIRST\nWHOLE CLIP SECOND", value["text"])
        self.assertEqual([r["recipe"] for r in row["commands"]], ["audio.probe", "audio.decode", "audio.asr"])

    def test_native_uses_reviewed_exact_screenshot_flag_and_no_other_flags(self):
        native = json.loads((self.out / "native/build.json").read_text())
        self.assertIsNotNone(native["materialization"]["observed"]["screens/cap.png"])
        self.assertTrue(all(v is None for k, v in native["materialization"]["observed"].items() if k != "screens/cap.png"))
        previous = self.flags["cap.png"]
        try:
            self.flags["cap.png"] = None
            with self.assertRaises(EvaluationError):
                self.verify()
        finally:
            self.flags["cap.png"] = previous

    def test_changed_source_asset_or_provenance_is_rejected(self):
        for name in ["assets/docs/a.md", "views/pdf/a.pdf.txt", "licenses/MIT.txt"]:
            with changed(self.base["bundle"] / name, b"changed"):
                with self.assertRaises(EvaluationError):
                    source_api.load_sources(self.base["bundle"], self.base["pin"], fixture=True)

    def test_missing_models_and_mutated_tools_fail_before_any_command(self):
        config = json.loads(self.base["config"].read_text())
        for resource in [config["models"]["asr"], config["tools"]["pdfinfo"]]:
            with changed(Path(resource["path"]), b"not-the-same-resource"), patch("search_comparison.source_views.commands.capture_command") as capture:
                with self.assertRaises(EvaluationError):
                    validate_config(self.base["config"], source_api.load_sources(self.base["bundle"], self.base["pin"], fixture=True), implementation_identity()["sha256"])
                capture.assert_not_called()

    def test_unregistered_materialized_documents_do_not_pass_with_rehashed_report(self):
        for corpus in ["native/corpus", "common/corpus"]:
            extra = self.out / corpus / "unregistered.md"
            report_path = self.out / "report.json"
            try:
                extra.write_text("unexpected searchable source")
                with changed(report_path, report_path.read_bytes()):
                    report = copy.deepcopy(self.report)
                    report["artifacts"] = _artifacts(self.out)
                    dump(report_path, report)
                    with self.assertRaises(EvaluationError):
                        self.verify()
            finally:
                extra.unlink(missing_ok=True)

    def test_unsupported_config_or_unresolved_placeholder_is_never_a_fallback(self):
        original = json.loads(self.base["config"].read_text())
        for change in [{"fallback": "auto"}, {"offline": False}, {"templates": {}}, {"implementation_sha256": "FILL_ME"}, {"runtime": {}}]:
            bad = {**original, **change}
            with changed(self.base["config"], json.dumps(bad).encode()):
                with self.assertRaises(EvaluationError):
                    validate_config(self.base["config"], source_api.load_sources(self.base["bundle"], self.base["pin"], fixture=True), implementation_identity()["sha256"])

    def test_fixed_registry_rejects_all_malformed_unknown_or_embedded_tokens(self):
        for token in ["{input", "input}", "prefix{input}", "{secret}", "{}", "{{input}}"]:
            with patch.dict(RECIPES, {"pdf.probe": ["{pdfinfo}", token]}):
                with self.assertRaises(EvaluationError):
                    compile_recipe("pdf.probe", {"pdfinfo": "/fixed/tool", "input": "/owned/原始 ' ` $.pdf", "secret": "ignored"})

    def test_fixed_runtime_call_rejects_tool_model_and_output_overrides(self):
        commands = Commands(self.out, self.out / "work/0001", {}, {}, timeout=1, max_output=100, replay=[])
        for override in [{"pdfinfo": "/some/other/tool"}, {"asr": "/some/other/model"}, {"prefix": str(self.out / "arbitrary")}]:
            with patch("search_comparison.source_views.commands.capture_command") as capture:
                with self.assertRaises(EvaluationError):
                    commands.call("pdf.probe", "no-execution", input=self.out / "native/corpus/pdf/a.pdf", **override)
                capture.assert_not_called()

    def test_installed_shadow_dependency_is_not_a_conversion_identity(self):
        with patch("search_comparison.source_views.run.goldmine.__file__", "/installed/elsewhere/goldmine/__init__.py"):
            with self.assertRaises(EvaluationError):
                implementation_identity()

    def test_eager_initializers_and_transitive_local_helpers_are_identity_bound(self):
        root = Path(__file__).resolve().parents[1]
        before = implementation_identity()
        paths = ("scripts/product_eval/__init__.py", "scripts/product_eval/scoring.py",
                 "scripts/answer_eval/__init__.py", "scripts/answer_eval/statistics.py",
                 "scripts/acceptance_lib/__init__.py", "goldmine/ingest/__init__.py", "goldmine/util.py")
        for name in paths:
            self.assertIn(name, before["files"])
            path = root / name
            with changed(path, path.read_bytes() + b"\n# controlled identity drift\n"):
                self.assertNotEqual(implementation_identity()["sha256"], before["sha256"])
                with self.assertRaises(EvaluationError):
                    self.verify()
        extra = root / "scripts/product_eval/identity_inventory_canary.py"
        try:
            extra.write_text("# Declared mechanical inventory canary, never imported.\n")
            self.assertNotEqual(implementation_identity()["sha256"], before["sha256"])
            with self.assertRaises(EvaluationError):
                self.verify()
        finally:
            extra.unlink(missing_ok=True)
        self.assertEqual(implementation_identity(), before)

    def test_fixture_never_becomes_real_by_omitting_mechanical_flag(self):
        with self.assertRaises(EvaluationError):
            source_api.load_sources(self.base["bundle"], self.base["pin"])

    def test_coverage_rejects_omitted_duplicate_or_reordered_pdf_pages(self):
        one, two = unit("page", 1, 1, "one", "test"), unit("page", 2, 2, "two", "test")
        for rows in [[one], [one, one], [two, one]]:
            with self.assertRaises(EvaluationError):
                validate_coverage("pdf", 2, rows)

    def test_saturated_only_file_window_rule_preserves_short_complete_results(self):
        self.assertEqual(file_window(["a"] * 99)["status"], "COMPLETE_RETURNED_WINDOW")
        self.assertEqual(file_window(["a"] * 100)["status"], "TRUNCATED")
        self.assertEqual(file_window([str(i % 10) for i in range(100)])["status"], "COMPLETE_RETURNED_WINDOW")

    def test_unknown_output_existing_path_and_symlink_escape_rejected(self):
        with self.assertRaises(EvaluationError):
            capture_sources(self.base["bundle"], self.base["pin"], self.base["config"], self.out, self.base["root"], fixture=True)
        link = self.base["root"] / "escape"
        link.symlink_to(self.root)
        try:
            with self.assertRaises(EvaluationError):
                capture_sources(self.base["bundle"], self.base["pin"], self.base["config"], link / "out", self.base["root"], fixture=True)
        finally:
            link.unlink()

    def test_ancestor_alias_cannot_turn_owned_output_into_source_bundle(self):
        link = self.base["root"] / "source-alias"
        link.symlink_to(self.base["bundle"], target_is_directory=True)
        try:
            bundle = source_api.load_sources(self.base["bundle"], self.base["pin"], fixture=True)
            with self.assertRaises(EvaluationError):
                owned_output(link / "assets/new-output", link / "assets", bundle)
            # Even an alias resolving inside the owner is not a converter I/O path.
            with self.assertRaises(EvaluationError):
                _owned_path(link / "assets/docs/a.md", self.base["root"])
        finally:
            link.unlink()

    def test_changed_raw_stream_cannot_be_hidden_by_rehashing_top_artifact_map(self):
        raw = self.out / "work/0003/commands/page-00001-text/stdout.log"
        report_path = self.out / "report.json"
        with changed(raw, b"forged page text"), changed(report_path, report_path.read_bytes()):
            report = copy.deepcopy(self.report)
            report["artifacts"] = _artifacts(self.out)
            dump(report_path, report)
            with self.assertRaises((EvaluationError, ValueError)):
                self.verify()

    def test_changed_visible_environment_rejected_after_all_receipt_hashes_updated(self):
        report = copy.deepcopy(self.report)
        row = report["sources"][2]
        manifest_path = self.out / row["commands"][1]["command"]["path"]
        manifest = json.loads(manifest_path.read_text())
        receipt_path = manifest_path.parent / manifest["receipt"]["path"]
        files = [receipt_path, manifest_path, self.out / "work/0003/source.json", self.out / "report.json"]
        original = [p.read_bytes() for p in files]
        try:
            receipt = json.loads(receipt_path.read_text())
            receipt["env"]["GOLDMINE_MODEL"] = "/user/unreviewed/model"
            dump(receipt_path, receipt)
            manifest["receipt"]["sha256"] = sha256_file(receipt_path)
            dump(manifest_path, manifest)
            row["commands"][1]["command"]["sha256"] = sha256_file(manifest_path)
            dump(files[2], row)
            report["artifacts"] = _artifacts(self.out)
            dump(files[3], report)
            with self.assertRaises(EvaluationError):
                self.verify()
        finally:
            for path, raw in zip(files, original):
                path.write_bytes(raw)

    def test_durable_source_row_cannot_contradict_the_replayed_aggregate(self):
        path = self.out / "work/0003/source.json"
        report_path = self.out / "report.json"
        with changed(path, path.read_bytes()), changed(report_path, report_path.read_bytes()):
            source = json.loads(path.read_text())
            source["units"] = []
            dump(path, source)
            report = copy.deepcopy(self.report)
            report["artifacts"] = _artifacts(self.out)
            dump(report_path, report)
            with self.assertRaises(EvaluationError):
                self.verify()

    def test_omitted_page_cannot_pass_after_all_summary_and_body_hashes_are_updated(self):
        report = copy.deepcopy(self.report)
        row = report["sources"][2]
        source = self.base["sources"][2]
        row["units"] = row["units"][:1]
        body, mapping = render(source, row["units"])
        row["mapping"] = mapping
        row["common_text_sha256"] = hashlib.sha256(body.encode()).hexdigest()
        files = [self.out / "report.json", self.out / "work/0003/source.json", self.out / "common/corpus/pdf/a.pdf.md"]
        original = [p.read_bytes() for p in files]
        try:
            files[2].write_text(body)
            dump(files[1], row)
            report["artifacts"] = _artifacts(self.out)
            dump(files[0], report)
            with self.assertRaises(EvaluationError):
                self.verify()
        finally:
            for path, raw in zip(files, original):
                path.write_bytes(raw)

    def test_failed_page_does_not_become_ocr_fallback_or_success(self):
        fixture = make_fixture(self.root / "failed-page", pdf_suffix=b"FAIL_TEXT_2")
        out = fixture["root"] / "capture"
        report = capture_sources(fixture["bundle"], fixture["pin"], fixture["config"], out, fixture["root"], fixture=True)
        pdf = report["sources"][2]
        self.assertEqual(report["status"], "MISSING")
        self.assertEqual([u["status"] for u in pdf["units"]], ["OK", "MISSING"])
        self.assertNotIn("pdf.raster", [row["recipe"] for row in pdf["commands"]])
        self.assertEqual(self.verify(fixture, out)["status"], "MISSING")

    def test_short_decoded_audio_keeps_fixed_duration_and_never_calls_asr(self):
        fixture = make_fixture(self.root / "short-audio", audio_suffix=b"SHORT_DECODE")
        out = fixture["root"] / "capture"
        report = capture_sources(fixture["bundle"], fixture["pin"], fixture["config"], out, fixture["root"], fixture=True)
        audio = report["sources"][-1]
        self.assertEqual(report["status"], "MISSING")
        self.assertEqual(audio["expected_coverage"], 1000)
        self.assertEqual(audio["units"][0]["status"], "MISSING")
        self.assertNotIn("audio.asr", [row["recipe"] for row in audio["commands"]])
        self.assertEqual(self.verify(fixture, out)["status"], "MISSING")


if __name__ == "__main__":
    unittest.main()
