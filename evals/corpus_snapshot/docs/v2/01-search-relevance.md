# goldmine v2 — Search & Relevance Design (01)

> Mission: **"search anything, local, ACCURATE."** v1's fusion machinery is sound and
> DigUp-proven, but every default in it is inherited unvalidated, and the only eval is a
> 35-doc synthetic corpus. v2 makes results genuinely accurate on *real* corpora: a durable
> hand-labeled real-corpus eval with regression gates, tuned fusion, query routing, real
> snippets, filters, and token-true chunking.
>
> Status: design. Companion docs: `docs/v2/02-ingestion-extraction.md` (extraction/OCR),
> `docs/v2/04-performance-scale.md` (measurements — all latency numbers below come from
> there, marked **[measured]**), `docs/v2/06-distill-knowledge.md`.
> Every v1/DigUp claim in this doc is backed by a `file:line` actually read (2026-10-16).

---

## 1. Mission & target metrics

The product owner's v2 goal is accuracy on real data, not new modalities (02 owns those) and
not scale (04 owns that). Concretely, v2 search must:

1. **Prove** accuracy on a real, hand-labeled corpus instead of asserting it on synthetic
   docs (today: `scripts/eval_relevance.py:2-14` — mock backend, 35 generated docs, 12 queries).
2. **Tune** the fusion weights v1 inherited from DigUp `(1.0, 2.0, zGate 2.0)`
   (`goldmine/search.py:57-63`) on *our* model + *our* corpus, with overfitting guards.
3. **Understand** the query just enough: lookup vs semantic vs code, routed to weights and
   prompt variants — without an LLM in the loop.
4. **Show** the user *why* a result matched (real highlighted snippets, per-component scores).
5. **Stop losing** accuracy to chunking that counts characters instead of tokens (code chunks
   blow the context window: one 2670-token stretch vs the 2048 ceiling → HTTP 400 → file
   failed; 04 §3.3, lines 113–116).

### Target metrics (v2 acceptance, real corpus `evals/real-*`, llama backend)

| Metric | v1 today | v2.0 gate | v2.2 target |
|---|---|---|---|
| hybrid success@5 (all queries) | unmeasured on real data | **≥ 0.85** (published target) | ≥ 0.90 (target) |
| hybrid success@1 | unmeasured | ≥ 0.55 (target) | ≥ 0.70 (target) |
| MRR | unmeasured | ≥ 0.65 (target) | ≥ 0.75 (target) |
| nDCG@10 (graded) | not computed | reported (baseline set at v2.0) | ≥ baseline + 0.05 over lexical |
| lookup subset success@1 | unmeasured | **≥ 0.90** (published target) | ≥ 0.95 (target) |
| zero-hit queries returning 0 hits | asserted in synthetic harness only | same, on real noise queries | — |
| Regression gate (the one accuracy gate) | none | any tracked metric −0.02 vs `evals/baseline.json` beyond bootstrap uncertainty → eval exits 1; per-tag deltas advisory below n ≥ 20 queries | — |
| Warm query latency @100k segs | p50 222 ms **[measured]** (04 §3.1:73–75) | ≤ 300 ms p50 incl. snippet + MMR | — |
| Opt-in rerank (`--rerank`) | n/a | ≤ 3.5 s total, off by default | — |

<!-- amended 2026-10-16: A4 (ChatGPT) + B12 (Reviewer 1) — R@5 renamed success@5 (it is a
     location-constrained success-rate metric, not set recall); 0.85/0.90-class numbers
     relabeled PUBLISHED TARGETS; "gate" is reserved for the −0.02-vs-baseline rule, which
     requires counts/uncertainty (a −0.02 on a 5-query tag is half a query). Per-tag
     deltas are advisory below n ≥ 20. -->

**success@5** (renamed from R@5, 2026-10-16) counts a query solved when any `expect` entry
is found in the top 5 with its location constraint satisfied (page/line-range semantics
copied from DigUp's eval, `main.swift:480-488`) — a success rate over queries, not set
recall, and named accordingly. The target numbers are *floors to establish*, not claims:
the whole point of §5.1 is that we currently do not know these numbers, and the only hard
accuracy gate is the regression rule against `baseline.json`.

---

## 2. v1 state (facts, with file:line)

### 2.1 Query path (`goldmine/search.py`, wired at `goldmine/cli.py:360-436`)

| Aspect | Where | Behavior |
|---|---|---|
| Terms | `search.py:85-93` | NFC → lower → split `[^0-9a-z]+` → len ≥ 2 → dedupe → drop stopwords (:32-39, DigUp's English subset) |
| Kind words | `search.py:43-55` | "pdf/screenshot/notes/…" → soft +1.0 kind hint (:209-211), never a filter |
| Lexical | `search.py:129-143` | quoted OR patterns, last term prefixed (:118-120); `store.search_fts` bm25 **ascending** LIMIT 1000 (`store.py:530-547`); per-file coverage = separate DISTINCT-file query per pattern (`store.py:549-563`) |
| Semantic | `search.py:145-160` | one query embed via `query_prompt` (`backends/base.py:26-27`, prompt `"task: search result | query: {text}"` defined `base.py:19`); full-matrix `table.matrix @ q` (:153); best (z, cosine, segment) per file |
| z-scores | `search.py:293-309` | per-modality mean centering (groups < 20 → global mean, :303-306), then one global std with 0.02 floor (:307-309) |
| Candidates | `search.py:171-174` | hybrid = {z ≥ 2.0} ∪ {coverage ≥ 0.5} |
| Fusion | `search.py:204-213` | `score = 1.0·max(z,0) + 2.0·coverage^1.5 + 1.0·kind_hint`; lookup inverts to `(0.5, 4.0)` (:180-182) |
| Lookup rule | `search.py:281-291` | regex only: **every** term digit-bearing AND (alphanumeric-mixed OR len ≥ 4) |
| Hit assembly | `search.py:215-254` | file-level; meaning segment when z ≥ gate else keyword segment (:225); excerpt = stored `segments.excerpt` (:233, `store.py:521-526`) |
| Filters | `search.py:322-335` | only `--kinds` (CLI `cli.py:119-120`), as SQL on `files.kind`; failed files always excluded |
| Excerpt render | `cli.py:480-486` | stored excerpt truncated to 240 chars (`util.py:131-137` — first line, hard cut, no query terms marked) |
| JSON | `cli.py:443-464` | rank/score/z/cosine/coverage/kind/path/loc/loc_end/segment/excerpt/file_status/scorer |
| Output honesty | `cli.py:434-435` | zero hits → `nothing found for …`, exit 0 (SPEC.md:199-201) |

### 2.2 Ingest-side facts that bound relevance

| Aspect | Where | Behavior |
|---|---|---|
| Chunking | `chunker.py:33-88` | char-based 1800/200, paragraph boundary preferred past size/2 |
| Code chunks | `chunker.py:100-132` | whole-line stretches of 1800 **chars** — no token awareness |
| Token estimate | `chunker.py:135-137` | `est_tokens = len // 4`, warn-only; never used to cut |
| ctx guard | `chunker.py:140-150` + `pipeline.py:640-647` | `effective_chunk_size` floors at 256 chars, reserves 96, assumes worst case 1 char ≈ 1 token |
| Doc prompt | `pipeline.py:709-710` | `document_prompt(title or file stem, chunk.text)`; webpages get page title or `"page"` (`pipeline.py:951`) |
| Excerpt stored | `pipeline.py:736-746` | `excerpt(chunk.text)` → chunk-start one-liner; this is all the user ever sees |
| Fingerprint | `fingerprints.py:29,44-66` | 8 canonical fields incl. `prompts` (one combined string, `base.py:21-23`) and `preprocessing: "pipeline.v1"`; mismatch ⇒ exit 3 (ARCHITECTURE.md:237-242) |
| Long-file scale | — | **no equivalent of DigUp's `setsScale`**: every vector, incl. the 500th page of a PDF, moves the z-scale |

### 2.3 Eval state

- `scripts/eval_relevance.py`: 35 deterministic synthetic docs (30 + 3 near-dup twins + 2
  cross-topic negatives, :96-154), 12 queries with target-or-sibling credit (:160-173,
  credit at :196-203), metrics R@1 / R@5 / MRR only (:240-257) — **no nDCG, no graded
  relevance, no persisted baseline, no gate**. Runs all four scorers plus a zero-hit
  honesty probe (:206-234). Mock by default; `--backend llama` exists (:284).
- TEST_PLAN §5 (docs/TEST_PLAN.md:99-134) pins this harness as intentionally
  "reported, not asserted"; the only v1 quality gate is TEST_PLAN.md:153-155 (≥ 10/12
  synthetic queries in top 5 with live embeddings).

---

## 3. DigUp reference (what to copy, with file:line)

| Mechanism | Where | What it does / evidence |
|---|---|---|
| Fusion weights | `Searcher.swift:113-114,165-179` | wordWeight 2.0 (code 1.0); lookup (0.5, 2×wordWeight). Tuned on real evals: "18 → 20 found first … 1.5 changed nothing, 0.5 lost the top 5" (:109-112) |
| Fusion vs RRF | `Searcher.swift:166-168` | "This beat reciprocal-rank fusion on both evals, 2026-10-07: R@1 0.88 vs 0.73 synthetic, 0.86 vs 0.82 real" — v1's RRF baseline exists only because of this |
| z-score scale | `Searcher.swift:316-331,333-350` | per-modality **means**, one **global** std (scaling each modality by its own std "buried PDF pages under pictures", 91 → 94 of 105, :306-310); tiny groups < 20 → global mean |
| `setsScale` | `Searcher.swift:38-39,80-86` + :310-315 | later pages/chunks of long files are scored but don't set the scale: "A book's hundreds of pages pulled the text average down … real eval 24 → 22 of 26 first; 24 with the scale kept" |
| `strongCount` fold | `Searcher.swift:278-281` | results below 0.5× top score fold under "weaker matches": 26.2 → 5.7 results/query, "never folded an expected answer" |
| Identifier lookup | `CodeExtractor.swift:252-269` | `identifier()` regex (snake/camel/dotted/digits) → one FTS **phrase** query instead of OR terms; `definition()` (:275-315) finds the defining line; `focus` line returned in hits (`Searcher.swift:191-199,241-263`) |
| Code titles | `CodeExtractor.swift:14-18` | document title = repo-relative path: "the file name alone had 22 vs 24 of 27 in the top 5, MRR 0.69 vs 0.79" |
| Code prompt | `Model.swift:92-95` | `task: code retrieval | query: …`: "24 vs 22 in the top 5, MRR 0.79 vs 0.75" (real-code eval, 27 queries) |
| Snippets | `SearchText.swift:79-105` + `:49-75` | «term»-marked ~12-word window anchored a few words before the **first** match; `matches()` maps normalized-space hits back to original-string ranges (Folded, :112-138); used over FTS5 `snippet()` output because FTS text is normalized (`Searcher.swift:143-152`) |
| `hasWords` | `SearchText.swift:37-43` | ≥ 20 % letters ⇒ text deserves a vector; digit dumps get keyword-only |
| Query routing | `main.swift:336-343` | `code:` prefix → code index + `Prompts.codeQuery`; why-line prints `z %.1f, words #rank` (:368-369) |
| Eval format | `main.swift:435-444` | `{base, queries: [{q, expect[], tag, kind}]}`; `expect` entries `path` or `path@a-b` (page/second/line range); tags partition the report |
| Eval runner | `main.swift:446-518` | R@1/R@5/R@10/MRR per tag + ALL; per-query `#rank|miss` print; location check `at()` (:480-484); tracks whether the fold would hide an expected answer (:467, 490-491) |
| Testbed | `scripts/make-testbed.sh:18-66` | `real/` APFS clones of sampled own files + MANIFEST; `synthetic/` known-content media (ground truth); `traps/` crawler cases; `code/` four small git repos with known functions (fed by `evals/code.json`, 41 queries tagged code/code-name/code-doc) |

---

## 4. Gap analysis

| # | Gap | Evidence | Consequence today |
|---|---|---|---|
| G1 | No real-corpus eval; synthetic-only, mock-first, 12 queries | §2.3 | v2 acceptance ("accurate") is unfalsifiable; the 0.85 R@5 gate below cannot even be baseline-lined |
| G2 | Fusion weights inherited, never tuned on our model/corpus | `search.py:57-63` vs DigUp's tuning notes `Searcher.swift:109-114` | Unknown accuracy headroom; zGate 2.0 unvalidated at 100k+ segment scale |
| G3 | Long-file z-scale poisoning | no `setsScale` (§2.2) | A long PDF depresses the text mean every query — DigUp measured this as −2 of 26 on real data (`Searcher.swift:310-315`) |
| G4 | Lookup detection = one regex, no code routing, no identifier phrase | `search.py:281-291` vs `CodeExtractor.swift:252-269` | `retry_with_backoff` searched as loose OR terms instead of one phrase; no `code:`-style escape hatch; doc/code prompt mismatch unresolved (single index) |
| G5 | No rerank, no diversity | — | near-duplicate files can fill the top 5; nothing reorders the fused head |
| G6 | Char-based chunks; code at 1800 chars ≈ 1200–2700 tokens (0.67 chars/tok, 04 §3.2:97, §3.3:113-116) | `chunker.py:14,135-150` | code chunks truncate or 400-fail; prose chunks ~366 tok are 20 % under target; CJK worst case floors chunk size at 256 chars for everyone |
| G7 | Chunk-start excerpts, no highlighting | `pipeline.py:736-746`, `cli.py:480-486` | user cannot see *why* a file matched; DigUp ships the fix (`SearchText.swift:79-105`) and v1 already stores the raw text to do it (`fts.original`/`body`) |
| G8 | Filters: only `--kinds` flag | `search.py:322-335` | no path/date/site scoping — table-stakes for "search anything" on a real disk |
| G9 | No per-component confidence output | JSON `cli.py:443-464` | z/cov are there but weights, hint, lookup-mode, bm25 rank and segment margin are not exposed |
| G10 | Fingerprint conflates query and document prompts | `base.py:21-23`, `fingerprints.py:53-62` | any future query-prompt change (e.g. code prompt) forces a full reindex although document vectors are unchanged |

---

## 5. v2 design

### 5.1 Real evaluation harness (`evals/`, v2.0, effort L)

**Corpus.** Three real sources, materialized by `scripts/build_eval_corpus.py` into a
gitignored work dir; the *manifest and labels are committed, the bytes are not* (third-party
reference sources must never be committed into goldmine):

1. **Code + docs**: `~/git/DigUp` sources and `~/git/goldmine` itself (118 files / 953
   segments for the two combined — 04 §3.3:108).
2. **Fixture PDFs**: 6–10 deterministic generated PDFs (DigUp `make_docs.swift` pattern,
   `make-testbed.sh:27`) committed under `evals/corpus-pdfs/` — multi-page, known sentences
   per page, one with a rare lookup token per page. Plus 2–3 real local PDFs *if* the
   maintainer donates them (path recorded in the manifest, hash-pinned, optional).
3. **Webpages**: 3–5 pages from an ingested fixture site (existing crawl fixtures suffice).

**Manifest** (`evals/manifest.toml`): for every source — path, sha256, size, kind, in_scope
flag. The builder verifies hashes; on drift it fails with the differing files and a
`--bump-corpus-version` escape that forces a conscious labels review. `corpus_version`
stamps every result row, so a corpus change invalidates old baselines loudly instead of
silently comparing across corpora.

**Labels** (`evals/real.jsonl`, one JSON object per line — DigUp's shape
(`main.swift:435-444`) extended with grades):

```json
{"id": "real-017", "q": "how does the indexer decide a page sets the z scale",
 "expect": [{"path": "Sources/DigUpKit/Searcher.swift", "lines": [80, 86], "grade": 3},
            {"path": "Sources/DigUpKit/Searcher.swift", "lines": [310, 315], "grade": 2}],
 "tag": "code-behavior"}
```

Graded relevance 0–3: 3 = exact target (file + location when given), 2 = same file other
location / strong partial answer, 1 = topical sibling, 0 = irrelevant. **~120 queries,
~200 labeled pairs**, tags: `doc-semantic` 30 · `lookup` 20 · `code-name` 15 ·
`code-behavior` 20 · `pdf-page` 15 · `filtered` 10 (kind:/under:/after: queries) ·
`web` 5 · `noise` 5 (expect: []). Labels are hand-authored by reading the sources once;
the file is committed and versioned with the manifest.

**Runner** (`scripts/eval_real.py`, reuses `eval_relevance.py`'s store/scorer plumbing):
build corpus → real ingest (llama backend, `GOLDMINE_LIVE`-gated like TEST_PLAN §6) → run
all queries × scorers → report per-tag and ALL: R@1, R@5, MRR, nDCG@10 (graded; DCG =
Σ relᵢ/log₂(i+1); IDCG from the query's own ideal ordering), plus mean latency and the
DigUp fold audit (`strongCount` semantics, `Searcher.swift:278-281`: "would the fold hide an
expected answer", `main.swift:467,490-491`). Location check follows DigUp `at()`:
pages/seconds must fall inside `@a-b`; code lines overlap it (`main.swift:480-484`).

<!-- amended 2026-10-16: B12/A4 — gate requires counts/uncertainty; per-tag deltas advisory
     below n ≥ 20; acceptance set gains OCR/ASR + multilingual queries, holdout untouched -->
**Gates (v2.0)**:

- `scripts/eval_real.py --gate` compares to `evals/baseline.json` (committed; written
  deliberately via `--update-baseline`, with the corpus_version, fingerprint id, and
  **per-metric query counts** recorded). **Exit 1 on any tracked metric dropping > 0.02
  absolute AND outside that metric's bootstrap 95 % CI** (resample queries; a −0.02 move
  inside the CI is reported, not failed — a bare −0.02 on a thin tag is noise, not a
  regression). This is the one accuracy gate; no design change merges with a red gate.
- New-feature acceptance (advisory below n ≥ 20): a feature merges only if ALL-metrics
  non-inferior AND at least one targeted tag improves ≥ 0.02 (mirrors DigUp's per-eval
  accept/reject discipline, `Searcher.swift:109-114,306-315`) — but tags with < 20
  queries (`web` = 5, `noise` = 5 today) carry **advisory** deltas only: they can neither
  justify nor block a merge until grown to n ≥ 20.
- **Acceptance-set breadth (A4):** the labels gain an OCR/ASR slice (10 audio + 10
  screenshot/scanned-PDF queries — 02 §9's media set migrates here when ASR/OCR labels
  mature, C11) and a multilingual slice (≥ 10 non-English queries — EmbeddingGemma and
  whisper are multilingual; acceptance must say so). Additions extend the corpus via a
  `corpus_version` bump + label review; the frozen held-out split ids are never retuned
  against — new queries enter the train/report pool and the split re-stratifies around
  them (existing holdout untouched).
- Offline tier: the same harness runs under the mock backend in CI **only** as a
  determinism/crash check (mock vectors are not semantically meaningful — TEST_PLAN.md:111-113
  and 06 §1 already established this), never as a quality claim.

### 5.2 Fusion tuning methodology (`scripts/tune_fusion.py`, v2.1, effort M)

v1 runs `(w_m, w_c, z_gate, kind_hint) = (1.0, 2.0, 2.0, 1.0)` and coverage exponent 1.5
(`search.py:57-63,209`). Nothing else in the pipeline was measured on EmbeddingGemma-2-under-
llama.cpp with our corpora.

**Procedure.**

1. **Parameterize** the six knobs (read from `Config.search`, defaults unchanged):
   `w_m ∈ {0.5, 1.0, 1.5}`, `w_c ∈ {1.0, 2.0, 3.0, 4.0}`, `z_gate ∈ {1.5, 2.0, 2.5, 3.0}`,
   `cov_pow ∈ {1.0, 1.5, 2.0}`, `hint ∈ {0.0, 0.5, 1.0}`, `cov_quorum ∈ {0.4, 0.5, 0.6}`.
2. **Coarse coordinate search**, not continuous optimization: tune (w_m, w_c) first (they
   dominate), then z_gate, then the rest one at a time; keep every run's full table in the
   output. Rationale: DigUp's experience is that weights move results in steps
   ("1.5 changed nothing, 0.5 lost the top 5", `Searcher.swift:112`) — a fine-grained
   optimizer would overfit noise.
3. **Objective**: nDCG@10 on ALL, with hard constraints lookup-R@1 ≥ current and noise
   queries still return 0 hits. When two configs tie within 0.01, prefer the simpler numbers
   (DigUp precedent: keep 2.0).
4. **Overfitting guards**:
   - Frozen split: tune on a 70 % query split **stratified by tag**; report the tuned config
     on the held-out 30 %. The held-out numbers are what the baseline records. Split is
     seeded and committed (query ids in `evals/splits.json`).
   - 5-fold rotated: report mean − 1 std across folds; the tuned config must beat the v1
     config's mean on ≥ 4/5 folds.
   - Sub-set non-regression: lookup and non-lookup are tuned against jointly; a config that
     buys 0.03 on docs but loses 0.05 on lookups is rejected outright.
5. **Corpus-size regimes.** The real corpus is ~2–4k segments; z-scores are corpus-relative
   by construction, but the *gate* is not: at 100k+ segments the max-z tail stretches. So
   tuning runs at two scales: (a) real corpus, (b) the 04 perf fixture scaled to 100k random
   vectors with the real corpus's relevant segments **planted** at known ranks — the check is
   that the chosen z_gate keeps recall of planted segments stable from 2k → 100k (report a
   small table; if z_gate needs a size term, gate on `max(z, 0)/sqrt(log(N))` — decide from
   data, do not pre-commit).
6. **Per-modality calibration**: keep DigUp's scheme exactly (per-modality means, global std,
   tiny-group rule) — v1 already mirrors it (`search.py:303-309` = `Searcher.swift:316-350`),
   and DigUp measured the alternative (per-modality std) as worse (:306-310). The llama
   backend is text-only (`backends/llama.py:158-162`), so multimodal calibration is
   **untestable until 02's image/audio vectors exist** — explicitly deferred to v2.1+ with a
   re-run of the 105-query-style media comparison when there is something to calibrate.
   Port `setsScale` in the same window (G3): mark the later pages/chunks of files beyond the
   first-pass steps (DigUp `Searcher.swift:80-86`; goldmine equivalents: `files.info.partial`
   + loc beyond `max_pages`/`max_chunks` steps) as score-but-not-scale rows. Effort S; accept
   on the `pdf-page` tag.

### 5.3 Query understanding & prompt routing (v2.1–v2.2, effort M)

**Layer 1 — rules (v2.1, S).** Extend the current regex with three high-precision,
cheaply-testable classes, all routed in `Searcher.search` before scoring:

| Class | Rule | Route |
|---|---|---|
| `exact-id` | current `is_lookup` (`search.py:281-291`), unchanged | weights (0.5, 4.0); plus: a single-term query matching the identifier regex becomes **one FTS phrase**, not OR terms (port of `CodeExtractor.swift:256-269`) |
| `code-explicit` | query starts with `code:` (DigUp `main.swift:336-338`; prefix stripped) | query embedded with the code prompt (below); kind hint favors kind=code; doc results still allowed (single index — DigUp's separate code index does not apply) |
| `semantic` | everything else | defaults |

Everything is recorded in the Hit (§5.8) so eval can slice by routed class. Wrong routings
are auditable and the rules stay greppable — no ML in the default path.

**Layer 2 — prototype classifier (v2.2, M, conditional).** Only if the eval shows a real
confusion class (candidates: natural-language questions about code like "retry a failed
call", which DigUp routes via `code:` but our users won't type). Design: a fixed set of
~15 seed phrases per class ("code", "exact-id", "semantic") embedded **once per fingerprint**
and stored in `evals/prototypes/<fingerprint_id>.json` (mock seeds for tests); at query time
reuse the already-computed query vector, take cosine to each class centroid; route when
top-cosine ≥ 0.5 AND margin over runner-up ≥ 0.05 (both tuned on the train split like §5.2);
default semantic otherwise. Cost: zero extra embeds (vector already exists), ~50 µs numpy.
No routing decision is silent: `--why` reports `routed: code-explicit` etc.

**Prompt variants & the fingerprint (v2.0 prerequisite, S).** EmbeddingGemma's code prompt
is measured: `task: code retrieval | query:` beat the search prompt 24 vs 22 top-5,
MRR 0.79 vs 0.75 on DigUp's real-code eval (`Model.swift:92-94`). But `PROMPT_SCHEME`
(`base.py:21-23`) bundles query and document prompts into one fingerprint field
(`fingerprints.py:59`), so adopting it would force a pointless reindex. Fix first: split the
fingerprint field into `prompts_doc` and `prompts_query`; changing a **query** prompt then
never invalidates document vectors. (One-time cost: the field rename itself changes the
fingerprint id — land it together with §5.5's `pipeline.v2` reindex so users reindex once.)

**Query-side prompt policy (v2.1, S).** EmbeddingGemma prompts are query-side only here
(document vectors stay `"title: … | text: …"`, `pipeline.py:709-710`). Policy: embed the
query with the search prompt always (9–12 ms **[measured]**, 04 §3.2:88); when routed
`code-explicit` (or Layer-2 code), **additionally** embed with the code prompt (+9–12 ms) and
score the semantic leg with `max(z_doc, z_code)` per segment — dual-prompt, per-segment max.
This fits the single index (DigUp's two-index routing does not) and costs ~one extra embed,
not a second scan. Accept/reject on the `code-behavior` + `code-name` tags with the §5.2
guards. Fingerprint: recorded in `prompts_query` as a *set*; scoring uses only vectors that
already exist.

**Document-side code titles (v2.1, M).** Adopt DigUp's repo-relative title for code chunks
("billing/retry.py" style): measured 22 vs 24 top-5, MRR 0.69 vs 0.79 vs filename-only
(`CodeExtractor.swift:16-18`). Walk already knows each root; repo = nearest ancestor with
`.git` (DigUp `CodeExtractor.swift:201-218` logic, ported to `ingest/walk.py`); title becomes
`<repo-name>/<rel-path>`. Changes document vectors ⇒ part of the §5.5 reindex.

### 5.4 Reranking & diversity (v2.0 MMR, v2.2 rerank; effort S + M)

**What we get for free — MMR + near-dup collapse (v2.0, S).** After fusion, all candidate
segment vectors are already in memory (the scan materialized them, `search.py:155-160`).
Reorder the top-50 with MMR over those vectors — no new embeddings, no model calls:

```
mmr(seg) = λ·score_norm(file) + (1−λ)·max_{s∈selected} cos(v_seg, v_s),  λ = 0.7
selected = greedy argmax, 50 → limit
```

plus a hard collapse of segments with pairwise cosine ≥ 0.97 (the distill near-dup
threshold, SPEC.md:225) keeping the higher-scored one's file. Budget: 50×50 cosine on
768-dim is ~2 MB of flops — microseconds. Cost is one numpy call inside the 21 ms Python
fusion loop (04 §3.1:67). Also port the **fold display**: hits below 0.5× the top score
print under a `— weaker matches —` divider (DigUp `Searcher.swift:278-281`: 26.2 → 5.7
shown per query, no expected answer folded). Accept on ALL non-inferiority + a
`duplicate-pollution` probe query set added to the labels (near-dup pairs in the corpus).

**Re-embed rerank — opt-in only (v2.2, M).** Re-embedding the top-50 segment texts with the
query task prompt costs, at the measured 16–31 texts/s (04 §3.2:90-95), **1.6–3.1 s** —
10× the warm search budget and unshippable as a default for a CLI that already pays ~1.1 s
spawn (04 §3.7:150-158). Ship as `goldmine search --rerank N` (default off): fused top-50 →
re-embed each candidate's best segment with `query_prompt(segment_text)` → final order =
0.5·z-norm + 0.5·rerank-cosine. Honest knob, evaluable (`--rerank` on/off columns in
`eval_real.py`), useful in the future warm `goldmine serve` path (04 §6.3) where the spawn
cost is gone. If the eval shows ≤ +0.02 nDCG for +2 s, cut it.

**Cross-encoder reranker — REJECTED** (§5.9): second model to pin/fingerprint/resident,
latency unmeasured but strictly worse than the 1.6–3.1 s embedding floor above for 50
candidates, and the task-prompt lever already demonstrably moves this model family
(`Model.swift:92-94`).

### 5.5 Token-accurate chunking (v2.0, effort M)

**Fact**: the vendor server exposes `POST /tokenize` (`vendor/llama.cpp/tools/server/server.cpp:290`,
with `/detokenize` at :291). v1 counts `len // 4` (`chunker.py:135-137`), which is why a code
stretch reached 2670 tokens against the 2048 `-ub` ceiling and 400-failed the file
(04 §3.3:113-116), while prose chunks (~366 tok, 04 §3.2:89) undershoot the ~512 model
context (ARCHITECTURE.md:84).

**Design.**

- `EmbeddingBackend.count_tokens(texts: list[str]) -> list[int]` (new ABC method,
  default `None` ⇒ callers fall back to `est_tokens`). llama backend implements it via
  `POST /tokenize` `{"content": ...}` per text (endpoint is single-content; batch = serial
  localhost HTTP, no GPU — estimate ~0.2–1 ms/request, unmeasured: probe on landing). The
  server is already up during ingest, so this adds no lifecycle.
- **Token-space chunking** in `chunker.py`: new `chunk_text_tokens(text, budget_tokens,
  counter, overlap_tokens)` keeping the existing paragraph/whitespace cut logic
  (`chunker.py:51-82`) but measuring window width in tokens. Target `~450 tokens` with
  `overlap ≈ 48 tokens`; code stretches accumulate whole lines to the same token budget
  (a single over-budget line is hard-cut at token boundary — the current whole-line-own-chunk
  rule, `chunker.py:116-123`, kept for lines within budget).
- **CJK/code accuracy**: real token counts replace the 1-char-≈-1-token worst case
  (`chunker.py:141-144`) — CJK chunks stop being shrunk for everyone, code stops blowing up.
- **ctx-guard interplay**: `effective_chunk_size` becomes `effective_chunk_budget =
  min(450, max_input_tokens − prompt_overhead(~40 for `title: … | text: …` framing) − 24)`.
  Before embedding, the pipeline asserts `count_tokens(prompt) ≤ ceiling − 1` per chunk
  (it already counted); on violation, split that chunk at a token boundary and continue —
  the §3.3 failure mode becomes structurally impossible instead of a per-file `failed`.
<!-- amended 2026-10-16: B11 (Reviewer 1) — the exit-3-on-chunker text is dead under the v2
     refusal model (refusal is reserved for incompatible embed identity only, A2) -->
- **Identity / migration**: the chunker bump records `chunker_version='chunk.v2-tok'`
  per file (05 §4.2) — there is **no global exit-3** under the v2 refusal model, and
  correctly so: stale-chunker vectors are still single-identity and comparable.
  Chunker-stale files keep working; `status` shows "N files chunker-stale since <date> —
  re-run ingest" with the cost at measured rates (~11 texts/s, 04 §3.3:108 — reuse 04's
  ETA work), and the stale set is re-done by ordinary ingest re-runs (the
  extractor-version skip fix, 02 G10) or, from v2.1, `goldmine reindex plan|run`. (The
  §5.3 prompts split moved to v2.1 with its consumers — its embed-identity rewrite will
  then be planner-paced; REDESIGN §5.)
- **Lexical-only / mock**: no server ⇒ `est_tokens` fallback keeps offline tests
  deterministic; chunk boundaries may differ between fallback and server runs — acceptable,
  recorded via the fingerprint (`preprocessing: pipeline.v2-tok` vs `pipeline.v2-tok/est`).

### 5.6 Snippets & highlighting (v2.0, effort M)

Port DigUp's snippet machinery to `util.py` (stdlib re, no new deps):

- `matches(term, text) -> list[range]`: normalized (NFC-lower, the v1 `_terms_of`
  normalization, `search.py:85-87`) case/accent-folded substring scan with word-boundary
  checks, mapping back to original-string offsets via a per-character origin array (DigUp
  `Folded`, `SearchText.swift:112-138`). Prefix semantics for the last query term (v1 FTS
  already does prefix-last, `search.py:118-120`).
- `snippet(text, terms, words=12) -> str | None`: «term»-marked window — DigUp anchors at
  the first match, v2 improves to **term-density windowing**: slide a ~12-word window over
  the text, score windows by distinct query terms hit (then earliest), tie-break to first
  match; mark all term occurrences inside with `«»`; `…` caps. Fallback when no term
  matches (pure-semantic hits): the stored excerpt, unchanged.
- **Where applied**: at render time in `Searcher` hit assembly — new
  `store.segment_text(segment_id)` (`SELECT coalesce(original, body) FROM fts WHERE rowid = ?`,
  DigUp `Searcher.swift:354-358`) for the ≤ `limit` hits only (≤ 20 row reads, ~µs each).
  The matched-word snippet is used **only when it comes from the segment we point at** —
  the DigUp rule that stops a page-5 hit showing page-2's words (`Searcher.swift:219-230`).
  `--excerpt-chars 0` keeps disabling excerpts (P3-13, `cli.py:428-432`); the «» markers are
  stripped in `--json` unless `--json --raw` (markers kept, field `excerpt_marked`).
- FTS5's own `snippet()` is *not* used for display (its text is tokenizer-normalized; DigUp
  re-snippets from the original for exactly this reason, `Searcher.swift:143-152`) — but it
  remains available as a cheap pre-filter for the density window if profiling ever needs it.

### 5.7 Filters & query syntax (v2.0, effort M)

Minimal grammar, parsed out of the query string in `cli.py` before `Searcher.search`
(unknown `word:` prefixes stay literal search terms):

```
kind:pdf|doc|code|image|audio|video|webpage   (repeatable → union)
under:PATH            (file paths only; tilde-expanded; prefix match)
after:DATE  before:DATE   (ISO YYYY-MM-DD, or Nd|Nw|Nm relative to today)
site:HOST             (source='url' rows only)
"quoted phrase"       → FTS phrase group added to the pattern list (exact, no prefix)
```

Applied **pre-fusion** as an intersection with the candidate set — the existing
`_allowed_files` seam (`search.py:322-335`, `candidates &= allowed` at :176):

```sql
kind:  … AND kind IN (…)                     -- existing
under: … AND path LIKE ? || '%'              -- files.source = 'file'
after: … AND coalesce(mtime, 0) >= ?         -- urls: json_extract(info,'$.fetched_at')
site:  … AND source = 'url' AND path LIKE '%' || ? || '%'
```

Semantics: filters are *honest* — a filter that matches nothing yields the standard
zero-hit output (exit 0, "nothing found"), never silent widening. `--json` echoes the parsed
filters so scripts can verify what was applied. Kind hints (§2.1) stay soft; `kind:` is the
hard version users opt into. Dates index nothing new (mtime/info already stored,
ARCHITECTURE.md:283-297).

### 5.8 Result confidence — `--why` (v2.0, effort S)

`goldmine search QUERY --why` extends both renderers with per-component contributions.
The data already exists at scoring time (`search.py:204-213`); it is currently discarded.

Human (one extra indented line per hit):

```
1. score 3.41  z 3.12  cov 0.67   doc   /notes/k8s.md  L120-128
   why: 1.0·z(3.12) + 2.0·cov^1.5(0.55) + kind(doc) 1.0 = 3.41 · words #2 · routed: semantic
        seg chunk#4821 z 3.12 (next-best seg z 1.9, same file)
```

JSON per hit (additive — existing keys untouched):

```json
"why": {"w_m": 1.0, "w_c": 2.0, "z_gate": 2.0, "lookup": false, "routed": "semantic",
        "terms": ["z_m": 1.0·3.12, "cov": 2.0·0.549, "kind_hint": 1.0],
        "bm25_rank": 2, "kind_hint": ["doc"],
        "segment_margin_z": 1.22, "prompt": "task: search result | query: …"}
```

`segment_margin_z` (best vs next-best segment of the same file) is the PDF-page debug lever:
a margin ≈ 0 means the file-level hit is spread thin across pages. No new SQL beyond §5.6's
text fetch; everything else is passed through from the scoring loop.

### 5.9 Explicit REJECT list

| Rejected | Why |
|---|---|
| **Learned rankers / LTR** (weight tuning beyond coordinate search, LambdaMART, etc.) | ~120 labeled queries cannot train a ranker; features pipeline is a second system to maintain; DigUp's hand-tuned 3-parameter fusion beat RRF on their real eval (`Searcher.swift:166-168`) — the ceiling at this label volume is hand-tuning with guards |
| **Cross-encoder / second-model reranking** | new model to pin, hash, fingerprint and keep resident (violates the one-model story, ARCHITECTURE.md:183-187); 50-pair inference is strictly slower than the measured 1.6–3.1 s embedding floor (04 §3.2); embedding prompts already measurably move this model (`Model.swift:92-94`) |
| **LLM query rewriting / expansion** | requires a chat model goldmine does not ship; nondeterministic output breaks the eval + the regression gate; hides what was searched from the user (anti-"honest") |
| **ANN / HNSW for accuracy** (and for speed below ~2M vectors) | exact scan is ≈ 45 ms @500k, 88–118 ms @1M (04 §3.4, re-measured 2026-10-16); the hnswlib wheel is fine — the earlier recall collapse was random-data geometry + ef, not a break (04 §3.6, B6) — rejection is on exact-scan speed; ANN only enters via 04's decision tree at ≥ 2M vectors |
| **sqlite-vec** | unusable with pyenv 3.12.0's sqlite (no loadable-extension support, 04 §3.5:130-138) |
| **ColBERT-style multi-vector / late interaction** | ~100× vector storage for marginal gain at file-level result granularity; brute-force budget (26 ms) is already spent on the single 768-dim scan |
| **Fine-tuning / adapter-tuning EmbeddingGemma** | the model is a pinned GGUF (README.md:26-29); serving adapters locally is outside the llama-server embedding endpoint's contract |
| **Per-user click-feedback learning** | single-user CLI; no impression/click volume; statelessly re-derivable preferences belong in the shell alias layer, not the index |
| **Chunk-level results as the primary unit** | v1 contract is file-level hits with best segment (SPEC.md:177); chunk-level output changes the product, not the accuracy — revisit only with a real consumer |
| **Synonym tables / WordNet expansion** | English-only lists, brittle, and the embedding leg already carries synonymy; coverage would need to count expanded terms, breaking the honest "share of *your* words found" meaning |

---

## 6. Phasing & effort

Sizes: S ≤ 1 day, M 1–3 days, L > 3 days (focused work, this machine).

<!-- amended 2026-10-16: Reviewer 2 C2 — the prompts split (item 3) moves to v2.1 with its
     consumers (query rules, dual-prompt); token chunking (item 4) no longer rides it and
     owns the one v2.0 chunker bump on its own (per-file identity, no exit-3). -->

| # | Item | Effort | Phase | Depends on |
|---|---|---|---|---|
| 1 | Real eval harness: manifest + builder + labels (~120 q) + `eval_real.py` with nDCG/grades/tags | L | v2.0 | — |
| 2 | Regression gate + `baseline.json` + fold audit | S | v2.0 | 1 |
| 3 | Fingerprint split `prompts_doc`/`prompts_query` | S | v2.1 (was v2.0) | planner (paces its reindex) |
| 4 | Token-accurate chunking (`/tokenize`, ctx-guard split, `chunk.v2-tok`) | M | v2.0 | 2 (the one v2.0 reindex; per-file staleness, no exit-3) |
| 5 | MMR + near-dup collapse + weaker-matches fold | S | v2.0 | 2 (gate) |
| 6 | Snippets & «highlighting» (density window, `segment_text`) | M | v2.0 | 2 |
| 7 | Query filters `kind:/under:/after:/before:/site:` + quoted phrases | M | v2.0 | 2 |
| 8 | `--why` confidence output | S | v2.0 | 2 |
| 9 | Fusion tuning runs → new defaults (guarded coordinate search, two scales) | M | v2.1 | 1, 2 |
| 10 | `setsScale` long-file exclusion | S | v2.1 | 1 (`pdf-page` tag) |
| 11 | Query rules: `code:`, identifier phrase, routing plumbing | S | v2.1 | 2 |
| 12 | Dual-prompt query experiment (search+code prompts, per-segment max) | S | v2.1 | 3, 11 |
| 13 | Repo-relative code titles | M | v2.1 | 4 (reindex rides along or waits) |
| 14 | Prototype centroid classifier (conditional on eval confusion) | M | v2.2 | 9, 11 |
| 15 | `--rerank` re-embed rerank (opt-in; serve-path first) | M | v2.2 | 2, 9 |
| 16 | Multimodal z-calibration re-run (after 02's image/audio vectors) | M | v2.2 | 02 |

Sequencing note: v2.0 is "measure + show" (everything gated by the harness lands with it),
v2.1 is "tune + route" (all changes must keep the gate green), v2.2 is conditional
experiments that the eval must justify. The v2.0 reindex is the single chunker bump
(item 4): announced by `status` ("N files chunker-stale"), absorbed by plain ingest re-runs
(no exit-3). The prompts split (item 3) lands in v2.1 planner-paced, with item 13 either
riding along or following on its own bump.

---

## 7. Risks & open questions

1. **Label circularity.** One maintainer authors labels *and* judges tuned weights. Guard:
   the held-out split is frozen before tuning starts; a re-label pass is scheduled ~6 months
   in (06 §1's rater pattern); noise queries and location checks are mechanical, not taste.
2. **Corpus drift.** DigUp/goldmine evolve; hash-pinned manifest makes drift
   loud (builder fails), and `corpus_version` invalidates baselines instead of mixing them.
   Open: how often to deliberately bump (default: only when labels are reviewed).
3. **Small-corpus overfit.** ~120 queries is enough to catch regressions, thin for setting
   6 parameters. The guards (§5.2.4) bound this, but if fold-spread stays wide, ship v1
   defaults unchanged and record that result — "measured, kept" is a valid outcome.
4. **z_gate across scales.** The 100k planted-scale check (§5.2.5) may show the gate needs a
   size-dependent term; if so, that changes candidate-set behavior for every query — needs
   its own mini-eval before adoption.
5. **`/tokenize` cost & shape.** Route confirmed (`server.cpp:290`), per-request cost and
   the exact response field are unmeasured — S probe before committing to per-chunk counting
   at ingest; fallback is counting one tokenized sample per file and interpolating per
   paragraph (weaker, documented).
6. **Dual-prompt max(z_doc, z_code) may widen the gate** (max of two z-scores inflates the
   tail); if the eval shows gate inflation, take max of *routed* prompt only, not both.
7. **nDCG grade subjectivity** (2 vs 3 boundary). Mitigation: written grade definitions in
   the labels header; disputes resolved toward the lower grade.
8. **Interaction with 02/04.** Token chunking changes segment counts ⇒ 04's latency tables
   shift slightly (more, smaller chunks); re-run `bench_search.py` after item 4 lands. The
   one-reindex coordination across 01+02 must be announced in the exit-3 message as a single
   action, not two.
