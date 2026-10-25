"""Live MEDIA reference vectors (digup-plan-002 T6, the P0-2 extension).

Pins the EXACT image/audio vectors the pinned mmproj produces through the
resident helper for four frozen inputs — the media half of
test_reference_vectors.py's tripwire ("every llama.cpp update must reproduce
the reference vectors, or come with a new version here"). Never copied from
DigUp's vectors.json: the first-stage pixels are goldmine's own (sips).

Run: GOLDMINE_LIVE=1 python3 -m pytest tests/test_reference_media_vectors.py
Regenerate (deliberate act only):
  GOLDMINE_LIVE=1 GOLDMINE_WRITE_REFERENCE=1 python3 -m pytest ...
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pytest

from goldmine.backends.embed_helper import MediaEmbedder, PINNED_MMPROJ_SHA256
from goldmine.config import LlamaCfg
from goldmine.util import pack_fp16, unpack_fp16

pytestmark = [
    pytest.mark.live,
    pytest.mark.native,
    pytest.mark.skipif(
        not os.environ.get("GOLDMINE_LIVE"), reason="GOLDMINE_LIVE=1 required"
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "vendor" / "llama.cpp" / "build-static" / "bin" / "goldmine-embed"
MODEL = REPO_ROOT / "models" / "embeddinggemma-2-Q8_0.gguf"
MMPROJ = REPO_ROOT / "models" / "mmproj-embeddinggemma-2-Q8_0.gguf"
REFERENCE = Path(__file__).with_name("fixtures_data") / "reference_media_vectors.jsonl"
MEDIA_DIR = Path(__file__).with_name("fixtures_data") / "media"
COSINE_GATE = 0.999  # the same gate as the text fixture

_skip = pytest.mark.skipif(
    not HELPER.is_file(), reason=f"helper not built at {HELPER}"
)
_skip_models = pytest.mark.skipif(
    not (MODEL.is_file() and MMPROJ.is_file()), reason="pinned models missing"
)

#: The frozen input set (id, file, kind, budget). APPEND-only; never reorder.
REFERENCE_INPUTS = [
    ("img-photo-b140", "ref-photo-b140.jpg", "image", 140),
    ("img-shot-b280", "ref-shot-b280.png", "image", 280),
    ("aud-chirp-3s", "ref-audio-3s.wav", "audio", None),
    ("aud-sine-2s", "ref-audio-2s.wav", "audio", None),
]


def _fixture_rows():
    lines = [json.loads(line) for line in REFERENCE.read_text().splitlines()
             if line.strip() and not line.startswith("//")]
    header, rows = lines[0], lines[1:]
    return header, rows


def _embedder(tmp_path):
    cfg = LlamaCfg()
    cfg.home = tmp_path
    cfg.embed_helper = str(HELPER)
    cfg.embed_helper_configured = True
    cfg.startup_timeout = 180.0
    cfg.request_timeout = 120.0
    return MediaEmbedder(cfg, MODEL)


@_skip
@_skip_models
def test_media_reference_vectors_reproduced(tmp_path):
    digest = hashlib.sha256(MMPROJ.read_bytes()).hexdigest()
    assert digest == PINNED_MMPROJ_SHA256, (
        f"mmproj sha256 {digest} != pin — media reference vectors are "
        "meaningless against a different mmproj"
    )
    assert REFERENCE.is_file(), (
        f"media reference fixture missing at {REFERENCE} — regenerate with "
        "GOLDMINE_WRITE_REFERENCE=1 (a deliberate, reviewed act)"
    )
    header, rows = _fixture_rows()
    assert header.get("schema") == 1
    assert header.get("mmproj_sha256") == PINNED_MMPROJ_SHA256
    assert [row["id"] for row in rows] == [i for i, *_ in REFERENCE_INPUTS], (
        "input id set drifted — inputs may only be APPENDED, then the "
        "fixture regenerated"
    )

    embedder = _embedder(tmp_path)
    try:
        embedder.startup()
        for (input_id, filename, kind, budget), frozen in zip(REFERENCE_INPUTS, rows):
            path = MEDIA_DIR / filename
            assert hashlib.sha256(path.read_bytes()).hexdigest() == frozen["file_sha256"], (
                f"{filename} bytes drifted — the fixture inputs are frozen"
            )
            if kind == "image":
                live = embedder.embed_image(path, budget)
            else:
                live = embedder.embed_audio_wav(path.read_bytes())
            expected = unpack_fp16(base64.b64decode(frozen["vec_fp16_b64"]))
            got = np.asarray(live, dtype=np.float64)
            cos = float(np.dot(expected, got) / (
                np.linalg.norm(expected) * np.linalg.norm(got)
            ))
            assert cos >= COSINE_GATE, (
                f"media reference vector {input_id!r} drifted: cosine "
                f"{cos:.6f} < {COSINE_GATE} — the vector family changed under "
                "this build; bump vector_version deliberately or fix the "
                "regression"
            )
            live_fp16 = hashlib.sha256(pack_fp16(np.asarray(live))).hexdigest()
            assert live_fp16 == frozen["vec_fp16_sha256"], (
                f"media reference vector {input_id!r}: fp16 bytes differ from "
                "the frozen sha256"
            )
    finally:
        embedder.close()


@_skip
@_skip_models
def test_regenerate_media_reference_vectors(tmp_path):
    """Deliberate regeneration path (GOLDMINE_WRITE_REFERENCE=1 only): writes
    the fixture and FAILS the run by design (the text fixture's protocol)."""
    if not os.environ.get("GOLDMINE_WRITE_REFERENCE"):
        pytest.skip("GOLDMINE_WRITE_REFERENCE=1 required to regenerate")
    embedder = _embedder(tmp_path)
    try:
        embedder.startup()
        rows = []
        for input_id, filename, kind, budget in REFERENCE_INPUTS:
            path = MEDIA_DIR / filename
            vec = (embedder.embed_image(path, budget) if kind == "image"
                   else embedder.embed_audio_wav(path.read_bytes()))
            rows.append({
                "id": input_id,
                "kind": kind,
                "budget": budget,
                "file": f"media/{filename}",
                "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "vec_fp16_b64": base64.b64encode(pack_fp16(vec)).decode("ascii"),
                "vec_fp16_sha256": hashlib.sha256(pack_fp16(vec)).hexdigest(),
            })
    finally:
        embedder.close()
    old_rows = []
    if REFERENCE.is_file():
        lines = [json.loads(line) for line in REFERENCE.read_text().splitlines()
                 if line.strip() and not line.startswith("//")]
        old_rows = lines[1:]
    old_shas = [row.get("vec_fp16_sha256") for row in old_rows]
    changed = any(
        old == new["vec_fp16_sha256"]
        for old, new in zip(old_shas, rows)
        if old is not None
    ) and old_shas != [row["vec_fp16_sha256"] for row in rows]
    header = {
        "schema": 1,
        "vector_version": 2 if changed else 1,
        "mmproj_sha256": PINNED_MMPROJ_SHA256,
        "helper": "goldmine-embed 1",
    }
    REFERENCE.write_text(
        json.dumps(header) + "\n"
        + "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    pytest.fail(
        "media reference fixture regenerated (vector_version "
        f"{'bumped — an existing vector changed' if changed else 'initial/unchanged'}); "
        "review the diff, commit deliberately, then re-run WITHOUT "
        "GOLDMINE_WRITE_REFERENCE"
    )
