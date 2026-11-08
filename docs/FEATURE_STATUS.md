# Current implementation status

Updated 2026-11-06; package `0.2.0` development. This is the current capability
and quality record; README links here rather than maintaining separate progress claims.
The authoritative task register is [tasks.json](execution-plan/tasks.json).
`IMPLEMENTED` means code exists; `VERIFIED` requires the task's complete evidence.
No final quality milestone, release candidate, hosted CI success or publication is claimed.

| Capability | Interface and default | Backend / input limits | Evidence and remaining acceptance |
|---|---|---|---|
| Latin/CJK lexical retrieval | `search --lexical`; ingest without a model | NFKC derived index preserves stored original; schema v6 | Mechanism tests pass; latest completed product dev quality fails its gates |
| Name/path/identifier lookup | Normal lexical/hybrid search | Complete typed anchors and same-file body predicates; metadata has no invented body position | C002 and legacy-read focused tests pass; historical four-profile CI passed for its recorded checkpoint; completed C002 development run fails quality gates |
| Semantic fast retrieval | Default hybrid, fixed embedding pin | llama embeds text; optional helper adds media; exact stored fingerprint required | Historical real baseline reproduced; final candidate regression recorded in PROGRESS |
| PDF / supporting evidence | One file per hit; up to three content segments | True page units, same-page chunks and bounded adjacent pages | Twenty-page and multi-block adversarial cases pass; latest completed real product PDF gate fails |
| OCR / ASR / photo metadata | Ingest with explicit tools; status records evidence states | Vision OCR; Poppler; whisper/ffmpeg; EXIF is metadata | Stored-evidence preflight and historical full-media mechanics gates were recorded; current-source quality needs fresh capture |
| Photograph visual semantics | Image vectors via the resident goldmine-embed helper + pinned mmproj (helper+mmproj resolve → `visual_search=true`); photo 140 / screenshot 280 / video-frame 70 budgets, thin-PDF page images, glyph-order meaning vectors, video keyframes (aHash dedup, maxFrames 1200), raw audio only without a transcript | No generated caption fallback; installs without helper/mmproj stay text-only (fingerprint unchanged) | Live media reference vectors (2 image + 2 audio) reproduce cosine 1.0 + exact fp16 sha; OCR-text-vector auto-close pinned; historical full-media 136-query gate recorded green (8 keyframe-retrieval questions, moment-level loc verdicts, 8/8 on the first v7 baseline — a mechanics proof + regression floor, NOT a natural-query quality claim: captions are synthetic, review video-eval-001 A1); legacy gate no-regression; text reference vectors bit-stable |
| Packs and opaque citations | `search --pack`, get/context APIs | Whole context, source IDs and why count toward budget; estimator is labeled | Real SDK Agent chain and real-token matrix pass at four budgets/three context widths; fresh answer A/B missing |
| Read-only MCP | Explicit `mcp --db PATH`, optional exact SDK 2.0.0 | Four tools; one active operation/two waiters; no source reread | SDK/stdio/queue/cancellation tests have passed at recorded checkpoints; fresh installed-wheel proof remains pending |
| Deterministic multi-query | `--mode quality --multi-query`, off by default | Original plus at most two source-derived clauses | Grammar, identity, filters and cancellation tests pass; compound benefit unmeasured |
| Local reranker | `--mode quality --rerank local`, off by default | Explicit pinned 639MB model, top twenty admitted segments, 384 tokens each | Six real endpoint probes and real bounded-input/cleanup test pass; dev A/B/resource gates missing |
| Vector cache | Explicit `reindex vectors`; separate opt-in | Immutable fp32 numpy generations; SQLite truth and epoch validation | Crash/corruption/invalidation tests pass; measured performance experiment recorded separately |
| Watch / serve | Explicit commands; no automatic installation | Existing power/thermal/memory holds retained | Historical paired 30-sample latency measured; latest source and full ten-minute controlled/real watch evidence pending |
| Excerpt cards | `distill` | Verbatim original text; v3 co-association ensemble, fixed default tau 0.45, manual override or inherited incremental tau | Real 120-document/eight-topic inputs frozen and independently audited. Strict runs: 2026-10-24 source PASS 13/13 (real-current-003); current text-only FAIL 12/13, topics 5/8 (real-current-005); current multimodal default FAIL 7/13 under the amended protocol (real-current-006) — purity 0.631 and topics 4/8, the first measured multimodal-default clustering quality, below gates; excerpt gates fail only on the pre-disclosed scan-PDF page-image residual (out of amendment scope) |
| Local delivery | Wheel/sdist and static native tar | Actually exercised on macOS arm64/Python 3.12 | Historical snapshot-006 isolated OCR/embedding/SDK/upgrade smoke pass; latest source needs a fresh build and installed smoke |
| Hosted delivery | Workflows written, configured remote, manual live entry | Lifecycle changes need their own hosted proof | Historical/local CI evidence does not certify these lifecycle changes |
| QMD / Recoll comparison | Isolated local installations and common-text diagnostic | QMD 2.8.3; Recoll 1.44.2; no user config writes | [Comparison](COMPARISON.md); incomplete seed cannot establish a market quality ranking |
| DigUp-parity operations | `estimate <root>` (read-only minutes preview); query-time deleted-file filter (root-scoped, URL rows exempt, `deleted_filtered` count in settings); iCloud dataless skip before any open; `\| head` EPIPE exits quietly; build/target/dist/out/.next skipped; audio tail kept only with ≥2s new audio (MEDIA_PREPROCESSING v5, media family v3); Arabic lost-letter PDF pages render→OCR→three-checks→replace (`+arocr` extractor variants) | Estimate uses DigUp M4 Pro rate priors with an explicit "goldmine calibration pending" label; nothing written; arocr falls back to the original text when any check fails | digup-p2-closeout-001: suite + dual eval gates green after the authorized fingerprint/eval rebuild; real evicted-iCloud and real pathological-Arabic-PDF paths are MISSING on this machine (mechanism tests + manual verification recipes recorded) |

## Data and release gates

The [frozen product suite](../evals/product_v4_expansion/README.md) contains 420
questions from 122 real sources, including PDF, OCR, audio and EXIF evidence.
Its grouped dev/holdout split is 210/210; holdout remains unopened. The
terminal development run, product-current-017 (2026-10-26, after the six
preregistered quality battles of all-gates-program-001), has strict success@5
155/180 answerable questions and passes 17 of 19 quality gates:
semantic@5 0.867, cjk@5 0.867, lookup@1 0.933, pdf-page@5 0.667,
media-text@5 0.867, media.metadata 10/10, media.asr 10/11. The two failing
gates are archived hard limits: noise-correct-empty 20/30 (the fabricated
premises carry no verbatim token; the retrieval-layer gate idea was disproven
by simulation in `reports/execution/P0-05/verbatim-gate-sim-001/`) and
media.ocr 6/9 (two questions sit under the frozen evaluator's 32-character
alignment floor and one under an OCR content gap). An independent audit
re-scored 840 row-metrics across runs 011-015 with zero mismatches
(`reports/execution/P0-05/all-gates-program-001/independent-audit-001/`).
The earlier checked-in summary, current005 (2026-10-22), had strict
success@5 96/180 and 12 failing gates. Compared with current004,
noise rejection improves while answerable retrieval and PACK point estimates fall;
the paired intervals for answerable metrics include zero. That historical 465-file
checkpoint passed four CI profiles: 2,821 tests, zero skips. These counts do not
certify the subsequently changed source. See the
[current005 summary](execution-plan/product-dev-post-anchor-20261022.json).

The [80-question answer inputs](../evals/answer_v3/README.md) and
[120-document card inputs](../evals/distill_real/README.md) are frozen and audited.
The full three-arm v3 study has run once (2026-10-24, run-002: 480/480 answers,
959/960 judges after one disclosed amendment) and a fresh single-arm
candidate_PACK diagnostic ran 2026-11-06 (capability-check-001, see below);
neither yields a certified comparison — each retains judge gaps that the
frozen reader correctly holds at MISSING, so descriptive diagnostics only.
The actual 120-document card clustering run on the current source fails one gate (topic recovery 5/8 vs target 6; real-current-005), while the 2026-10-24 source passed all 13 (real-current-003). The independent full-media suite has 136 queries
(78 real + 58 media, including 8 keyframe questions). Its historical provenance
preflight and multimodal/video mechanics gates have recorded passes; the
multimodal implementation has landed. Synthetic video captions and these small
mechanics checks do not establish natural-query quality. The current lifecycle,
filtered-retrieval and source-PTS implementation needs a fresh real-model run.
Historical gate receipts remain scoped to their original source/index hashes.
Mechanical tests do not replace product, answer or card quality results.

Search admission parameters are [preregistered](../evals/search_parameters.json)
with at most three arms and the original control. All three registered numeric
arms failed their dev quality gates, so `selected=null`; no fourth numeric arm
has been added. C002 is a separately reviewed structural candidate, currently
UNVALIDATED. Optional multi-query and local reranking remain off by default.

Current reports are under ignored local `reports/execution/`; compact pointers
and scope are recorded in [PROGRESS](execution-plan/PROGRESS.md). A snapshot report
applies only to its recorded source hashes. A later implementation cannot inherit
its performance, installation or quality pass merely because the branch name is
the same. `local_rc` remains nonzero until all local mandatory checks are complete.

## Index lifecycle and targeted maintenance

The 2026-10-25 lifecycle repair snapshot is on `codex/index-lifecycle-repair`.
Its three formal profiles share identical executable source hashes: offline
**2,925 passed**, native **323 passed**, optional MCP **9 passed**, all with zero
failures/skips and `source.stable: PASS`. Their raw reports were replayed through
the acceptance validator. Nine separately captured real-model tests also passed
without skips, covering exact text/media reference vectors, lexical backfill,
search, tokenizer budgets, citations and child cleanup. Local raw receipts and
the compact summary are in
`reports/execution/lifecycle-repair-20261025/verification-summary.json` (local evidence tree, not committed).
These full-profile counts belong to the captured pre-merge source. Subsequent
main integration preserves the runtime bytes and records its focused checks
separately in `reports/execution/lifecycle-repair-20261025/merge-integration/`.

Existing-index query regressions passed both legacy and full-media gates.
Success@5 is unchanged; `real-103` moves from rank 1 to rank 2 in both suites,
with Top1 deltas of -0.0096/-0.0076 and MRR deltas of -0.0033/-0.0027. A green
gate therefore does not mean identical rankings. These runs use the existing
`evals/eval.sqlite`; fresh extraction/rebuild quality, product development gates,
answer jobs and real card quality retain their separate status above. Frozen
baselines and gate thresholds were not changed.

The first concurrent offline/native run is retained as FAIL: a native
source-drift test temporarily edits runtime files while the offline preparation
validator hashes them. The final offline profile runs serially. Three missing
mandatory FIFO tests were restored from their original commit without reducing
the inventory; the native resource fixture now releases its child after actual
counter collection, preserving the original strict assertions.

- `repair --plan [PATH/URL...]` lists stored pending/failed state, missing vectors,
  changed chunker/extractor, available OCR/ASR/media upgrades and unavailable
  sources. The index stays read-only and no model is started.
- `repair --missing-vectors [PATH/URL...]` fills eligible stored text vectors in
  place. Text, segment IDs and previously stored vectors are preserved; inputs
  requiring re-chunking or media source bytes are re-extracted within scope.
- `repair --failed-only [PATH/URL...]` retries only stored failed files. Default
  repair selects all reported repair candidates. It does not scan new scope.
- Same-model `ingest --reindex PATH...` replaces selected files atomically and
  retains other roots. A model change preflights all recorded sources, invalidates
  the old model atomically and persists the new identity before any vector commit.
  Missing/unreadable roots refuse that model transition.
- `sources [list]` shows roots and exclusions; `forget PATH/URL...` or
  `sources remove PATH/URL...` removes indexed rows and affected card snapshots.
  `--dry-run` previews without index writes. Forgotten nested scopes remain
  excluded when their parent is ingested or watched. `sources restore` removes
  that exclusion; a subsequent explicit ingest restores indexed content.
- Segment identities use a durable high-water mark. Persistent deletion cleanup
  prevents card memberships from attaching to replacement segments, including
  writes through older compatible connections. Empty cards dissolve.
- Filtered FTS candidates apply file eligibility before the 1000-segment cap.
  Site filters match host boundaries. Video locations use actual source PTS;
  legacy grid-timestamp video rows are marked for refresh.

## Upgrade and recovery

1. Stop the services you started, then run `goldmine backup --db OLD.sqlite --out
   pre-upgrade.sqlite`. Keep this pre-migration copy for rollback.
2. `goldmine reindex lexical --db OLD.sqlite` upgrades/rebuilds only derived indexes.
   Stored original text and vector BLOBs are retained. Read-only search/status/doctor
   do not perform this upgrade. Supported schema4/5 reads use their actual stored
   format and bounded source verification; `lexical_upgrade_required` reports that
   the full current derived recall is unavailable. A missing required table in a
   modern index remains an error.
3. A changed chunker/extractor marks existing files stale. Explicit ingest refreshes
   them from the originally chosen roots; no new scope is inferred.
4. To use an older program, restore the pre-upgrade backup to a different path.
   Inspect it read-only with the older program before resuming its old-format use.
   Do not point an old writer at the upgraded index. Old open writers are fenced.

The installed-wheel upgrade smoke exercised original v4 → new v6, backup, citation
staleness, future-schema refusal and restoration with the original program. It is
a mechanical compatibility result, separate from retrieval accuracy.

## Equivalent retrieval and ownership candidate (2026-11-05)

The isolated `codex/high-confidence-optimizations-learn-20261104` candidate keeps
the existing vector math, scoring weights, gate and admission policy. After exact
full-corpus normalization and per-file maxima, it materializes semantic records
only for the conservative possible-admission set. Lexical/coverage/exact/name
records retain their complete semantic values; hybrid quorum zero and nonfinite
inputs use the original full path. Empty evidence candidates skip posting SQL
after the existing cancellation/deadline check.

Pack-window reranker leases now return on every request outcome. Idle callbacks
check generation identity, model operations serialize against close, and normal
process exit closes only process-owned cached children. The registered live-process
idle policy remains 300 seconds. SIGKILL cleanup is outside this guarantee.

The captured core profiles are **PASS**: offline **3,046**, native **323**, optional
MCP **9**, all without failures/required skips and with stable executable source.
Independent scalar/frozen-function oracles cover complete hits, why, evidence,
pack/citations, ties, sparse IDs, zero quorum and numeric fallbacks. Three unsafe
mutants are retained as expected FAIL evidence.

One preregistered synthetic experiment measured **RRF aggregate materialization
plus admission**, with 30 paired observations per case and 3 excluded warmups.
At 500k rows / one segment per file, phase p50 is **457.447 → 42.437 ms**
(90.723% lower); the paired median difference's 95% bootstrap interval is
**[-416.301, -413.695] ms**. Python file records drop from 500,000 to **662** on
that fixed input. All nine sparse cases improved; the all-retained 100k stress
case has **0.424% higher p50**. Its unadjusted 95% interval is positive while the
ten-case Bonferroni interval includes zero. All samples are retained. Independent
replay reproduces all quantiles/intervals exactly across 87 checks.

These are phase measurements. Fresh whole-command CLI/MCP latency, model RSS
and product-quality improvement remain **MISSING** for this candidate. Existing
quality failures are not converted into passes. No baseline, evaluation question,
fingerprint or sealed holdout was changed.

### End-to-end A/B evidence (2026-11-08, corrected v2; source range 756098e vs 8b75498)

The 2026-11-08 task `e2e-evidence-20261108-005211` measured the candidate against
a counterfactual control built from the same HEAD with only these four files
restored to `43b373a^` (per-file SHA proof; the four files are unchanged between
`43b373a` and `756098e`). Real models, real MCP SDK (mcp==2.0.0), 24 latency
unit-pairs (23 complete; L11-real disclosed MISSING after both workers hit request timeouts) × 42 samples, instrumented memory units M1–M5 with a real ≥358s idle
watch, full 210-query dev fold per arm, legacy/full-media regression gates, all
three public sets × four scorers, and the 12-cell synthetic capacity matrix.
An owner review retracted the first report's equivalence and cross-check claims;
the corrected verdicts below come from per-arm citation resolution, balanced-
order repeats, a symmetric-cache scale rerun, and gate evaluation that the first
pass had skipped (preregistration revisions 15–16).

- **Quality**: per-query metrics and hit rankings are identical across arms
  (210 dev rows, 109 legacy + 136 full-media rows, all 12 public-set cells incl.
  per-query ranked docids). At PACK granularity the result is **NOT equivalent**:
  after the preregistered per-arm citation/segment resolution, 24/210 dev
  queries differ in resolved record content (window splits / record counts) —
  recorded as a strict pack-level FAIL; leading hypothesis is the
  deadline-sensitive rank-pool selection, with the within-arm run-to-run share
  unseparable (MISSING). Existing red gates (`noise_correct_empty 0.667 < 0.90`,
  `media.ocr 0.667 < 0.70`) remain FAIL.
- **Quality-warm performance gate FAIL (both arms)**: W3 MCP warm (controlled,
  all models resident) p95 A=7583 ms / B=7564 ms vs the frozen 2500 ms gate;
  degraded 0%. The tail is real local ranking cost, not degradation and not an
  arm difference — but the gate is FAIL and is reported as such.
- **Latency**: a balanced-order repeat (both episode orders, controlled and
  real) evaluates the frozen regression criterion (p50 ratio ≤ 1.2) per pair at
  **0.998 / 1.002 — no regression** (the bootstrap CI is descriptive); the
  first pass's W1 warm B/A p50 ratio 1.23 was episode-session state, though
  code-independence of that ratio cannot be proven (MISSING). For
  rank-participating warm workloads (W2) episode-level variance dominates
  (same-arm p50 drifted 2.1 s → 3.7 s across episodes), so A/B attribution
  there is MISSING while the absolute per-arm data stands. Synthetic capacity
  (mock vectors) with a symmetric no-cache rerun shows no consistent arm
  advantage (mixed small deltas both directions; the first pass's large
  candidate-faster deltas were a candidate-only vector cache). **The scale
  suite itself is FAIL: 254 of 4,320 formal samples failed with the product's
  own 'request deadline exceeded' (500k-1/10/100 = 21/135/98, arm-symmetric;
  worst cells ~40%), quantiles use successful samples only with failure counts
  attached, and the high-failure 500k-10/100 cells are marked not comparable.
- **Memory/lifecycle**: cold-CLI same-instant task peak ≈1.83 GiB; reranker peak
  1,258 MiB ≤ 1.5 GiB gate. The candidate releases its reranker at t≈302 s
  (controlled and real) and re-borrows cleanly; the control never releases and
  leaked a ~612 MiB child in 30/42 W3 cold samples (lifecycle FAIL recorded).
  M5: zero residual task model processes.
- **MCP overhead (frozen paired p95 ≤100 ms gate, NO workload exemption)**:
  W1 controlled A 7.2 / B 5.6 ms and W2 controlled A 74.55 / B 15.42 ms — PASS;
  **W3 FAIL**: controlled A 57.4 / B 403.6 ms, real A 28.4 / B 237.4 ms. The
  reranker-lifecycle explanation is an unverified hypothesis (MISSING), not a
  gate narrowing. Real-profile MCP warm units carry 34/42 memory-pressure
  fallbacks per arm (recomputed from raw payloads; controlled units 0).

Scope: small real library (evals/eval.sqlite snapshot) for latency; frozen
product_v4_expansion/build-c, frozen BEIR cache and frozen legacy/media
baselines for quality; synthetic fp16 fixtures per the frozen rules for scale
(mock vectors — capacity cost only, never a real-model claim; two-arm symmetric
cache-off). Open gaps: W2/W3 warm A/B attribution under episode variance, the
deadline-vs-rundown split of the 24 pack diffs, the W3 overhead mechanism, and
the observer's kernel-peak guarantee — all recorded as MISSING. The suite's
built-in audits pin their reference to the historical original and reject the
constructed control by design; verdicts come from the task's frozen replayer
(290+ checks incl. per-receipt exit codes, quality-metric and
pack-equivalence recomputation, gate evaluation, per-cell scale recomputation
vs published values, suite-status consistency, and compile↔replay
consistency; a rewritten fault-injection selftest runs a no-fault baseline
first and asserts the expected failing check for each injected fault). Holdout exposure 0; no quality retries; every
superseded attempt preserved. Driver provenance is recorded honestly in
`registration/driver-sha256-history.json` (the revision_15 claimed refreeze was
never written to disk — launch-time driver state for the reruns is MISSING).
Evidence: `reports/execution/e2e-evidence-20261108-005211/`
(RESULT.md v4, REPRODUCE.md, results.json, q1-equivalence.json,
preregistration + revisions 1–18, review/, replay/).

Receipts, source hashes, mathematical reviews, registration revisions and replay
are under `reports/execution/high-confidence-optimizations-learn-20261104/`.
The learn-page candidate retains all 145 original concepts and adds five with
interactive scoring, exact pruning, empty intersections, snapshots/cache and
lease explanations. Its model/build/browser results have their own receipts;
they do not establish retrieval quality.

The final learning models received independent mathematical and ownership reviews:
1,439 analytic/content checks, 3,905 snapshot transitions and 19,607 lease
transitions pass against captured source identities. Subsequent UI-only amendments
restore focus without scrolling and subscribe to live reduced-motion changes;
they leave those teaching models unchanged. Browser evidence distinguishes native
interaction checks from synthetic visibility events. Native background-tab
visibility remains **MISSING** in Ego, whose controlled target keeps reporting
visible. The original intermittent pointer-reopen timeout remains recorded as
**FAIL**; fixed before/after probes both pass, so its causal explanation is still
**MISSING**. These limits are retained in the final browser report.

<a id="historical-qmd-comparison-2026-10-25"></a>

## Historical QMD comparison summary (2026-10-25)

The website comparison refers to a recorded **210-query dev fold** (180 answerable
and 30 noise queries) over **122 source files**, with QMD pinned to **2.8.3**.
This is separate from the earlier 28-query lexical diagnostic in `COMPARISON.md`.
On native source files, Goldmine fast Success@5 was **78.3%**, and QMD query
**66.1%**; their native readers handled 122 and 60 sources respectively. On the
same preconverted text, Success@5 was **91.7%** and **100%**. These are historical
results, not claims about the latest Goldmine source or current QMD releases.

Per-query wall-clock observations accompanying that quality run, including CLI
model loading, rounded to p50 **1.4s / 21.8s** and p95 **1.7s / 36.5s**. The
separate repeated performance/update experiment was not completed; these figures
must not be presented as a fresh performance certification. The displayed
**43–60%** noise abstention range spans Goldmine configurations (lexical control
13/30, fast 18/30), rather than describing a single fast configuration; QMD's
recorded value was 0/30.

This public summary transcribes the existing local execution record
`reports/execution/P2-08/comparison-report-001/` (`REPORT.md` and
`comparison-matrix.json`, matrix SHA256
`b648ae404d46fc475e1cf7cdd2fa4bfb7b1bbbb8f641c3ad3c932075d0b6496e`). Raw local
execution receipts are not claimed to be tracked or publicly hosted on GitHub.
No comparison, quality or holdout query was rerun for the homepage rewrite.

## Capability check campaign (2026-11-06)

capability-check-001 (`reports/execution/capability-check-001/`, local evidence tree, not committed) exercised
the current source (687edf2) across delivery, MCP, latency, watch/serve and
distill, plus a fresh GLM-5.3-Flash single-arm answer run. Fresh installed-wheel
MCP proof passed (42 SDK calls, refusal semantics, deadlines; CLI parity
byte-identical), whole-command CLI/MCP latency now has a measured baseline
(warm p50 149.8 ms / p95 203.4 ms; MCP overhead p50 1.33 ms; llama-server RSS
489 MiB), and controlled watch/serve suites passed with zero success-skips.
The answer diagnostic rebuilt the candidate index from this source (file-level
parity with run-002's index) and executed 160/160 answers + 317/320 judges:
descriptive mean 1.80 vs run-002's 1.6656 on the same frozen 80 questions,
traps 20/20 refused, no fabricated facts; three judges produced fenced JSON and
stay quarantined per the no-retry contract, so formal validation remains
MISSING. The strict real 120-document distill run on this source (text-only, real-current-005) fails topic recovery 5/8 (python-environments lost; purity 0.711, max share 0.092, other 12 gates pass); with the media helper installed the frozen preparation refuses the index (real-current-004). A single-file ablation exonerated goldmine/distill.py (bit-identical metrics) and the chunker is not isolatable by file revert; the 5/8 regression stays unlocalized (distill-ablation-001). Follow-up (distill-protocol-amendment-001, preregistered and adversarially reviewed): distill backfills empty-text image items with their file's OCR text, the frozen validator accepts the multimodal evidence shape and audits image members byte-strict against the same fallback; real-current-006 runs the full protocol under the multimodal default — 1058/1058 member provenance passes, the single excerpt failure is the pre-disclosed US174465 scan-PDF residual, and genuine multimodal clustering quality lands at purity 0.631 / 4-of-8 topics, below the text-only shape (0.711/5). Distill canary under the llama backend recovered 6/8 topics (the
recorded 8/8 was mock-backend), failing the incremental gate's own
eight-topic requirement. Fixes landed on this branch: five-file native release
check, brew-note conditionality, process-group server kill, temp-copy cleanup.

## Hosted main CI follow-up (2026-11-05)

The push run for `43b373a`, GitHub Actions **41876468326**, completed with
**success**. The tier selector and Linux/Python3.12 guard passed; the four
full-matrix jobs were intentionally skipped by the existing push policy. This
is a successful main push gate, not certification of the full scheduled matrix.
The homepage task adds an independent website workflow for locked installation,
TypeScript/build, factual examples, lesson links and animation lifecycle checks;
its actual result belongs to the new commit and must be read from its own run.
