# goldmine v2 — Master Redesign Plan

> Consolidated plan across the six domain docs (`01`–`06`). This file resolves their
> conflicts, merges their phasing tables into one roadmap, and is the document a maintainer
> executes from. Domain docs remain the detailed spec for their area; where this file and a
> domain doc disagree, this file wins (conflicts are listed explicitly in §3).
> Status: design, pre-implementation. Consolidated 2026-10-16; **revised the same day after
> three reviews (§4 records every accepted finding and the amended calls)**.

---

## 1. Vision & goals

Two product targets, verbatim from the docs:

1. **Search anything, local, ACCURATE** (01, 02, 04): every byte of a real Mac corpus
   becomes searchable — speech via ASR, screenshots/scans via Vision OCR, any image via
   sips thumbnails, video via soundtrack + frames — and results are *proven* accurate on a
   hand-labeled real corpus, not asserted on synthetic fixtures.
2. **Easy to use** (03, 05): a stranger installs, runs `goldmine init`, and gets a relevant
   result inside ten minutes; every failure names a copy-pasteable fix; nothing opens or
   runs that the user wasn't told about. No GUI, no daemon you didn't ask for.

### 1.1 What v2.0 claims — and what it does not (falsifiable; review C3)

v2.0 makes exactly three accuracy claims, each falsifiable by the harnesses it ships:

1. **Measured, published accuracy on a real corpus**: success@5 / MRR / lookup-success@1
   from `eval_real.py` on the hash-pinned real corpus, printed with per-tag n.
2. **Zero file-level embed failures from token overflow** (the measured 2670-token 400
   class — structurally impossible after D3's `/tokenize` counting + C2's isolation).
3. **Per-kind coverage at the stated floors**, measured by `status --json` + the coverage
   fixture (02 §9).

v2.0 does **not** claim better ranking than v1: if the inherited v1 fusion weights pass
the gates, "**measured, kept**" is success — tuning is v2.1 work (G1).

### 1.2 Combined acceptance gates (merged from 01 §1, 02 §1/§9, 03 §1, 04 §1, 06 §1)

| Area | Metric | v1 today | v2.0 gate (floor to establish) | v2.2 target |
|---|---|---|---|---|
| Accuracy | hybrid success@5 / success@1 / MRR, real corpus `evals/real-*`, llama backend | unmeasured on real data | **≥ 0.85 / ≥ 0.55 / ≥ 0.65** (published targets) | ≥ 0.90 / ≥ 0.70 / ≥ 0.75 (targets) |
| Accuracy | nDCG@10 (graded) | not computed | baseline set at v2.0 | ≥ baseline + 0.05 over lexical |
| Accuracy | lookup subset success@1 | unmeasured | **≥ 0.90** (published target) | ≥ 0.95 (target) |
| Accuracy | zero-hit honesty on real noise queries | synthetic harness only | 0 hits, exit 0 | — |
| Accuracy | regression gate (the one accuracy gate) | none | any tracked metric −0.02 vs `evals/baseline.json` beyond bootstrap CI → eval exits 1; per-tag deltas advisory below n ≥ 20 | — |
| Accuracy | opt-in `--rerank` | n/a | n/a in v2.0 (v2.2): ≤ 3.5 s total, off by default | cut if ≤ +0.02 nDCG for +2 s |
| UX | install → first relevant result (U1) | ~7 commands, 30+ min | **≤ 4 commands, ≤ 10 min**: `pip install goldmine` (real PyPI publish, C1) → `init` → `search` | — |
| UX | `find` keystroke → rendered, warm (U2) | no TUI | **deferred** — TUI killed 2026-10-16; warm in-process `search` p50 ≤ 300 ms stays | — |
| UX | failure paths with `fix:` + `more:` (U3) | partial | **≥ 95 %** of audited paths | — |
| UX | ingest ticker ≤ 2 s; ETA ±20 % (U4) | none | yes | self-calibrating rates (v2.1) |
| UX | zero-hit next step (U5) / versioned JSON (U6) / open honesty (U7) | partial | yes | — |
| Perf | search e2e cold @100k (T1) / spawns per command (T8) | 1.9 s; 0.63–1.1 s spawn every command | ≤ 0.7 s cold CLI (T8 unchanged in v2.0; daemon ≤ 0.3 s + 0 spawns: v2.1) | — |
| Perf | warm query p50/p95 @100k (T2) | 222 ms in-process; ~0.9 s/command | **≤ 300 ms / ≤ 500 ms** in-process (daemon ~40 ms: v2.1) | — |
| Perf | scan+fusion @500k / @1M (T3/T4) | 1.0 s / not viable | unchanged in v2.0; ≤ 60 ms / ≤ 150 ms with the mmap pack (v2.1, gated on G0) | — |
| Perf | embed throughput (T6) / ingest rate (T7) | 16–18 texts/s; 94 files/min | keep ≥ 16 texts/s; **≥ measured v1 rate (94 files/min), no regression** — token chunking is the lever (28.2 vs 16.3 texts/s), overlap ≤ 1 % (B8) | — |
| Perf | resident memory @100k (T9) | 729 MB tree | ≤ 600 MB steady; fp32 residency forbidden > 250k vectors (pack: v2.1 per G0) | 1M = file-backed pages, not RSS |
| Coverage | per-kind vector coverage (02 §9, `status --json` KPI) | text only (images/audio metadata-only under llama) | txt/md/html/docx/rtf/doc/odt/eml/epub/pdf-text/code/webpage/image **100 %** · pdf-scanned ≥ 95 % · audio ≥ 98 % · video ≥ 95 % (soundtrack) · screenshot OCR text 100 % · secrets/minified/archives 0 % with reasons | frames where foresight |
| Coverage | 1 h podcast transcribed + indexed | impossible | **< 15 min** (ASR measured 7.2× realtime) | — |
| Coverage | 100-file mixed corpus end-to-end | — | < 20 min | — |
| Distill | canary purity / ARI / recovery (8 planted topics → one card each, ≥ 80 % docs) | no metric; one mega-card failure mode | **v2.1** (E1 deferred): ≥ 0.70 / ≥ 0.50 / ≥ 6 of 8 | — |
| Distill | excerpt fidelity / `--pack` budget integrity / human coherence (30 cards, 2 raters, annual) | verbatim kept | **v2.1** (E1 deferred): 100 % / 100 % / ≥ 70 % (manual; purity is the automatic proxy) | — |

Gate naming (2026-10-16, B12 — normative): **"gate" is reserved for** (a) the
−0.02-vs-`baseline.json` regression rule *with counts/uncertainty* (01 §5.1) and (b) the
machine-honest perf gates in `scripts/bench.py` (04 §7 — v2.0 ships the local two-gate
sliver; the GHA nightly is v2.1). The 0.85/0.90-class accuracy numbers are **published
targets**: measured, printed with per-tag n, and owned — but not pass/fail. Per-tag deltas
below n ≥ 20 queries (`web` = 5, `noise` = 5 today) are advisory. Coverage KPIs are
enforced by `goldmine status --json` + the coverage fixture (02 §9); distill gates arrive
with the v2.1 canary (06 §1).

---

## 2. v2 architecture at a glance

### 2.1 What changes vs v1 (delta)

| Axis | v1 | v2 |
|---|---|---|
| Process model | one OS process per command; llama-server spawned/killed per command (0.6–1.1 s) | optional `goldmine serve` UDS daemon (05 §4.4 contract; **v2.1**): flock single instance, per-index leases on one resident llama-server, linger 300 s, pressure-aware memory guard; one-shots fall back to the v1 cold path, footer says `via: serve\|cold` |
| Identity | one global 8-field fingerprint incl. `preprocessing`; any change ⇒ full reindex | per-dimension identity (05 §4.2): `embed_id` (vector comparability; `prompts_doc` only — §3 C2, rename itself v2.1) + per-file `chunker_version`/`extractor_version` + content sha; one-way migration ladder with fixture tests and pre-migration snapshot |
| Reindex | `--reindex` clears everything (95–150 min at 100k chunks) | v2.0: stale sets are re-done by plain ingest (02 G10 skip fix), surfaced by `status` ("N files stale since") with no refusal; v2.1: `goldmine reindex plan\|run` per-file stale-reason planner, self-calibrating estimates; refusal exists only on incompatible embed identity |
| Vector scan | fp32 matrix rebuilt from SQLite blobs every query (0.17–2.0 s) | ≤ 250k vectors: resident fp16 cache, exact scan (04 §5 tree); above that a derived `<index>.vecs` fp16 pack (rebuildable, pack_epoch-header-checked, mmap blockwise f32 scan + `argpartition` top-k) — **v2.1, built only if G0's measured corpus justifies it**; SQLite blobs stay source of truth |
| Model obtainment | manual GGUF placement in repo `models/`; arch trap found at first search | manifest-pinned `goldmine model install\|list\|verify\|use\|check` into `<home>/models`; live arch probe kills the b11146-class failure at init; llama-server and goldmine-ocr ship as prebuilt release assets (05 §4.3/§4.7) |
| Extraction | text + raw-bytes images under foresight | whisper ASR (VAD, 7.2× realtime measured), Apple Vision OCR via prebuilt helper, sips thumbnails + EXIF (HEIC/RAW), video frames (v2.1), .eml/.epub, honest `unsupported` archives (02) |
| Search | inherited DigUp fusion, char-based chunks, chunk-start excerpts, `--kinds` only | real-corpus-measured accuracy with a regression gate, token-true chunking (`/tokenize`), «highlighted» snippets, `kind:/under:/after:/before:/site:` filters, `--why`, MMR + weaker-matches fold (01); `--format fzf` as the interactive layer |
| Distill | full-recompute τ=0.70 demo (observed one-mega-card failure) | v1 distill keeps working in v2.0; Otsu-calibrated τ + average-link admission guard, medoid + coverage excerpts, incremental views, `--pack` RAG digest, `--llm` polish land with E1 (v2.1) (06) |
| UX | 7 commands, no onboarding, no opener | `init`/`search` (+`--format fzf`)/`open`; `serve`/`find` defer to v2.1; universal 4-line error shape, ticker/ETA with media costs, NO_COLOR, schema-versioned JSON (03) |

### 2.2 v2 process model

```
 one-shot commands (stateless; identical semantics cold or warm, `via: serve|cold`)
  init · model install/check · ingest · reindex plan|run (v2.1) · search · open · distill · status/doctor/backup
      │ per-file commits (single writer)        │ embed/query if the socket answers
      ▼                                         ▼
      │ <home>/<index>.sqlite = TRUTH    ┌─────────────────────────────────────────┐
      │   meta/files/segments/fts/       │ goldmine serve (UDS daemon) — v2.1      │
      │   (cards-v2/distill_runs: v2.1)  │ $TMPDIR/goldmine-serve-<uid>/serve.sock │
      └─────────────────────────────────►│ flock · hello handshake · leases        │
      ▲                                  │ embed/search{id}/status/reload/shutdown │
      │ ingest · reindex run ·           │ cache: <index>.vecs fp16 pack (G0-gated)│
      │ goldmine watch (v2.1, polls);    │ derived + epoch-checked (rebuild ≈2 s)  │
      │ flock between writers;           │ linger 300 s → exit · pressure guard    │
      │ heartbeat <home>/watch.json      │ (04 §6.3 — never raw free pages)        │
      └──────────────────────────────────┴──────────────────┬──────────────────────┘
                                                            │ spawns & owns, never adopts
                                                            ▼
                                    llama-server 127.0.0.1:<ephemeral> (~484 MB resident)
 ├─► <home>/models/*.gguf + .sha256 (manifest-pinned, resumable) · <home>/bin/llama-server · goldmine-ocr
 └─► derived/aux: <index>.vecs pack (v2.1, G0-gated) · serve.json · registry.json (v2.1) · init_state.json · watch.json
```

Ownership: daemon contract 05 §4.4; perf budgets 04 §6.3; user surface 03 §5.4; watch 02 §5.6.

---

## 3. Resolved cross-domain conflicts

Each conflict below was flagged by the docs or found during consolidation. Resolutions here
are binding; domain docs keep their detail. (2026-10-16 review amendments are marked inline;
§4 lists them exhaustively.)

- **C1 — Model command naming** (02 §5.1/§5.8 `goldmine models add|list` vs 05 §4.3
  `goldmine model install|list|verify|use|check`). **Resolve: singular `goldmine model`**
  (05 owns the manager mechanics and already ratified the brief); `goldmine models` remains
  a hidden alias so 02's examples work; verb map `models add X` → `model install X`. 02's
  `ocr` key is a *prebuilt release asset install* (compile = dev fallback, A5/B13) behind
  the same command surface. Docs converge on the singular form. (Closes 05 §7 open question 3.)
- **C2 — Fingerprint `prompts` split vs `embed_identity`** (01 §5.3/G10 wants
  `prompts_doc`/`prompts_query`; 05 §4.2 said "prompts stay in embed identity"). **Resolve:
  one identity model** — `embed_id = sha256({backend, model_id, model_sha256, dim,
  prompts_doc, pooling, normalization, backend extras})`; `prompts_query` is recorded in
  meta for provenance and echoed by `--why`, but excluded from `embed_id` (only a
  *document*-prompt change alters stored vectors; a query-prompt change invalidates
  nothing). **Backfill binding rule (B2):** the migrated index's live `prompts_doc` is
  parsed from the legacy combined `PROMPT_SCHEME`'s `d='…'` part verbatim — byte-identical
  to fresh-index values — so migrated `embed_id`s equal fresh-index ids, the migration
  stays zero-re-embed, and `reindex plan` reports ZERO stale. **Amended 2026-10-16 (C2):**
  the rename itself moved to v2.1 with its consumers (G3/G4), planner-paced; the v2.0
  chunker bump (D3) no longer rides it.
- **C3 — Request id in serve search protocol** (03 §7 needs stale-query cancellation for
  `find`; 05 §4.4 protocol lacked it). **Resolve: optional backward-compatible `id` key**
  added to 05 §4.4's `search` request; daemon echoes `id` on responses and may drop
  superseded same-client in-flight queries; clients discard renders whose id is stale. 05
  owns the protocol field; 03 owns its use. (Ships with C4, v2.1.)
- **C4 — Watch mode phasing** (02 §5.6 ships `goldmine watch` at **v2.1** (stdlib polling;
  FSEvents only as a v2.2 opt-in extra); 03 watch UX at v2.2; 04 thermal/LPM backfill gate
  at v2.2; 05 §4.8/§6 item 17 says "when watch lands (v2.2, 02 §5.6)" — a miscitation of 02).
  **Resolve: watch core = v2.1 (02 §5.6); the watch ecosystem = v2.2** — 03's UX surfaces,
  04's backfill gate, 05's `operations` table + flock arbitration (trigger corrected to
  "after watch has shipped in v2.1"), FSEvents extra only if polling disappoints.
- **C5 — Token chunking double ownership** (01 §5.5: full token-space chunking, v2.0, M;
  02 §5.5: CJK count_tokens validate+resplit, v2.1, S/M — same mechanism, different phases).
  **Resolve: one item, one owner — 01 §5.5, v2.0, M.** 02's CJK re-split rule (deepest
  whitespace or sentence punctuation `。！？…`, never mid-ideograph) folds into the re-split
  design; 02's /tokenize cost fallback (per-file sampling, tokenize only CJK-heavy chunks)
  folds in as the perf fallback; 02 §6's "CJK /tokenize chunking v2.1" row is absorbed.
  Rationale: two chunker versions would mean two fingerprint bumps and two reindexes.
- **C6 — Token guard overlap** (04 §6.2 kind-aware `est_tokens` guard vs 01 §5.5 real
  `/tokenize` counting — both answer the measured 2670-token 400 failure). **Resolve: both
  ship in v2.0, complementary** — 04's per-input failure isolation (a 400 fails that input
  vectorless-with-detail, never the file) is independent and stays; once
  `EmbeddingBackend.count_tokens` exists (01), the kind-aware estimate becomes its fallback
  path. Counting = 01; server flags + isolation = 04.
- **C7 — `/tokenize` shared client** (01 §5.5 chunking and 06 §4.6b `--pack` budget both
  call the endpoint). **Resolve: one client util** — `EmbeddingBackend.count_tokens`
  (llama backend HTTP, ABC method with `est_tokens` fallback, 01 §5.5); 06's `--pack`
  `mode=tokenize` consumes it; chars/4 stays the offline estimator for both, mode printed
  in the pack header.
- **C8 — Model manifest duplication** (02 §5.1 `goldmine/models_manifest.py` vs 05 §4.3
  `goldmine/model_manifest.py`). **Resolve: one module — 05 §4.3's
  `goldmine/model_manifest.py`** with roles `embedding|mmproj|asr|vad|ocr|server`; 02
  supplies the ASR/VAD pins (already marked [confirmed-02]; VAD bytes corrected to 885,098,
  B7); binaries install into `<home>/bin` and `<home>/models`.
- **C9 — "Never auto-starts servers" (06 §4.7) vs auto-start daemon (05 §4.4 decided ON)**.
  **Resolve: refine the blanket rule.** The one decided exception is the embedding daemon:
  search auto-starts `goldmine serve` (consented at init; opt-outs `--no-serve` /
  `serve.auto_start=false`). The `--llm` chat GGUF is never started, fetched, or managed by
  goldmine — doctor only reports `distill.llm.base_url` reachability. **Amended 2026-10-16
  (C2): moot in v2.0** — the daemon is v2.1, so no auto-start and no init consent step ship
  in v2.0; re-ratify the ON-with-opt-outs decision at C4 acceptance.
- **C10 — Date-filter semantics** (01 §5.7 `coalesce(mtime,0)`; 02 §5.4 contract
  `COALESCE(info.taken, mtime)`; 05 §4.1c ships `files.content_date` NULL-safe; 06 §4.5
  keeps distill `--after` mtime-only). **Resolve:** the migration ships `content_date`;
  search `after:`/`before:` implement `COALESCE(content_date, mtime)` from day one
  (behavior equals 01's SQL until extractors populate it; URLs keep `fetched_at`).
  Population order: EXIF via sips in v2.0; PDF/transcript dates [owner 02, mid-v2.0 —
  half of 05 §7 q2]. Distill `--after` stays mtime-only in v2.0 (view scope must not
  change meaning under incremental runs); revisit v2.1.
- **C11 — Two relevance harnesses** (01 §5.1 `eval_real.py` ~120 real-corpus queries vs
  02 §9 coverage fixture extending `eval_relevance.py` with 50 media queries). **Resolve:
  both exist, non-overlapping mandates.** `eval_real.py` = the accuracy/regression gate
  (hash-pinned real corpus, llama backend, exit-1 gate); the coverage fixture queries =
  per-kind KPI non-regression via `status --json`. Migrating media tags into `eval_real`
  when ASR/OCR labels mature is a v2.1 decision [owner 01] (A4 adds OCR/ASR +
  multilingual slices then); not merged in v2.0 to keep the gate's corpus stable.
- **C12 — `serve` slices across 03/04/05** (04 item 4 = 05 item 8 = 03 items 6/7 partial).
  **Resolve: one roadmap item (C4 below), three owners by slice** — substrate/protocol 05
  §4.4, perf budgets + bench gates 04 §6.3/§7, UX surface (status rendering, init consent,
  security note) 03 §5.4; launchd install + status polish are the v2.1 follow-on (05 item
  15). **Amended 2026-10-16 (C1): PyPI publish pulls INTO v2.0** (A10). Same dedup for
  shared embedder (04 item 7 = 05 item 13), pack per-modality ranges (04 item 9 = 05 item
  18), `--estimate` copy (04 item 8 = 03 item 11).

---

## 4. Review amendments (2026-10-16)

Three reviews were folded into this revision. Verdict counts: **ChatGPT (external)** — 6
verdicts (A1–A6), all accepted (A5's pyobjc point amended, below). **Reviewer 1
(technical)** — 4 P1 + 8 P2 + 1 P3 (7 miscites) = 13 findings (B1–B14), all accepted.
**Reviewer 2 (product)** — 2 P0 + 5 P1 + 3 P2 = 10 findings (C1–C10), all accepted except
one amended call (registry, below). Accepted findings, one line each:
- **A1** v2.0 scope cut: defer distill redesign, registry, overlap, opener extras, TUI polish; promote packaging/installability (→ §5).
- **A2** Refuse only on incompatible embed identity, never stale chunker/extractor; pin the active model per-index; document model-switch downtime (→ §6).
- **A3** Daemon index generation must be transactional — (fingerprint,count,max-id) unsafe; memory guard pressure-aware (→ B3/B4, C4).
- **A4** Rename R@5 → success@5 semantics; −0.02 rule needs counts/uncertainty; acceptance gains OCR/ASR + multilingual queries; holdout untouched (→ §1.2, 01 §1/§5.1).
- **A5** OCR ships prebuilt binaries (compile = dev fallback); pyobjc evaluation note; CRITICAL: the OCR-text-FTS-only decision assumed an accompanying image vector — under llama there is none, so evaluate embedding OCR text there or explicitly promise keyword-only (→ 02 §5.2, Q21).
- **A6** Add gaps: CJK lexical retrieval, source lifecycle contract, observed-usability testing (→ §7 risks 13–15, §9 Q20–23).
- **B1** whisper-cli emits no segment-level `no_speech_prob`/`avg_logprob` → guard re-specified on per-segment mean token p + < 20 chars/min yield gate; optional pinned build with segment probs (→ 02 §5.1).
- **B2** Backfill seam: `embed_id` ≠ `fingerprint_id`; binding rule = parse legacy `PROMPT_SCHEME` `d='…'` as `prompts_doc`; fixture: migrate v1 index → zero stale (→ 05 §4.1).
- **B3** vm_stat free-pages is meaningless on macOS (idle-free ≈ 1.5 GB) → memory-pressure % + hysteresis, never exit on raw free pages (→ 04 §6.3).
- **B4** `segments.id` has no AUTOINCREMENT; rowid reuse defeats (fingerprint,count,max-id) → `meta.pack_epoch` bumped in the `replace_segments` transaction, carried in the pack header (→ 04 §6.1).
- **B5** 26 ms@500k not reproducible (44.6–47 ms; block-insensitive 4096–16384; whole-astype 93.6 ms) — targets hold, numbers republished (→ 04 §3.4).
- **B6** hnswlib wheel NOT broken (recall@5 ≈ 0.8 on sane data; collapse = random-data geometry + ef) — kill stands on exact-scan speed (→ 04 §3.6).
- **B7** VAD manifest bytes 885,098 (not 885,000; sha was right) + manifest-vs-file test (→ 05 §4.3).
- **B8** ≥ 100 files/min mis-attributed to overlap (extraction < 1 % of wall); real lever = token chunking; restated "≥ measured v1 rate, no regression" (→ §1.2 T7, 04 §6.2).
- **B9** Otsu t\* can land on the mode inside the clamp → mode-proximity percentile fallback; canary asserts `tau_source != otsu` (→ 06 §4.1).
- **B10** τ calibrated on pairwise but applied to cos-to-centroid (~1.1–1.6× higher) → scale stated, offset documented, canary absorbs; centroid calibration a v2.1 option (→ 06 §4.1).
- **B11** 01 §5.5's exit-3-on-chunker text dead under the refusal model; 05 §5's "v1.2 binary" sentence meaningless — both rewritten (→ 01 §5.5, 05 §6).
- **B12** Small-n tag deltas are noise → advisory below n ≥ 20; 0.85/0.90 relabeled published targets; "gate" reserved for the −0.02 rule (→ §1.2, 01 §5.1).
- **B13** Swift OCR fails on a CLT-less Mac with no fix hint → prebuilt release asset + doctor CLT row (→ 02 §5.2).
- **B14** Seven file:line miscites corrected (base.py docstring 3-7/17-18; llama.py 62-63, 103-129, 377-397; store.py user_version stamp 200-203; search.py loop 155-160; `is_project_root` has a test caller) (→ 02/04/05).
- **C1** U1 depends on publish truth → PyPI publish + GHA llama-server release asset + installed-copy vendor-path fix promoted INTO v2.0 (→ §5 item 4).
- **C2** v2.0 shrunk to the 21-item table below; daemon/TUI/pack/planner/registry/A4/A8/E1+DDL/C5/GHA-nightly → v2.1; cards migration additive-only; killed: find TUI, v2.0 secrets scan, U2-as-v2.0-metric (→ §5, §8).
- **C3** Falsifiable v2.0 accuracy statement added; "measured, kept" is success (→ §1.1).
- **C4** v2.0 interactive layer = excellent `search` output + `--format fzf` + shell widget pulled forward; REPL noted; curses deferred (→ §5 items 15/19).
- **C5** init surfaces media costs: per-root media counts, audio hours in the ETA, ASR/OCR model-install offer / `init --media` (→ §5 item 20, 03 §5.1).
- **C6** E1's cards-v2 DDL defers with E1 — v1→v2 purely additive (→ §6).
- **C7** Gate G0: measure the owner's real corpus before any pack/daemon work (→ §5).
- **C8** A9 backup stays in v2.0 (ASR/OCR transcripts are irreplaceable derived data); Time-Machine posture noted (→ §5 item 3).
- **C9** Opener: plain open + QuickTime/IINA jump + `$EDITOR` stay v2.0 (DigUp-proven, pays off ASR locs); 23-editor table + URL schemes defer (→ 03 §5.3).
- **C10** Journey specs: stale-since visibility, mid-ingest search behavior, 100 GB-root guard, headless/SSH story, whisper working-set scheduling, thermal notes (→ §5 preamble).

Amended reviewer points (decided here, not silently):
1. **Amended (Reviewer 2):** "kill the registry outright" → **deferred to v2.1 with a
   re-entry gate**: A7 ships when a second real index exists on this machine — killing the
   design outright would orphan G8 (cross-index search) and H9 (multi-index UX).
2. **Amended (ChatGPT A5):** "evaluate pyobjc" → an evaluation note only (02 §5.2): the
   numpy-only stance is unchanged; the fix for the CLT-less Mac is the prebuilt binary.
3. **Amended (Reviewer 2):** "kill the curses find TUI" → killed from the roadmap and
   moved to the kill list **with the design kept as design-of-record** (03 §5.2) plus a
   named-user re-entry condition, rather than deleting the knowledge.

---

## 5. Phased roadmap

Sizes: S ≤ 1 day, M ≤ 3 days, L > 3 days (one engineer). IDs are stable for
cross-referencing (`·` marks items merged into one row); effort/owner/deps come from the
domain docs' own tables. Every phase is independently shippable.

**Gate G0 (review C7): before ANY pack (C3) or daemon (C4) work, measure the owner's real
corpus (`goldmine status` on the live index) and record the vector count.** If it is under
~250k vectors, the pack is a growth contingency per 04 §5's own decision tree — it ships
only when a real corpus crosses the threshold; the same measurement calibrates which v2.1
perf items matter at all.

**Journey specs (review C10, folded into the items below):** staleness visible without a
watcher (`status` "stale since" row + re-run hint — item 2); mid-ingest search defined
(lexical works during ingest; semantic reflects committed files); a 100 GB-class root
triggers a confirm/estimate guard at init and ingest; headless/SSH gets an honest story
(`search`/`--json` are the interface — nothing assumes a TTY now that `find` is killed);
whisper's ~1.6 GB working set yields on the same pressure signal as 04 §6.3's guard;
multi-hour passes print a thermal note from `pmset -g therm` (02 §5.6's probes, no watcher).

### Phase v2.0 — "install, migrate, cover, measure, search well" (21 items)

Standalone value: `pip install goldmine` works (PyPI + prebuilt llama-server/OCR release
assets); a v1 index migrates purely additively with zero re-embedding; the index hears
speech, reads screenshots and scans, decodes any image, opens .eml/.epub; real-corpus
accuracy is measured and published behind a regression gate; search output is excellent
(snippets, `--why`, cards) with `--format fzf` as the interactive layer; a stranger reaches
a result in ≤ 4 commands.

| # | ID | Item | Effort | Owner | Depends on |
|---|---|---|---|---|---|
| 1 | A1·A2 | `migrations.py` ladder (fixture-tested, pre-migration snapshot) + v1→v2 migration — **purely additive**: per-file identity backfill per the B2 binding rule (zero stale vs fresh-index ids) + NULL-safe `content_date`; **NO cards DDL** (defers with E1); fixture: migrate v1 → zero stale | M | 05 | — |
| 2 | A3 | Per-file identity plumbing at commit (`chunker_version`, `embed_id`) + `EXTRACTOR_VERSIONS` registry + `status` "stale since" row + re-run hint (C10) | S | 05 | 1 |
| 3 | A9 | `goldmine backup` (VACUUM INTO) + `doctor --deep` — ASR/OCR transcripts are irreplaceable derived data (C8); Time-Machine posture documented (`~/Library` is TM-covered) | S | 05 | — |
| 4 | A10 | **Install truth (C1): PyPI publish** + installed-copy vendor-path fix (llama.py:56-57) + `version --full` + minimal GHA job building the pinned llama.cpp into a release tar | M | 05 | — |
| 5 | B1·B2 | Model manager: manifest, `model install/list/verify/use/adopt` (4-state UI copy), resumable downloads, `<home>` layout + `model check` live arch probe + doctor row + ingest first-fingerprint probe | M | 05/02 | 4 |
| 6 | B3 | ASR: whisper models + VAD pipeline + merge/loc + backend decision matrix — guard on per-segment mean token p + yield gate (B1) | L | 02 | 5, 10 |
| 7 | B4 | OCR: **prebuilt** goldmine-ocr release asset (compile = dev fallback; B13/A5) + screenshot segments + scanned-PDF thin upgrade + `--refresh-thin` + extractor-version skip fix (G10 — the v2.0 re-do path) + OCR-vector-under-llama A/B eval (A5) | L | 02 | 5, 10 |
| 8 | B5 | Image pipeline: sips thumbnails (HEIC/RAW) + minSide refusal + EXIF `info.taken` + fingerprint bump | M | 02 | 10 |
| 9 | B6·B7 | `.eml` + `.epub` extraction + archive honesty rows + rules quick-fixes (CODE_FILE_NAMES latent ImportError, minImageSide, bundle skip) | S | 02 | — |
| 10 | B8 | Coverage fixture + KPI harness (`status --json` per-kind) + 50 media eval queries — must exist before items 6–8 | M | 02 | — |
| 11 | C1·C2·C6s | Measured perf fixes: vectorized per-file max + `argpartition`, server flags `-np 2`, kind-aware token guard + per-input 400 isolation (falsifiable claim 2) + `scripts/bench.py` **local two-gate sliver** (in-process scan+fusion p50, embed rate) | M | 04 | — |
| 12 | D1 | Real eval harness: corpus manifest + builder + ~120 hand labels + `eval_real.py` (success@5/nDCG/grades/tags/fold audit) | L | 01 | — |
| 13 | D2 | Regression gate + `baseline.json` — the −0.02 rule **with counts/uncertainty** (B12/A4); the only accuracy gate | S | 01 | 12 |
| 14 | D3 | Token-true chunking via `/tokenize` + CJK re-split + ctx-guard split + `chunk.v2-tok` — the one v2.0 chunker bump; stale set re-done by plain ingest, no exit-3 (B11) | M | 01 | 2, 13 |
| 15 | D4·D5·D7 | Search output excellence: snippets + «highlighting», MMR + near-dup collapse + weaker-matches fold, `--why` | M | 01 | 13 |
| 16 | D6 | Filters `kind:/under:/after:/before:/site:` + quoted phrases (C10 date coalesce) | M | 01 | 13, 1 |
| 17 | F1 | Universal 4-line error shape + retrofit table + `--json` error keys — do first; every later command inherits it | M | 03 | — |
| 18 | F2′ | Opener sliver: `goldmine open` — plain open + QuickTime/IINA osascript jump + `$EDITOR` (C9; editor table + URL schemes → v2.1) | S | 03 | — |
| 19 | F3 | Output polish: ingest ticker/ETA **with per-root media counts + audio hours** (C5), result-card renderer, NO_COLOR, `--format fzf` + shell widget (pulled forward, C4) | M | 03 | 17 |
| 20 | F4 | `goldmine init` state machine (resume, `--yes`, demo step, TCC pre-warning, **media-model install offer / `--media`** (C5), 100 GB-root guard (C10); NO serve-consent step — daemon deferred) | M | 03 | 5, 19 |
| 21 | F7·F8 | README rewrite (3-command quickstart) + `goldmine help` topics + TCC unreadable-root detection in walk | S | 03 | 20 |

Sequencing: 17 (F1) first among UX items; 10 (B8) before 6–8; 12→13 gate everything in 01;
observed-usability sessions (A6, Q23) run on the release candidate before ship.

### Phase v2.1 — "tune, plan, warm, distill" (absorbs the 2026-10-16 deferrals)

Standalone value: measured-tuned fusion; the planner makes every future improvement a
scoped re-do; one resident warm model serves search *and* ingest; the pack lands only if
G0's measurement says the corpus needs it; distill v2 rebuilds cards (the one announced
destructive migration); `goldmine watch` keeps the index fresh.

| ID | Item | Effort | Depends on |
|---|---|---|---|
| C4 | `goldmine serve` daemon: UDS line-JSON protocol (+ optional `id`, C3), flock, leases, linger 300 s, **pressure-aware memory guard** (B3); re-ratify auto-start-on-search at acceptance | L | C3, G0 |
| C3 | Pack file `<index>.vecs` + mmap blockwise scan + pack_epoch header (B4) — **only if G0's measured corpus justifies it** (04 §5 tree: < 250k vectors = growth contingency) | M | G0 |
| F5 | `serve status` rendering + doctor daemon row + security note (face of C4) | S | C4 |
| F6 | `goldmine find` curses TUI — **re-entry gated**: build only when a named user commits to it (C2); REPL alternative noted; design of record 03 §5.2 | L | C4 |
| A4 | Embed identity rename `prompts_doc`/`prompts_query` split (C2) — with its consumers G3/G4; the planner paces its reindex | S | A5·A6 |
| A5·A6 | `goldmine reindex plan\|run`: stale reasons, self-calibrating estimates, plan id; **fixture: migrated v1 index reports ZERO stale** (B2) | M | A3 |
| A7 | `registry.json` + `status --all` + doctor checks — deferred-with-reentry: ships when a second real index exists | S | — |
| A8 | `config_version` + env audit (secrets scan killed, C2) | S | — |
| C5 | Ingest overlap pipeline (depth-2 queue, cross-file batching, sha256 off serial path) — ≤ 1 % on text corpora (B8); justified by OCR/media kinds | M | — |
| C6b | GHA nightly bench (absolute gates) on top of the v2.0 local sliver | S | C6s |
| E1 | Distill v2 core: Otsu τ calibration (mode-proximity fallback B9, scale statement B10) + sampled gram; average-link admission guard; `--preview` histogram; terms fixes; medoid + coverage excerpts; scoping; markdown TOC + audio time fmt; `--pack` (estimator budget); **cards-v2 DDL + the announced cards-rebuild migration** (C6 — the first destructive step, loudly announced); `eval_distill.py` canary — fully offline | M | A1·A2 |
| G1 | Fusion tuning runs → new defaults (frozen-split coordinate search, 2-scale z_gate check, overfit guards) | M | D1, D2 |
| G2 | `setsScale` long-file z-scale exclusion (accept on `pdf-page` tag) | S | D1 |
| G3 | Query rules: `code:` prefix, identifier→FTS-phrase, routing plumbing | S | D2 |
| G4 | Dual-prompt query experiment (search+code prompts, per-segment max; gate-inflation guard) | S | A4, G3 |
| G5 | Repo-relative code titles (planner-paced reindex) | M | D3, A5·A6 |
| G6 | Shared embedder: ingest embeds via daemon leases (= 04 item 7 = 05 item 13) | M | C4 |
| G7 | `ingest --estimate` self-calibrating cost model (+03 personal ETA copy; = 04 item 8 = 03 item 11) | M | C6s |
| G8 | Cross-index `--index a,b` RRF rank fusion + shared-model guard (never merged z) | M | A7 |
| G9 | `serve install/uninstall` (opt-in launchd, KeepAlive=false) + status polish | S | C4 |
| G10 | Multi-model LRU resident (`serve.models_resident=2`, idle 600 s) + `model use` UX | M | C4 |
| G11 | `goldmine watch`: adaptive polling + sha-skip reuse + pmset power probes + linger + heartbeat (C4 alignment) | M | A3 |
| G12 | Video frames: ffmpeg one-pass split + aHash dedup (Hamming ≤ 4, maxFrames 1200), foresight-gated | M/L | B5 |
| G13 | HTML main-content lite (link-density/class filter, 40 % yield fallback) | M | B8 |
| G14 | Rules parity batch: vendored/build folders, `.git`-as-file skip, content-level secret/generated/minified gates, ipynb cells, `is_project_root` disposition | M | — |
| G15 | Incremental distill (centroid reuse, dissolve, corpus_hash, `--refresh`) + two-level topics + backend-scored titles + `--llm` polish (provenance-hard) + `/tokenize` pack budget (C7) | M | E1, C7 |
| G16 | Integrations: `--alfred`, VS Code snippet + `help integrations` (`--format fzf` + widget moved to v2.0) | S | F2′ |

### Phase v2.2 — "conditional, experimental, scale"

Standalone value: only what the evals justify — a prototype router and opt-in rerank if
they beat the gate, OCR for photos and video slides, 1M+-vector partitioning, ANN only on
a measured p95 breach, watch hardening after real use.

| ID | Item | Effort | Depends on |
|---|---|---|---|
| H1 | Prototype centroid query router (only if eval shows a real confusion class) | M | G1, G3 |
| H2 | `--rerank` opt-in re-embed rerank (cut if ≤ +0.02 nDCG for +2 s) | M | D2, G1 |
| H3 | Multimodal z-calibration re-run (needs 02's image/audio vectors) | M | B3, B5 |
| H4 | `--ocr-images` (photos/whiteboards; needs its own false-positive eval) | S | B4 |
| H5 | OCR-of-video-frames (slide text) | M | B4, G12 |
| H6 | FSEvents optional extra for watch latency (only if polling disappoints) | S | G11 |
| H7 | tesseract fallback (non-macOS packaging track) | S | B4 |
| H8 | Watch UX surfaces in status (find is dead; TUI re-entry would revisit) | S | G11 |
| H9 | Multi-index UX pass (init `--index`, `status --all` rendering, registry errors as §5.7 shape) | M | A7 |
| H10 | Pack per-modality row ranges + `--kinds` region skip + registry bulk ops (= 04 item 9 = 05 item 18) | M | C3 |
| H11 | ANN evaluation gate — only on measured p95 breach at a real corpus; wheel re-validation + recall@5 = 1.00 vs exact required (B6) | L | C6b |
| H12 | Thermal/LPM backfill gate + `operations` table + watch/serve flock arbitration (= 04 item 11 = 05 item 17; C4 alignment) | M/L | G11 |
| H13 | ASR distill refinements (citation collapse, title bias, filler stoplist) + pack polish + views ergonomics + order-robustness eval variant | S | G11, G15 |

**Counts: v2.0 = 21 rows (29 work items after the review's merges) · v2.1 = 27 · v2.2 = 13.**
Critical path: A1·A2 → A3 identity spine; D1 → D2 gates all of 01; 10 (B8) before the media
items; F1 first among UX items; **G0 before any C3/C4 work**; the one full reindex (D3) is
announced by `status` and absorbed by plain ingest runs — not big-bang.

---

## 6. Migration & compatibility (v1 → v2)

Per 05 §4.1/§4.2/§5, amended 2026-10-16: a fixture-tested migration ladder lands before
schema 2 exists; the v1→v2 migration is **purely additive** (C6) — it backfills per-file
identity columns (`chunker_version='chunk.v1'`; `embed_id` recomputed per the B2 binding
rule: `prompts_doc` parsed from the legacy scheme's `d='…'` part, `preprocessing`
excluded — so migrated ids equal fresh-index ids and the planner reports zero stale) plus
NULL-safe `content_date`. **Nothing is re-embedded, nothing is destroyed**: cards keep
their v1 tables and v1 distill keeps working; the cards-v2 DDL + rebuild arrive with E1
in v2.1 as a separately announced migration.

**Refusal boundary (A2, tightened):** semantic search/distill refuse (exit 3) ONLY on an
incompatible *embed identity* — i.e. while vectors from a model/prompt-set switch are
mid-sweep in the queryable set. A stale chunker or extractor version NEVER refuses: those
vectors stay single-identity and comparable; `status` shows "N files stale since `<date>`
— re-run ingest" and ordinary ingest re-does them (02 G10's skip fix is the v2.0 re-do
path; the planner is v2.1). The active model is pinned per-index (`meta.model_binding`,
05 §4.3); switching models = a full re-embed whose downtime (semantic refused with
count/ETA, lexical fine) is documented, not discovered.

**What a v1 user experiences on upgrade:** open → automatic one-transaction additive
migration (with a `<index>.pre-v2.sqlite` snapshot if > 1 MB) → search works immediately,
lexical and semantic, nothing re-embedded → after D3 lands, `status` reports the
chunker-stale set with an ETA; the user re-runs ingest at their leisure (or `reindex run`
from v2.1). Cards are untouched until the v2.1 distill migration announces their rebuild.
(A v1 binary on a v2 index refuses at open — "upgrade goldmine"; `--db`/`--index` and
exit codes are unchanged.)---

## 7. Risks registry (cross-doc)

| # | Risk | Owner | Mitigation |
|---|---|---|---|
| 1 | Eval label circularity + ~120 queries thin for 6 knobs (overfit) | 01 | frozen held-out split before tuning; 5-fold guards; "measured, kept" is a valid outcome; re-label pass ~6 months in; per-tag n printed (B12) |
| 2 | z_gate needs a size term at 100k+; dual-prompt max inflates the gate | 01 | planted-segment 2k→100k scale check; fall back to max of *routed* prompt only |
| 3 | `/tokenize` per-request cost unmeasured (route confirmed, cost not) | 01 | S probe before committing; fallback per-file sampling / CJK-only tokenization (02 §7) |
| 4 | Otsu τ degeneracy: t\* on the mode inside the clamp; floor 0.55 unvalidated; pairwise-vs-centroid scale offset | 06 | mode-proximity percentile fallback + `tau_source` reporting (B9); offset documented, canary absorbs (B10); canary must include single-topic and 20-chunk degenerate cases asserting `tau_source != otsu` |
| 5 | goldmine-ocr binary lifecycle: macOS majors can stale the helper; human must run doctor | 02 | prebuilt release asset per release (A5/B13) + doctor handshake (`goldmine-ocr 1`) treats stale binary as "OCR off" + fix hint; CLT row covers the source-build path |
| 6 | ASR WER measured on `say`-synthesized speech; hallucination on music despite VAD | 02 | real 1 h-podcast acceptance before v2.0 ships; per-segment mean token-p filter + < 20 chars/min yield gate (B1 — whisper-cli has no segment probs; pin a build that serializes them if found first) |
| 7 | Daemon complexity vs one-shot simplicity: stale sockets, orphaned llama-server, version skew, guard false positives | 05 | crash-only statelessness; flock + pid receipts; child dies on pipe EOF + watchdog; proto_version handshake → cold fallback, never fail; pressure-aware guard, never raw free pages (B3); `--no-serve` opt-out keeps the v1 path intact |
| 8 | Pack file desync vs SQLite blobs (two representations of vectors) | 04 | blobs stay source of truth; header carries `pack_epoch` from the same transaction as every `replace_segments` (rowids reusable — count/max-id secondary, B4); mismatch = rebuild (~2 s @500k), never trust |
| 9 | AppleScript/TCC permission UX lands at a confusing moment (first find-Enter on video; unreadable roots walk as empty) | 03 | init/folders pre-warning; refusal → plain open + one printed line; walk records `unreadable_roots` (F8) |
| 10 | Measured-fact fragility: perf numbers from one machine; several first-draft numbers corrected by review (B5/B6/B7) | 04/02 | corrected numbers republished (04 §3.4/§3.6, 05 §4.3); bench tolerances (22 %) + machine-keyed baselines; manifest-vs-file test; rotated-EXIF fixture is B5's first task |
| 11 | Two identities in flight during a model-switch sweep (user searches mid-sweep) | 05 | refusal gate names the count/ETA (A2); lexical unaffected; per-file atomic commits make Ctrl-C resumable |
| 12 | Incremental distill label drift (frozen memberships never self-correct); planner estimates drift | 06/05 | `--refresh` one flag away; `distill_runs` history makes drift diffable; rates self-calibrate from `meta.rates_json`, plan-vs-actual printed |
| 13 | CJK lexical retrieval gap (A6): unicode61 does not segment Chinese — embedding leg carries CJK semantics, lexical leg degrades | 01/02 | decision pending (Q20): bigram segmentation, trigram index, or documented limitation; honesty first — docs stop claiming "CJK queries work" |
| 14 | Source lifecycle contract missing (A6): deletion/rename/exclusion semantics + stale locations on open | 05/03 | spec'd as Q22 before v2.1; `status` staleness surfaces (C10) are the v2.0 down payment |
| 15 | All UX claims are design-stage — never observed with real first-time users | 03 | observed-usability sessions on clean Macs before v2.0 ships (A6, Q23); failures fold into F1/F4/F7 |

---

## 8. What we will NOT do (union of all kill lists)

| Rejected | Why | Source |
|---|---|---|
| Learned rankers / LTR beyond coordinate search | ~120 labels can't train a ranker; hand-tuned 3-param fusion beat RRF on DigUp's real eval | 01 §5.9 |
| Cross-encoder / second-model reranking | second model to pin/fingerprint/keep resident; strictly slower than the 1.6–3.1 s embedding floor | 01 §5.9 |
| LLM query rewriting/expansion | needs a chat model in the loop; nondeterministic; hides what was searched | 01 §5.9 |
| ANN/HNSW below ~2M vectors; sqlite-vec | exact scan is ≈ 45 ms @500k; the 2026-10-16 re-check found the hnswlib 0.8.0 wheel fine (earlier recall collapse = random-data geometry + ef, B6) — rejection is on speed, re-entry only via H11's measured gate; sqlite-vec unusable (pyenv 3.12 sqlite lacks loadable-extension support) | 01, 04 §3.5/§3.6 |
| ColBERT multi-vector / late interaction | ~100× vector storage for file-level granularity | 01 §5.9 |
| Fine-tuning / adapters for EmbeddingGemma | pinned GGUF; outside llama-server's embedding contract | 01 §5.9 |
| Per-user click-feedback learning; synonym/WordNet tables; chunk-level results as primary unit | single-user CLI; breaks honest coverage semantics; changes the product, not accuracy | 01 §5.9 |
| GUI app, menu bar item; editor/browser extensions; mouse support in a TUI | CLI identity; `search` + `--format fzf` are the interactive layer; `serve status` is the state surface; thin integrations cover entry points; curses mouse is terminal-fragile | 03 §5.9 |
| Telemetry / crash reporting; accounts / sync; auto-update (self or model) | local-first; index portability is a file copy; model changes stay a human command behind the fingerprint gate | 03 §5.9 |
| Man page | pip/uv installs lack mandir wiring; `--help` + `goldmine help` instead | 03 §5.8 |
| **curses `find` TUI** (killed 2026-10-16) | no named user; REPL + `--format fzf` cover the need; revisit only when a real user commits (design of record kept in 03 §5.2) | review C2 |
| **Secrets-pattern scan in doctor** (killed 2026-10-16) | scope creep; config stays credential-free by design (05 §4.6a) + env audit | review C2 |
| **Destructive v2.0 migration (cards-v2 DDL at upgrade)** | v1→v2 must be purely additive; the cards rebuild is an announced v2.1 migration | review C2/C6 |
| **U2 keystroke latency as a v2.0 gate** | the metric died with the TUI; warm in-process `search` p50 ≤ 300 ms remains | review C2 |
| Postgres/DuckDB/any second storage engine (incl. distill sidecar) | two consistency domains for a bottleneck that doesn't exist | 05 §4.9 |
| Cross-machine TCP serve / remote indexes / public HTTP or OpenAI-compatible API on the daemon | local-first; UDS is deliberately un-extendable to TCP; internal protocol, not a stability promise | 05 §4.9 |
| Per-segment fingerprinting | z-score math makes mixed-vector sets unqueryable; per-file is the finest honest unit | 05 §4.2 |
| Plugin marketplace / third-party backend registry | backend ABC + pip extras is the extension mechanism | 05 §4.9 |
| Multi-process parallel writers; asyncio pipeline rewrite | single-writer is load-bearing; threads+subprocesses measured sufficient | 05 §4.9 |
| FSEvents/pyobjc watcher as the v2 requirement; per-index config files; durable job queue before watch exists | pyobjc not installed; identity belongs in the index; queue machinery needs ≥ 2 producers | 05 §4.9, 02 §5.6 |
| Knowledge-graph/entity extraction; HDBSCAN/UMAP/sklearn; complete-link agglomerative (10 GB matrix) | no consumer for edges; heavy non-deterministic deps; admission guard buys the chain-resistance at O(1) | 06 §4.9 |
| Multi-LLM synthesis; per-sentence embeddings at full scale; locally adaptive per-cluster thresholds | cost without eval gain; backend round-trips; unexplainable + breaks label stability | 06 §4.9 |
| pyobjc Vision bindings (re-checked 2026-10-16: stays out — prebuilt helper is the answer, A5); `shortcuts` CLI; tesseract as macOS default; `.msg` parsing | not installed / can't be provisioned / worse on UI text / OLE parser dep — documented non-macOS or on-demand only | 02 §5.2/§5.5 |
| ASR `--translate`; word-level timestamps (`--dtw`); archive content extraction | destroys fidelity; per-language models for nobody's ask; zip-bomb surface for path-findable contents | 02 §5.1/§5.5 |
| Resident fp32 matrix > 250k vectors; whole-array astype as the scan path; spawn-with-linger ownership handoff | 1.5–3 GB residency measured; memory spike; orphan/adopt bug family | 04 §3.4, §5 |

---

## 9. Open questions (owner + decision deadline)

| Q | Question | Owner | Decide by |
|---|---|---|---|
| 1 | ~~Auto-start daemon default ON~~ / ~~`find` as default entry~~ / ~~fzf vs find positioning~~ **Resolved 2026-10-16**: daemon (C4) is v2.1 — no auto-start, no consent step in v2.0 (re-ratify at C4 acceptance); `find` killed — `search` stays the entry and `--format fzf` (v2.0) is the interactive path | 05 + 03 | done / v2.1 (C4) |
| 2 | Migration fixture: generated at test time + frozen sha, or checked-in binary? (leaning generated) | tests | **v2.0 start** |
| 3 | BackendStartupError exit 2 → 4 SPEC change? (leaning 4, prerequisite-missing) — unchanged by review | 03/SPEC | **v2.0 start** (SPEC pass) |
| 4 | `content_date` extractor priority (EXIF → PDF date → transcript date?) — direction set in C10 | 02 | mid-v2.0 (before D6 ships) |
| 5 | `model use` binding UX: global default + implicit per-index pin, or always explicit? (A2: the active model is pinned per-index regardless — this is about how visible the pin is) | 02 + 05 | mid-v2.0 (before F4) |
| 6 | Daemon serve `distill` in v2.1, or distill stays cold? (leaning cold; moot in v2.0) | 06 + 04 | v2.1 planning |
| 7 | `goldmine open` jump report on stdout (pipeable) or stderr? (leaning stderr) | 03 | mid-v2.0 |
| 8 | Demo-step term sampling: top TF-IDF vs mid-ranked distill card terms | 03 + 06 | mid-v2.0 (cheap to iterate) |
| 9 | Corpus `corpus_version` bump cadence (default: only with label review) | 01 | mid-v2.0 |
| 10 | ~~`find` as documented default entry~~ **Resolved 2026-10-16**: `find` killed; `search` stays the entry; `--format fzf` is the interactive path | 03 | done |
| 11 | Photo OCR auto-promotion ("photo with ≥ 200 OCR chars" → screenshot-like handling?) | 02 | v2.1 planning |
| 12 | ASR FTS granularity: whisper's own ~4 s segments as FTS-only rows too? | 02 + 01 | v2.1 planning (needs eval data) |
| 13 | Frames under llama backend: metadata+timestamps-only frame rows? | 02 | v2.1 planning (G12) |
| 14 | Watch deployment: document `--forever` + user LaunchAgent, or ship a plist? | 02 + 03 | mid-v2.1 |
| 15 | `--refresh` scheduled auto-full every Nth incremental run? | 06 | v2.1 planning |
| 16 | ~~fzf widget vs `find` positioning~~ **Resolved 2026-10-16**: the widget ships in v2.0 (F3) | 03 | done (v2.0) |
| 17 | `.msg` via optional `olefile` dependency? (hold stdlib line until asked) | 02 | on user demand |
| 18 | `--min-files` for transcript episodes? | 06 | v2.2 (pending transcript fixture) |
| 19 | Per-script chars/token heuristic for non-Latin `--pack` budgets? | 06 | v2.2 |
| 20 | CJK lexical retrieval (A6): bigram segmentation vs trigram index vs documented limitation — the embedding leg must not silently carry the blame | 01 + 02 | v2.1 planning |
| 21 | OCR text under llama (A5): keyword-only FTS vs OCR-text vector — A/B on the coverage fixture; blocks B4 | 01 + 02 | mid-v2.0 |
| 22 | Source lifecycle contract (A6): deletion/rename/exclusion semantics; stale locations on open — spec + status surfaces | 05 + 03 | v2.1 planning |
| 23 | Observed-usability protocol (A6): first-run sessions on clean Macs before v2.0 ships; findings fold into F1/F4/F7 | 03 | before v2.0 release |

---

## 10. Maintenance of this document

REDESIGN.md is the coordination layer: when a domain doc changes a phase, an interface, or a
decision listed in §3, update the corresponding row here in the same commit, and back-port
the resolution into the owning domain doc so the docs converge instead of forking.
Review-driven amendments are recorded in §4 and marked inline (`amended 2026-10-16`) in the
domain docs they touched.
