# goldmine — Architecture

> Implementation update (2026-10-21): see [the current feature status](docs/FEATURE_STATUS.md) and the [P0–P2 execution record](docs/execution-plan/PROGRESS.md). Older design and benchmark sections below retain their historical scope; they are not final candidate verification.

> "Ingest anything, search anything." A local-first macOS CLI that ingests files and web links,
> embeds stored text with the local EmbeddingGemma 2 llama backend (768 dimensions), searches it with
> hybrid semantic + exact-match scoring, and distills the corpus into source-backed "golden
> knowledge" cards.
>
> Status: **0.2.0 development implementation**. Current boundaries are below;
> the numbered v1 design sections remain historical context.
> Companion docs: [SPEC.md](SPEC.md) (CLI contract), [docs/MODULE_MAP.md](docs/MODULE_MAP.md),
> [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md), [docs/TEST_PLAN.md](docs/TEST_PLAN.md).

---

## Current request boundaries (2026-10-22)

CLI command adapters and the optional stdio MCP adapter call `Application` with
typed contracts. A request owns one read-only Store connection/transaction and
one monotonic deadline. Ranking, original evidence, supporting/neighbor citations
and final packing use that same snapshot. The adapters serialize results; they
do not independently rerun retrieval or read source files. The MCP SDK is an
explicit optional dependency, and model operations are serialized with two
bounded waiters.

`query_obligations.py`, `anchor_resolution.py` and `anchor_admission.py` enforce
complete typed source anchors and same-file lexical remainder evidence. The
closed compare/two-clause grammar records actual owners, and final result limits
must preserve every required clause. Whole metadata matches identify a file;
they do not supply body evidence or a body location. The structural registration
is `anchor.v1-lexical`; semantic remainder is off and its product quality remains
unvalidated. Existing request deadlines cover bounded source verification.

Supported schema4 reads validate stored originals with the historical NFC rule;
schema5/6 retain NFKC+casefold validation. A shared read projection also supports
the declared body-only schema4 variant without creating views or changing bytes.
Legacy anchor recall is bounded and explicitly incomplete; lexical reindex remains
an explicit operation. Complete metadata-field eligibility runs before the optional
candidate limit on every supported format. The 2000-source limit concerns distinct
returned/materialized source rows; indexed SQL may inspect additional metadata
under the same request deadline. Modern missing derived tables and selected corrupt
pointers remain errors.

Mutations use the registered writer transaction. Explicit lexical reindex upgrades
derived schema fields, while persistent old-writer guards reject the whole stale
transaction. Vector-cache generations are explicitly built/enabled derivatives;
epoch/identity defects fall back to the authoritative SQLite rows. Ingest publishes
new text, vectors and completed file state together. Search excludes pending files.

`serve` and `watch` are explicitly started daemons with their existing guard and
ownership rules. Temporary request backends and optional rerankers close on errors,
deadline, cancellation and termination; clients do not kill shared serve. Local
HTTP applies the remaining total budget to every socket step, including slow
headers and body. [The module map](docs/MODULE_MAP.md) identifies the implemented
owners; [execution contracts](docs/execution-plan/CONTRACTS.md) define exact limits.

## 1. Historical v1 component diagram

```
                 goldmine — one OS process per command (no daemon of our own)
┌───────────────────────────────────────────────────────────────────────────────────┐
│  cli.py — argparse wiring, exit codes, human/JSON rendering, logging setup        │
│     │                                                                             │
│     ├── ingest ──► IngestPipeline (ingest/__init__.py)                            │
│     │       │  rules.py ── classify / skip reasons / secrets / MPEG-TS sniff      │
│     │       │  walk.py ── scandir walker, exclusions, symlink policy, deletions   │
│     │       │  extract_text.py  extract_pdf.py  extract_html.py                   │
│     │       │  crawl.py  media.py          (each a small leaf, optional tools)    │
│     │       └─ chunker.py ── paragraph-aware chunks / line-aligned code stretches │
│     │                                                                             │
│     ├── search ──► search.py — z-scores, fusion, coverage, RRF/lexical baselines  │
│     ├── distill ─► distill.py — dedupe, deterministic clustering, TF-IDF, cards   │
│     ├── status ──► cli.py + store.counts() + backend probes                       │
│     ├── doctor ──► doctor.py — tool/backend/model/index checks with fix hints     │
│     └── backends ─► backends/__init__.py registry + probes                        │
│                                                                                    │
│  fingerprints.py — canonical fingerprint + refuse-on-mismatch gate (every path    │
│                    that touches vectors goes through it)                           │
│                                                                                    │
│  ┌──────────────────────── backends (EmbeddingBackend ABC) ────────────────────┐  │
│  │  llama.py     subprocess llama-server + stdlib HTTP  (text only)            │  │
│  │  mock.py      deterministic hash embeddings, in-process (text+image+audio)  │  │
│  └───────────────┬─────────────────────────────────────────────────────────────┘  │
│                  ▼                                                    │           │
│          llama-server (child)                                   in-process     │
│          127.0.0.1:<ephemeral>                                  deterministic   │
│          owned by this process,                                 hash vectors    │
│          killed at exit                                                          │
│                                                                                    │
│  store.py — SQLite (WAL, single writer)                                           │
│  meta │ files │ segments (768×fp16 BLOBs) │ fts5 │ cards │ card_chunks            │
└───────────────────────────────────────────────────────────────────────────────────┘
   Optional external tools, probed at runtime and never required for the command to
   exit honestly: pdftotext (poppler), textutil (macOS built-in), ffmpeg/ffprobe,
   whisper-cli + model (stretch ASR hook), llama-server binary.
```

Design invariant: **the store is the only shared state**. Extraction never writes to the DB;
embedding never reads it; the writer thread is the only one that touches SQLite. The
fingerprint gate sits in front of every cross-boundary call (embed, search, distill).

---

## 2. Data flow

### 2.1 Ingest

```
roots (paths / URLs)
   │
   ▼
walk.py ── Candidate(path, kind, size, mtime) ──┐   skip notices (reason) ──► report
   │                                            │
   │  unchanged? (size+mtime prefilter → sha256) │── yes ──► skip (counted)
   ▼                                            ▼
extractor dispatch (by kind)
   │  txt/md  → decode_bytes (BOM-aware)                     [extract_text]
   │  html    → charset decl + HTMLParser strip              [extract_html]
   │  docx    → zipfile+xml stdlib; rtf/doc/odt → textutil   [extract_text]
   │  pdf     → pdftotext per page batch, max_pages cap      [extract_pdf]
   │  code    → decode, line-aligned stretches               [chunker.chunk_code]
   │  audio   → ffmpeg 16 kHz mono windows 30 s / hop 25 s   [media]   (embeddable only
   │  video   → ffmpeg frames every 3 s, aHash ≤ 4-bit dedup [media]    via a multimodal
   │  image   → (metadata only in v1 llama backend)             backend)  [media sniff]
   ▼
chunker.py ── Chunk(text, loc, loc_end, kind)   (≤ ~1800 chars ≈ 450 tokens, paragraph bounds)
   ▼
embed: backend.embed_texts(batch of 16) ── fingerprint gate ──► llama-server HTTP
   ▼                                                            (or mock)
store.replace_segments(file_id, segments)   ← ONE transaction per file:
   name row + chunks/pages/frames + fts rows + fp16 vec BLOBs
   files row ← state, capability, detail, info, sha256, extractor_version, indexed_at
```

Per-file outcomes are always recorded (verdict 5a): `content_indexed`, `metadata_only`,
`unsupported`, `failed` — see [SPEC.md § Capability matrix](SPEC.md#15-capability-matrix-normative-definitions--verdict-5a).
Deletions (files under a root that vanished) are pruned and reported. Re-ingest of a changed
file (sha256 mismatch) does **atomic segment replacement**: extract → embed → single
transaction that deletes the old segments and inserts the new ones.

### 2.2 Search

```
query text
   ├─► terms() ── stopword/kind-word split ── fts OR-patterns ("term"* quoting)
   │        │
   │        ▼
   │   fts5 MATCH … ORDER BY bm25(fts,3.0,1.0) LIMIT 1000     ← bm25: LOWER is BETTER
   │        │                                                  (ascending = best first);
   │        ▼                                                  never added to a
   │   per-file word coverage + best keyword segment           higher-is-better score
   │
   └─► backend.embed_texts([query prompt])  ← "task: search result | query: {text}"
            │  (fingerprint gate; lexical-only skips this)
            ▼
       numpy matrix @ q  over ALL segment vectors (loaded once, fp16→f32 blocks)
            ▼
       z-scores: per-modality centering (groups < 20 use global mean; std floor 0.02)
            ▼
   candidates = {file: z ≥ z_gate(2.0)} ∪ {file: coverage ≥ 0.5}
   score(f)   = w_m·max(z_f,0) + w_c·coverage_f^1.5 + kind_hint(f)·1.0 (capped)
                default (w_m, w_c) = (1.0, 2.0); lookup queries → (0.5, 4.0)
            ▼
   collapse to file-level hits, best segment per file (meaning segment if z ≥ gate,
   else keyword segment) → top `limit`, with excerpt, loc/loc_end, kind
```

`--scorer lexical|semantic|rrf` run the ablation baselines required by verdict 3 (the eval
harness compares them; see [docs/TEST_PLAN.md](docs/TEST_PLAN.md)).

### 2.3 Distill

> v2.1 (R6): τ is CALIBRATED from the corpus (Otsu over sampled pairwise cosines,
> percentile fallback, `tau_source`), clustering adds the average-link admission guard, and
> cards are medoid + coverage excerpts — normative algorithm: docs/v2.1-r6-distill.md
> (the v1 diagram below is kept for the data flow, not the algorithm).

```
segments (vectors + text, fingerprint-matched, extractor-matched)
   ▼
dedupe: exact (sha256 of normalized text) then near-dup (greedy, cosine ≥ 0.97,
        deterministic order by (file_id, segment_id))
   ▼
deterministic greedy leader clustering (spec'd in §7.3 below): τ = 0.70 default,
running-mean centroids renormalized after each assignment, singletons dropped
   ▼
per cluster: representative chunk = member with max cosine to final centroid
             (ties → lowest segment id, verbatim excerpt, never synthesized)
             + TF-IDF top terms (corpus-level df over clusters)
             + citations (file path, kind, loc/loc_end)
   ▼
markdown to stdout / --out FILE / --json
```

---

## 3. Module responsibilities

| Module | Owns | Does NOT |
|---|---|---|
| `cli.py` | arg parsing, exit codes, rendering, logging config | business logic |
| `config.py` | defaults, config.toml, env resolution (flag > env > file > default) | touching disk beyond reading config |
| `ingest/rules.py` | extension sets, classification, secrets, skip reasons | reading file contents |
| `ingest/walk.py` | filesystem traversal, exclusion, symlink policy, prune list | extraction |
| `ingest/extract_*`, `ingest/crawl.py`, `ingest/media.py` | bytes → text / frames / windows; tool subprocesses | chunking, embedding, DB |
| `chunker.py` | text → chunks with locations; token-limit guard | embedding |
| `backends/*` | turning strings/bytes into 768-dim L2-normalized vectors; child process lifecycle | persistence, scoring |
| `fingerprints.py` | fingerprint identity, mismatch refusal | fixing mismatches (user decides reindex) |
| `store.py` | SQLite schema, transactions, fp16 packing, FTS rows | scoring, external calls |
| `search.py` | candidate generation and fusion | ingestion |
| `distill.py` | clustering and cards | modifying the index |
| `doctor.py` | honest reporting of what works and what's missing | fixing anything |

---

## 4. Backend abstraction

```python
class EmbeddingBackend(ABC):
    backend_id: str                        # "llama" | "mock"
    def capability(self) -> Capability     # dataclass(text: bool, image: bool, audio: bool)
    def fingerprint_fields(self) -> dict   # model id, sha256, dim, prompts, pooling, norm
    def embed_texts(self, texts: list[str]) -> list[np.ndarray]      # 768-dim, L2-normalized
    def embed_image(self, path: Path) -> np.ndarray                  # raises NotSupported
    def embed_audio_wav(self, wav: bytes) -> np.ndarray              # raises NotSupported
    def describe(self) -> dict             # version, availability, fix hints
    def close(self) -> None                # kill only processes WE spawned
```

* **`llama` (default, v1)** — spawns `llama-server` (see §7), calls OpenAI-compatible
  `POST /v1/embeddings` with `{"input": [ ... ], }` batches via `urllib.request` only.
  `/v1/embeddings` is **text-only**: images/audio are metadata-only under this backend.
  Client re-normalizes (L2) defensively; pooling is whatever the GGUF metadata selects
  and is recorded verbatim in the fingerprint.
* **`mock` (offline tests)** — deterministic bag-of-token hashing via `blake2b` (NOT Python
  `hash()`, which is salted per process), 768-dim, L2-normalized; image/audio hashed from
  content bytes. Deterministic across runs and machines, so the whole suite runs offline and
  the relevance eval is reproducible.

Registry `backends/__init__.create_backend(cfg, name)` is the single construction point;
`probe_all(cfg)` powers `goldmine backends` and `goldmine doctor`.

---

## 5. Fingerprint / versioning model

Verdict 2 requirement: never mix vectors across embedding fingerprints; changing it forces
reindex; never silently switch backends within one index.

```json
{
  "backend": "llama",                       // family — switching ⇒ different fingerprint ⇒ reindex
  "model_id": "embeddinggemma-2-Q8_0",
  "model_sha256": "<sha256 of the GGUF>",
  "dim": 768,
  "prompts": "v1:q='task: search result | query: {}' d='title: {} | text: {}'",
  "pooling": "server-default",              // recorded verbatim; flag change ⇒ new value
  "normalization": "l2-client",
  "preprocessing": "pipeline.v1"            // chunker + decode + extraction semantics
}
```

* `fingerprint_id = sha256(canonical_json)[:16]`; the full JSON is stored in `meta`
  (`fingerprint_json`, `fingerprint_id`).
* **Deviation from the verdict, deliberately**: the llama.cpp *build* string is recorded in
  `meta` (`llama_build`) but is **not** part of the fingerprint. DigUp learned the hard way
  (IndexStore.swift migration 3→4, `migrateVectorNames`) that keying vectors to the brew
  build forces pointless full reindexes on every `brew upgrade` that doesn't change outputs.
  Mitigation: `doctor` WARNs and `status` surfaces a `build_changed` notice when the recorded
  build differs from the installed one, recommending reindex without forcing it.
* Files record `extractor_version` (per-file; e.g. `pdftotext@24.08`, `textutil@macOS26.5`,
  `docx-stdlib.v1`). A `doctor`/`status` notice lists counts of files whose extractor version
  differs from current — the user can `goldmine ingest --reindex` to refresh.
* On any command that would embed or semantically score, `fingerprints.check()` compares the
  index's stored fingerprint to the live one. Mismatch ⇒ exit code 3, message naming the
  differing fields and the two remediations: `goldmine ingest --reindex` (rebuild vectors) or
  `goldmine reset --reindex` (full rebuild). `search --lexical` remains available: FTS text
  depends on the *extraction* pipeline, not the embedding fingerprint, and stale-but-lexical
  results are labeled as such in output.
* `distill` additionally refuses when the stored cards' fingerprint differs from the current
  one (cards table stores `fingerprint_id` + `distill_algo` version).

---

## 6. Storage schema (complete DDL)

One SQLite file per index (`index.sqlite`), WAL journal mode, `busy_timeout 60000`,
`foreign_keys ON`. Vectors live in the same DB as their rows so a crash can never desync them
(same argument as DigUp IndexStore.swift). Derived from DigUp schema v5, extended per verdict 5.

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 60000;

CREATE TABLE meta(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- keys: schema_version, created_at, app_version, index_uuid,
--       fingerprint_id, fingerprint_json, llama_build (provenance),
--       roots_json (last ingest roots), config_snapshot_json

CREATE TABLE files(
    id                INTEGER PRIMARY KEY,
    path              TEXT NOT NULL UNIQUE,   -- absolute path, or canonical URL for webpage kind
    source            TEXT NOT NULL DEFAULT 'file',   -- 'file' | 'url'
    kind              TEXT NOT NULL,          -- doc|pdf|code|image|audio|video|webpage
    size              INTEGER NOT NULL,       -- bytes (HTML: decoded bytes for webpages)
    mtime             REAL,                   -- NULL for URLs (use fetched_at in info)
    sha256            TEXT,                   -- content hash; NULL until first successful read
    state             TEXT NOT NULL DEFAULT 'pending',  -- pending|done|failed (lifecycle)
    capability        TEXT,                   -- content_indexed|metadata_only|unsupported|failed
                                                -- NULL while pending
    detail            TEXT,                   -- why skipped/failed/metadata-only; lossy-decode notes
    info              TEXT,                   -- JSON: pages, duration, encoding, partial:true,
                                                -- read_to/read_end, width/height, fetched_at, final_url
    extractor_version TEXT,                   -- e.g. "pdftotext@24.08" — verdict 5d
    indexed_at        REAL
);
CREATE INDEX files_state ON files(state, kind);
CREATE INDEX files_capability ON files(capability);

CREATE TABLE segments(
    id       INTEGER PRIMARY KEY,
    file     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind     TEXT NOT NULL,       -- name|chunk|page|audio|frame|image
    modality TEXT NOT NULL,       -- none|text|image|audio  ('none' ⇒ no vector)
    loc      REAL,                -- pdf page number (1-based) / audio-video start second /
                                  -- code start line
    loc_end  REAL,                -- end second / end line
    excerpt  TEXT,                -- short display line (≤ 240 chars)
    vec      BLOB                 -- 768 × float16, L2-normalized (1536 bytes); NULL ⇒ no vector
);
CREATE INDEX segments_file ON segments(file);

-- Keyword search. body = normalized text (NFC); original keeps the text as written
-- when normalization changed it ( DigUp SearchText pattern).
CREATE VIRTUAL TABLE fts USING fts5(
    name, body, original UNINDEXED,
    tokenize = "porter unicode61 remove_diacritics 2 categories 'L* N* Co M*'"
);
-- fts.rowid == segments.id. The per-file 'name' segment row carries:
--   name = filename (or URL slug+host), body = folder-path words (or host), no vector.

CREATE TABLE cards(
    id            INTEGER PRIMARY KEY,
    title_terms   TEXT NOT NULL,        -- JSON array of TF-IDF top terms
    size          INTEGER NOT NULL,     -- member chunk count
    n_files       INTEGER NOT NULL,
    fingerprint_id TEXT NOT NULL,       -- vectors the clustering ran on
    distill_algo  TEXT NOT NULL,        -- e.g. "greedy-leader.v1 t=0.70 min=3"
    created_at    REAL NOT NULL
);

CREATE TABLE card_chunks(
    card   INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    chunk  INTEGER NOT NULL REFERENCES segments(id),
    weight REAL NOT NULL               -- cosine to final centroid (1.0 for representative)
);
CREATE INDEX card_chunks_card ON card_chunks(card);

PRAGMA user_version = 1;               -- schema v1; migrations ship one-per-version like DigUp
```

Search reads all `vec` blobs once per command into a `(N, 768)` float32 numpy matrix
(fp16→fp32 in blocks, exactly DigUp's pattern). At DigUp-measured scale (~20k vectors ≈ 30 MB)
brute force is milliseconds; no ANN index, no vector database.

---

## 7. Threading / process model

### 7.1 In-process threads (per command, no background daemons of our own)

```
main thread ──┬─ walker (inline, os.scandir recursion) — single-threaded on purpose
              ├─ ThreadPoolExecutor(workers=2, cfg)  — extraction leaves
              │    (pdftotext/textutil/ffmpeg subprocesses, docx unzip, decode)
              ├─ embedding: main thread serial batches (batch=16) — the HTTP round trip
              │    is the bottleneck; more in-flight batches only add latency spikes
              └─ SQLite writer: main thread only (the thread that embeds also writes)
                 one connection, check_same_thread=True, WAL, busy_timeout 60 s
```

Rationale: DigUp's indexer is deliberately one-threaded ("simple and easy to reason about").
We add exactly one pool for extraction because subprocess fan-out is where wall-clock time
actually goes; everything vector- or DB-related stays serial and therefore testable.
fp16 packing happens in the embedding step (numpy `astype(float16).tobytes()`).

### 7.2 llama-server lifecycle (LlamaServerBackend)

1. **Probe**: binary exists and is executable; `llama-server --version` (3 s timeout) →
   record e.g. `0.5.0 (build 11146, commit 7fe450e19)` in `meta.llama_build`.
2. **Model identity**: sha256 of the GGUF (cached in a sidecar `<model>.sha256`; recomputed
   when size or mtime changed). Fed into the fingerprint.
3. **Spawn**: `llama-server --host 127.0.0.1 --port <P> --embedding -m <model> -c 2048`,
   `P` picked by binding a temp socket to `127.0.0.1:0` (documented race; on failure to become
   healthy, retry with a new port up to 3 times). stdout/stderr → ring buffer (last 4 KB kept
   in memory for error reports) + append to `<home>/logs/llama-server-<ts>.log` (bounded:
   rotated at 1 MB, keep 3).
4. **Readiness**: poll `GET /health` every 250 ms until HTTP 200, timeout `startup_timeout`
   (default 120 s — cold model load). Probe failure ⇒ kill child, raise `EmbeddingError` with
   the ring buffer tail.
5. **Requests**: `POST /v1/embeddings`, `{"input": [...]}`; deadline per request
   (`request_timeout`, default 60 s) via socket timeout; one in-flight batch; cancellation =
   close the socket (SIGINT path), child killed in `finally`.
6. **Shutdown**: SIGTERM to the child we spawned, wait ≤ 5 s, SIGKILL. **Only processes we
   spawned** (Popen handles we hold) — never `pkill`/name-matching, so a user-run llama-server
   is never touched.

### 7.3 Signals

SIGINT/SIGTERM: set a cancel flag → finish current file (transaction boundary) → close backend
→ exit 130 with a one-line summary ("indexed 37/120 files; 83 pending — resume with the same
command"). Ingest is idempotent and resumable by design (pending rows + sha256 skip).

---

## 8. Failure / degradation model

The governing principle (verdict 5): **correctness and honesty of status over feature count**.
A file is never silently dropped and a capability is never silently missing.

| Failure | Behavior | Where surfaced |
|---|---|---|
| `pdftotext` missing | PDFs recorded, `capability=metadata_only`, detail=`"pdftotext not found"`; ingest continues | `status` counts + reasons; `doctor` fix hint (`brew install poppler`) |
| `textutil` missing (non-macOS) | rtf/doc/odt → `metadata_only` (docx still works: stdlib zip+xml) | status/doctor |
| ffmpeg missing | audio/video → `metadata_only`, detail names the tool | status/doctor |
| backend unavailable at ingest start | **refuse** (exit 4) unless `--lexical-only` explicitly given — prevents silently building a name/keyword-only index | error message; `status` shows `vectors=0` |
| llama-server fails to start | per-file rows stay `pending`; command fails (exit 2) with ring-buffer tail; retry safe | error message; doctor |
| model file changed (sha256) | fingerprint mismatch → exit 3, reindex instructions | every embedding command |
| llama.cpp build changed | WARN + `build_changed` notice; vectors still trusted | status/doctor |
| subprocess timeout (pdftotext 60 s, ffmpeg 120 s/window-batch) | file `capability=failed`, detail=`"timeout after 60s"`; continue | status |
| decode failure / unknown encoding | BOM → UTF-8 → declared charset → windows-1252 lossy; `detail` records `lossy-decode` + what was assumed | status detail |
| PDF page with < 200 chars text | page kept for FTS, **no vector**, detail `"thin text (OCR unavailable in v1)"`; never faked | status counts |
| size/page/duration caps hit | read what fits, `info.partial=true`, `read_to/read_end` recorded; searchable for what was read | status, per-file info |
| symlink cycle | directory symlinks not followed (default); visited-dir `(device,inode)` set as belt-and-braces | walk report |
| secrets matched | never read, `capability=unsupported`, reason `"key or password file"` | walk report |
| file deleted since last ingest | row + segments removed, counted in report | ingest summary |
| file changed (sha256 mismatch) | atomic re-extract + single-transaction segment swap | ingest summary |
| crawler: 429/5xx | exponential backoff (1s→2s→4s, max 3 tries), then give up that URL with detail | ingest summary |
| crawler: robots.txt disallow / non-HTML content-type / off-host redirect | URL skipped with reason | ingest summary |
| distill corpus > max_chunks | refuse with count + `--max-chunks` guidance; no silent truncation | error message |
| numpy missing | hard error at startup with install hint (declared dependency) | all commands |

`goldmine status` and `goldmine doctor` make every one of these visible; `metadata_only` is
always visibly distinct from `content_indexed` (verdict 5a) — including in search output,
where metadata-only files can only ever arrive via the name row.

---

## 9. What is explicitly out of scope for v1

OCR (no bundled Vision-equivalent), chat-model synthesis in distill cards (extractive only;
`--llm polish` deferred), a second "code index" (v1 has one index; `--code` folds code files
in, line-aligned), ANN indexes, daemon/watch mode, Xattr-based screenshot detection
(name-prefix heuristic only), non-macOS packaging (design is portable but tested on this Mac).
