"""Tier-3 LIVE reference vectors (P0-2, DigUp A3 second half).

Pins the EXACT vectors the pinned model (embeddinggemma-2-Q8_0 @ manifest
sha256) produces through the vendored llama-server (b11461) for a frozen set
of real prompts. This is goldmine's port of DigUp's ReferenceVectorTests
(LlamaEmbedder.swift:18-22): "every llama.cpp update must reproduce the
reference vectors, or come with a new version here (then every index embeds
again)". A llama.cpp/build change that leaves vectors identical keeps every
index (the fingerprint deliberately excludes the build string); a change that
moves vectors is caught HERE before it can silently mix vector families.

Run: GOLDMINE_LIVE=1 python3 -m pytest tests/test_reference_vectors.py
Regenerate (deliberate act only, bumps vector_version in the fixture):
  GOLDMINE_LIVE=1 GOLDMINE_WRITE_REFERENCE=1 python3 -m pytest ...
The model file's sha256 must equal the manifest pin before anything runs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pytest

from goldmine.backends.base import document_prompt, query_prompt
from goldmine.backends.llama import VENDOR_SERVER, LlamaServerBackend
from goldmine.config import LlamaCfg
from goldmine.model_manifest import by_key
from goldmine.util import pack_fp16, unpack_fp16

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("GOLDMINE_LIVE"),
        reason="GOLDMINE_LIVE=1 required for live llama tests",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL = REPO_ROOT / "models" / "embeddinggemma-2-Q8_0.gguf"
REFERENCE = Path(__file__).with_name("fixtures_data") / "reference_vectors.json"
COSINE_GATE = 0.999  # DigUp's acceptance: "cosine >= 0.999 vs reference"

_skip_binary = pytest.mark.skipif(
    not VENDOR_SERVER.is_file(),
    reason=f"vendor llama-server not built at {VENDOR_SERVER}",
)
_skip_model = pytest.mark.skipif(
    not MODEL.is_file(), reason=f"model GGUF missing at {MODEL}"
)

#: The frozen input set (P0-2 preregistration digup-plan-001). IDs are stable;
#: adding inputs APPENDS (never reorders) — regeneration rewrites vectors for
#: all ids and bumps vector_version only when an existing id's vector changed.
REFERENCE_INPUTS: list[tuple[str, str]] = [
    ("q-cjk", query_prompt("退款期限为四十五天")),
    ("q-english", query_prompt("memory pressure in the indexing worker")),
    ("q-lookup", query_prompt("K7Q2LM")),
    ("d-cjk", document_prompt("notes", "中文知识库:退款期限为四十五天。")),
    (
        "d-code",
        document_prompt(
            "retry.py",
            "def retry(fn, attempts=3):\n"
            "    for i in range(attempts):\n"
            "        try:\n"
            "            return fn()\n"
            "        except Exception:\n"
            "            continue\n"
            "    raise RuntimeError('exhausted')\n",
        ),
    ),
    (
        "d-words",
        document_prompt(
            None,
            "alpha beta gamma delta epsilon zeta eta theta iota kappa "
            "lambda mu nu xi omicron pi rho sigma tau",
        ),
    ),
    (
        "d-long",
        document_prompt(
            "policy",
            "The refund window is forty-five days from the date of purchase. "
            "Requests must include the original receipt. "
            * 18,
        ),
    ),
    (
        "d-mixed",
        document_prompt(
            "mixed 中英 mixed",
            "The refund window 退款期限 is forty-five days, per policy v2.",
        ),
    ),
]
#: P1-2/P1-1 inputs (q-code / d-code-title) were appended for the 2026-10-25
#: A/B, then removed in lockstep with the measured REJECTION of that change
#: (reports/execution/digup-plan-001/p1-code-title-prompt/). Inputs may only
#: be APPENDED for an ACTIVE scheme; do not reuse the retired ids.


def _model_sha256() -> str:
    digest = hashlib.sha256()
    with open(MODEL, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 23), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _encode(vec: np.ndarray) -> str:
    return base64.b64encode(pack_fp16(vec)).decode("ascii")


def _decode(blob: str) -> np.ndarray:
    return unpack_fp16(base64.b64decode(blob))


@_skip_binary
@_skip_model
def test_reference_vectors_reproduced_by_current_build(tmp_path):
    manifest = by_key("embeddinggemma-2-Q8_0")
    assert manifest is not None
    live_sha = _model_sha256()
    assert live_sha == manifest.sha256, (
        f"model file sha256 {live_sha} != manifest pin — reference vectors "
        "are meaningless against a different model"
    )
    assert REFERENCE.is_file(), (
        f"reference fixture missing at {REFERENCE} — regenerate with "
        "GOLDMINE_WRITE_REFERENCE=1 (a deliberate, reviewed act)"
    )
    fixture = json.loads(REFERENCE.read_text(encoding="utf-8"))
    assert fixture["model_sha256"] == manifest.sha256
    assert [i for i, _ in REFERENCE_INPUTS] == fixture["ids"], (
        "input id set drifted from the fixture — inputs may only be APPENDED, "
        "then the fixture regenerated"
    )

    cfg = LlamaCfg(
        server=VENDOR_SERVER,
        model=MODEL,
        ctx=2048,
        batch=4,
        startup_timeout=180.0,
        request_timeout=60.0,
    )
    backend = LlamaServerBackend(cfg)
    try:
        backend.startup()
        live = backend.embed_texts([prompt for _, prompt in REFERENCE_INPUTS])
    finally:
        backend.close()

    worst = 1.0
    for idx, (input_id, _) in enumerate(REFERENCE_INPUTS):
        expected = _decode(fixture["vectors"][idx])
        got = np.asarray(live[idx], dtype=np.float64)
        cos = float(np.dot(expected, got) / (
            np.linalg.norm(expected) * np.linalg.norm(got)
        ))
        worst = min(worst, cos)
        assert cos >= COSINE_GATE, (
            f"reference vector {input_id!r} drifted: cosine {cos:.6f} < "
            f"{COSINE_GATE} — vectors changed under this build; bump "
            "vector_version in the fixture (deliberate act: every index "
            "re-embeds) or fix the regression"
        )
        # exact-bytes pin: fp16 round-trip of the live vector must hash to
        # the frozen per-vector sha256 (guards silent low-bit drift that a
        # loose cosine could hide)
        live_fp16 = hashlib.sha256(pack_fp16(np.asarray(live[idx]))).hexdigest()
        assert live_fp16 == fixture["vectors_sha256"][idx], (
            f"reference vector {input_id!r}: fp16 bytes differ from the "
            "frozen sha256 (cosine was within gate)"
        )
    print(f"reference vectors: {len(REFERENCE_INPUTS)} inputs, worst cosine {worst:.6f}")


@_skip_binary
@_skip_model
def test_regenerate_reference_vectors(tmp_path):
    """Deliberate regeneration path (GOLDMINE_WRITE_REFERENCE=1 only).

    Writes the fixture from the live backend and FAILS the run by design, so
    a regeneration can never pass unnoticed: the operator reviews the diff,
    bumps vector_version when an existing vector changed, and re-runs the
    reproduction test without the flag.
    """
    if not os.environ.get("GOLDMINE_WRITE_REFERENCE"):
        pytest.skip("GOLDMINE_WRITE_REFERENCE=1 required to regenerate")
    cfg = LlamaCfg(
        server=VENDOR_SERVER,
        model=MODEL,
        ctx=2048,
        batch=4,
        startup_timeout=180.0,
        request_timeout=60.0,
    )
    backend = LlamaServerBackend(cfg)
    try:
        backend.startup()
        live = backend.embed_texts([prompt for _, prompt in REFERENCE_INPUTS])
        build = backend.build_string
    finally:
        backend.close()
    vectors = [np.asarray(v) for v in live]
    old = json.loads(REFERENCE.read_text(encoding="utf-8")) if REFERENCE.is_file() else {}
    old_shas = old.get("vectors_sha256", [])
    # zip stops at the shorter list: APPENDED inputs never bump the version —
    # only a changed vector of an EXISTING id does.
    changed = any(
        old_sha != hashlib.sha256(pack_fp16(v)).hexdigest()
        for old_sha, v in zip(old_shas, vectors)
    )
    REFERENCE.parent.mkdir(parents=True, exist_ok=True)
    REFERENCE.write_text(
        json.dumps(
            {
                "schema": 1,
                "vector_version": (old.get("vector_version", 0) + 1)
                if changed
                else old.get("vector_version", 1),
                "model_sha256": _model_sha256(),
                "llama_build_recorded": build,
                "ids": [i for i, _ in REFERENCE_INPUTS],
                "vectors": [_encode(v) for v in vectors],
                "vectors_sha256": [
                    hashlib.sha256(pack_fp16(v)).hexdigest() for v in vectors
                ],
                "inputs_sha256": hashlib.sha256(
                    "\n".join(prompt for _, prompt in REFERENCE_INPUTS).encode()
                ).hexdigest(),
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    pytest.fail(
        "reference fixture regenerated (vector_version "
        f"{'bumped — an existing vector changed' if changed else 'unchanged'}); "
        "review the diff, commit deliberately, then re-run the reproduction "
        "test WITHOUT GOLDMINE_WRITE_REFERENCE"
    )
