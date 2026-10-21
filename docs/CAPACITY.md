# Replaying synthetic capacity measurements

`scripts/bench_scale.py --full-cli` measures capacity with deterministic random
vectors and stored synthetic English FTS bodies. The in-process mock backend is
a query/tokencount control. These results say nothing about retrieval quality,
real model latency, OCR/ASR, or real tokenizer behavior.

The registered gate requires all 12 combinations of 10k/100k/250k/500k vectors
and 1/10/100 segments per file. It keeps seed 20261021, 768 dimensions, chunked
float32 generation and fp16 SQLite storage. The exact original source builds its
native schema4 database; the current source builds schema6. Their file/segment
IDs, vectors, modalities, FTS source bodies, locations and common logical digest
must agree. Physical database hashes differ between native schemas.

Run this only after source changes and other model jobs have stopped. Supply a
new output directory and the exact original source checkout/snapshot. For example:

```sh
python scripts/bench_scale.py --full-cli \
  --reference-root reports/execution/P2-02/scale-snapshots-001/reference \
  --candidate-root . \
  --out reports/execution/P2-02/full-capacity-001
```

Add `--cache-experiment` to explicitly build and enable the derived vector cache
inside each disposable candidate database. The reference remains SQLite-only.
This does not enable caching in a user index. The report recomputes the registered
250k trigger and the 30% cold-load/RSS benefit thresholds from the raw phase
measurements. A failed benefit gate cannot be hidden by a summary PASS.
An uncached run that triggers the registered capacity condition leaves the
required cache comparison `not_evaluated` and the complete gate MISSING. The
summary's `cache_benefit_pass` is null until all triggered cases have actual
paired cache measurements; only measured required benefits can make it true.
If no case triggers, the cache gate is `not_required`, with a null benefit flag.

Every case uses independent, exited builders and separate reference/candidate
query processes. There are three warmups and at least ten samples for vector
load, same-Store hot cache, cosine, modality zscores, file aggregation, admission,
sorting and details. Python import time is recorded separately.

For each of three CLI forms—search JSON, pack JSON and emitted human pack—the
runner captures three warmups plus at least 30 samples per arm per temperature.
It alternates reference/candidate order for each identical query, then reverses
that order on the next query. Cold means a fresh Python process. Warm means one
pre-imported Python process per case/arm calling the actual `goldmine.cli.main`
entrypoint; every call still opens and closes its own Store and mock backend.
No persistent Store cache or model server is implied by warm CLI measurements.
OS caches are never flushed. Full database hash scans outside the measured
interval can warm filesystem pages, so neither temperature is a cold-disk claim.

The fixed capacity controls use semantic scoring, gate0, limit10, context1 and
pack budget4000. Gate0 ensures that even the tiny mechanical fixtures exercise
nonempty details and pack bodies. It changes no product/relevance acceptance
threshold. Unrounded vector rankings must match at absolute error<=1e-5; CLI
scores are checked at their actual rendered precision with the same error bound.
Top file/segment IDs, emitted source/context bodies and cold/warm results must
agree. The actual human pack text is checked against its JSON source records and
the mock backend's count (`len(text)//4`), including the whole envelope.

Raw command argv, actual entrypoint kind, cwd/config/source, stdout/stderr, exit,
monotonic interval and per-process RSS accompany every attempt. Darwin's raw RSS
unit is bytes; Linux's is KiB. A warm process's high-water mark is explicitly
scoped to that one arm/case, and never reused for another arm or fixture. Failed,
timed-out or degraded attempts remain in the denominator and make the evidence
incomplete. No retry discards an unfavorable attempt. A worker interruption
retains the partial sample ledger and cannot produce a complete case.
Startup and request reads share a fixed absolute deadline for the entire JSON
line, including partial output; both input and output lines are capped at1MiB.
Spawn/startup/request failure kills and reaps the owned process, closes its
streams and saves an explicit failed lifecycle receipt and all partial output.

Every CLI call has full SQLite file SHA/size and WAL/SHM/journal snapshots before
and after. Database changes or nonempty write journals are rejected. SQLite's
transient SHM read marks may change, including in the unmodified original CLI
whose constructor opens a read/write connection. Returned records have separate
actual SQLite FTS/vector readback proofs. Common logical content is also checked
before and after the complete case. Neither original source nor its Store is
monkeypatched.
The emitted human pack grammar is validated in full: ordered canonical file and
location headers, complete bodies, source footer and candidate citation IDs
derived from the actual fixture UUID and immutable row identity. Equal-length
substitutions of paths, locations, citation digests or source order are rejected.

Fixtures are built, measured and removed one case at a time by default. Only
these generated databases and their explicitly built cache are removed; all
raw receipts, native schema identities, source/readback proofs and deterministic
reconstruction inputs stay in the output. `--keep-fixtures` retains databases for
manual diagnosis and needs substantially more disk space for all12 cases. Input
source snapshots and tool hashes must remain available and unchanged for formal
validation. Keep reports at their recorded execution location: raw argv/cwd paths
are historical facts and are never rewritten when copying evidence elsewhere.

Wire the final `manifest.json` and its SHA into the existing
`evidence.scale.{manifest_path,sha256}` fields in acceptance inputs. The
`capacity.scale` check is mandatory for `task-check --task P2-02`, `local_rc` and
hosted release. It recomputes all cases and raw metrics, binds the reference to
the pinned original revision and the candidate to current production content.
Source/tool changes, missing forms, missing scales or stale fixtures yield
MISSING. Output text saying PASS is never evidence by itself.

The legacy mode and historical `scale.v1` reports cover vector capacity only and
cannot pass this complete gate. A diagnostic subset such as `--scales 121
--distributions 10` can exit0 when mechanics agree, while its manifest remains
MISSING for the12-case release requirement. No full-size measurements are
performed by the focused mechanical tests.
