# goldmine — CLI Specification (v1)

Normative contract for the `goldmine` command. Implementation must match this document or
update it in the same change. Design rationale lives in [ARCHITECTURE.md](ARCHITECTURE.md) and
[docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md).

Python 3.12, stdlib-first. Dependencies: `numpy` (required), `grpcio`+`protobuf` (optional
`foresight` extra), `pytest` (dev). External tools (`pdftotext`, `textutil`, `ffmpeg`,
`llama-server`) are optional at runtime and always capability-reported.

---

## 1. Global behavior

```
goldmine [GLOBAL OPTIONS] <command> [args]
```

Global options (accepted before or after the command):

| Option | Default | Meaning |
|---|---|---|
| `--index NAME` | `default` | Use `<home>/<NAME>.sqlite` (named index) |
| `--db PATH` | — | Explicit index file path (overrides `--index`) |
| `--backend NAME` | config `backend.default` = `llama` | `llama` \| `foresight` \| `mock` |
| `--config PATH` | `~/.config/goldmine/config.toml` | Config file location |
| `-v / -vv` | off | Log INFO / DEBUG to stderr |
| `-q` | off | Errors only |
| `--log-file PATH` | off | Also append full-level logs to PATH |
| `--json` | off | Machine-readable stdout (per-command schema below). Errors stay on stderr |
| `--no-color` | auto | Disable ANSI (auto-disables when stdout is not a tty) |

**Index location (decided):** data directory default
`~/Library/Application Support/goldmine/` (macOS-native; this is a macOS-first tool), the
index itself `<home>/index.sqlite` (or `<home>/<NAME>.sqlite`). Override with `GOLDMINE_HOME`
or `--db`. Configuration stays at the XDG path `~/.config/goldmine/config.toml` — config is
portable convention, data is platform convention. Logs: `<home>/logs/`.

**Config resolution order:** CLI flag > environment variable > config.toml > built-in default.

### 1.1 Exit codes

| Code | Meaning | Examples |
|---|---|---|
| 0 | success | |
| 1 | runtime error | backend crashed, DB corrupt, crawler hard failure |
| 2 | usage error (argparse) | unknown flag, missing required arg |
| 3 | fingerprint mismatch / refusal | index built with different model or backend |
| 4 | prerequisite missing | no backend available at ingest; `doctor` found FAIL items |
| 5 | partial success | ingest completed with ≥1 failed file (summary still printed) |
| 130 | interrupted (SIGINT) | cancel flag honored; partial summary printed |

### 1.2 Logging

stderr, level from `-v/-vv/-q` or `GOLDMINE_LOG_LEVEL` (`DEBUG|INFO|WARNING|ERROR`).
Format: `%(asctime)s %(levelname)s %(name)s: %(message)s`. stdout carries **data only** —
progress and diagnostics never touch stdout, so `goldmine search … --json | jq` is always
clean. Ingest progress lines (INFO): `ingest: 42/120 files, 1 failed (pdf: no text layer)`.

### 1.3 Commands

`ingest` · `search` · `distill` · `status` · `doctor` · `backends` · `reset`

---

## 2. `goldmine ingest`

```
goldmine ingest PATH... [URL...] [flags]

  --code                    include code/data files (one index; line-aligned chunks).
                            Default off — matches DigUp: ripgrep searches code better and
                            code floods an index.
  --dry-run                 classify only: print per-kind counts, top skip reasons, cap
                            impact; no DB writes, no backend, no tool spawns. Exit 0.
  --lexical-only            explicit opt-in to ingest without embeddings (all content becomes
                            FTS-only). Without it, an unavailable backend is a refusal (exit 4).
  --reindex                 clear all segments + fingerprint, re-extract and re-embed
                            everything under the stored/current roots (fingerprint-mismatch
                            remediation).
  --exclude GLOB            repeatable; glob relative to any root (also matched against the
                            absolute path). Adds to the built-in skip rules.
  --include-datasets        do not apply dataset-folder detection (DigUp heuristic, optional).
  --max-file-mb N           per-file size cap [default 200; code files 1 MB hard cap]
  --max-pages N             PDF page cap [default 5000; first-pass step 30 pages]
  --max-chunks N            per-file chunk cap [default 5000; first-pass step 40 chunks]
  --audio-max-minutes N     audio/video soundtrack duration cap [default 120]
  --max-depth N             crawler depth [default 2] (0 = the URL itself only)
  --crawl-max-pages N       crawler page cap per ingest [default 50]
  --crawl-max-mb N          crawler per-page byte cap [default 5]
  --crawl-no-robots         ignore robots.txt (default: respect it)
  --politeness SECONDS      crawler delay between same-host requests [default 1.0]
  --workers N               extraction thread pool [default 2]
  --batch N                 embedding batch size [default 16]
  --backend NAME            override global backend for this run
```

Behavior:

- At least one PATH or URL required (exit 2 otherwise). URLs (`http(s)://`) are crawled within
  the bounds above, same-host only, robots.txt respected; each fetched page is stored as
  `kind=webpage`, `path=<canonical URL>`, `source='url'`.
- Walk order deterministic (sorted); skip decisions recorded with reasons
  (secrets, minified, license files, developer folders, size caps, unknown type).
- Unchanged files (size+mtime match, sha256 confirms) skipped; changed files re-extracted with
  atomic segment replacement; deleted files pruned; all counted in the summary.
- First ingest records `meta.roots_json`; later runs merge roots unless `--reindex`.

stdout (human):

```
ingest: 3 roots, 214 files, 4 URLs
  content-indexed  182   (pdf 40 · doc 96 · code 0 · webpage 4 · image 0 · audio 0 · video 0)
  metadata-only     21   (image 14: no image backend · audio 4: no audio backend · pdf 3: pdftotext not found)
  unsupported        9   (secrets 2 · minified 4 · unknown type 3)
  failed             2   (/a/b.pdf: timeout after 60s · /a/c.rtf: textutil failed rc=1)
  skipped-unchanged 89 · deleted 3 · partial reads 5
vectors: 4,231 · segments: 5,102 · index 41.2 MB · fingerprint llama/embg2-q8/768/a1b2c3d4e5f6a7b8
warnings: llama.cpp build changed since last ingest (recorded: b11146, installed: b11202) — reindex recommended
```

stdout (`--json`):

```json
{"roots": ["/a", "https://x.dev"], "files": {"scanned": 214, "content_indexed": 182,
 "metadata_only": 21, "unsupported": 9, "failed": 2, "unchanged": 89, "deleted": 3,
 "partial": 5},
 "by_kind": {"pdf": {"content_indexed": 40, "metadata_only": 3}},
 "failures": [{"path": "/a/b.pdf", "detail": "timeout after 60s"}],
 "metadata_reasons": [{"reason": "no image backend", "count": 14}],
 "vectors": 4231, "segments": 5102,
 "fingerprint": {"id": "a1b2c3d4e5f6a7b8", "backend": "llama", "model_id": "embeddinggemma-2-Q8_0", "dim": 768},
 "notices": [{"code": "build_changed", "message": "..."}]}
```

`--dry-run` prints the same shape with `"dry_run": true` and no vectors/fingerprint change.

---

## 3. `goldmine search`

```
goldmine search QUERY [flags]

  --limit N            results [default 20]
  --scorer NAME        hybrid (default) | lexical | semantic | rrf
                       (ablation baselines kept per design verdict 3; rrf = k=60
                       reciprocal-rank fusion of the two ranked lists)
  --kinds KINDS        comma list filter: doc,pdf,code,image,audio,video,webpage
  --z-gate X           semantic qualification threshold [default 2.0]
  --lexical            force lexical-only AND suppress the fingerprint refusal (results
                       labeled `scorer: lexical (stale-vectors)` in output)
  --excerpt-chars N    excerpt width [default 240]
```

Query semantics (normative):

- Document/query prompts per EmbeddingGemma 2 model card, verbatim:
  `task: search result | query: {text}` for the query; `title: {title} | text: {text}` for
  documents (title = file stem / page number label); media vectors carry no prompt.
- Terms: lowercased alphanumeric tokens, deduped, stopwords and kind-words dropped (DigUp
  `Searcher.terms` list, English subset for v1).
- Lexical: `fts MATCH "t1"* OR "t2" ... ORDER BY bm25(fts,3.0,1.0) LIMIT 1000` —
  **SQLite bm25 is lower-is-better; ascending order = best first; the raw value is never
  added to a higher-is-better score** (verdict 3 fix). Coverage = share of query terms found
  anywhere in the file's rows.
- Semantic: z-scores with per-modality centering; modality groups < 20 vectors use the global
  mean; std floor 0.02 (verdict 3 tiny-group fix, DigUp-measured).
- Fusion: candidates = {z ≥ z_gate} ∪ {coverage ≥ 0.5};
  `score = w_m·max(z,0) + w_c·coverage^1.5 + kind_hint·1.0 (flat, capped — never stacks)`;
  `(w_m, w_c) = (1.0, 2.0)`, lookup queries (every term digit-bearing and alphanumeric-mixed
  or ≥4 digits) invert to `(0.5, 4.0)`.
- Missing-vector behavior (verdict 3 fix): a chunk without a vector (digits-only text, thin
  PDF page) competes lexically only; a file with zero vectors can still qualify on coverage;
  its `z`/`cosine` fields are `null`.
- Hits are file-level: a file's overlapping chunk matches collapse to its best segment
  (meaning segment when z ≥ gate, else keyword segment).

stdout (human), one block per hit:

```
1. score 3.41  z 3.12  cov 0.67   doc   /notes/k8s.md  L120-128
   "the scheduler evicts a pod when a node is under pressure and the pod has no toleration…"
2. score 2.90  z —     cov 1.00   pdf   /papers/trx50-datasheet.pdf  p.4
   "TRX50 chipset requires BIOS version 1.0.0.4a or later…"
```

( metadata-only files show `no-vector` in place of z; `--scorer lexical` shows `lexical`. )

stdout (`--json`): array of

```json
{"rank": 1, "score": 3.41, "z": 3.12, "cosine": 0.812, "coverage": 0.67,
 "kind": "doc", "path": "/notes/k8s.md", "loc": 120, "loc_end": 128,
 "segment": "chunk", "excerpt": "...", "file_status": "content_indexed",
 "scorer": "hybrid"}
```

Exit 0 even with zero hits (`"hits": []` / `nothing found for …` — an honest empty is a
feature). Exit 3 on fingerprint mismatch (unless `--lexical`), 4 if a semantic scorer is
requested and no backend is available.

---

## 4. `goldmine distill`

```
goldmine distill [flags]

  --threshold X        cluster cosine threshold τ [default 0.70]
  --min-members N      drop clusters smaller than N [default 3]
  --max-chunks N       corpus bound; refuse (not truncate) beyond it [default 50000]
  --kinds KINDS        restrict corpus by kind (default: all vectorized kinds)
  --limit-cards N      cards in output, by member count [default 20]
  --out FILE           write markdown to FILE (default: stdout)
  --json               JSON cards to stdout instead of markdown
```

Algorithm (normative, deterministic — no RNG anywhere):

1. Corpus = all segments with vectors, ordered by `(file_id, segment_id)`.
2. Dedupe: exact duplicates by sha256 of normalized text; then near-duplicates, greedy in
   order, cosine ≥ 0.97 to an already-kept chunk.
3. Greedy leader clustering: for each chunk in order, compare to each cluster centroid
   (vectors are L2-normalized); assign to the first centroid with cosine ≥ τ (ties on equal
   similarity → lowest cluster index); else found a new cluster. Centroid update rule:
   `centroid ← normalize(sum of member vectors)` maintained incrementally as a running sum,
   renormalized after each assignment.
4. Singleton/small-cluster handling: clusters < `min_members` are dropped (count reported);
   `--min-members 1` keeps everything.
5. Cards sorted by member count desc, ties by earliest first-member `(file_id, segment_id)`.
6. Card content: representative = member with max cosine to final centroid (ties → lowest
   segment id), excerpt **verbatim**; TF-IDF top 8 terms (alphanumeric tokens, English
   stopword list shared with search; idf over cluster-level document frequency);
   citations: up to 10 `(path, kind, loc, loc_end)` plus distinct-file count.
   **Never synthesized prose** — extractive only.

stdout (human, markdown):

```
## goldmine cards — 12 topics from 4,231 chunks (τ=0.70, min=3, algo greedy-leader.v1)

### 1. k8s pod eviction node scheduler kubelet taint toleration pressure   [14 chunks · 5 files]
> the scheduler evicts a pod when a node is under pressure and the pod has no toleration…
  — /notes/k8s.md (doc, L120-128)
  — /git/ops/runbook.md (doc, L400-411)   [+3 more files]
```

Exit 3 on fingerprint mismatch; exit 1 if corpus has < 2 vectorized chunks.

---

## 5. `goldmine status`

```
goldmine status [--json] [--failures N]     (N = how many failure/metadata rows to list, default 10)
```

Human output: index path + size, schema/fingerprint summary, backend availability one-liner,
counts table by kind × capability, vector/segment counts, pending/failed lists with detail,
notices (build_changed, extractor_version drift, metadata_reasons aggregates). Exit 0; exit 4
if the index file does not exist (with a hint to run `goldmine ingest`).

`--json`: one object with the same fields (capability counts keyed `content_indexed`,
`metadata_only`, `unsupported`, `failed`).

---

## 6. `goldmine doctor`

```
goldmine doctor [--json]
```

Checks (each yields OK / WARN / FAIL + detail + fix hint):

- python 3.12+, numpy importable
- backends: `llama` (binary found? `--version` parses? model file exists? sha256 cache valid?
  optional live probe: spawn + embed `"ping"` → expect dim 768), `foresight` (grpcio
  importable? socket exists? `Ping` → version), `mock` (always OK)
- tools: `pdftotext -v`, `textutil` (macOS), `ffmpeg -version`, `ffprobe`; stretch:
  `whisper-cli` + `--asr-model` path
- index: exists? schema_version current? fingerprint present and matching live backend?
  extractor_version drift counts; orphaned rows (segments without files — should be none)
- config file parses; env vars valid; disk free on `<home>`

Exit 0 if no FAIL; exit 4 if any FAIL. `--json`: `{"checks": [{"name", "status", "detail",
"fix_hint"}], "summary": {...}}`.

---

## 7. `goldmine backends`

```
goldmine backends [--json] [--probe]
```

Lists the three backends with: id, available, dim, capability (text/image/audio), source of
truth (binary path / socket path / in-process), and — with `--probe` — a live round trip per
available backend. This is where the v1 media truth is visible at a glance:

| backend | text | image | audio | note |
|---|---|---|---|---|
| llama | yes | no | no | `/v1/embeddings` is text-only (llama.cpp) |
| foresight | yes | yes | yes | experimental; helper must be running |
| mock | yes | yes | yes | deterministic hash vectors, tests only |

Exit 0 always (unavailability is data, not an error). `--probe` failures mark the backend
unavailable with the error tail.

---

## 8. `goldmine reset`

```
goldmine reset [--yes] [--reindex]
```

- `goldmine reset` (no flags): interactive confirm (or `--yes`) → **delete** the index file
  (and its WAL/SHM). Nothing else is touched.
- `--reindex`: keep files/roots, delete segments + cards + fingerprint, then run ingest
  immediately (same backend/config). Fingerprint-mismatch remediation for "model changed on
  purpose". Exit 5 if the subsequent ingest had failures.

---

## 9. Capability matrix (normative definitions — verdict 5a)

Every scanned file (and crawled URL) ends in exactly one capability, stored in
`files.capability`, visible in `status`/`doctor`, and per-hit in search output:

| capability | Definition | Typical causes |
|---|---|---|
| `content_indexed` | ≥1 segment carries a vector **or** FTS text extracted from the file's content | normal doc/pdf/code/webpage |
| `metadata_only` | File recognized and recorded (name-row FTS), but zero content extracted or zero content vectors possible with current backend | image under llama backend; pdftotext missing; rtf without textutil |
| `unsupported` | Intentionally not read, reason stored | secrets (pem/key/.env/id_rsa…), minified bundles, license files, unknown binary type, size cap |
| `failed` | Extraction attempted and errored | subprocess timeout, corrupt zip, decode crash |

Orthogonal flags in `info`/`detail` (a file can be `content_indexed` **and** degraded):

- `partial: true` + `read_to/read_end` — caps truncated the read (pages/chunks/duration)
- `lossy-decode: <assumed charset>` — bytes decoded with a fallback encoding
- `thin-text` — PDF page(s) below 200 chars kept for FTS but vectorless (no OCR in v1)
- `dedup: n frames dropped` — video shot dedup count

Honesty rules: `metadata_only` must never render as a success equal to `content_indexed`;
`unsupported` always carries a machine-stable `reason` code; the ingest summary and `status`
always break out all four capabilities by kind.

---

## 10. Config file

`~/.config/goldmine/config.toml` (stdlib `tomllib`; missing file = all defaults; unknown keys
WARN). Every value here is overridable by env/flag per the resolution order.

```toml
[core]
home = "~/Library/Application Support/goldmine"   # data dir (index + logs)

[backend]
default = "llama"

[llama]
server   = "/opt/homebrew/opt/llama.cpp/bin/llama-server"
model    = "~/git/goldmine/models/embeddinggemma-2-Q8_0.gguf"
ctx      = 2048
batch    = 16
startup_timeout  = 120     # seconds to /health
request_timeout  = 60      # per embeddings call

[foresight]
socket = ""                # empty = $TMPDIR/foresight_helper.sock
expect_version = ""        # optional pin; mismatch ⇒ WARN (fingerprint uses live version)

[ingest]
max_file_mb = 200
max_pages = 5000
max_chunks = 5000
audio_max_minutes = 120
workers = 2
politeness = 1.0

[search]
limit  = 20
z_gate = 2.0
scorer = "hybrid"

[distill]
threshold   = 0.70
min_members = 3
max_chunks  = 50000
```

## 11. Environment variables

| Var | Overrides | Notes |
|---|---|---|
| `GOLDMINE_HOME` | `core.home` | data directory root |
| `GOLDMINE_CONFIG` | `--config` | config file path |
| `GOLDMINE_BACKEND` | `backend.default` | `llama`/`foresight`/`mock` |
| `GOLDMINE_LLAMA_SERVER` | `llama.server` | binary path |
| `GOLDMINE_MODEL` | `llama.model` | GGUF path |
| `GOLDMINE_FORESIGHT_SOCKET` | `foresight.socket` | UDS path |
| `GOLDMINE_LOG_LEVEL` | `-v/-q` | `DEBUG/INFO/WARNING/ERROR` |
| `GOLDMINE_LIVE` | — | test gate only: enables live-backend tests (see TEST_PLAN) |

## 12. Pinned runtime facts (this machine, recorded for reproducibility)

- llama.cpp `0.5.0 (build 11146, commit 7fe450e19)` at `/opt/homebrew/opt/llama.cpp/bin/llama-server`
- Model `models/embeddinggemma-2-Q8_0.gguf` (310 MB) + `mmproj-embeddinggemma-2-Q8_0.gguf`
  (555 MB), pinned ggml-org rev `bfcd298762cc34d0357ece5ebdd31791a3a374d8` (DigUp's exact pin)
- ForesightHelper live at `$TMPDIR/foresight_helper.sock`; grpcio 1.84.0 (pip --user);
  `python3` = pyenv 3.12.0; poppler + ffmpeg + whisper-cpp via brew
