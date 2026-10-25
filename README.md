# goldmine

A local macOS CLI for indexed documents, source code, PDF pages, OCR and timestamped
speech transcripts. SQLite holds the source text, lexical indexes and 768-dimensional
fp16 vectors. You explicitly choose every indexed folder or URL.

The default llama backend embeds text. With the optional media helper
(goldmine-embed) and the pinned mmproj model installed, photos, screenshots
and thin PDF pages also get image vectors, transcript-less audio gets
raw-audio vectors, and videos get keyframe image vectors (one sample every
~3 s, aHash near-duplicate merge, JPEG ≤1024, at most 1200 frames — visual
search); without them photo names/metadata and
actual OCR remain separate evidence types and images are metadata-only.
Cards contain source excerpts. The optional quality mode is an unvalidated
experiment.

This branch implements the [P0–P2 execution plan](docs/execution-plan/README.md).
The package stays **0.2.0 development** until the required quality and release checks
pass. Current capabilities and missing evidence are listed in
[FEATURE_STATUS](docs/FEATURE_STATUS.md), with implementation evidence in
[PROGRESS](docs/execution-plan/PROGRESS.md). No public release is configured.

## Start with local keyword search

Python 3.12+; the base package depends only on numpy. From a checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
mkdir -p sample
printf '中文知识库：退款期限为四十五天。\n' > sample/notes.md
.venv/bin/goldmine ingest sample --db sample.sqlite --lexical-only
.venv/bin/goldmine search '退款期限' --db sample.sqlite --lexical --pack
```

Use `--code` to include source files. `goldmine status --db sample.sqlite --json`
shows stored capabilities and staleness. `goldmine backup --db sample.sqlite --out
backup.sqlite` creates a verified backup. `goldmine reindex lexical --db sample.sqlite`
explicitly rebuilds derived CJK/exact indexes from stored text, with no model or
source-file reads. Searches and diagnostics open existing indexes read-only.

## Semantic search and optional tools

Explicitly install the pinned embedding model with `goldmine model install
embeddinggemma-2-Q8_0`. Supply an existing compatible llama-server through
`GOLDMINE_LLAMA_SERVER=/absolute/path/llama-server`; the project pins llama.cpp b11461
and its reviewed patch. [CI and installation](docs/CI.md) describe local artifact
validation. The native build entry is `python3 scripts/build_native.py --help`.
A downloaded model alone does not provide the server binary.

```sh
goldmine ingest /chosen/folder --db library.sqlite
goldmine search 'memory pressure' --db library.sqlite --pack --budget 4000
goldmine serve --db library.sqlite
goldmine watch --roots /chosen/folder --db library.sqlite
```

`serve` and `watch` are explicitly started commands. `init` provides resumable setup
and asks for scope; it never defaults to scanning your whole home directory.
OCR needs the native Vision helper, PDF extraction needs Poppler, and ASR needs
ffmpeg, whisper-cli and pinned speech/VAD models. Missing, failed and no-text
results remain distinguishable in stored evidence status.

Install `.[mcp]` from a checkout, or the MCP extra of the locally built wheel,
then run `goldmine mcp --db library.sqlite`. The four read-only tools are search,
get, context and status. [MCP](docs/MCP.md) documents the exact SDK lock, budgets,
opaque citations and cancellation behavior.

`--mode quality --multi-query` enables conservative deterministic compound queries.
`--mode quality --rerank local` additionally requires the separately installed
`Qwen3-Reranker-0.6B-Q8_0` model. Both default off. Compatibility checks pass;
quality benefit is **not established**. See [the experiment](docs/RERANK_EXPERIMENT.md).
The [vector cache](docs/VECTOR_CACHE.md) is separately built and enabled explicitly;
SQLite remains authoritative and cache defects fall back to it.

## Evidence and validation

The historical legacy suite has 109 queries (104 answerable items and 5 noise
items; the 19 capability-gated media queries are excluded from its frozen
denominator). On the original production baseline, the 2026-10-21 real-model
reproduction matched success@5 **0.7862**, success@1 **0.5931**, MRR **0.6835** and
nDCG@10 **0.6885** exactly. These numbers describe that baseline, not an improvement
claim for this branch. The independent full-media suite has 136 fixed queries
(78 real + 58 media, 27 of them needs-gated; 8 are keyframe-retrieval questions with moment-level location verdicts); its stored-evidence preflight
(EXIF/OCR/ASR provenance) passes against the real-backend corpus index, and its
formal gate is pending the multimodal landing. Re-baselined from the stale 169
on 2026-10-25 (receipt: reports/execution/digup-plan-002/step-1-baseline/).

The portable product suite requires 420 audited questions. Its current draft has
28 questions from 12 original MIT sources, with no frozen holdout. The remaining
392 questions and real PDF/media assets are missing. New independent 80-question
answer/grading runs and the real 100-document, eight-topic card corpus are also
missing. No final quality milestone or release candidate is declared.

Run the fixed hermetic profile with:

```sh
python3 scripts/acceptance.py run --profile offline --out reports/execution/offline-001
```

Install test dependencies from `requirements/test.lock` first. Native, real assets,
optional SDKs and live validation use separate profiles; required skips and missing
raw evidence do not count as passes. [CI](docs/CI.md) explains local versus hosted
verification. [COMPARISON](docs/COMPARISON.md) records the reproducible QMD/Recoll work.

Further references: [SPEC](SPEC.md), [architecture](ARCHITECTURE.md),
[user guide](docs/USER_GUIDE.md), [module map](docs/MODULE_MAP.md),
[test plan](docs/TEST_PLAN.md), [execution contracts](docs/execution-plan/CONTRACTS.md).
