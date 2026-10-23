"""Incremental passes measure actual work and avoid an unchanged model load."""

import os
import stat
from pathlib import Path

import pytest

from goldmine.backends.llama import LlamaServerBackend
from goldmine.backends.mock import MockBackend
from goldmine.config import Config, LlamaCfg
from goldmine.fingerprints import FingerprintMismatch
from goldmine.ingest import IngestPipeline
from goldmine.ingest import asr, media
from goldmine.ingest.extraction import _Outcome
from goldmine.render import render_ingest_summary
from goldmine.store import Store
from test_store import _v2_card, _v2_run


def fake_config(tmp_path):
    server = Path(__file__).with_name("fake_llama_server.py")
    server.chmod(server.stat().st_mode | stat.S_IXUSR)
    model = tmp_path / "fixture-model.gguf"
    model.write_bytes(b"fixture model, no real weights")
    cfg = Config()
    cfg.home = tmp_path / "home"
    cfg.llama = LlamaCfg(server=server, server_configured=True, model=model, ctx=512,
                        batch=8, startup_timeout=5, request_timeout=2)
    return cfg


def contents(store):
    return store.db.execute("SELECT id,file,kind,loc,loc_end,vec FROM segments ORDER BY id").fetchall()


def pass_with_llama(cfg, store, root):
    backend = LlamaServerBackend(cfg.llama)
    report = IngestPipeline(cfg, store, backend).run([str(root)])
    assert backend._proc is None  # every actually owned process was reaped
    return report, backend


def test_actual_llama_process_first_unchanged_modify_delete_and_card_members(tmp_path):
    cfg = fake_config(tmp_path)
    root = tmp_path / "sources"
    root.mkdir()
    alpha, beta = root / "alpha.txt", root / "beta.txt"
    alpha.write_text("first alpha evidence\n")
    beta.write_text("second beta evidence\n")
    store = Store(tmp_path / "index.sqlite")
    try:
        first, _ = pass_with_llama(cfg, store, root)
        assert first.metrics["model_spawns"] == 1
        assert first.metrics["embedding_http_requests"] == first.metrics["embed_calls"] + 1  # actual dim probe
        assert first.metrics["embed_calls"] > 0
        segment_ids = [r[0] for r in store.db.execute("SELECT id FROM segments WHERE vec IS NOT NULL")]
        store.save_run(_v2_run("existing-run", "existing-view"), [_v2_card("existing-run", "existing-view", segment_ids)])
        before = contents(store)
        members = store.db.execute("SELECT card,chunk,weight FROM card_chunks ORDER BY card,chunk").fetchall()
        unchanged, backend = pass_with_llama(cfg, store, root)
        assert unchanged.unchanged == unchanged.metrics["files_skipped"] == 2
        assert all(unchanged.metrics[name] == 0 for name in ("model_spawns", "embedding_http_requests", "embed_calls", "ocr_calls", "asr_calls"))
        assert backend.model_spawn_count == 0 and backend.embedding_http_requests == 0
        assert contents(store) == before
        assert store.db.execute("SELECT card,chunk,weight FROM card_chunks ORDER BY card,chunk").fetchall() == members
        beta_id = store.find_by_path(str(beta)).id
        beta_rows = [r for r in before if r[1] == beta_id]
        original = alpha.stat()
        alpha.write_text("fresh alpha evidence\n")  # same size and preserved mtime: hash must still repair
        os.utime(alpha, ns=(original.st_atime_ns, original.st_mtime_ns))
        changed, _ = pass_with_llama(cfg, store, root)
        assert changed.content_indexed == 1 and changed.unchanged == 1
        assert changed.metrics["model_spawns"] == 1
        assert [r for r in contents(store) if r[1] == beta_id] == beta_rows
        beta.unlink()
        deleted, _ = pass_with_llama(cfg, store, root)
        assert deleted.deleted == 1 and deleted.unchanged == 1
        assert deleted.metrics["model_spawns"] == deleted.metrics["embed_calls"] == 0
        assert store.find_by_path(str(beta)) is None
        assert all(deleted.metrics[name] >= 0 for name in ("scan_ms", "hash_ms", "extract_ms", "embed_ms", "commit_ms"))
    finally:
        store.close()


def test_unchanged_still_checks_fingerprint_without_starting_incompatible_llama(tmp_path):
    cfg = fake_config(tmp_path)
    root = tmp_path / "sources"
    root.mkdir()
    (root / "source.txt").write_text("index evidence")
    store = Store(tmp_path / "index.sqlite")
    try:
        pass_with_llama(cfg, store, root)
        before = contents(store)
        cfg.llama.model.write_bytes(b"different incompatible model")
        backend = LlamaServerBackend(cfg.llama)
        with pytest.raises(FingerprintMismatch):
            IngestPipeline(cfg, store, backend).run([str(root)])
        assert backend.model_spawn_count == backend.embedding_http_requests == 0
        assert contents(store) == before
    finally:
        store.close()


def test_completed_ocr_asr_not_called_again_on_unchanged_pass(tmp_path, monkeypatch):
    from conftest import write_fake_ocr_helper
    from goldmine.ingest.exif import CaptureResult

    root = tmp_path / "sources"
    root.mkdir()
    (root / "Screenshot opaque.png").write_bytes(b"image fixture")
    (root / "opaque-audio.wav").write_bytes(b"audio fixture")
    monkeypatch.setenv("GOLDMINE_OCR_HELPER", str(write_fake_ocr_helper(tmp_path)))
    monkeypatch.setattr(media, "sips_path", lambda: None)
    # These opaque fixture bytes exercise OCR/ASR accounting. A real metadata
    # subprocess is outside this hermetic test and its failure must stay retryable.
    monkeypatch.setattr(media, "exif_capture", lambda _: CaptureResult(None, "fixture", "unsupported"))
    monkeypatch.setattr(asr, "resolve_tools", lambda cfg: object())
    transcribes = []

    def transcribe(path, cfg):
        transcribes.append(path)
        return asr.AsrResult("en", [asr.AsrSegment("beacon transmission accepted", 10, 20)], duration_s=20)

    monkeypatch.setattr(asr, "transcribe", transcribe)
    cfg, store = Config(), Store(tmp_path / "index.sqlite")
    cfg.home = tmp_path / "home"
    try:
        first = IngestPipeline(cfg, store, MockBackend()).run([str(root)])
        assert first.metrics["ocr_calls"] == first.metrics["asr_calls"] == 1
        assert first.metrics["ocr_inputs"] == 1 and len(transcribes) == 1
        before = contents(store)
        second = IngestPipeline(cfg, store, MockBackend()).run([str(root)])
        assert second.unchanged == 2
        assert second.metrics["ocr_calls"] == second.metrics["asr_calls"] == second.metrics["embed_calls"] == 0
        assert len(transcribes) == 1 and contents(store) == before
    finally:
        store.close()


def test_failure_recovery_and_disabled_metrics_preserve_default_human_output(tmp_path, monkeypatch, capsys):
    root = tmp_path / "sources"
    root.mkdir()
    source = root / "source.txt"
    source.write_text("initial stable body")
    cfg, store = Config(), Store(tmp_path / "index.sqlite")
    try:
        first = IngestPipeline(cfg, store, MockBackend()).run([str(root)])
        other = Store(tmp_path / "other.sqlite")
        try:
            disabled = IngestPipeline(cfg, other, MockBackend()).run([str(root)], instrument=False)
            assert disabled.metrics is None and "metrics" not in disabled.to_json()
            assert render_ingest_summary(first, json_mode=False) == render_ingest_summary(disabled, json_mode=False)
        finally:
            other.close()
        source.write_text("modified body needs extraction")
        failed_pipeline = IngestPipeline(cfg, store, MockBackend())
        monkeypatch.setattr(failed_pipeline, "_extract", lambda *a, **kw: _Outcome("failed", "fixture extraction failure"))
        failed = failed_pipeline.run([str(root)])
        assert failed.failed == 1 and failed.metrics["embed_calls"] == 0
        assert not store.db.execute("SELECT 1 FROM segments WHERE kind!='name'").fetchone()
        recovered = IngestPipeline(cfg, store, MockBackend()).run([str(root)])
        assert recovered.content_indexed == 1 and recovered.metrics["embed_calls"] > 0
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not store.db.execute("PRAGMA foreign_key_check").fetchall()
        assert capsys.readouterr().out == ""
    finally:
        store.close()


def test_unknown_native_counters_are_null_not_fabricated_zero(tmp_path):
    class UnobservedBackend(MockBackend):
        model_spawn_count = None
        embedding_http_requests = None

    root = tmp_path / "sources"
    root.mkdir()
    (root / "source.txt").write_text("stored evidence")
    store = Store(tmp_path / "index.sqlite")
    try:
        report = IngestPipeline(Config(), store, UnobservedBackend()).run([str(root)])
        assert report.metrics["model_spawns"] is report.metrics["embedding_http_requests"] is None
        assert not report.metrics["native_observability"]["model_spawns"]
    finally:
        store.close()
