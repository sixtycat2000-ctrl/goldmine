# goldmine v2 — Performance & Scale (04)

> Measured-first performance design for v2. Every number below was produced on the target
> machine (Mac mini M4, 10-core, 16GB, macOS 26.5.2, Python 3.12.0 pyenv, numpy 2.5.4,
> vendor llama.cpp b11461 / Metal, EmbeddingGemma-2 Q8_0 310MB) with throwaway scripts in
> `/tmp/gm_perf/` (listed in §3.8). Companion docs: `02-ingestion-extraction.md`,
> `06-distill-knowledge.md`.

---

## 1. Mission & performance targets

v2 goal: **search anything LOCAL and ACCURATE and easy**. Performance serves those: warm
search must be sub-second at realistic corpus sizes, indexing must not make users wait, and
16GB of unified memory is a hard ceiling shared with the user's actual work.

| # | Metric | v1 measured (this machine) | v2 target |
|---|---|---|---|
| T1 | Search e2e, cold command, 100k vectors (spawn + load + query) | **1.9s** (1.10 spawn + 0.55 load + 0.25 first query) | ≤ 0.7s cold CLI; ≤ 0.3s with daemon |
| T2 | Search warm p50, 100k vectors | 222ms in-process; ~0.9s per command (reload each time) | **p50 ≤ 300ms, p95 ≤ 500ms** (design lands ~40ms) |
| T3 | Vector scan + fusion, 500k vectors | 874ms load + 21ms matmul + 106ms Python loop ≈ **1.0s** | ≤ 60ms total (mmap fp16 blockwise + vectorized fusion) |
| T4 | Same, 1M vectors | not viable (≈1.75s load, 3GB fp32 resident) | ≤ 150ms scan; file-backed, no fp32 residency |
| T5 | Query embed (search path) | 9–12ms warm | unchanged (already negligible) |
| T6 | Embed throughput, 1800-char (~366 tok) chunks | 16–18 texts/s (6.0–6.5k tok/s), **GPU-bound**: −np 1→16 and batch 8→64 change nothing | keep ≥ 16 texts/s; overlap extraction so embed is 100% busy |
| T7 | Ingest, real code+docs corpus (118 files, 3.3MB) | 75.4s, of which embed 74.7s (**99%**); 94 files/min | ≥ measured v1 rate (94 files/min, no regression); ETA shown before run, ±20% |
| T8 | llama-server spawn per command | 0.63–1.1s to healthy (+1.1s ingest startup incl. probe) | 0 per command (persistent daemon); ≤ 0.7s daemon cold start |
| T9 | Resident memory, search @100k | 729MB process tree (server 484MB + fp32 matrix 307MB) | ≤ 600MB steady @100k; ≥ 2GB fp32 residency **never**; 1M vectors = file-backed pages, not RSS |
| T10 | Ingest ETA error | none exists | first-pass estimate within ±20% after first calibrated run |

<!-- amended 2026-10-16: B8 (Reviewer 1) / C2 (Reviewer 2) — T7 restated: the old
     "≥ 100 files/min" mis-attributed headroom to ingest overlap; extraction is <1% of
     wall (§3.3) and overlap recovers ≤1%. The real lever is token chunking (28.2 vs
     16.3 texts/s at ~250-tok chunks, §3.2). Target is now "no regression vs measured
     v1 (94 files/min)"; improvements ride the chunker work (01 §5.5), measured and
     published per run. -->
Non-goals: distributed/sharded indexes, GPU other than Apple Metal, sub-10ms latencies
(local CLI, not a service), recall loss of any kind in v2.0 (exact search stays exact).

---

## 2. v1 state (what exists, with file:line)

| Area | Where | What it does | Cost (measured) |
|---|---|---|---|
| Vector load per query | `goldmine/store.py:489-519` (`load_vectors`), block size `store.py:23` (`BLOCK_ROWS=8192`) | SELECTs every `vec` blob, fp16→fp32 in blocks, builds `(N,768)` f32 matrix | 173ms @100k warm, 874ms @500k warm, 2.0s @500k semi-cold; peak RSS 2.4GB @500k |
| Search calls it every query | `goldmine/search.py:149` (`table = self.store.load_vectors()`), matmul at `search.py:153` | brute-force `matrix @ q` over ALL segments | matmul itself: 0.4/4.2/21ms @10k/100k/500k — **not** the bottleneck |
| Per-row Python fusion loop | `goldmine/search.py:155-160` | per-row dict updates for best-per-file z | 20.9ms @100k, **106ms @500k** — second bottleneck |
| z-scores | `goldmine/search.py:294-309` | per-modality centering, vectorized numpy | 1.5ms @100k (incl. matmul) — fine |
| Per-command server spawn | `goldmine/backends/llama.py:258-281` (`_spawn`), readiness `llama.py:283-296`, retry `llama.py:332-372` | llama-server subprocess per command; `-c ctx*batch -b same -ub ctx -np batch` | 0.63–1.1s spawn every search/ingest command |
| Serial embed batches | `goldmine/backends/llama.py:466-475`; called per file from `ingest/pipeline.py:710-715` | one in-flight batch of `cfg.batch` (16), no overlap with extraction | throughput is GPU-bound at 16–18 texts/s; batch/np tuning has no effect (§3.2) |
| Extraction pool | `ingest/pipeline.py:259-274` (waves of `workers*8`), pool `pipeline.py:260`; workers default `config.py:47`; sha256 serial in main loop `pipeline.py:273` | ThreadPool(2) extraction; embedding serial in main thread after wave futures resolve | extraction is <1% of wall clock for text/code/PDF-text corpora (§3.3) |
| Flags summary | `ARCHITECTURE.md` §7.1 (lines 337–352, thread model), §7.2 (354–374, llama lifecycle), §6 (storage DDL 250–327, "brute force is milliseconds" at 329–332) | — | the §6 claim holds to ~100k, breaks at 500k because of load, not matmul |

v1 is honest and simple, and its two real problems are exactly measurable: **it re-acquires
the whole vector corpus per command** (load 0.17–2.0s + spawn 0.6–1.1s) and **it leaves the
GPU idle while Python loops and SQLite fetch**. The embed rate itself is a hardware ceiling
no code change will move.

---

## 3. Measurements (method + numbers)

### 3.1 Search latency vs corpus size (synthetic fp16, real store.py)

Method: `/tmp/gm_perf/gen_db.py` builds real goldmine indexes (Store, `replace_segments`,
fp16 blobs) with random-unit-normal 768-dim vectors at 10k/100k/500k; `/tmp/gm_perf/bench_search.py`
times each phase (medians; warm = 2nd+ run in-process, OS page cache warm).

| N vectors | load_vectors cold / warm | matmul f32 | zscores (incl matmul) | Python fusion loop | semantic path (no embed) | fp16→f32 conv+matmul | numpy fp16-native matmul |
|---|---|---|---|---|---|---|---|
| 10k | 44.5 / 15.9 ms | 0.42 ms | 0.60 ms | 2.1 ms | 2.1 ms | 0.86 ms | 8.7 ms |
| 100k | 445 / 173 ms | 4.2 ms | 5.7 ms | 20.9 ms | 20.7 ms | 9.0 ms | 93 ms |
| 500k | 2459 / 874 ms | 21.3 ms | 28.7 ms | 106 ms | 106 ms | 48 ms | 435 ms |

- Matrix is `(N,768)` fp32: 31/307/1536MB. fp16 blobs: half that on top during load.
- **numpy 2.5.4 fp16-native matmul is ~10× slower than fp32** on this build (no fast fp16
  GEMM path): converting fp16→fp32 first is mandatory. DigUp's vDSP design translates to
  numpy as "blockwise `astype(float32)` then f32 matmul", not as fp16 arithmetic.
- v1 full query path with the real backend, 100k (`bench_e2e.py`): spawn 1.10s +
  load_vectors 552ms + first hybrid search 246ms (incl. query embed); warm same-process
  search p50 222ms; peak process-tree RSS 729MB.

### 3.2 llama-server embed throughput and spawn cost

Method: `/tmp/gm_perf/bench_embed.py`, `bench_embed2.py` spawn the vendor b11461 server
directly (same flags as `llama.py:_spawn`) and time `/v1/embeddings` over HTTP, serial
client like v1.

- Spawn → `/health` 200: **912ms** first spawn, **629–670ms** re-spawn (model in page
  cache). First embed after spawn: 7ms. `startup()` as the pipeline does it (`--version`,
  model sha256 sidecar, dim probe): **1.08–1.09s**.
- Server RSS: **483–484MB** steady (`-c 8192 -np 4`; the GGUF dominates; KV for this model
  is small). This is the number a "keep it resident" decision costs: ~0.5GB of 16GB.
- Query embed (14-token prompt): **p50 9–12ms** warm — the search-path embed is noise.
- Doc chunks, 1800 chars ≈ **366 tok** (server-reported prompt_tokens): serial batches of
  16 → **16.3 texts/s (5981 tok/s)**; one 64-text request → **17.6 texts/s**; np=4 →
  **17.7–17.9**. With ~250-tok chunks: 28.2 texts/s at np=1 → 31.6 at np=8 → 29.4 at np=16.
- **−np is irrelevant**: np 1/2/4/8/16 → 28.2/30.1/31.2/31.4/29.4 texts/s (±5%, within
  run variance). Client batch 8→64: 29.5→30.3. Two concurrent clients: 30.4 aggregate.
  Embedding is compute-bound on the GPU; parallel slots and bigger batches only change
  latency granularity (a 16-text request takes 509ms; a 64-text request 3.65s).
- Effective ingest rate is lower than microbench: **11.1 texts/s** on the code corpus
  (code tokenizes at ~0.67 chars/tok vs ~4.9 for prose, plus `title:` prompt framing).

### 3.3 Ingest end-to-end, extraction vs embedding

Method: `/tmp/gm_perf/bench_ingest.py`, `bench_ingest_pdf.py` drive the real
`IngestPipeline`; `embed_texts` is shimmed to accumulate wall time (no repo changes);
lexical-only runs give the no-embed floor.

| Corpus | files | segs | lexical-only (extract+FTS+DB) | full ingest | embed share | effective rate |
|---|---|---|---|---|---|---|
| `/tmp/gm_demo` (md) | 5 | 37 | <0.05s | 3.90s | ~2.8s (32 texts) | 11.4 texts/s |
| DigUp+goldmine sources (code+md, 3.3MB) | 118 | 953 | **0.11–0.13s** (workers 1–8 identical) | **75.4s** | **74.7s = 99%** (831 texts) | 11.1 texts/s, 94 files/min |
| 3 real PDFs, 14.3MB | 3 | 83 | 0.12s (w≥2), 0.30s (w=1) | 7.2s | 6.2s (60 texts) | 9.7 texts/s, 25 files/min |

- Extraction pool sizing is a non-issue for text corpora: workers 1/2/4/8 all land 0.11–0.13s.
  (DigUp's Estimator costs — image 0.12s, screenshot 0.32s — only matter for scanned
  PDFs/OCR, which v1 doesn't do.)
- One real failure observed: a code stretch produced a **2670-token** input against the
  2048 `-ub` ceiling → HTTP 400 → retry → file `failed`. `chunker.est_tokens` assumes prose
  density; code at 1800 chars can exceed ctx after `title:` framing.

### 3.4 Vector-scan options at 500k / 1M (`bench_scan.py`)

<!-- amended 2026-10-16: B5 (Reviewer 1) — numbers re-measured and republished: option B
     is 44.6–47 ms @500k (the earlier 26 ms did not reproduce); block size is insensitive
     across 4096–16384 (the old "16384: 48ms" note was run noise); whole-array astype is
     93.6 ms, not 575 ms (rejected for the 1.5GB spike, not speed). All §1 targets still
     hold: scan ~45 ms + fusion ~2 ms ≤ the 60 ms T3 budget. -->
Raw fp16 pack file (768 cols), query vector random unit, medians:

| Option | 500k | 1M | Notes |
|---|---|---|---|
| A: v1 (SQLite blob read + convert + matmul) | 1986ms read + 90ms | — | read dominates; 2.4GB peak RSS |
| B: mmap fp16 file, blockwise f32 convert + matmul, block 4096 | **44.6–47 ms** | 88–118 ms | RSS = page cache only (file-backed, purgeable); block size insensitive 4096–16384 |
| C: whole-array astype + matmul | 93.6 ms (1.5GB spike) | n/a | rejected: memory spike, not speed |
| D: resident fp32 matrix, matmul only | 21 ms (but 1536MB resident + 503ms one-time conv) | 42 ms (3GB!) | rejected as default; acceptable ≤250k |
| E: `np.argpartition` top-5 on scores | 4.3 ms | 5.6 ms | the missing top-k piece |

### 3.5 sqlite-vec availability (macOS arm64 + Python)

`sqlite-vec 0.1.9` installs cleanly as a wheel, **but is unusable in goldmine's runtime**:
loading it requires `sqlite3.Connection.enable_load_extension`, and both pyenv 3.12.0
(goldmine's pinned interpreter) and /usr/bin/python3 3.9.6 ship sqlite3 **without loadable
extension support** (verified: attribute absent). Adopting sqlite-vec means forcing every
user to rebuild Python (`CONFIGURE_OPTS=--enable-loadable-sqlite-extensions`) or add
`pysqlite3-binary` — a hard, invisible dependency for zero measured benefit (§3.4: exact
scan is ≈ 45 ms @500k).

<!-- amended 2026-10-16: B6 (Reviewer 1) — attribution corrected: the 0.8.0 wheel is NOT
     broken; the recall collapse was random-data geometry + moderate ef, a documented HNSW
     behavior. The kill decision stands, but on exact-scan speed, not correctness. -->
### 3.6 ANN (hnswlib) — measured and rejected for v2

`hnswlib 0.8.0` (wheel, macOS 26.5.2 arm64, CPython 3.12) installs and works: on sane,
cluster-structured data recall@5 is ≈ 0.8 (1k×64 sanity check, `space='ip'`). This doc's
first pass misattributed a recall collapse ("recall@5 0.05–0.09 at 100k×768") to a broken
wheel; re-examination showed those vectors were random-unit geometry (no cluster
structure) queried at a moderate `ef` — documented HNSW behavior on degenerate data, not
a defect. The kill decision STANDS, but on speed: the measured exact scan (≈45 ms @500k,
~90 ms @1M, §3.4) leaves ANN nothing to win below ~2M vectors. Any future adoption still
re-validates the wheel on the target OS and requires recall@5 = 1.00 vs exact on real
data. faiss-cpu was not benchmarked: same adoption bar (heavy wheel, new dep) with no
problem left to solve at goldmine's scale.

### 3.7 Spawn cost, cold vs warm

| Scenario | to healthy | incl. probe |
|---|---|---|
| First spawn after boot-ish (model possibly cached) | 912ms | 1.08s (as `startup()` does) |
| Warm re-spawn (pages cached) | 629–670ms | ~1.0s |

v1 docs said "~2s cold"; the measured floor is ~0.6–1.1s on this machine — still a full
second of dead time per search command, which only a persistent process removes.

### 3.8 Scripts (all throwaway, /tmp only)

`/tmp/gm_perf/`: `gen_db.py`, `bench_search.py`, `bench_scan.py`, `bench_embed.py`,
`bench_embed2.py`, `bench_ingest.py`, `bench_ingest_pdf.py`, `bench_e2e.py`, `venv/`
(throwaway venv for sqlite-vec/hnswlib checks). Nothing in the repo was modified except
this doc.

---

## 4. DigUp reference (what to copy, with file:line)

| Idea | Where | What it does | Adopt for v2 |
|---|---|---|---|
| fp16 as storage AND scan format | `Vectors.swift:4-20` | fp16 kept in memory, converted to f32 per block for scoring | yes — numpy blockwise `astype` |
| Anonymous-mmap vector buffer | `Vectors.swift:22-52` (`VectorStorage`, `blockRows=1024` at :29) | mmap'd halves + f32 block so release really frees (malloc caches freed 60MB blocks, measured :23-27) | yes for the daemon cache |
| Block matmul | `Searcher.swift:283-304` (`score`, `vDSP_mmul` at :296) | 1024-row blocks, f32 convert then GEMV | numpy equivalent, block 4096–16384 (measured insensitive, §3.4) |
| Load-once / release-when-idle | `Searcher.swift:62-92` (`reload`), :94-97 (`releaseVectors`), :44-49 (20k ≈ 30MB idle) | vectors reload after indexing or on first meaning search; freed when idle | daemon cache invalidation + idle release |
| Overlap embed with extraction | `Indexer.swift:211-235` (`process`: "while the model embeds one job, the next one is read and decoded on another thread"), prepare queue :345-356 | depth-1 pipeline: batch N+1 prepared during batch N's embed | asyncio/thread producer-consumer, depth 2 batches |
| Batching by kind | `Indexer.swift:244-260` (`batches`: images 8 at a time, others 1) | groups media into model batches | cross-file text batching with flush rule (§6.2) |
| Linger | `IndexService.swift:17-19`, `:44/:54/:61`, `waitForWork` :236-251 | warm worker kept for `linger` seconds after work ends, then exit | `goldmine serve` idle timeout (§6.3) |
| Backfill gating | `IndexService.swift:98-101` (`backfillAllowed`), new-files-first :93-97 | big passes yield to new files and to "backfill off" | ingest priorities if a watcher lands (v2.2) |
| Power/thermal gate | `PowerPolicy.swift:54-63` (`backfillBlocker`: Low Power Mode, thermal serious+, battery) | event-driven backfill permission | mini is AC-powered: adopt thermal+LPM gate only; battery branch dead here (§6.5) |
| Measured cost model | `Estimator.swift:8-18` (`Costs`, M4 Pro numbers), estimate :66-114, Spotlight metadata :133-146 | bytes/pages/duration → seconds, before any decode | `goldmine ingest --estimate` (§6.6), calibrated from our own bench |

---

## 5. Design decision tree (vector scan — the ONE path)

```
How many vectors? (cached COUNT from meta; updated at ingest commit)
│
├─ N ≤ ~250k  ("laptop-scale", ≈ ≤375MB fp16)
│    └─ EXACT, resident fp16 cache (anonymous mmap or bytes), blockwise
│       f32 convert (4096 rows) + f32 GEMV. ~2-9ms scan. No ANN, no pack file
│       required (SQLite blob read ≈ 16-173ms acceptable even cold).
│
├─ 250k < N ≤ ~2M  ("library-scale", 0.75-3GB fp16)         ← the v2.1 default path
│    └─ EXACT over a derived, fingerprint-keyed PACK FILE (<index>.vecs, fp16,
│       row = segment id order) that is mmapped and scanned blockwise.
│       Measured: ≈45ms @500k, ~90-120ms @1M. File-backed pages = no RSS ceiling.
│       SQLite blobs remain the source of truth (crash-atomic per-file swaps);
│       the pack is a rebuildable cache (header: fingerprint_id + meta.pack_epoch
│       carried from the same transaction as every replace_segments; count/max-id
│       secondary). Daemon keeps the mmap + id maps hot (data_version check).
│
└─ N > ~2M  (pack > 3GB, scan > ~250ms extrapolated)
     └─ Still EXACT first, two cheaper levers before any ANN:
        1. kind/modality partition mask (--kinds already exists as file filter;
           apply as a row mask before the scan — skips blocks wholesale).
        2. partitioned pack files per modality (text|image|audio), scan only
           what the query needs (z-scores already treat modalities separately).
        ANN (hnswlib/faiss) only if bench.py shows p95 breach at a REAL corpus —
        and only after wheel re-validation + recall@5 = 1.00 vs exact (§3.6).
        Recall risk is never accepted silently: eval_relevance.py must show
        recall@5 == 1.00 vs exact on the eval corpus for any ANN default.
```

Why this tree: brute force is measured fast enough to 1–2M vectors (T4), sqlite-vec is
unusable (§3.5), ANN adds a dependency + recall risk for speed we don't need. The one real
defect in v1's scan is not the matmul — it's the per-command reload and the Python fusion
loop, both cheap to fix.

Companion fix (mandatory, S): vectorize the per-file max (search.py:155-160). Rows come
ordered by segment id so `file` is non-decreasing → file boundaries via
`np.flatnonzero(np.diff(file_ids))`, then `np.maximum.reduceat` for per-file best z +
`reduceat`-style argmax for the best segment. Replaces 106ms @500k of Python with ~2ms.

---

## 6. Design per area

<!-- amended 2026-10-16: B4 (Reviewer 1) — pack header carries meta.pack_epoch; the
     (fingerprint, count, max-id) digest alone is unsafe because segments.id has no
     AUTOINCREMENT and replace_segments can reuse the top rowid after deletions. -->
### 6.1 Vector scan (v2.1; gate G0 — build only if the measured corpus needs it)

- Storage: SQLite `segments.vec` stays the source of truth (the ARCHITECTURE.md:251-253
  crash-atomicity argument stands — vectors die with their rows in one transaction).
  Add a derived pack file `<index>.vecs`: contiguous fp16 rows in `segments.id` order +
  a 72-byte header (fingerprint_id, **pack_epoch**, vector count, max segment id, pack
  schema version). `segments.id` has **no AUTOINCREMENT**: after deletions,
  `replace_segments` can reuse the top rowid, so a content change can leave (fingerprint,
  count, max-id) unchanged and a stale pack would silently serve old vectors. Fix:
  `meta.pack_epoch` is bumped **inside the same transaction as every `replace_segments`**
  and the pack builder carries it in the header; count and max-id are demoted to
  secondary sanity checks. Rebuild (streaming, chunked SELECT, ~2s @500k from measured
  read rate) when the epoch or any secondary check mismatches — rebuild is cheaper than
  verifying at this size.
- Scan: `np.memmap(pack)` → per 4096-row block: `astype(float32) @ q` → scores; top-k via
  `argpartition`. Modality/kind row-mask applied per block when `--kinds` given.
- z-scores unchanged (already vectorized, search.py:294-309).

### 6.2 Indexing pipeline (v2.0)

Embedding is 99% of wall clock and GPU-bound (§3.2/§3.3), so the wins are ordering, not
parallelism:

1. **Overlap (DigUp Indexer.process pattern)**: extraction pool feeds a bounded queue
   (depth = 2 batches); the embed loop pulls chunk pieces ACROSS files, flushes a batch at
   `cfg.batch` texts or 100ms, and commits per file when its vectors land (per-file
   transaction invariant kept; a file's pieces carry their file id through the queue).
   Depth 2 is enough because extraction is <1% for text corpora; it matters for future
   scanned-PDF/OCR work where extraction ≈ embed time (DigUp Indexer.swift:212-216).
2. **Server flags**: drop `-np batch` (16 slots buy nothing measured, cost KV memory);
   default `-np 2 -c 2*ctx -b 2*ctx -ub ctx`. Batching stays 16 for latency granularity —
   never grow it for throughput (§3.2).
3. **Token guard (the measured 2670-token failure)**: kind-aware token estimate
   (`est_tokens`: prose ~4.9 chars/tok, code ~0.67 chars/tok measured) shrinking code
   stretches pre-send; a 400 on one input fails that input as vectorless-with-detail
   instead of failing the file after a retry.
4. **Workers**: keep default 2 (measured saturation at 2); document that more only helps
   future OCR/media kinds.
5. **sha256 off the serial path** (pipeline.py:273): hash inside the extraction task.

Targets (restated 2026-10-16 review): **≥ measured v1 rate — 94 files/min on the
reference code corpus, no regression** (prose ≥ 200 files/min at 17.5 texts/s and PDF ≥ 25
files/min stay as published baselines). Attribution correction: extraction is <1% of wall
and overlap recovers ≤1% of it — the real ingest-rate lever is token chunking (~250-tok
chunks embed at 28.2 vs 16.3 texts/s, §3.2), landing with the chunker work (01 §5.5).
Publish per-run files/min + texts/s in the ingest summary so regressions are visible to
users, not just CI.

<!-- amended 2026-10-16: Reviewer 2 C2 — the daemon moved to v2.1; B3 (Reviewer 1) — the
     memory guard is pressure-aware, never raw free pages (vm_stat idle-free ≈ 1.5GB makes
     free-page thresholds meaningless: guard-at-1.2GB either never fires or respawn-storms). -->
### 6.3 Warm path — `goldmine serve` (v2.1) + shared embedder (v2.1)

- `goldmine serve` is a small stdlib daemon: spawns llama-server exactly as
  `LlamaServerBackend` does (same never-adopt-foreign rule, llama.py:235-244), owns the
  VectorTable cache (pack mmap + file-id/modality arrays), serves a line-JSON protocol
  over a UDS at `<home>/serve.sock`: `ping`, `embed {texts}`, `search {query, limit,
  scorer, kinds}`, `status`, `shutdown`. CLI search connects if the socket answers
  `ping` with matching index_uuid; else falls back to today's in-process path (auto-start
  on first semantic search — decided at C4 acceptance in v2.1; `--no-serve` opt-out).
- Why a daemon at all: spawn is 0.6–1.1s per command (§3.7) and load_vectors is up to 2s —
  together 3s of every cold query at 500k. With the daemon warm: query embed 9–12ms +
  scan 9ms @100k + fusion ~2ms + hit-detail queries ~10ms + IPC ~2ms ≈ **40ms p50**,
  ~80ms @500k, ~130ms @1M — T2 met with 7× headroom.
- Linger: default 300s idle exit (DigUp uses 60s; our restart is only ~0.7s, so even a
  short linger is honest — 300s covers "search, read, search again" bursts). Config
  `serve.linger`, `--no-serve` escape hatch. The daemon is stateless: every response is
  computed from the store, and the cache self-invalidates via `PRAGMA data_version`
  (bumped by the ingest process's commits) with a fallback COUNT check.
- Is a resident 484MB acceptable on 16GB? Yes — but earned, not assumed: (a) it exits on
  idle; (b) the memory guard below; (c) the vector cache is file-backed (purgeable) not
  malloc'd. Ingest keeps its own spawned server in v2.0 (1.1s on a multi-minute run is
  noise); v2.1 moves ingest embedding onto the daemon's server (one resident model, one
  lifecycle; the port is ours and ephemeral, published only to our own processes).
- **Memory guard (pressure-aware, never raw free pages)**: before answering `search` and
  every 60s while idle, sample the kernel memory-pressure percentage (parse-only, no new
  deps). At sustained **warn** pressure: drop the vector cache (reload on next query) and
  start no new resident loads. At sustained **critical** pressure with hysteresis (two
  consecutive readings 30s apart): exit 0. The daemon NEVER exits on raw free pages, and
  mostly needn't act at all — the fp16 pack is file-backed and purgeable, so the kernel
  reclaims it under pressure before the guard matters; at most the daemon unloads an
  idle llama-server. `goldmine doctor` reports daemon state, RSS and the last pressure
  reading honestly.

### 6.4 Memory ceiling policy (16GB)

| Object | Policy | Measured cost |
|---|---|---|
| llama-server | resident only while daemon/ingest alive | 484MB |
| Vector cache (fp16) | file-backed pack mmap — **no cap** (pages are purgeable); anonymous-mmap fallback only ≤ 250k vectors | page cache, not RSS |
| fp32 scan blocks | fixed 4096×768 | 12MB |
| fp32 resident matrix | **forbidden** above 250k vectors (1.5GB @500k measured); below that only inside a command | ≤ 768MB @250k |
| Ingest | streaming per-file commits (already true); no vector accumulation | flat |
| 1M-vector corpus | pack 1.5GB file + DB ≈ 2.2GB on disk — fine on 228GB | scan ~90–120ms |

### 6.5 Large-corpus behaviors (1M+)

- **Partitioning, not sharding**: one machine, one disk — physical shards buy nothing.
  Logical partitions (modality; kind on demand) exist as row masks (§5) and keep the
  z-score modality semantics that already exist (search.py:294-309). Only the pack header
  gains per-modality row ranges (v2.2) so `--kinds` scans skip whole regions.
- **Incremental reindex** is already sha256-skip driven; the cost model for the honest
  answer is `pending_texts / 17.5 texts/s` (prose) or `/ 11.1` (code): a full 100k-chunk
  reindex is **95–150 min** — `--reindex` and `reset --reindex` must print this estimate
  before starting.
- **Backfill scheduling**: this is a Mac mini — always on AC, no battery (the target
  machine is a headless desktop). Adopt DigUp's gate shape (PowerPolicy.swift:54-63) but
  only the thermal ("Mac is warm") and Low-Power-Mode branches; wire them into the future
  background watcher (v2.2, depends on a `goldmine watch` existing at all). The battery
  branch stays in the function for the day this is portable.

### 6.6 First-pass ETA estimator (`goldmine ingest --estimate`, v2.1)

Walk-only (no decode), Estimator.swift:66-114 shape, with our measured Costs (this
machine, to be recalibrated on first real run and stored in `meta`):

| Kind | Unit | Measured seconds/unit | Source |
|---|---|---|---|
| prose doc/webpage chunk (1800c) | chunk | 0.057 | §3.2 (17.5/s) |
| code stretch | stretch | 0.090 | §3.3 (11.1/s effective) |
| pdf text chunk | chunk | 0.103 | §3.3 (9.7/s) |
| extraction, text/code/pdf-text | file | 0.001–0.04 | §3.3 (workers ≥ 2) |
| server start | run | 1.1 (v1) / 0.7 (daemon) | §3.7 |
| search, per query | N-dep | load+scan from §3.1/§3.4 table | interpolated by N |

Counts come from the walk (`size/1800` chunks for doc/code, pages from Spotlight
`kMDItemNumberOfPages` / `pdfinfo` when present, durations from `ffprobe` only when cheap).
Output: per-kind counts, estimated vectors, estimated minutes, and — after ingest — the
real numbers, so the following run's estimate self-corrects (DigUp: "the app will
calibrate these with a few seconds of real work").

---

## 7. Regression gates: `scripts/bench.py`

One script, subcommands mirroring §3, thresholds checked against absolute numbers
(machine-honest) with an optional same-machine baseline for relative drift:

| Subcommand | Measures | Gate (fail = exit 1) |
|---|---|---|
| `bench search --db <synthetic 100k>` (auto-builds fixture) | scan+fusion path, no embed | p50 ≤ 60ms; vectorized-fusion assertion (no >1000-row Python loop) |
| `bench search --daemon` | full warm query via serve socket | p50 ≤ 300ms, p95 ≤ 500ms @100k |
| `bench embed` | spawns server, 1800-char chunks | ≥ 14 texts/s (22% under measured 17.9 — GPU thermal noise tolerated); query embed p50 ≤ 25ms |
| `bench ingest --corpus <checked-in ~50-file fixture>` | lexical-only pipeline | ≤ 1.0s (extraction must stay noise) |
| `bench scan --n 500000` | pack mmap blockwise | ≤ 100ms |

Baselines: `scripts/bench-baseline.json` holds absolute thresholds (this file). `make
bench` runs the suite locally and writes `--json` results; `bench.py compare` diffs against
the previous local run keyed by (chip, cores, memory) stored in
`~/.cache/goldmine/bench-local.json` — relative regressions > 20% fail locally.

<!-- amended 2026-10-16: Reviewer 2 C2 — v2.0 ships the LOCAL suite with two absolute
     gates only (in-process scan+fusion p50, embed throughput); the GHA nightly moves to
     v2.1 with the daemon/pack work it exists to gate. -->
CI: GitHub Actions `macos-14` runner (M1 — same magnitude, not identical numbers) runs
**only the absolute gates, nightly — v2.1** (spawn + Metal variance makes per-PR too
flaky); PRs run a CPU-only subset (scan/fusion/ingest-lexical, no llama-server). The
gates are also the acceptance test for each phasing item below.

---

## 8. Phasing & effort

(S ≤ 1 day, M ≤ 3 days, L > 3 days, one engineer)

<!-- amended 2026-10-16: Reviewer 2 C2 — pack (3), daemon (4), overlap (5) and the GHA
     nightly (6) moved to v2.1; v2.0 keeps items 1-2 plus the local two-gate bench sliver.
     Pack additionally gated on REDESIGN §5 gate G0 (measure the real corpus first). -->

| # | Item | Size | Release | Justification |
|---|---|---|---|---|
| 1 | Vectorized per-file max + `argpartition` top-k (search.py:155-160) | S | v2.0 | removes 106ms @500k of pure Python |
| 2 | Server flags: `-np 2`, kind-aware token guard (the 2670-tok fix) | S | v2.0 | §3.2/§3.3 measured |
| 3 | Pack file (`<index>.vecs`) + mmap blockwise scan + pack_epoch validation | M | v2.1 (was v2.0) | T3/T4: ~45ms @500k vs 1.0s; only if the G0 measurement justifies it |
| 4 | `goldmine serve` daemon: search + embed over UDS, linger 300s, pressure-aware memory guard | L | v2.1 (was v2.0) | T1/T2/T8: 1.9s → 40ms warm |
| 5 | Ingest overlap pipeline (queue + cross-file batching, per-file commit kept) | M | v2.1 (was v2.0) | ≤1% on text corpora (B8); pays off for OCR/media kinds |
| 6 | `scripts/bench.py` + gates + GHA nightly | M | v2.0 local 2-gate sliver; nightly v2.1 | keeps all of the above honest |
| 7 | Daemon-owned embedder shared with ingest (drop per-command spawn) | M | v2.1 | T8 to zero; one model resident |
| 8 | `ingest --estimate` (measured-cost model, self-calibrating) | M | v2.1 | T10; §6.6 |
| 9 | Pack per-modality row ranges + `--kinds` region skip | M | v2.2 | >1M scans; §6.5 |
| 10 | ANN evaluation gate (only on measured p95 breach; validated build required) | L | v2.2 | §3.6 — probably never |
| 11 | Background watcher + thermal/LPM backfill gate | L | v2.2 | needs `watch` to exist; §6.5 |

---

## 9. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Pack file desync vs SQLite blobs (two representations) | medium | blobs stay source of truth; pack is derived + header-checked — `meta.pack_epoch` bumped in the SAME transaction as every `replace_segments` (segments.id has no AUTOINCREMENT; count/max-id are secondary only — rowids get reused); rebuild is ~2s @500k; mismatch = rebuild, never trust |
| Daemon lifecycle bugs (stale socket, orphaned llama-server) | medium | pid+index_uuid in socket handshake; adopt-never rule inherited from llama.py:235-244; SIGTERM→5s→SIGKILL chain unchanged; doctor reports state |
| Metal contention (user compiles/games while searching) | certain, transient | honest ETA + bench tolerances (22%); nothing to fix — report latency as measured |
| Memory guard false positives | low | pressure-percentage signal + hysteresis, never raw free pages (vm_stat idle-free ≈ 1.5GB makes free-page thresholds meaningless — B3); wrong-side failure is "drop cache / exit and respawn in 0.7s" |
| FTS `coverage()` IN-list cost at 5k+ files (store.py:549-563) | medium at scale | paginate the pattern loop; measured only trivially so far — add a gate to bench.py before v2.0 ships |
| Synthetic-vector numbers diverge from real corpora | low | scan/GEMV cost is data-independent; embed throughput varies with length — bench pins 1800-char chunks; ingest gates use a real checked-in fixture |
| numpy fp16 behavior changes across versions | low | we never matmul in fp16 (measured 10× slower); blockwise astype is plain ufunc work, stable |
| hnswlib/faiss reconsidered later | low | §3.6: the 0.8.0 wheel works (the earlier recall collapse was random-data geometry + ef, not a break — B6); rejection is on exact-scan speed; any adoption re-validates wheels + eval recall@5 = 1.00 vs exact before defaulting |
| Daemon keeps 484MB resident while user is memory-tight | medium | linger exit + pressure-aware guard + file-backed cache; the daemon is opt-out (`--no-serve`) per command |
