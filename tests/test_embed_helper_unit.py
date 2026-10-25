"""Offline unit tests for the media-embedding sidecar (digup-plan-002 T3-T5).

Protocol/lifecycle coverage runs against tests/fake_embed_helper.py (a Python
stub installed via GOLDMINE_EMBED_HELPER) — no models, no C build. The
targetSize table is the pure-Python port of DigUp ImageSizing.swift:9-26
(the spec the C helper implements; the C side is pinned by the live
--selftest-resize golden test, test_embed_helper_live.py).

Design note (verified while porting): the `zero_both` TOO_THIN branch is
mathematically unreachable for legal budgets (floor(factor*h/48) == 0 iff
h < w/budget; both sides zero would need budget < 1) — the guard exists for
parity with the Swift source and stays.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from goldmine.backends import embed_helper as eh
from goldmine.backends.base import BackendStartupError, EmbeddingError
from goldmine.backends.llama import LlamaServerBackend
from goldmine.config import Config, LlamaCfg
from goldmine import fingerprints

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE = REPO_ROOT / "tests" / "fake_embed_helper.py"


def _cfg(tmp_path, *, helper: Path | None = None, mmproj: Path | None = None,
         startup_timeout: float = 10.0, request_timeout: float = 5.0):
    cfg = LlamaCfg()
    cfg.home = tmp_path / "home"
    cfg.embed_helper = str(helper if helper is not None else FAKE)
    cfg.embed_helper_configured = True
    cfg.mmproj = str(mmproj if mmproj is not None else (tmp_path / "mmproj.gguf"))
    cfg.mmproj_configured = True
    cfg.startup_timeout = startup_timeout
    cfg.request_timeout = request_timeout
    return cfg


@pytest.fixture()
def mmproj_file(tmp_path):
    path = tmp_path / "mmproj.gguf"
    path.write_bytes(b"fake-mmproj-bytes-for-resolution-tests")
    model = tmp_path / "model.gguf"
    model.write_bytes(b"fake-model")
    return path


# ------------------------------------------------------------- resolution

def test_explicit_missing_helper_is_not_silently_repaired(tmp_path, mmproj_file):
    cfg = _cfg(tmp_path, helper=tmp_path / "does-not-exist", mmproj=mmproj_file)
    path, source = eh.resolve_helper_path(cfg)
    assert path is None and source == "explicit"


def test_home_before_vendor_resolution(tmp_path, mmproj_file, monkeypatch):
    cfg = _cfg(tmp_path, helper=None, mmproj=mmproj_file)
    cfg.embed_helper = Path("goldmine-embed")
    cfg.embed_helper_configured = False
    home_bin = tmp_path / "home" / "bin" / "goldmine-embed"
    home_bin.parent.mkdir(parents=True)
    home_bin.write_bytes(b"#!/bin/sh\n")
    monkeypatch.setattr(eh, "home_helper_path", lambda cfg: home_bin)
    monkeypatch.setattr(eh, "VENDOR_HELPER", tmp_path / "vendor" / "nope")
    path, source = eh.resolve_helper_path(cfg)
    assert path == home_bin and source == "home"


def test_mmproj_resolution_home_then_repo(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.mmproj = Path("mmproj-embeddinggemma-2-Q8_0.gguf")
    cfg.mmproj_configured = False
    home_copy = tmp_path / "home" / "models" / "mmproj-embeddinggemma-2-Q8_0.gguf"
    home_copy.parent.mkdir(parents=True)
    home_copy.write_bytes(b"x")
    monkeypatch.setattr(eh, "_goldmine_home", lambda cfg: tmp_path / "home")
    monkeypatch.setattr(eh, "REPO_ROOT", tmp_path)
    assert eh.resolve_mmproj_path(cfg) == home_copy


# ------------------------------------------------------------- lifecycle

def _embedder(tmp_path, mmproj_file, mode: str, **kw):
    import os

    cfg = _cfg(tmp_path, mmproj=mmproj_file, **kw)
    embedder = eh.MediaEmbedder(cfg, mmproj_file)  # model file = mmproj stand-in
    return embedder, mode


def test_ready_handshake_and_ping(tmp_path, mmproj_file, monkeypatch):
    embedder, _ = _embedder(tmp_path, mmproj_file, "ready")
    monkeypatch.setenv("FAKE_MODE", "ready")
    assert embedder.resolvable()
    assert embedder.capability() == (True, True)
    embedder.startup()
    try:
        assert embedder.ping() is True
        assert embedder.load_seconds is not None
    finally:
        embedder.close()


def test_ready_dim_mismatch_is_startup_error(tmp_path, mmproj_file, monkeypatch):
    embedder, _ = _embedder(tmp_path, mmproj_file, "bad-dim")
    monkeypatch.setenv("FAKE_MODE", "bad-dim")
    with pytest.raises(BackendStartupError):
        embedder.startup()
    embedder.close()


def test_ready_timeout_is_startup_error(tmp_path, mmproj_file, monkeypatch):
    embedder, _ = _embedder(tmp_path, mmproj_file, "slow", startup_timeout=0.5)
    monkeypatch.setenv("FAKE_MODE", "slow")
    with pytest.raises(EmbeddingError):  # read timeout surfaces from _read_line
        embedder.startup()
    embedder.close()


def test_bad_proto_ready_line(tmp_path, mmproj_file, monkeypatch):
    embedder, _ = _embedder(tmp_path, mmproj_file, "bad-proto")
    monkeypatch.setenv("FAKE_MODE", "bad-proto")
    with pytest.raises(BackendStartupError):
        embedder.startup()
    embedder.close()


def test_embed_roundtrip_unit_vector(tmp_path, mmproj_file, monkeypatch):
    embedder, _ = _embedder(tmp_path, mmproj_file, "ready")
    monkeypatch.setenv("FAKE_MODE", "ready")
    embedder.startup()
    try:
        png = tmp_path / "thumb.png"
        png.write_bytes(b"\x89PNG-fake")
        vec = embedder.embed_image(png, 140)
        assert vec.shape == (768,)
        assert np.all(np.isfinite(vec))
        assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-5
        wav = embedder.embed_audio_wav(b"RIFF-fake-wav-bytes")
        assert wav.shape == (768,)
    finally:
        embedder.close()


def test_budget_validation_is_client_side(tmp_path, mmproj_file, monkeypatch):
    embedder, _ = _embedder(tmp_path, mmproj_file, "ready")
    monkeypatch.setenv("FAKE_MODE", "ready")
    embedder.startup()
    try:
        with pytest.raises(EmbeddingError):
            embedder.embed_image(Path("/x.png"), 999)
    finally:
        embedder.close()


def test_helper_error_code_surfaced(tmp_path, mmproj_file, monkeypatch):
    embedder, _ = _embedder(tmp_path, mmproj_file, "err-decode")
    monkeypatch.setenv("FAKE_MODE", "err-decode")
    embedder.startup()
    try:
        with pytest.raises(EmbeddingError) as excinfo:
            embedder.embed_image(Path("/x.png"), 140)
        assert "decode_failed" in str(excinfo.value)
    finally:
        embedder.close()


def test_death_mid_run_is_hard_error_no_respawn(tmp_path, mmproj_file, monkeypatch):
    embedder, _ = _embedder(tmp_path, mmproj_file, "die-midrun")
    monkeypatch.setenv("FAKE_MODE", "die-midrun")
    embedder.startup()
    try:
        png = tmp_path / "a.png"
        png.write_bytes(b"x")
        embedder.embed_image(png, 140)  # first request ok (then child dies)
        with pytest.raises(EmbeddingError):
            embedder.embed_image(png, 140)  # EOF -> died mid-run
        # M11: the NEXT request may respawn — startup is idempotent-but-dead
        # children are re-spawnable; a broken fake then dies again, which is
        # the honest outcome.
        with pytest.raises(EmbeddingError):
            embedder.embed_image(png, 140)
    finally:
        embedder.close()


def test_no_audio_capability_degrades_honestly(tmp_path, mmproj_file, monkeypatch):
    embedder, _ = _embedder(tmp_path, mmproj_file, "no-audio")
    monkeypatch.setenv("FAKE_MODE", "no-audio")
    embedder.startup()
    try:
        assert embedder.capability() == (True, False)
        with pytest.raises(EmbeddingError) as excinfo:
            embedder.embed_audio_wav(b"RIFF")
        assert "audio" in str(excinfo.value)
    finally:
        embedder.close()


def test_close_kills_only_own_child(tmp_path, mmproj_file, monkeypatch):
    embedder, _ = _embedder(tmp_path, mmproj_file, "ready")
    monkeypatch.setenv("FAKE_MODE", "ready")
    embedder.startup()
    proc = embedder._proc
    assert proc is not None and proc.poll() is None
    embedder.close()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover
        pytest.fail("close() left the child alive")
    assert proc.returncode is not None


# ------------------------------------------------------- fingerprint guard

def test_fingerprint_without_media_is_byte_identical_to_premilestone(tmp_path, mmproj_file, monkeypatch):
    """P3/P5 guard (preregistration): with the helper/mmproj resolution
    disabled, fingerprints.build and live_embed_identity are EXACTLY the
    pre-multimodal shapes — text-only installs never reindex."""
    cfg = _cfg(tmp_path, helper=tmp_path / "nope", mmproj=mmproj_file)
    cfg.model = tmp_path / "no-model.gguf"  # nonexistent: sha is "missing"
    cfg.model_configured = True
    backend = LlamaServerBackend(cfg)
    monkeypatch.setattr(
        type(backend._media), "resolvable", lambda self: False
    )
    fp = fingerprints.build(backend)
    assert fp == {
        "backend": "llama",
        "model_id": cfg.model.stem,
        "model_sha256": "missing",  # tmp model file does not exist
        "dim": 768,
        "prompts": __import__(
            "goldmine.backends.base", fromlist=["PROMPT_SCHEME"]
        ).PROMPT_SCHEME,
        "pooling": "server:mean",
        "normalization": "l2_post",
        "preprocessing": fingerprints.PREPROCESSING_VERSION,
    }
    ident = fingerprints.live_embed_identity(backend)
    assert "media" not in ident and "mmproj_sha256" not in ident
    assert "media_budgets" not in ident and "media_routing" not in ident
    assert set(ident) == {
        "backend", "model_id", "model_sha256", "dim", "prompts_doc",
        "pooling", "normalization",
    }


def test_fingerprint_with_media_joins_all_four_extras(tmp_path, mmproj_file, monkeypatch):
    cfg = _cfg(tmp_path, mmproj=mmproj_file)
    backend = LlamaServerBackend(cfg)
    assert backend._media.resolvable()
    fields = backend.fingerprint_fields()
    # v5 (digup-p2-closeout-001 B1): audio tail kept only with >=2 s NEW
    # audio — the audio clause gains tail-min2s; frames/budgets/routing
    # unchanged from v4 (video-frames-001).
    assert fields["media"].startswith("v5:")
    assert "rms-gate0.003,tail-min2s;" in fields["media"]
    assert "resize=bicubic-a-0.5-48px-fill-budget" in fields["media"]
    assert "frames=fps1/3,ahash4bit,jpeg1024,max1200" in fields["media"]
    assert fields["media_routing"] == "raw-audio-only-when-no-transcript.v1"
    assert "photo140" in fields["media_budgets"] and "frame70" in fields["media_budgets"]
    assert "1120" in fields["media_budgets"]
    # mmproj sha recorded and stable via the sidecar cache
    assert fields["mmproj_sha256"] == eh._sha256_cached(mmproj_file, {})
    ident = fingerprints.live_embed_identity(backend)
    assert ident["media_preproc"] == fields["media"]
    assert ident["mmproj_sha256"] == fields["mmproj_sha256"]
    assert ident["media_budgets"] == fields["media_budgets"]
    assert ident["media_routing"] == fields["media_routing"]
    # the six canonical text-path values are UNTOUCHED (constraint 7)
    assert fields["prompts"] == __import__(
        "goldmine.backends.base", fromlist=["PROMPT_SCHEME"]
    ).PROMPT_SCHEME
    assert fields["pooling"] == "server:mean"
    assert fields["normalization"] == "l2_post"


def test_capability_flips_with_resolution(tmp_path, mmproj_file):
    cfg_off = _cfg(tmp_path, helper=tmp_path / "nope", mmproj=mmproj_file)
    off = LlamaServerBackend(cfg_off)
    assert off.capability() == off.capability().__class__(text=True, image=False, audio=False)
    assert not off.capability().image

    cfg_on = _cfg(tmp_path, mmproj=mmproj_file)
    on = LlamaServerBackend(cfg_on)
    assert on.capability().image and on.capability().audio


# --------------------------------------------------------- targetSize port

def _target_size(height: int, width: int, budget: int):
    """DigUp ImageSizing.swift:9-26, literal Python twin (h-first)."""
    patch, pooling = 16, 3
    max_patches = budget * pooling * pooling
    factor = math.sqrt(max_patches * patch * patch / (height * width))
    side = pooling * patch
    target_h = int(factor * height / side) * side  # Swift Int() = truncate
    target_w = int(factor * width / side) * side
    if target_h == 0 and target_w == 0:
        return None
    max_side = (max_patches // (pooling * pooling)) * side
    if target_h == 0:
        target_h = side
        target_w = min(width // height * side, max_side)  # INTEGER division
    elif target_w == 0:
        target_w = side
        target_h = min(height // width * side, max_side)
    return target_h, target_w


@pytest.mark.parametrize("h,w,budget,expected", [
    (1024, 768, 140, (624, 480)),
    (800, 1280, 280, (624, 1008)),
    (3000, 30, 70, (3360, 48)),      # rescue branch + integer division cap
    (32, 32, 70, (384, 384)),        # budget-filling UPSCALE (not too-thin —
                                     # see module docstring: unreachable guard)
    (256, 256, 140, (528, 528)),
    (48, 48, 1120, (1584, 1584)),
])
def test_target_size_table(h, w, budget, expected):
    assert _target_size(h, w, budget) == expected
