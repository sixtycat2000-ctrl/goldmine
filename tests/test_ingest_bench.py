"""Resource measurement never substitutes successful model/extractor outputs."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from ingest_bench.measure import BackendProxy, Meter, stored_snapshot
from goldmine.backends.base import EmbeddingError
from goldmine.backends.mock import MockBackend
from goldmine.config import Config
from goldmine.ingest import IngestPipeline
from goldmine.store import Store


def test_meter_calls_original_backend_and_accounts_for_injected_failure():
    real_operation = MockBackend()
    meter = Meter()
    proxy = BackendProxy(real_operation, meter)
    assert (proxy.embed_texts(["proof"]) == real_operation.embed_texts(["proof"])).all()
    assert meter.calls["embed_texts"] == 1
    failing = BackendProxy(real_operation, meter, fail_embed=True)
    with pytest.raises(EmbeddingError, match="controlled benchmark fault"):
        failing.embed_texts(["proof"])
    assert meter.calls["embed_texts"] == 2


def test_real_pipeline_unchanged_work_is_measured_without_replacing_output(tmp_path, tmp_home):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "sample.txt").write_text("Zircon lantern original record.\n" * 5)
    config = Config()
    config.home = tmp_home / "resource-mechanics"
    config.backend = "mock"
    store = Store(tmp_path / "index.sqlite")
    try:
        initial = Meter()
        with initial:
            IngestPipeline(config, store, BackendProxy(MockBackend(), initial)).run([str(corpus)])
        assert initial.calls["embed_texts"] > 0 and initial.calls["extract"] > 0
        before = stored_snapshot(store)
        repeated = Meter()
        with repeated:
            report = IngestPipeline(config, store, BackendProxy(MockBackend(), repeated)).run([str(corpus)])
        assert report.unchanged == 1
        assert repeated.calls["embed_texts"] == repeated.calls["ocr"] == repeated.calls["asr"] == repeated.calls["extract"] == 0
        assert stored_snapshot(store) == before
    finally:
        store.close()
