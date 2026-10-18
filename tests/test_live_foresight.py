"""Tier-3 LIVE: the real ForesightHelper over its live UDS socket.

Skips unless GOLDMINE_LIVE=1 AND the helper socket exists (auto-skip with the
doctor-style hint otherwise). Embeds one text, one hand-built PNG and one
stdlib-generated WAV and asserts 768-dim on all three (live-verified fact from
the foresight-rea findings, 2026-10-11).
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from goldmine.backends.foresight import ForesightBackend, default_socket
from goldmine.config import ForesightCfg

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("GOLDMINE_LIVE"),
        reason="GOLDMINE_LIVE=1 required for live foresight tests",
    ),
    pytest.mark.skipif(
        not default_socket().exists(),
        reason=f"ForesightHelper socket not at {default_socket()} "
        "(start the Foresight app; unavailability is data)",
    ),
]


@pytest.fixture
def backend():
    b = ForesightBackend(ForesightCfg())  # env > config > $TMPDIR live socket
    yield b
    b.close()


def test_live_ping_version(backend):
    version = backend.ping()
    assert version  # e.g. "1.0.3"; identity slot in the fingerprint
    assert backend.fingerprint_fields()["model_id"] == f"helper@{version}"
    assert backend.fingerprint_fields()["family"] == "foresight-gemma-embedding"


def test_live_text_embedding_768(backend):
    vectors = backend.embed_texts(["kubernetes pod eviction"])
    assert vectors.shape == (1, 768)
    assert np.isclose(np.linalg.norm(vectors[0]), 1.0, atol=1e-3)


def test_live_image_embedding_768(backend, tmp_path):
    from conftest import tiny_png

    png = tmp_path / "tiny.png"
    png.write_bytes(tiny_png())
    vector = backend.embed_image(png)
    assert vector.shape == (768,)
    assert np.isclose(np.linalg.norm(vector), 1.0, atol=1e-3)


def test_live_audio_embedding_768(backend):
    from conftest import tiny_wav_bytes

    vector = backend.embed_audio_wav(tiny_wav_bytes(0.5))
    assert vector.shape == (768,)
    assert np.isclose(np.linalg.norm(vector), 1.0, atol=1e-3)


def test_live_fingerprint_changes_with_helper_version(backend):
    from goldmine import fingerprints

    fp = fingerprints.build(backend)  # pings + probes dim
    assert fp["backend"] == "foresight"
    assert fp["dim"] == 768
    forged = dict(fp, model_id="helper@upgraded")
    assert fingerprints.fingerprint_id(fp) != fingerprints.fingerprint_id(forged)


def test_live_media_ingest_content_indexes_image_and_audio(backend, tmp_path):
    """End-to-end through the PIPELINE: a real PNG + a 1 s wav ingested via the
    foresight backend become image/audio-modality segments with 768-dim fp16
    vectors, and a hybrid search over the mixed corpus runs without error.
    Tiny fixtures: no semantic-relevance assertions, shape + honesty only."""
    from conftest import tiny_png, tiny_wav_bytes

    from goldmine.config import Config
    from goldmine.ingest import IngestPipeline
    from goldmine.search import Searcher
    from goldmine.store import Store

    root = tmp_path / "media"
    root.mkdir()
    # 256x256: the pipeline's v2 image prep (B5) refuses sub-256 images
    # (DigUp minImageSide) BEFORE any backend call — an 8x8 fixture would be
    # honestly 'unsupported', which is a different test than this one
    (root / "tiny.png").write_bytes(tiny_png(256, 256))
    (root / "tone.wav").write_bytes(tiny_wav_bytes(1.0))

    cfg = Config()
    cfg.ingest.workers = 1
    store = Store(tmp_path / "live-media.sqlite")
    pipeline = IngestPipeline(cfg, store, backend)
    try:
        report = pipeline.run([str(root)])
        assert report.failed == 0, report.failures
        assert report.content_indexed == 2, report.failures
        assert report.fingerprint is not None
        assert report.fingerprint["backend"] == "foresight"

        rows = store.db.execute(
            "SELECT kind, modality, loc, loc_end, vec FROM segments "
            "WHERE modality IN ('image', 'audio')"
        ).fetchall()
        modalities = {r[1] for r in rows}
        assert {"image", "audio"} <= modalities
        for kind, modality, loc, loc_end, blob in rows:
            assert kind == modality
            assert len(blob) == 768 * 2                      # fp16 BLOB contract
            vec = np.frombuffer(blob, dtype="<f2").astype(np.float32)
            assert np.isclose(np.linalg.norm(vec), 1.0, atol=0.02)
            if modality == "audio":
                assert loc == pytest.approx(0.0, abs=0.1)    # window start
                assert loc_end == pytest.approx(1.0, abs=0.3)
            else:
                assert loc is None and loc_end is None       # image: no location

        # the fingerprint recorded by ingest must match a live search's gate
        hits = Searcher(store, backend).search("tone")
        assert isinstance(hits, list)                        # fusion ran, no error
        for hit in hits:
            assert hit.kind in ("image", "audio")
            assert hit.file_status == "content_indexed"
    finally:
        store.close()
