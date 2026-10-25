"""Live tests against the REAL goldmine-embed helper (digup-plan-002 T6).

Tier-3 gating like test_reference_vectors.py: GOLDMINE_LIVE=1 plus the built
binary + pinned models on disk. The resize golden table pins the ImageSizing
C port; the embed round trips pin determinism and unit norms; the ingest
integration pins the OCR-text-vector auto-close (§5.1) — the whole point of
the capability flip.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import numpy as np
import pytest

from goldmine.backends.embed_helper import MediaEmbedder, resolve_helper_path
from goldmine.config import LlamaCfg

pytestmark = [pytest.mark.live, pytest.mark.native]

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "vendor" / "llama.cpp" / "build-static" / "bin" / "goldmine-embed"
MODEL = REPO_ROOT / "models" / "embeddinggemma-2-Q8_0.gguf"
MMPROJ = REPO_ROOT / "models" / "mmproj-embeddinggemma-2-Q8_0.gguf"
GOLDEN = Path(__file__).with_name("fixtures_data") / "resize_golden.json"
MEDIA = Path(__file__).with_name("fixtures_data"[:0] or "fixtures_data") / "media"

_skip = pytest.mark.skipif(
    not os.environ.get("GOLDMINE_LIVE"), reason="GOLDMINE_LIVE=1 required"
)
_skip_binary = pytest.mark.skipif(
    not HELPER.is_file(), reason=f"helper not built at {HELPER}"
)
_skip_models = pytest.mark.skipif(
    not (MODEL.is_file() and MMPROJ.is_file()), reason="pinned models missing"
)


def _embedder(tmp_path):
    cfg = LlamaCfg()
    cfg.home = tmp_path
    cfg.embed_helper = str(HELPER)
    cfg.embed_helper_configured = True
    cfg.startup_timeout = 180.0
    cfg.request_timeout = 120.0
    return MediaEmbedder(cfg, MODEL)


@_skip
@_skip_binary
def test_selftest_resize_matches_golden_table():
    """The C ImageSizing port against the frozen golden table (P1)."""
    out = subprocess.run(
        [str(HELPER), "--selftest-resize"], capture_output=True, text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr[-400:]
    lines = [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
    golden = [json.loads(line) for line in GOLDEN.read_text().splitlines() if line.strip()]
    cases = [entry for entry in lines if "case" in entry]
    golden_cases = [entry for entry in golden if "case" in entry]
    assert len(cases) == len(golden_cases)
    for live, frozen in zip(cases, golden_cases):
        assert live["case"] == frozen["case"]
        assert live["target"] == frozen["target"], frozen["case"]
        assert live["out_sha256"] == frozen["out_sha256"], frozen["case"]
        assert live["first_pixel"] == frozen["first_pixel"], frozen["case"]


@_skip
@_skip_binary
@_skip_models
def test_real_helper_caps_and_deterministic_vectors(tmp_path):
    embedder = _embedder(tmp_path)
    try:
        embedder.startup()
        caps = embedder.caps()
        assert caps["ok"] and caps["dim"] == 768
        assert caps["vision"] is True and caps["audio"] is True
        assert caps["sample_rate"] == 16000
        photo = Path(__file__).with_name("fixtures_data") / "media" / "ref-photo-b140.jpg"
        v1 = embedder.embed_image(photo, 140)
        v2 = embedder.embed_image(photo, 140)
        assert v1.shape == (768,) and np.all(np.isfinite(v1))
        assert abs(float(np.linalg.norm(v1)) - 1.0) < 1e-5
        assert np.array_equal(v1, v2)  # byte-deterministic
        shot = Path(__file__).with_name("fixtures_data") / "media" / "ref-shot-b280.png"
        s1 = embedder.embed_image(shot, 280)
        assert abs(float(np.linalg.norm(s1)) - 1.0) < 1e-5
        assert not np.array_equal(v1, s1)  # budget branch actually differs
        wav = (Path(__file__).with_name("fixtures_data") / "media" / "ref-audio-3s.wav").read_bytes()
        a1 = embedder.embed_audio_wav(wav)
        a2 = embedder.embed_audio_wav(wav)
        assert a1.shape == (768,)
        assert abs(float(np.linalg.norm(a1)) - 1.0) < 1e-5
        assert np.array_equal(a1, a2)
    finally:
        embedder.close()


@_skip
@_skip_binary
@_skip_models
def test_real_helper_error_paths_stay_alive(tmp_path):
    embedder = _embedder(tmp_path)
    try:
        embedder.startup()
        from goldmine.backends.base import EmbeddingError

        with pytest.raises(EmbeddingError) as excinfo:
            embedder.embed_image(Path("/nonexistent/x.png"), 140)
        assert "decode_failed" in str(excinfo.value)
        wav = (Path(__file__).with_name("fixtures_data") / "media" / "ref-audio-3s.wav").read_bytes()
        with pytest.raises(EmbeddingError) as excinfo:
            embedder.embed_image(Path(
                Path(__file__).with_name("fixtures_data") / "media" / "ref-audio-3s.wav"
            ), 140)
        assert "not_image" in str(excinfo.value)
        assert embedder.ping() is True  # helper ALIVE after per-request errors
    finally:
        embedder.close()


@_skip
@_skip_binary
@_skip_models
@pytest.mark.assets
def test_real_backend_end_to_end_photo_ingest(tmp_path):
    """The full chain under the REAL llama backend: extraction (sips prep,
    budget pinned) -> commit (embed_image through the helper) -> store row
    with an image-modality vector + the media audit markers."""
    import sqlite3

    from goldmine.backends import create_backend
    from goldmine import config as config_mod
    from goldmine.ingest import IngestPipeline
    from goldmine.store import Store

    root = tmp_path / "tree"
    root.mkdir()
    photo = root / "photo-b140.jpg"
    photo.write_bytes(
        (Path(__file__).with_name("fixtures_data") / "media" / "ref-photo-b140.jpg").read_bytes()
    )
    cfg = config_mod.load(env={**os.environ, "GOLDMINE_BACKEND": "llama",
                               "GOLDMINE_HOME": str(tmp_path / "home")})
    cfg.llama.embed_helper = str(HELPER)
    cfg.llama.embed_helper_configured = True
    db = tmp_path / "index.sqlite"
    store = Store(db)
    backend = create_backend(cfg, "llama")
    try:
        assert backend.capability().image, "media capability must be live here"
        report = IngestPipeline(cfg, store, backend).run([str(root)])
        assert report.failed == 0
        assert report.content_indexed == 1, report
    finally:
        backend.close()
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT f.capability, f.info, s.kind, s.modality, s.vec IS NOT NULL "
            "FROM files f JOIN segments s ON s.file=f.id WHERE s.kind='image'"
        ).fetchone()
        assert row is not None, "no image segment landed"
        capability, info_raw, kind, modality, has_vec = row
        assert capability == "content_indexed"
        assert kind == "image" and modality == "image" and has_vec
        info = json.loads(info_raw)
        assert info.get("media_vectors") == "mm-v1"
        assert info.get("thumb"), "sips thumbnail info recorded"
    finally:
        con.close()
        store.close()


@_skip
@_skip_binary
@_skip_models
@pytest.mark.assets
@pytest.mark.skipif(
    not (Path("/usr/bin/ffprobe").exists() or Path("/opt/homebrew/bin/ffprobe").exists()),
    reason="ffmpeg/ffprobe not installed",
)
def test_real_backend_end_to_end_video_frames(tmp_path):
    """video-frames-001 end to end under the REAL backend: the deterministic
    fixture video lands frame segments with unit-norm image vectors, the
    media audit markers, and honest routing info (ASR outcome whatever it
    is — frames ride both routings)."""
    import sqlite3

    from goldmine.backends import create_backend
    from goldmine import config as config_mod
    from goldmine.ingest import IngestPipeline
    from goldmine.store import Store

    root = tmp_path / "tree"
    root.mkdir()
    video = root / "ref-video.mp4"
    video.write_bytes((MEDIA / "ref-video.mp4").read_bytes())
    cfg = config_mod.load(env={**os.environ, "GOLDMINE_BACKEND": "llama",
                               "GOLDMINE_HOME": str(tmp_path / "home")})
    cfg.llama.embed_helper = str(HELPER)
    cfg.llama.embed_helper_configured = True
    cfg.ingest.asr = "off"          # routing determinism: raw windows + frames
    db = tmp_path / "index.sqlite"
    store = Store(db)
    backend = create_backend(cfg, "llama")
    try:
        assert backend.capability().image, "media capability must be live here"
        report = IngestPipeline(cfg, store, backend).run([str(root)])
        assert report.failed == 0, report.failures
        assert report.content_indexed == 1, report
    finally:
        backend.close()
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT s.kind, s.modality, s.loc, s.loc_end, s.vec, f.info "
            "FROM files f JOIN segments s ON s.file=f.id "
            "WHERE s.kind='frame' ORDER BY s.loc"
        ).fetchall()
        assert len(rows) == 4, f"expected 4 scene keyframes, got {len(rows)}"
        import struct

        for kind, modality, loc, loc_end, blob, _info in rows:
            assert kind == "frame" and modality == "image"
            assert blob is not None
            vec = np.frombuffer(blob, dtype="<f2").astype(np.float32)
            assert vec.shape[0] == 768, vec.shape       # embeddinggemma-2 dim
            assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-2
        assert [r[2] for r in rows] == [0.5, 6.5, 12.5, 15.5]
        info = json.loads(rows[0][5])
        assert info.get("frames") == 4 and info.get("frame_interval") == 3.0
        assert info.get("media_vectors") == "mm-v1"
        assert info.get("audio_routing", "").startswith(("raw:", "text:", "none:"))
    finally:
        con.close()
        store.close()
