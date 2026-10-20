# Current implementation status

Updated 2026-10-21. Branch `codex/p0-p2-improvements`; package `0.2.0` development.
The authoritative task register is [tasks.json](execution-plan/tasks.json).
`IMPLEMENTED` means code exists; `VERIFIED` requires the task's complete evidence.
No final quality milestone, release candidate, hosted CI success or publication is claimed.

| Capability | Interface and default | Backend / input limits | Evidence and remaining acceptance |
|---|---|---|---|
| Latin/CJK lexical retrieval | `search --lexical`; ingest without a model | NFKC derived index preserves stored original; schema v6 | CJK, phrase, alias and old-writer rollback tests pass; full product quality missing |
| Name/path/identifier lookup | Normal lexical/hybrid search | Raw exact and alias are distinguished; metadata has no invented body position | Filename-only, mixed-query/noise and migration tests pass |
| Semantic fast retrieval | Default hybrid, fixed embedding pin | llama is text-only; exact stored fingerprint required | Historical real baseline reproduced; final candidate regression recorded in PROGRESS |
| PDF / supporting evidence | One file per hit; up to three content segments | True page units, same-page chunks and bounded adjacent pages | Twenty-page and multi-block adversarial cases pass; real product PDF gate missing |
| OCR / ASR / photo metadata | Ingest with explicit tools; status records evidence states | Vision OCR; Poppler; whisper/ffmpeg; EXIF is metadata | Tool probes and focused cases pass; complete full-media provenance/quality missing |
| Photograph visual semantics | Not available through llama | No generated caption fallback | Reports `visual_search=false`; Foresight is a separate optional backend |
| Packs and opaque citations | `search --pack`, get/context APIs | Whole context, source IDs and why count toward budget; estimator is labeled | Real SDK Agent chain and real-token matrix pass at four budgets/three context widths; fresh answer A/B missing |
| Read-only MCP | Explicit `mcp --db PATH`, optional exact SDK 2.0.0 | Four tools; one active operation/two waiters; no source reread | Final six official SDK tests, actual stdio, queue/cancellation and installed-wheel smoke pass |
| Deterministic multi-query | `--mode quality --multi-query`, off by default | Original plus at most two source-derived clauses | Grammar, identity, filters and cancellation tests pass; compound benefit unmeasured |
| Local reranker | `--mode quality --rerank local`, off by default | Explicit pinned 639MB model, top twenty admitted segments, 384 tokens each | Six real endpoint probes and real bounded-input/cleanup test pass; dev A/B/resource gates missing |
| Vector cache | Explicit `reindex vectors`; separate opt-in | Immutable fp32 numpy generations; SQLite truth and epoch validation | Crash/corruption/invalidation tests pass; measured performance experiment recorded separately |
| Watch / serve | Explicit commands; no automatic installation | Existing power/thermal/memory holds retained | Final-source paired 30-sample latency measured; full ten-minute controlled/real watch evidence pending |
| Excerpt cards | `distill` | Verbatim original text; existing calibrated clustering retained | Mock hard-variant gate retained; real 100-document/eight-topic quality inputs missing |
| Local delivery | Wheel/sdist and static native tar | Actually exercised on macOS arm64/Python 3.12 | Final snapshot-005 build, isolated real OCR/embedding, SDK and old-index upgrade/restore smoke pass |
| Hosted delivery | Workflows written, manual live entry | No configured remote or hosted runner | Local actionlint passes; hosted verification remains MISSING |
| QMD / Recoll comparison | Isolated local installations and common-text diagnostic | QMD 2.8.3; Recoll 1.44.2; no user config writes | [Comparison](COMPARISON.md); incomplete seed cannot establish a market quality ranking |

## Data and release gates

The fixed product target is 420 questions: only 28 independently annotated seed
questions from twelve permitted original sources currently exist. No sealed
holdout has been opened or invented. The missing 392 questions include all real
product PDF/media sources and a valid grouped split. New independent 80-question
answer/grading runs and real card labels are also missing. Those gaps keep the
relevant tasks at IMPLEMENTED/MISSING, even when mechanical checks pass.

Search admission parameters are [preregistered](../evals/search_parameters.json)
with at most three arms and the original control. `selected=null` is deliberate;
the draft seed cannot calibrate a production winner. Optional quality features
report their unvalidated experimental state and remain off by default.

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
   do not perform this upgrade.
3. A changed chunker/extractor marks existing files stale. Explicit ingest refreshes
   them from the originally chosen roots; no new scope is inferred.
4. To use an older program, restore the pre-upgrade backup to a different path.
   Inspect it read-only with the older program before resuming its old-format use.
   Do not point an old writer at the upgraded index. Old open writers are fenced.

The installed-wheel upgrade smoke exercised original v4 → new v6, backup, citation
staleness, future-schema refusal and restoration with the original program. It is
a mechanical compatibility result, separate from retrieval accuracy.
