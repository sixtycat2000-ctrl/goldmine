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
`goldmine estimate <folder>` previews first-ingest time from metadata alone
(read-only). iCloud placeholders that were never downloaded are skipped —
indexing never triggers a download — and hits whose file vanished since the
last ingest are filtered at query time, so a result always points at
something on disk.

## Repair and source scope

```sh
goldmine repair --db library.sqlite --plan
goldmine repair /chosen/folder --db library.sqlite --missing-vectors
goldmine repair --db library.sqlite --failed-only
goldmine ingest /chosen/folder --db library.sqlite --reindex
goldmine sources --db library.sqlite
goldmine forget /chosen/folder/private --db library.sqlite --dry-run
goldmine forget /chosen/folder/private --db library.sqlite
goldmine sources restore /chosen/folder/private --db library.sqlite
```

`repair --plan` opens the index read-only and lists per-file reasons. Text
backfill retains stored original text, segment IDs and existing vectors; oversized
inputs and media that need source bytes use targeted extraction. Add
`--lexical-only` when retrying extraction without an embedding backend.
Same-model `ingest --reindex` rebuilds only the selected scope. Changing models
requires all recorded sources to be available and rebuilds their shared index.

`forget` removes indexed evidence and affected card snapshots, records a persistent
exclusion for ingest/watch, and leaves source files on disk. `sources remove` is
an alias. Restore the exclusion explicitly, then ingest to index that scope again.

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

[FEATURE_STATUS](docs/FEATURE_STATUS.md) is the current capability and quality
record. The frozen product suite contains 420 questions from 122 sources with
a grouped 210/210 dev/holdout split; the older 28-question corpus is a diagnostic
seed. The separate answer and card inputs contain 80 tasks and 120 documents.
Those prepared inputs do not establish answer or clustering quality.

Historical text/media gate results apply only to their recorded source and
index snapshots. The current lifecycle, filtered-retrieval and source-PTS changes
need fresh real-model quality measurements; no historical score is inherited.
The remaining real answer/grading jobs and card quality runs are recorded in
FEATURE_STATUS. Optional reranking and multi-query remain unvalidated experiments.

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
