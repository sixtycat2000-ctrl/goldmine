# goldmine — Design Decisions

Decision log for v1. Sources: DigUp reference implementation (Swift, production-tested),
Foresight verified assets, and an independent ChatGPT review (via `codex exec`, 2026-10-12)
whose five verdicts are treated as agreed requirements. Each decision records: context,
decision, and what was adopted/deferred/deviated.

---

## 1. Consultation summary — the five verdicts (quoted, 2026-10-12)

> **1. Python AGREE.** Deps allowed: numpy (vector scans), grpcio+protobuf (optional extra for
> foresight backend), pytest (dev). Everything else stdlib or external CLI tools (pdftotext
> from poppler, ffmpeg) detected at runtime with graceful degradation.

> **2. llama-server subprocess + HTTP AGREE.** Must: pin & record llama.cpp build version +
> model file SHA256 in index meta; bind 127.0.0.1 only; readiness probe; batching; request
> deadlines; cancellation; bounded log capture; kill only processes we spawned. Foresight
> backend = optional experimental adapter. CRITICAL: persist per-index embedding fingerprint
> (backend, model id, model hash, dim, prompt scheme, pooling, normalization, preprocessing
> version); refuse to mix vectors across fingerprints; changing fingerprint requires reindex;
> never silently switch backends within one index.

> **3. Hybrid search AGREE as baseline with fixes:** handle tiny/empty modality groups (fall
> back to global z), explicit bm25 direction inversion, defined missing-vector behavior, cap
> kind-hint boost, collapse overlapping chunk hits to file-level results, keep
> lexical-only/semantic-only/RRF baselines available for a small eval harness. Chunking must
> respect paragraph boundaries and stay under model token limit (~512 tokens text-only).

> **4. Distill AGREE extractive-first, no bundled chat model.** Deterministic clustering (spec
> the exact algorithm: deterministic ordering, cosine threshold default, centroid update rule,
> singleton handling, max corpus bounds). Dedupe near-identical chunks before clustering.
> Output = source-backed topic cards: representative excerpt verbatim + TF-IDF top terms +
> citations (file, kind, loc/loc_end) — never synthesized prose. Optional --llm polish later,
> preserving provenance.

> **5. Robustness CHANGE (required):** (a) capability matrix — every ingest result classified
> as content-indexed | metadata-only | unsupported | failed, with per-file status in DB and
> `goldmine status`/`doctor` surfacing it; (b) BOM-aware decoding, HTML charset declaration
> handling, --encoding override, lossy-decode noted in status; (c) bounded work: size caps,
> page caps, duration caps, subprocess timeouts, symlink-cycle detection, ignore patterns;
> (d) incremental: content hash (sha256), skip unchanged files, atomic segment replacement on
> reindex, deletions handled, extractor version recorded per file; (e) crawler bounds: max
> pages/bytes/depth, same-host, URL canonicalization+dedupe, content-type check, redirect
> scope, robots.txt via stdlib urllib.robotparser, politeness delay default 1s, backoff on
> 429/5xx; (f) `goldmine doctor` diagnosing backends/tools/models; metadata-only must be
> visibly distinct from content-indexed.

All five verdicts: **adopted**. Disposition details below; deferrals listed in §10.

---

## 2. Language & dependencies — D1 (verdict 1) — ADOPTED

- Python 3.12 (matches the machine's pyenv 3.12.0), stdlib-first.
- `numpy` is the only required third-party dep (matrix scans, fp16 packing, z-score math).
- `grpcio`+`protobuf` live behind the `foresight` extra and are imported lazily — the llama
  path never imports them.
- `pdftotext` (poppler, brew-installed), `textutil` (macOS built-in), `ffmpeg/ffprobe` are
  detected at runtime; absence degrades capability, never crashes, and is always reported.
- `whisper-cpp` (installed, no model bundled) is a **stretch** ASR hook: `--asr` opt-in,
  user-provided model path; without it, audio stays honestly metadata-only under llama.
- Deviation note: verdict 1 allows docx via external tools; we extract **docx with stdlib
  (zipfile + xml.etree)** because .docx is a zip of XML and this removes a tool dependency.
  `textutil` remains the path for rtf/doc/odt (macOS built-in, no install burden).

## 3. Backend execution — D2 (verdict 2) — ADOPTED with one refinement

- llama-server as a child process, HTTP `127.0.0.1` only, readiness probe on `/health`,
  batch=16, per-request deadlines, cancellation via socket close + SIGTERM→SIGKILL of **only**
  our Popen children, bounded stderr ring buffer + rotated log files. Build string recorded.
- Fingerprint gate as specified: `backend` family, model id, model **sha256**, dim, prompt
  scheme, pooling, normalization, preprocessing version — canonical JSON, sha256-truncated id,
  stored in `meta`, checked before every embed/semantic-search/distill. Mismatch ⇒ exit 3,
  explicit remediation (`ingest --reindex` / `reset --reindex`). Never silently switch.
- **Refinement (deliberate deviation): the llama.cpp *build* string is provenance, not
  fingerprint.** Verdict 2 says "pin & record llama.cpp build version + model file SHA256 in
  index meta" — recording: yes; in the fingerprint: no. DigUp's `IndexStore.migrateVectorNames`
  (schema 3→4) exists precisely because keying vectors to a brew build forces full reindexes
  on unrelated `brew upgrade`s. Compromise implemented: `meta.llama_build` + a
  `build_changed` WARN notice in `status`/`doctor` recommending (not forcing) reindex.
- Foresight adapter: experimental, clearly labeled. Since we cannot hash the helper's model
  files, the `Ping().version` string takes the model-identity slot in the fingerprint; a
  helper upgrade ⇒ fingerprint change ⇒ reindex. That is conservative in the right direction.

## 4. Hybrid search — D3 (verdict 3) — ADOPTED, all six fixes integrated

- Kept DigUp's measured fusion (it beat RRF on both DigUp evals, 2026-10-07: R@1 0.88 vs 0.73
  synthetic, 0.86 vs 0.82 real): `score = w_m·max(z,0) + w_c·coverage^1.5 + kind-hint·1.0`,
  lookup queries invert to `(0.5, 4.0)`, qualification `z ≥ 2.0 OR coverage ≥ 0.5`.
- Fix: modality groups < 20 vectors fall back to global mean (DigUp already did this — now
  normative, with the std floor 0.02).
- Fix: **bm25 direction** — SQLite `bm25()` is lower-is-better. We use it only for
  `ORDER BY bm25(fts,3.0,1.0) LIMIT 1000` (ascending = best first) and for rank generation in
  the RRF baseline; it is never added into a higher-is-better score. Spec'd verbatim in SPEC.
- Fix: missing-vector behavior defined — vectorless chunks compete lexically only; zero-vector
  files qualify on coverage; `z`/`cosine` render as `null`.
- Fix: kind-hint boost capped at a flat +1.0, never stacking.
- Fix: file-level collapse — best segment per file (meaning segment when z ≥ gate else keyword
  segment), mirroring DigUp's per-file best but made explicit.
- Fix: baselines kept — `--scorer lexical|semantic|rrf` (k=60) feed the eval harness.
- Chunking: 1800 chars / 200 overlap (DigUp defaults ≈ 450 tokens), paragraph-boundary
  preference inside the window, whitespace fallback, hard guidance under ~512 tokens;
  code chunks align to whole lines (DigUp's codeStretch=1800 won its eval) with
  loc/loc_end = line numbers.

## 5. Distill — D4 (verdict 4) — ADOPTED, algorithm pinned

- Extractive-first; no chat model bundled. `--llm polish` deferred (§10).
- Algorithm (v2.1 R6 SUPERSEDED the v1 fixed τ=0.70 — design of record:
  `docs/v2.1-r6-distill.md`): deterministic ordering by `(file_id, segment_id)`; exact
  dedupe by sha256 of normalized text, then greedy near-dup removal at cosine ≥ 0.97; τ
  CALIBRATED per run (sampled pairwise Otsu with a percentile fallback — real corpora are
  unimodal and expected to run `tau_source='percentile'`; `--threshold` is now a manual
  override); leader clustering WITH an average-link admission guard (`mean-cos-to-members ≥
  max(τ−0.10, 0.35)`) — the v1 centroid-only rule was the mega-cluster bug (measured: one
  card absorbing 99.84% of a real corpus). Clusters < `min_members` (3) dropped, never
  silently (size histogram reported); corpus bound 50k chunks **refuses** rather than
  truncates.
- Cards (v2): medoid primary excerpt + up to 2 coverage picks (greedy max-min-distance),
  filtered TF-IDF top-8 terms (member-df ≥ 2 gate, bigram competition), extractive titles,
  per-card fp16 centroids under named views (`distill_runs`/`distill_views`, schema v4);
  incremental reruns with frozen memberships (dissolve + tombstone sweep); citations
  `(path, kind, loc/loc_end)`, never synthesized prose. Cards store their `fingerprint_id` +
  `distill_algo` string; the v1→v4 migration recreated cards empty (announced, snapshotted).

## 6. Robustness — D5 (verdict 5) — ADOPTED, all six items

- (a) Capability matrix: `content_indexed | metadata_only | unsupported | failed` in
  `files.capability`, orthogonal degradation flags (`partial`, `lossy-decode`, `thin-text`,
  frame-dedup counts) in `info`/`detail`; surfaced by `status`, `doctor`, ingest summary, and
  search hits (`file_status`).
- (b) Decoding: BOM sniff (utf-8-sig/utf-16/utf-32) → UTF-8 → declared charset (HTTP header
  for crawled pages, `<meta charset>` for files) → windows-1252 lossy with `detail` note;
  `--encoding` override per ingest run.
- (c) Bounded work: `--max-file-mb` 200 (code hard cap 1 MB, DigUp `maxCodeBytes`), page cap
  5000 (first step 30, DigUp `pdfStep`), chunk cap 5000 (first step 40, DigUp `docStep`),
  audio cap 120 min (windows 30 s / hop 25 s = 5 s overlap, 16 kHz mono — DigUp `audioWindow`
  was 30 s because the model silently ignores more), subprocess timeouts, dir-symlink policy
  (not followed by default) + visited `(device, inode)` set, `--exclude` globs.
- (d) Incremental: sha256 content hash; (size, mtime) prefilter → hash confirm; atomic
  segment replacement in one transaction; deletions pruned and counted; `extractor_version`
  per file with drift notices.
- (e) Crawler: stdlib `urllib` + `html.parser`; same-host, canonicalization (scheme/host
  lowercase, fragment stripped, path resolved), URL dedupe, content-type check
  (`text/html`), max 5 redirects staying in-host, robots.txt via `urllib.robotparser`
  (opt-out `--crawl-no-robots`), politeness 1.0 s, exponential backoff 1→2→4 s (max 3) on
  429/5xx, per-page byte cap, `--max-depth`, `--crawl-max-pages`.
- (f) `doctor` with fix hints; `metadata_only` always visually and structurally distinct.

## 7. Storage — D6 — DigUp schema v5 adapted

- Single SQLite file, WAL, vectors as 768×float16 L2-normalized BLOBs beside their rows
  (crash-atomicity argument), FTS5 tokenizer
  `porter unicode61 remove_diacritics 2 categories 'L* N* Co M*'` kept **verbatim** (it fixes
  real CJK/diacritics tokenizer bugs DigUp hit), name-row convention kept
  (name = filename, body = folder words, no vector).
- Additions per verdict 5: `files.source` (file|url), `sha256`, `capability`,
  `extractor_version`; new `cards` / `card_chunks` tables for distill with fingerprint
  pinning. `schema_version = 1` for goldmine (DigUp's v5 numbering does not transfer).

## 8. Prompts & preprocessing — D7 — verbatim from the model card

`query = "task: search result | query: {text}"`, `document = "title: {title} | text: {text}"`
(title = file stem for chunks, page label for PDF pages), media vectors unprompted
(DigUp `Prompts`, EmbeddingGemma 2 model card). Prompt scheme versioned `v1` inside the
fingerprint; changing a prompt is a reindex. Client re-normalizes output vectors (L2) —
defensive, no-op when the server already normalized.

## 9. Index location & config — D8 — decided

- Data: `~/Library/Application Support/goldmine/` (macOS-native — this tool is explicitly a
  local-first macOS CLI; `GOLDMINE_HOME` / `--db` escape hatches exist).
- Config: `~/.config/goldmine/config.toml` (XDG, stdlib `tomllib`).
- Rationale: don't fight platform convention for data; use the portable convention for the
  hand-editable config. Precedence flag > env > config > default, documented in SPEC.

## 10. Deferred (recorded, not designed away)

| Item | Verdict/source | When |
|---|---|---|
| `--llm polish` for cards (provenance-preserving) | verdict 4 "optional, later" | post-v1 |
| ASR hook (`whisper-cli` + user model) making audio text-indexable under llama | runtime fact "optional ASR hook" | stretch, v1.x |
| Image/audio embedding via mmproj | `/v1/embeddings` is text-only; mmproj route unverified for embeddings | blocked on llama.cpp capability; foresight covers it meanwhile |
| Second dedicated code index (DigUp `code.sqlite`) | DigUp architecture | v1 keeps ONE index (`--code`); split only if eval shows contamination |
| Xattr screenshot detection (`com.apple.metadata:kMDItemIsScreenCapture`) | DigUp `isScreenshot` | name-prefix heuristic only in v1 |
| ANN / persistence for search | unnecessary at measured scale (~20k vectors, ms brute-force) | revisit >1M vectors |
| Watch/daemon mode, incremental FSEvents | out of scope | v2 |
| Non-macOS packaging (Homebrew formula, PyPI) | | after v1 stabilizes |

## 11. Open items for implementation

- `--threshold 0.70` and near-dup `0.97` defaults are first-guess values; the eval harness
  (TEST_PLAN §5) must report sensitivity before we freeze them.
- llama-server port pick has a bind race (socket closed before child binds); mitigation is
  retry-on-unhealthy ×3. If flaky in practice, fall back to a fixed high port + probe.
- Foresight audio bytes: DigUp sends 16 kHz mono PCM16 WAV windows; the live helper's exact
  expectation is verified by GOLDMINE_LIVE tests before foresight leaves "experimental".
