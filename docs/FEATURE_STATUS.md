# Current implementation status

Updated 2026-10-25; package `0.2.0` development. This is the current capability
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
| Excerpt cards | `distill` | Verbatim original text; v3 co-association ensemble, fixed default tau 0.45, manual override or inherited incremental tau | Real 120-document/eight-topic inputs frozen and independently audited; actual native clustering/incremental quality run pending |
| Local delivery | Wheel/sdist and static native tar | Actually exercised on macOS arm64/Python 3.12 | Historical snapshot-006 isolated OCR/embedding/SDK/upgrade smoke pass; latest source needs a fresh build and installed smoke |
| Hosted delivery | Workflows written, configured remote, manual live entry | Lifecycle changes need their own hosted proof | Historical/local CI evidence does not certify these lifecycle changes |
| QMD / Recoll comparison | Isolated local installations and common-text diagnostic | QMD 2.8.3; Recoll 1.44.2; no user config writes | [Comparison](COMPARISON.md); incomplete seed cannot establish a market quality ranking |
| DigUp-parity operations | `estimate <root>` (read-only minutes preview); query-time deleted-file filter (root-scoped, URL rows exempt, `deleted_filtered` count in settings); iCloud dataless skip before any open; `\| head` EPIPE exits quietly; build/target/dist/out/.next skipped; audio tail kept only with ≥2s new audio (MEDIA_PREPROCESSING v5, media family v3); Arabic lost-letter PDF pages render→OCR→three-checks→replace (`+arocr` extractor variants) | Estimate uses DigUp M4 Pro rate priors with an explicit "goldmine calibration pending" label; nothing written; arocr falls back to the original text when any check fails | digup-p2-closeout-001: suite + dual eval gates green after the authorized fingerprint/eval rebuild; real evicted-iCloud and real pathological-Arabic-PDF paths are MISSING on this machine (mechanism tests + manual verification recipes recorded) |

## Data and release gates

The [frozen product suite](../evals/product_v4_expansion/README.md) contains 420
questions from 122 real sources, including PDF, OCR, audio and EXIF evidence.
Its grouped dev/holdout split is 210/210; holdout remains unopened. The latest checked-in
420-question development summary, current005 (2026-10-22), has strict success@5 96/180 answerable questions,
all-facts PACK 97/180, and correct empty results on 20/30 noise questions. It fails
12 quality gates with no missing evaluation evidence. Compared with current004,
noise rejection improves while answerable retrieval and PACK point estimates fall;
the paired intervals for answerable metrics include zero. That historical 465-file
checkpoint passed four CI profiles: 2,821 tests, zero skips. These counts do not
certify the subsequently changed source. See the
[current005 summary](execution-plan/product-dev-post-anchor-20261022.json).

The [80-question answer inputs](../evals/answer_v3/README.md) and
[120-document card inputs](../evals/distill_real/README.md) are frozen and audited.
The 480 real answer jobs, 960 real grading jobs and actual card clustering run
are still missing. The independent full-media suite has 136 queries
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
[`reports/execution/lifecycle-repair-20261025/verification-summary.json`](../reports/execution/lifecycle-repair-20261025/verification-summary.json).
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
