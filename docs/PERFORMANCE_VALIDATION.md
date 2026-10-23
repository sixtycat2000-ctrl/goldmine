# Reproducible performance evidence

Use an explicit frozen source copy, prepared model, server and index. Queries
are a JSON list of objects containing exactly id and query; use development
queries and never expose sealed holdout for tuning. The selected Python must
have the locked MCP SDK. The driver copies the SQLite input consistently and
explicitly upgrades only its disposable copy for citation-capable MCP calls.

```sh
python3 scripts/bench_search.py --python /absolute/mcp-venv/bin/python --source-root /absolute/frozen-source --db /absolute/index.sqlite --model /absolute/model.gguf --server /absolute/llama-server --queries /absolute/dev-queries.json --profile controlled --out reports/execution/search-controlled-001
```

Repeat with --profile real. Controlled pins residency at pressure1; real has no
override. Three fixed warmups precede at least30 warm/cold pairs, alternating
order per query. Each CLI sample is a new Python subprocess with raw argv,
JSON, stderr/via, process exit, measured subprocess wall time and pressure.
Nearest-rank p50/p95 are recomputed from those receipts. MCP initialization is
outside warm-call timing; at least30 same-request direct Application/stdio
pairs retain both complete payloads and alternate request order. Extra overhead
is the p95 of paired differences, never a subtraction of independent p95s.

Build the pre-task revision identified in baseline-20261021.json into another
frozen source copy and repeat controlled with --cli-only. This historical
reference lacks MCP and cannot satisfy MCP evidence. The three runs must use
the same queries/index/model/server. Fast acceptance requires p50<=300ms,
p95<=500ms, p50<=cold/3 and candidate/reference p50<=1.20. MCP extra p95<=100ms.
Changing current product sources makes an earlier snapshot insufficient for
final candidate acceptance even if its historical numbers passed.

Repeated-ingest evidence uses scripts/bench_ingest.py with --source-root,
--manifest, --model, --server and --out. Optional --ocr-helper, --whisper and
--models-dir select explicitly prepared real dependencies. It copies only
licensed source-manifest assets, counts the actual production calls and times
scan/hash/extract/embed/commit work. Worker-thread/inclusive busy times are
reported as such, rather than added up as end-to-end latency. The sequence is
first, unchanged, modified one, controlled embedding failure, recovery and
deleted one. Only disposable copies are edited. Stored file/segment/FTS/card
hashes must be identical on the unchanged pass; embed/OCR/ASR/extraction calls
must all be zero. A full proof requires those real media paths and persisted
cards to have been exercised initially; a text-only seed is MISSING.

The strict real distill dataset and command are documented in
evals/distill_real/README.md. scripts/capture_distill.py captures that evaluator
with raw receipts. Its real quality adapter replays the frozen original inputs,
compares every quality/provenance/membership result and ignores only observed
wall-clock durations. No qualifying100-document/8-topic dataset has been
claimed from the current12-source diagnostic product seed.
