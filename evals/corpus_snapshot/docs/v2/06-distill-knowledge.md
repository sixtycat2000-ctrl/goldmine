# 06 — Distill v2: from clustering demo to golden-knowledge pipeline

Status: design (v2 pass) · Owner: distill/knowledge · 2026-10-16
Supersedes: SPEC.md §4 / ARCHITECTURE.md §2.3 behavior (kept as v1 baseline below).

---

## 1. Mission & card-quality target metrics

v1 distill is a clustering demo: it proves vectors group, then prints groups. The v2
product goal is different — `goldmine distill` must produce **golden knowledge**: cards a
human can read as a trustworthy table of contents of the corpus, and a context pack an LLM
can consume as ready-to-paste RAG context. Everything in this doc serves one of those two
consumers; features that serve neither are on the kill list (§4.9).

Target metrics (all automatic unless stated):

| Metric | Definition | Target |
|---|---|---|
| Canary purity | weighted mean over cards of (fraction of members from the card's dominant planted topic), on the canary fixture | ≥ 0.70 |
| ARI | adjusted rand index between planted topic labels and card assignment (singletons = own label) | ≥ 0.50 |
| Canary recovery | planted topics rendered as exactly one card with ≥ 80% of their docs (8 of 10 planted on the 100-doc fixture) | ≥ 6 / 8 |
| Excerpt fidelity | every excerpt byte-traceable to a stored segment (existing Tier-1 property, kept) | 100% |
| Pack integrity | `--pack` output ≤ declared token budget (estimated or /tokenize-measured) | 100% |
| Human coherence | 30 cards sampled from 3 real corpora, 2 raters, majority "is this one coherent topic?" — annual manual check that the automatic proxy still correlates | ≥ 70% |

The canary fixture (new `scripts/eval_distill.py`, pattern copied from
`scripts/eval_relevance.py`): 100 deterministic docs = 8 planted topics × 10 docs + 12
noise/cross-topic docs + 4 near-dup pairs; known member sets → purity/ARI/canary-recovery
are computed, not judged. DigUp's `evals/*.json` methodology (`{q, expect[], tag}` —
planted answers, exact match, tagged subsets) is the shape: the fixture is the "expect"
list for clustering. The mock backend's orthogonal seed vectors make similarity-based
metrics meaningless, so calibration metrics run against the real llama backend; mock runs
in CI assert determinism only.

---

## 2. v1 state, including the observed failure

v1 pipeline (normative in SPEC §4 / ARCHITECTURE §2.3, implemented in
`goldmine/distill.py`): sha256 exact dedupe → greedy near-dup @ cos ≥ 0.97
(`distill.py:101-139`) → greedy leader clustering τ=0.70 (`distill.py:145-170`) → drop
clusters < min_members (`distill.py:168`) → card = centroid-nearest member's verbatim
excerpt + TF-IDF top-8 + all-member citations (`distill.py:277-308`) → cards/card_chunks
tables wiped and rewritten per run (`store.py:581-583`) → markdown/--json.

Observed failure on a real 5-doc corpus: 32 chunks → **ONE card absorbing everything**.
Root causes, in code:

1. **τ=0.70 is a magic constant wrong for mean-pooled embedding cosines**
   (`distill.py:146`, default threaded from `cli.py:132`/SPEC §4). Sentence-embedding
   cosines concentrate in a narrow high band (empirically ~0.4–0.9, cross-topic mass well
   above 0.5 for related-domain corpora). 0.70 sits *inside* the cross-topic distribution,
   so "same card" and "different topic" are not separable at that cut.
2. **Running-centroid snowball** (`distill.py:156-161`): each assignment adds the member's
   vector to the running sum; a cluster that has absorbed a broad mix has a centroid
   pulled toward the *corpus mean direction*, which has moderate cosine (≈0.6–0.75) to
   almost everything. First-leader bias + renormalized mean = rich-get-richer: the first
   cluster founded drifts toward generic and absorbs the rest. Assignment tests similarity
   to the drifted **centroid only** — there is no check against members.
3. **Terms junk** (`distill.py:92-95`): the letter filter (`any(ch.isalpha())`) passes hex
   addresses (`3f5a2b`), mixed alphanumeric ids, sizes; idf over cluster-df is degenerate
   when there are few clusters (the mega-cluster case: n_clusters=1 → every idf=0).
4. **One excerpt per card** (`distill.py:282-285`): max-cosine-to-centroid favors the most
   average — usually longest/genericest — chunk; a 40-chunk card is represented by one
   240-char line.
5. **No steering, no hierarchy, full recompute** (`store.py:574-601` wipes both tables;
   `distill_algo` string pins algo+τ but nothing is reusable), so iterating on τ means
   re-running everything and re-reading everything.

---

## 3. Quality references

- **DigUp** (`~/git/DigUp`): confirmed no distill/summarization anywhere (encode-only
  model). What transfers: (a) the eval methodology — `evals/{code,synthetic,multilingual}.json`
  plant known answers with `expect[]` lists and tag subsets; our canary fixture copies
  that discipline; (b) `scripts/make-testbed.sh` — a deletable sandbox corpus with trap
  directories (duplicates, odd formats); our distill fixture gets the same treatment
  (planted dups, cross-topic memo docs); (c) the determinism ethos — same inputs, byte
  -identical outputs, no RNG (v1 already normative on this; v2 must not regress).
- **Foresight context retrieve** (`~/git/mac/work/foresight-rea/app-src/docs/rpc-contracts.md`,
  RetrieveContext row): retrieved context is a flat, ordered list of
  `chunks[]{text, relevance_score, document_id, mime_type}` with `max_results` and
  `min_similarity_score` knobs — plain fields, no prose wrapper, truncation knob explicit.
  Our `--pack` (§4.6) copies this shape: flat card/excerpt records, one line of metadata
  each, explicit budget knob, nothing the consuming LLM has to parse cleverly.
- **General IR practice**: Otsu thresholding on the pairwise-similarity histogram for τ
  calibration; average-link admission as the cheap guard against single-linkage chaining;
  k-center (farthest-point) selection for coverage excerpts; medoid as the order
  -independent representative.

---

## 4. v2 design

### 4.1 Calibrated τ (no magic constant)

`distill --preview` (and every real run) first **measures** the corpus:

1. Pairwise cosines of the deduped corpus, sampled deterministically: if N ≤ 300 use all
   pairs; else stride-sample rows to ~300 (indices `i = k·N/300`, fixed seed = none
   needed — stride is deterministic), compute the sampled 300×300 gram in one matmul.
2. 256-bin histogram over [0, 1]; **Otsu threshold** t\* = argmax over bins t of
   `ω₀(t)·ω₁(t)·(μ₀(t) − μ₁(t))²` (between-class variance of the two similarity classes).
<!-- amended 2026-10-16: B9/B10 (Reviewer 1) — mode-proximity fallback (the Otsu cut can
     land ON the mode inside the clamp: over-merges monothematic corpora) + tau scale
     statement (pairwise calibration applied to cos-to-centroid reads ~1.1-1.6x higher) -->
3. Derived τ = clamp(t\*, 0.55, 0.85). Guardrails exist because Otsu degenerates on
   single-topic corpora: with one hump, t\* lands **on the mode, inside the clamp** —
   applying it over-merges a monothematic corpus into one card, so the floor alone is not
   a guard. Fallback rule: take the percentile path whenever the histogram has < 2 humps
   by the valley-depth check **or** the Otsu cut falls within k·σ (k = 1, calibrated on
   the canary) of the dominant mode's mean; there τ = P90 of sampled pairs and the
   preview says so. Fallbacks recorded in stats:
   `tau_source: otsu|floor|percentile`. Canary normative assertion: the single-topic
   degenerate case must yield `tau_source != otsu`.
4. `--threshold` always overrides and sets `tau_source: manual`.
5. **Scale statement (B10).** The histogram is over *pairwise member-member* cosines,
   but τ is *applied* to cos-to-centroid, which reads systematically higher (~1.1–1.6×
   on the distance scale — the centroid direction carries corpus-mean pull). v2.0
   documents this offset in `--preview` output and lets the canary absorb it (purity/
   recovery gates are the arbiter); if the canary shows over-merge, v2.1 switches
   calibration to sampled cos-to-centroid, recorded as `tau_source: centroid` — a
   one-function change, no new models.

### 4.2 Clustering v2: leader + average-link admission, two-level, honest ordering

**Admission guard (the anti-snowball fix, one extra dot product).** A chunk joins cluster
c only if *both*:

- `cos(x̂, ĉ) ≥ τ` (v1 rule, ĉ = renormalized running sum), **and**
- `cos(x̂, S_c)/n_c ≥ τ_lo` — mean cosine to all existing members, where `S_c` is the
  running (unnormalized) sum and `n_c` the member count, `τ_lo = max(τ − 0.10, 0.35)`
  (simplified 2026-10-16: the old second max-term `0.5·(τ+1) − 0.5 = 0.5·τ` is dominated
  by `τ − 0.10` for every τ in the clamp range — dead branch, dropped).

The second term is exactly average-link membership computed incrementally: it is what
stops a drifted centroid from absorbing unrelated chunks, at O(1) per comparison. A chunk
that fails both opens a new cluster. This converts leader clustering into leader with
average-link admission — order sensitivity collapses to genuine ties at the margin, and
we keep O(n·k) instead of agglomerative O(n²·d) memory/time (the 50k×50k similarity
matrix alone is 10 GB fp32 — complete-link agglomerative is disqualified on this machine,
see §4.9).

**Determinism stance (DigUp-style, retained):** canonical `(file_id, segment_id)` order,
no RNG, ties → lowest cluster index. Honest caveat, stated in ARCHITECTURE v2: leader
clustering is order-*dependent* (a different valid order could merge differently); the
admission guard shrinks the disagreement band, and the canary eval includes a
re-ingest-in-different-order variant asserting canary recovery is unchanged — if that
assert ever fails on real corpora, the fallback plan is two-pass refinement: leader at
τ+0.05, then deterministic merge pass over cluster pairs by descending cross-cluster mean
similarity (set-function, order-free). Not built until the eval demands it.

**Two-level topics.** After leaders: for each cluster with n ≥ 12, compute mean intra
-member cosine `s̄ = (‖S_c‖² − n)/(n(n−1))`. If `s̄ < τ − 0.12` the cluster is internally
loose → split by recursive leader at `τ_sub = τ + 0.08`, depth ≤ 2. Children become cards
with `parent_id` set; the parent is retained as a section header (not a card) in markdown.
Accept the split only if children's mean `s̄` improves by ≥ 0.05, else keep one card
(guards against over-splitting a genuinely uniform topic).

**Singleton/small clusters:** still dropped from *cards* by `--min-members` (default 3),
but no longer silently: `--preview` and `--json` stats report the size histogram including
dropped buckets, and the 10 largest dropped clusters' top-3 terms are printed in the
preview so users can decide to lower min_members.

### 4.3 Card content: medoid + coverage excerpts, real terms, a title

**Representative selection.** Replace max-cosine-to-centroid with:

- **Medoid (primary excerpt)**: member maximizing mean cosine to all other members,
  `m* = argmax_i (1/(n−1)) Σ_{j≠i} cos(x̂_i, x̂_j)` — computed on the within-cluster gram
  (clusters are ≤ a few hundred members; one small matmul). Order-independent, not
  length-biased the way centroid-cosine is. Ties → lowest segment id.
- **Coverage excerpts (up to 3 total)**: greedy k-center on remaining members — repeatedly
  add the member with max min-cosine-distance to already-picked excerpts; stop at 3 or
  when the next candidate's min-distance < 0.25 (i.e. next pick is ≥ 0.75 cosine to
  something already shown — no redundancy). Each excerpt carries its own citation and
  `role: primary|coverage`. All verbatim, excerpt-capped at 240 chars by `util.excerpt`
  (unchanged).

**Terms (fixing the junk):**

1. Token filter tightens to: ≥ 3 chars, contains a letter, **letters ≥ digits**, and
   not hex-shaped (`^[0-9a-fA-F]{6,}$`) and not unit-shaped (`^\d+(\.\d+)?(kb|mb|gb|tb|px|hz)$`).
2. **Cluster-df gate: a term must appear in ≥ 2 distinct members** (this alone kills
   one-chunk distinctive junk; the mega-cluster degenerate idf case is gone with the
   mega-cluster itself, but df≥2 holds regardless).
3. **Phrases**: adjacent-token bigrams with cluster-df ≥ 2 compete with unigrams on the
   same tf-idf score; a bigram replaces its two halves if it outscores both.
4. (v2.1, optional, no new models) if the embedding backend is reachable, top candidate
   terms/bigrams are re-scored by cosine to the cluster centroid; the algo string records
   `terms=embed|tfidf` so output is reproducible either way. Never mixed within a run.

**Title.** v2.0 extractive, no backend: first sentence of the medoid member, trimmed at
word boundary ≤ 70 chars, verbatim (fall back to v1 terms-join if no sentence boundary).
v2.1 backend option: sentences from the medoid + top-2 coverage members scored by cosine
to centroid; highest wins (for transcripts, §4.8 overrides). `title_kind: terms|sentence|llm`
records provenance; the terms line stays visible under the title either way.

### 4.4 Incremental distill and cards v2 DDL

**Identity & stability.** Cards live under a **view** = (scope flags + algo family + τ).
Incremental run: (1) load previous run's cards for the view, with stored centroids;
(2) assign *new* chunks to the nearest existing centroid with cos ≥ τ (admission guard
applied); (3) leader-cluster only the remainder into new cards; (4) existing memberships
are **frozen** — never reassigned (that is what makes labels stable and cost tiny).
Chunks whose segments were deleted become tombstones: their card_chunks rows go, size
recomputed; a card falling below min_members is marked `status='dissolved'` and its
surviving members re-enter the remainder. **Full recompute triggers:** fingerprint change,
algo/τ/scope change, > 20% of the view's member files deleted since last run, or
`--refresh` (explicit: forget incremental state, rebuild; the previous run stays in
distill_runs as history).

**Cost model.** Full: O(N·k) centroid comparisons + the one-time sampled-gram calibration
(~300² · 768 flops — nothing). Measured v1 headroom: 50k chunks leader ≈ seconds on M4.
Incremental: O(new·cards·768) + remainder leader — 100 new chunks vs 40 cards ≈ 3 M
flops, sub-millisecond. The dominant real cost of any distill run is loading vectors from
SQLite (fp16 decode), unchanged.

**Schema v2** (`SCHEMA_VERSION = 2`; cards are derived, fingerprint-bound data — migration
recreates card tables empty and tells the user to re-run `goldmine distill`; no data
migration):

```sql
CREATE TABLE IF NOT EXISTS distill_runs(
    id          TEXT PRIMARY KEY,     -- sha256(algo|tau|scope|corpus_hash|mode)
    algo        TEXT NOT NULL,        -- "leader-avglink.v2"
    tau         REAL NOT NULL,
    tau_source  TEXT NOT NULL,        -- otsu|floor|percentile|manual
    scope_json  TEXT NOT NULL,        -- {under, kinds, after, ...}
    corpus_hash TEXT NOT NULL,        -- sha256 over sorted (file path, file sha256)
    mode        TEXT NOT NULL,        -- full|incremental
    stats_json  TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cards(
    id             INTEGER PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES distill_runs(id),
    view_id        TEXT NOT NULL,     -- stable view key (scope+algo family+tau bucket)
    title          TEXT NOT NULL,
    title_kind     TEXT NOT NULL DEFAULT 'terms',   -- terms|sentence|llm
    terms          TEXT NOT NULL,     -- JSON array (renamed from title_terms)
    excerpts       TEXT NOT NULL,     -- JSON [{text,path,kind,loc,loc_end,role}]
    size           INTEGER NOT NULL,
    n_files        INTEGER NOT NULL,
    parent_id      INTEGER REFERENCES cards(id),    -- two-level subtopics
    centroid       BLOB,              -- 768-dim fp16 (1536 B), incremental anchor
    status         TEXT NOT NULL DEFAULT 'active',  -- active|dissolved
    llm_title      TEXT,
    llm_summary    TEXT,
    llm_model      TEXT,
    fingerprint_id TEXT NOT NULL,
    distill_algo   TEXT NOT NULL,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS card_chunks(          -- unchanged shape, now UNIQUE
    card INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    chunk INTEGER NOT NULL REFERENCES segments(id),
    weight REAL NOT NULL,
    UNIQUE(card, chunk)
);
CREATE INDEX IF NOT EXISTS card_chunks_card ON card_chunks(card);
CREATE INDEX IF NOT EXISTS cards_view ON cards(view_id, status);
CREATE TABLE IF NOT EXISTS distill_views(        -- named, re-runnable scopes
    name TEXT PRIMARY KEY,
    scope_json TEXT NOT NULL,
    params_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
```

### 4.5 Scoping & steering

```
goldmine distill
  --under PATH        files under PATH only (reuse store._under_roots semantics)
  --kinds KINDS       (v1, kept)
  --after DATE        files.mtime ≥ DATE (file mtime, NOT content date — stated in help;
                      transcripts carry recording time as mtime in practice)
  --as NAME           save scope+params as a named distill view (distill_views)
  --rerun NAME        run a saved view (scope + params reapplied; incremental if possible)
  --preview           dry run: corpus size, derived τ + source, pairwise-sim quartiles,
                      cluster size histogram (1, 2, 3-5, 6-10, 11-25, 26-100, 100+),
                      top terms of the largest dropped small clusters — no cards written
  --threshold X --min-members N --max-chunks N --limit-cards N --out F --json  (v1, kept)
  --pack [--budget N] --llm --refresh                                    (see §4.6/§4.7)
```

Iteration loop the UX is designed for: `distill --preview` (tune τ/min-members against
the histogram) → `distill --as notes-q4 --under ~/notes/2024` → next week
`distill --rerun notes-q4` (incremental). Scoping composes with `--pack` and `--llm`.

### 4.6 Output for USE

**(a) Human markdown.** Cards page: H1 + run header (algo, τ + source, scope, corpus
hash short); TOC of card titles with anchors; per card: title (+ `title_kind` marker when
llm), terms line, up to 3 verbatim excerpts each as a blockquote **followed by its
citation as a markdown link** `` — `path:L120-128` / `p.4` / `12:07-18:45`
(`_fmt_loc` extended: h:mm:ss for audio/video, seconds kept in JSON). Citations for all
members remain in `--json`; markdown shows per-file collapsed citations (§4.8) capped at
10 + "+N more chunks".

**(b) RAG context pack (`--pack`).** Plain text to stdout, pipeable
(`goldmine distill --pack --budget 3000 | codex exec "summarize themes: $(cat -)"` — no,
simpler: the pack is the stdin/document; it must stand alone). Exact format:

```
# goldmine context pack v2
# scope: under=/notes/2024 kinds=doc after=2024-10-01 | cards: 12/14 shown | budget: 3000 tokens (mode=tokenize)
# cards are machine-clustered from the index; excerpts are verbatim; citations are path:location

## [C1] Sourdough starter maintenance (9 chunks, 4 files)
keywords: sourdough, starter, feeding, hydration
- "Feed the starter daily..." — /notes/2024/bread.md:L12-18
- "Bulk ferment takes..." — /notes/2024/bread.md:L44-51
keywords-llm(gemma-3-4b-it): maintaining a sourdough starter — feeding, hydration, timing   [only if --llm]
```

Budget policy: `--budget N` tokens (default 4000, clamp 512–32768). Cards fill in size
order; a card's cost = title + keywords + all shown excerpts + cite lines; **never split
an excerpt**; if a card does not fit, skip it and continue (smaller cards may still fit —
keeps fill rate high); if the *primary excerpt alone* of C1 exceeds the budget, emit C1
as title+keywords+primary-excerpt only and stop. Token counting: llama-server
`POST /tokenize` when configured/reachable (mode=tokenize), else chars/4 estimator
(mode=estimate) — the mode is printed in the header line so the budget claim is honest.
Output ends with a one-line manifest: `# omitted: C7, C12 (budget) · dropped-small: 3 clusters`.

**(c) `--json` schema v2** — stable, versioned, additive-only:

```json
{"schema": "goldmine.cards/v2",
 "stats": {"corpus_chunks": 0, "after_dedupe": 0, "duplicates_removed": 0,
           "clusters_kept": 0, "clusters_dropped_small": 0,
           "tau": 0.74, "tau_source": "otsu", "histogram": {"3-5": 4, "6-10": 2},
           "algo": "leader-avglink.v2 t=0.74 min=3", "mode": "full",
           "run_id": "…", "corpus_hash": "…", "budget": null},
 "cards": [{"id": 1, "parent_id": null, "title": "…", "title_kind": "sentence",
   "terms": ["…"], "excerpts": [{"text": "…", "path": "…", "kind": "doc",
     "loc": 120, "loc_end": 128, "role": "primary"}],
   "size": 9, "n_files": 4, "status": "active",
   "citations": [{"path": "…", "kind": "…", "loc": 0, "loc_end": 0}],
   "distill_algo": "…", "run_id": "…",
   "llm": null | {"model": "gemma-3-4b-it", "title": "…", "summary": "…"}}]}
```

### 4.7 LLM polish (optional tier, `--llm`)

- Requires llama-server already running with the chat GGUF (gemma-3-4b-it Q4 fits the
  16GB machine alongside everything else); goldmine **never auto-starts servers** —
  `goldmine doctor` reports `distill.llm.base_url` reachability. Offline-only guarantee
  retained: without `--llm`, zero network calls, behavior identical to v2.0.
- Per card, ONE `POST /v1/chat/completions`: temperature 0, max_tokens 80, locked
  template — system: "Compress a topic card for a personal knowledge base. Output exactly
  two lines — `TITLE:` (≤ 8 words) and `SUMMARY:` (≤ 25 words). Use only the given
  material."; user: card title/terms + primary excerpt + sizes. 32 cards × ~1 s ≈ 30 s.
- Per-call timeout 10 s (`--llm-timeout`); any failure → that card ships unpunched
  (polish is best-effort, never fatal; failure count in stats).
- **Provenance rule (hard):** polished text goes only to `llm_title`/`llm_summary` +
  `llm_model` columns; it NEVER replaces the verbatim excerpts or the extractive title.
  Markdown shows polished title/summary labeled `(gemma-3-4b-it)` with the verbatim
  excerpt directly beneath; `--pack` prints the llm line tagged per §4.6; `--json` keeps
  it in the separate `llm` block. A card with polish but no excerpt is a bug, not a mode.

### 4.8 ASR synergy (transcripts from v2 ingestion)

Transcripts already flow: audio segments carry `loc` = seconds and `kind='audio'`, and
`_fmt_loc` already renders `12s-35s`. v2 additions:

1. **Citation collapse (episode effect):** within a card, when > 3 chunks share one audio
   file, collapse to one citation per file spanning `min(loc)..max(loc_end)` — an
   "episode mention" reads as one entry, not nine.
2. **Human time format:** audio/video locs render `12:07–18:45` in markdown/pack (raw
   seconds in JSON), so a citation is seekable in any player.
3. **ASR term hygiene:** small fixed stoplist for transcript fillers/hostnames
   ("um", "uh", "yeah", "okay", speaker-label patterns `^speakers?$|^\d$`) applied at the
   term filter only — text itself never altered (verbatim rule).
4. **Title bias for transcripts:** first sentence of a transcript is usually a greeting;
   when the medoid member is kind=audio, pick the medoid's sentence with **highest
   term-overlap with the card's terms** (deterministic, no backend) instead of sentence #1.

No episode-level special machinery beyond this: episodes emerge as cards whose members
are dominated by one file; if the eval on a transcript-heavy fixture shows card = episode
trivially, add `--min-files` later (open question §6).

### 4.9 Kill list (not worth it, with reasons)

- **Knowledge-graph entity extraction / graph DB.** Needs NER (a new model, or one LLM
  call per chunk = 1000s of calls) and produces edges nobody queries; cards + FTS answer
  "what is this corpus about". Revisit only if users ask "how do topics relate" — and
  then as card-to-card centroid similarity, not a graph store.
- **HDBSCAN / UMAP / scikit-learn.** Heavy deps (numba), non-deterministic defaults,
  hyperparameter burden for marginal gain over calibrated-τ leader at card scale
  (dozens–hundreds of cards). Also breaks the numpy-only posture.
- **Complete-link agglomerative over the whole corpus.** The 50k×50k similarity matrix is
  10 GB fp32 on a 16 GB machine; the admission guard buys most of complete-link's
  chain-resistance at O(1) per assignment. Revisit only under a max_chunks drop.
- **Multi-LLM synthesis / cross-model debate for card summaries.** Cost × models with no
  eval showing gain over one locked 4-bit template; conflicts with offline-default.
- **Per-sentence embeddings at full scale.** Backend round-trips for every sentence of
  every member; we score sentences only for the medoid + 2 coverage members' titles, and
  only when the backend is reachable (§4.3/§4.8).
- **Locally adaptive per-cluster thresholds.** Untunable by users, unexplainable in a
  header line, and they break incremental label stability (τ moves → labels move).
  One global calibrated τ + two-level split covers the real need.

---

## 5. Phasing & effort

<!-- amended 2026-10-16: Reviewer 2 C2/C6 — E1 (this whole redesign) + the cards-v2 DDL
     deferred TOGETHER to v2.1; the v1→v2 migration stays purely additive (v1 cards
     untouched, v1 distill keeps working until this lands). Phasing owned by REDESIGN §5. -->

| Phase | Items | Size |
|---|---|---|
| **v2.1** (moved from v2.0 by the 2026-10-16 review) | τ calibration + sampled gram (§4.1); admission guard + histogram stats + `--preview` (§4.2); terms fixes df≥2/filters/bigrams (§4.3); medoid + coverage excerpts + extractive title (§4.3); scoping `--under/--after/--as/--rerun` (§4.5); markdown TOC + file links + audio time fmt (§4.6a); `--pack` with estimator budget (§4.6b); json v2 + cards v2 DDL + the announced cards-rebuild migration (§4.4); `scripts/eval_distill.py` canary fixture + purity/ARI/canary-recovery | **M** (~1 week) |
| **v2.1** | incremental distill (centroid reuse, dissolve, corpus_hash, `--refresh`) (§4.4); two-level subtopics (§4.2); backend-scored titles/phrases (§4.3); LLM polish `--llm` + provenance fields + doctor llm check (§4.7); `/tokenize` budget mode (§4.6b) | **M** (~1 week) |
| **v2.2** | ASR refinements: citation collapse, title bias, stoplist (§4.8); pack polish (`--pack --template`, manifest detail); view management ergonomics (`distill views --list/--forget`); order-robustness eval variant, merge-pass fallback if the eval demands (§4.2) | **S** (few days) |

Sequencing rule: v2.0 ships nothing that depends on llama-server being up (fully
offline); v2.1's incremental work sits on the v2.0 DDL (centroids stored from day one,
even if unused until v2.1 — that is why the column is in the v2.0 migration).

---

## 6. Risks & open questions

- **Otsu on small/degenerate corpora.** < 50 chunks → histogram too sparse; single-topic
  corpora → no valley. Mitigated by clamps + `tau_source` reporting, but the floor value
  (0.55) is itself unvalidated until the canary eval runs on the llama backend. The eval
  must include a single-topic and a 20-chunk micro-corpus as degenerate cases.
  Mitigation upgraded 2026-10-16 (B9): mode-proximity fallback to the percentile path
  (§4.1) — the clamp alone is insufficient because t\* can sit on the mode *inside* the
  clamp; the single-topic canary asserts `tau_source != otsu`. τ's pairwise-vs-centroid
  scale offset is stated and canary-absorbed (§4.1 item 5, B10).
- **Mock-backend blind spot.** Orthogonal seed vectors make every cosine ≈ 0; all
  quality metrics are meaningless under mock. CI keeps mock for determinism tests only;
  quality gates run on llama backend (documented in TEST_PLAN v2).
- **Incremental label drift vs trust.** Frozen memberships mean new chunks can never fix
  an old card's mistakes; a bad v2.0-era card persists through increments. Mitigation:
  `--refresh` is one flag away, and distill_runs keeps history so drift is diffable.
  Open question: should `--refresh` also be *scheduled* (auto-full every Nth run)?
- **`--after` semantics.** File mtime is a proxy for content date and is wrong for copied
  archives. Open question: does v2 ingestion extract content dates (transcript metadata,
  PDF creation dates) into a column `--after` should prefer?
- **fp16 vector quantization near τ.** Calibration, clustering, and incremental
  assignment all read the same fp16-decoded vectors (`load_vectors`), so decisions are
  self-consistent; but τ calibrated on decoded fp16 may differ ~0.01 from full-precision
  expectations. Accepted; not worth re-embedding.
- **Pack budget honesty for non-llama consumers.** chars/4 estimator overcounts for
  CJK-heavy corpora (relevant given multilingual content); open question whether to ship
  a per-script chars/token heuristic in v2.2 or require /tokenize for non-Latin packs.
- **Human coherence check is manual.** The 70% human-judged target has no automatic
  oracle; the canary purity/ARI proxy is our stand-in and the manual protocol (30 cards,
  2 raters) is annual. Risk: proxy and humans diverge on transcript corpora specifically
  — schedule the first manual check on a transcript-heavy corpus when ASR lands.
- **Transcript corpora and min_files.** If cards end up = single episodes (all members
  from one file), `--min-files` may be needed; deferred to v2.2 pending the transcript
  fixture eval (§4.8).
