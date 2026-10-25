"""Pinned model manifest (05 §4.3) — the DigUp ModelFiles pattern in Python.

Every downloadable asset goldmine knows about is a frozen constant here: HF
repo + immutable revision + filename + exact bytes + sha256. The manifest is
*code* (reviewed, versioned), not data — a manifest change is a deliberate act.

Manifest-vs-file test (B7, normative): every entry's `bytes` AND `sha256` are
asserted against the real file when it exists on disk — the VAD byte pin first
shipped as 885,000 in the design doc (real value 885,098), which is exactly the
typo class that test exists to catch.

Roles (C8): embedding | mmproj | asr | vad | ocr | server. Binaries (role
`server`, a future entry) install into `<home>/bin`; everything else into
`<home>/models`.

`subdir` (deviation from the §4.3 sketch, noted in the v2 report): the repo
development layout keeps ASR/VAD models under `models/whisper/`; the installed
layout under `<home>/models` is flat per §4.3. The field only affects repo-side
resolution, never the install destination.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelFile:
    key: str            # manifest name used by `goldmine model <cmd> <key>`
    role: str           # embedding | mmproj | asr | vad | reranker | ocr | server
    repo: str           # huggingface repo (org/name)
    revision: str       # immutable HF revision (commit sha) or branch
    name: str           # filename inside the repo / install dir
    bytes: int
    sha256: str
    dim: int | None = None   # embedding models: expected vector dim
    subdir: str = ""          # repo-side subdirectory ("" = models/ root)

    def url(self) -> str:
        return f"https://huggingface.co/{self.repo}/resolve/{self.revision}/{self.name}"


MANIFEST: list[ModelFile] = [
    ModelFile(
        key="embeddinggemma-2-Q8_0", role="embedding",
        repo="ggml-org/embeddinggemma-2-GGUF",
        revision="bfcd298762cc34d0357ece5ebdd31791a3a374d8",
        name="embeddinggemma-2-Q8_0.gguf",
        bytes=309_855_456,
        sha256="2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135",
        dim=768,
    ),
    ModelFile(
        key="mmproj-embeddinggemma-2-Q8_0", role="mmproj",
        repo="ggml-org/embeddinggemma-2-GGUF",
        revision="bfcd298762cc34d0357ece5ebdd31791a3a374d8",
        name="mmproj-embeddinggemma-2-Q8_0.gguf",
        bytes=554_821_024,
        sha256="c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f",
    ),
    ModelFile(
        key="whisper-large-v3-turbo-q5_0", role="asr",
        repo="ggerganov/whisper.cpp", revision="main",
        name="ggml-large-v3-turbo-q5_0.bin",
        bytes=574_041_195,
        sha256="394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
        subdir="whisper",
    ),
    ModelFile(
        # B7: bytes are 885,098 — NOT 885,000 (the design doc's first-draft
        # typo); the manifest-vs-file test pins this.
        key="silero-vad-v5.1.2", role="vad",
        repo="ggml-org/whisper-vad", revision="main",
        name="ggml-silero-v5.1.2.bin",
        bytes=885_098,
        sha256="29940d98d42b91fbd05ce489f3ecf7c72f0a42f027e4875919a28fb4c04ea2cf",
        subdir="whisper",
    ),
    ModelFile(
        # Qwen3 rank-head GGUF, verified against pinned b11461 /v1/rerank.
        # This head-aware conversion is distinct from ordinary causal GGUFs.
        key="Qwen3-Reranker-0.6B-Q8_0", role="reranker",
        repo="gorse-io/Qwen3-Reranker-0.6B-GGUF",
        revision="257c2d8ebe01523d5a93bd9eab2698b9c7f95ee1",
        name="Qwen3-Reranker-0.6B-Q8_0.gguf",
        bytes=639_153_376,
        sha256="a4e436162177a8b147940cd40fc952ca42ce91d39b0ca3247fa94770a4cb9990",
        subdir="reranker",
    ),
]

#: The default embedding model (`model use`, default resolution order).
DEFAULT_EMBEDDING_KEY = "embeddinggemma-2-Q8_0"
DEFAULT_RERANKER_KEY = "Qwen3-Reranker-0.6B-Q8_0"

_BY_KEY = {entry.key: entry for entry in MANIFEST}


def by_key(key: str) -> ModelFile | None:
    return _BY_KEY.get(key)


def entries_with_role(role: str) -> list[ModelFile]:
    return [entry for entry in MANIFEST if entry.role == role]


def repo_relpath(entry: ModelFile) -> str:
    """Path of `entry` relative to the repo models/ dir (dev checkout)."""
    return f"{entry.subdir}/{entry.name}" if entry.subdir else entry.name
