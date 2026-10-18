"""Tier-2: ForesightBackend client logic against an in-process fake helper.

The fake serves the same generated proto stubs over a UDS socket, so the
gRPC-over-UDS path, Ping identity, and the three embed RPCs are tested offline.
grpcio absence => the whole module skips (honest unavailability, never crash).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

grpc = pytest.importorskip("grpc", reason="grpcio not installed — foresight unavailable")

from goldmine import fingerprints  # noqa: E402
from goldmine.backends.base import EmbeddingError  # noqa: E402
from goldmine.backends.foresight import ForesightBackend  # noqa: E402
from goldmine.config import ForesightCfg  # noqa: E402
from tests.fake_foresight_helper import serve  # noqa: E402

sys_path_tests = Path(__file__).parent


@pytest.fixture
def helper(tmp_path, monkeypatch):
    # UDS paths cap at ~104 chars on macOS; pytest's tmp_path is far longer,
    # so the fake helper binds a short path under /tmp instead
    import shutil
    import tempfile

    short_dir = Path(tempfile.mkdtemp(prefix="gmfk"))
    socket_path = short_dir / "h.sock"
    server, servicer = serve(socket_path, version="py-1.0.3-fake", dim=768)
    monkeypatch.setenv("GOLDMINE_FORESIGHT_SOCKET", str(socket_path))
    yield socket_path, servicer
    server.stop(0)
    shutil.rmtree(short_dir, ignore_errors=True)


def _backend(socket_path: Path) -> ForesightBackend:
    return ForesightBackend(ForesightCfg(socket=str(socket_path)))


def test_ping_returns_version(helper):
    socket_path, servicer = helper
    backend = _backend(socket_path)
    assert backend.ping() == "py-1.0.3-fake"
    backend.close()


def test_embed_texts_normalized_and_deterministic(helper):
    socket_path, _servicer = helper
    backend = _backend(socket_path)
    matrix = backend.embed_texts(["alpha", "beta", "alpha"])
    assert matrix.shape == (3, 768)
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)
    assert np.allclose(matrix[0], matrix[2])          # deterministic
    assert not np.allclose(matrix[0], matrix[1])      # text-dependent
    backend.close()


def test_embed_image_and_audio(helper, tmp_path):
    from conftest import tiny_png, tiny_wav_bytes

    socket_path, _servicer = helper
    backend = _backend(socket_path)
    png = tmp_path / "tiny.png"
    png.write_bytes(tiny_png())
    vec_img = backend.embed_image(png)
    assert vec_img.shape == (768,)
    assert np.isclose(np.linalg.norm(vec_img), 1.0, atol=1e-5)
    vec_wav = backend.embed_audio_wav(tiny_wav_bytes(0.5))
    assert vec_wav.shape == (768,)
    assert not np.allclose(vec_img, vec_wav)
    backend.close()


def test_fingerprint_identity_is_helper_version(helper):
    socket_path, servicer = helper
    backend = _backend(socket_path)
    fields = backend.fingerprint_fields()
    assert fields["model_id"] == "helper@unknown"  # not started yet
    backend.startup()
    fp_before = fingerprints.build(backend)
    assert fp_before["model_id"] == "helper@py-1.0.3-fake"
    assert fp_before["family"] == "foresight-gemma-embedding"
    assert fp_before["dim"] == 768
    assert fp_before["task_type"] == {"query": 6, "document": "helper-default"}
    # media preprocessing (windows/raw bytes) is part of the fingerprint
    from goldmine.ingest.media import MEDIA_PREPROCESSING

    assert fp_before["media"] == MEDIA_PREPROCESSING
    backend.close()

    # helper upgrade => version change => fingerprint change => reindex
    server2, _ = serve(socket_path, version="py-1.0.4-fake", dim=768)
    try:
        backend2 = _backend(socket_path)
        fp_after = fingerprints.build(backend2)
        assert fingerprints.fingerprint_id(fp_before) != fingerprints.fingerprint_id(
            fingerprints.build(backend2)
        )
        report = fingerprints.check(
            fingerprints.fingerprint_id(fp_before),
            fingerprints.canonical(fp_before).decode(),
            fp_after,
        )
        assert not report.ok and "model_id" in report.diff_fields
        backend2.close()
    finally:
        server2.stop(0)
        servicer.version = "py-1.0.3-fake"


def test_fingerprint_isolation_from_llama_and_mock(helper):
    """Foresight vectors can never share an index with llama/mock vectors."""
    socket_path, _servicer = helper
    backend = _backend(socket_path)
    backend.startup()
    foresight_fp = fingerprints.build(backend)
    backend.close()

    class _FakeLlama:
        backend_id = "llama"
        dim = 768

        def fingerprint_fields(self):
            return {
                "model_id": "embeddinggemma-2-Q8_0",
                "model_sha256": "x",
                "dim": 768,
                "prompts": "p",
                "pooling": "server:mean",
                "normalization": "l2_post",
            }

    report = fingerprints.check(
        fingerprints.fingerprint_id(foresight_fp),
        fingerprints.canonical(foresight_fp).decode(),
        fingerprints.build(_FakeLlama()),
    )
    assert not report.ok and "backend" in report.diff_fields


def test_missing_socket_is_honest_error(tmp_path, monkeypatch):
    monkeypatch.setenv("GOLDMINE_FORESIGHT_SOCKET", str(tmp_path / "absent.sock"))
    backend = ForesightBackend(ForesightCfg())
    with pytest.raises(EmbeddingError) as excinfo:
        backend.ping()
    assert "socket not found" in str(excinfo.value)
    info = backend.describe()
    assert info["available"] is False
    assert info["fix_hint"]  # doctor-style hint, no crash


def test_describe_with_live_socket_no_rpc(helper):
    socket_path, _servicer = helper
    backend = _backend(socket_path)
    info = backend.describe()
    assert info["available"] is True
    assert info["version"] is None  # no RPC was made
    assert info["capability"] == {"text": True, "image": True, "audio": True}


def test_empty_texts_short_circuit(helper):
    socket_path, _servicer = helper
    backend = _backend(socket_path)
    out = backend.embed_texts([])
    assert out.shape[0] == 0
    backend.close()
