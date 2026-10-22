# Current implementation status

Updated 2026-10-22. Branch `codex/p0-p2-improvements`; package `0.2.0` development.
The authoritative task register is [tasks.json](execution-plan/tasks.json).
`IMPLEMENTED` means code exists; `VERIFIED` requires the task's complete evidence.
No final quality milestone, release candidate, hosted CI success or publication is claimed.

| Capability | Interface and default | Backend / input limits | Evidence and remaining acceptance |
|---|---|---|---|
| Latin/CJK lexical retrieval | `search --lexical`; ingest without a model | NFKC derived index preserves stored original; schema v6 | Mechanism tests pass; latest completed product dev quality fails its gates |
| Name/path/identifier lookup | Normal lexical/hybrid search | Complete typed anchors and same-file body predicates; metadata has no invented body position | C002 and legacy-read focused tests pass; new structural candidate has not been quality-scored; full current-source CI is pending |
| Semantic fast retrieval | Default hybrid, fixed embedding pin | llama is text-only; exact stored fingerprint required | Historical real baseline reproduced; final candidate regression recorded in PROGRESS |
| PDF / supporting evidence | One file per hit; up to three content segments | True page units, same-page chunks and bounded adjacent pages | Twenty-page and multi-block adversarial cases pass; latest completed real product PDF gate fails |
| OCR / ASR / photo metadata | Ingest with explicit tools; status records evidence states | Vision OCR; Poppler; whisper/ffmpeg; EXIF is metadata | Tool probes and focused cases pass; complete full-media provenance/quality missing |
| Photograph visual semantics | Not available through llama | No generated caption fallback | Reports `visual_search=false`; Foresight is a separate optional backend |
| Packs and opaque citations | `search --pack`, get/context APIs | Whole context, source IDs and why count toward budget; estimator is labeled | Real SDK Agent chain and real-token matrix pass at four budgets/three context widths; fresh answer A/B missing |
| Read-only MCP | Explicit `mcp --db PATH`, optional exact SDK 2.0.0 | Four tools; one active operation/two waiters; no source reread | Historical SDK/stdio/queue/cancellation/installed smoke pass; latest source needs fresh CI and installation proof |
| Deterministic multi-query | `--mode quality --multi-query`, off by default | Original plus at most two source-derived clauses | Grammar, identity, filters and cancellation tests pass; compound benefit unmeasured |
| Local reranker | `--mode quality --rerank local`, off by default | Explicit pinned 639MB model, top twenty admitted segments, 384 tokens each | Six real endpoint probes and real bounded-input/cleanup test pass; dev A/B/resource gates missing |
| Vector cache | Explicit `reindex vectors`; separate opt-in | Immutable fp32 numpy generations; SQLite truth and epoch validation | Crash/corruption/invalidation tests pass; measured performance experiment recorded separately |
| Watch / serve | Explicit commands; no automatic installation | Existing power/thermal/memory holds retained | Historical paired 30-sample latency measured; latest source and full ten-minute controlled/real watch evidence pending |
| Excerpt cards | `distill` | Verbatim original text; existing calibrated clustering retained | Real 120-document/eight-topic inputs frozen and independently audited; actual native clustering/incremental quality run pending |
| Local delivery | Wheel/sdist and static native tar | Actually exercised on macOS arm64/Python 3.12 | Historical snapshot-006 isolated OCR/embedding/SDK/upgrade smoke pass; latest source needs a fresh build and installed smoke |
| Hosted delivery | Workflows written, manual live entry | No configured remote or hosted runner | Local actionlint passes; hosted verification remains MISSING |
| QMD / Recoll comparison | Isolated local installations and common-text diagnostic | QMD 2.8.3; Recoll 1.44.2; no user config writes | [Comparison](COMPARISON.md); incomplete seed cannot establish a market quality ranking |

## Data and release gates

The [frozen product suite](../evals/product_v4_expansion/README.md) contains 420
questions from 122 real sources, including PDF, OCR, audio and EXIF evidence.
Its grouped dev/holdout split is 210/210; holdout remains unopened. The latest
completed dev run, current004, has strict success@5 101/180 answerable questions,
all-facts PACK 102/180, and correct empty results on 5/30 noise questions. It fails
11 quality gates with no missing evaluation evidence. This result precedes the
new C002 anchor implementation; a fresh fixed development run is pending.

The [80-question answer inputs](../evals/answer_v3/README.md) and
[120-document card inputs](../evals/distill_real/README.md) are frozen and audited.
The 480 real answer jobs, 960 real grading jobs and actual card clustering run
are still missing. Separately, the legacy 169-row media suite has source bytes
that lack required EXIF/OCR evidence; its strict provenance check remains
MISSING. Mechanical tests do not replace any of these outcomes.

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
