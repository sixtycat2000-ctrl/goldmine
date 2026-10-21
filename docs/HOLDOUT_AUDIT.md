# One final joint holdout audit

Daily development and task checks use `dev`. The release adapter never launches
models, opens a new exposure, resets a ledger or fills missing evidence. With no
explicit `product.sealed_holdout_audit`, it returns exit2/MISSING. This tooling
has mechanical tests; no actual new-corpus holdout quality is claimed.

Use the frozen complete real bundle and its pinned reference environment. Both
indexes must already be independently prepared from their verified materialized
build receipts. The original source is a clean separate checkout at
`769483752f49fb0bf8241dca778b1895b9a1b70b`. Keep source bytes and capture xattrs;
the builder and readback verify both. Checkpoint the indexes deliberately before
freezing so their single-file SQLite SHAs remain fixed.

First capture the original reference with `capture_holdout.py seal-reference`.
It invokes the registered evaluator with `--reference-code --split holdout
--seal-reference`, stores `sealed/run.json` as0600 inside a0700 directory, and
prints only its seal. Do not open either aggregate or per-query reference
results while choosing the final candidate. No reference metric is used to
generate the freeze.

```sh
python3 scripts/capture_holdout.py seal-reference --bundle /absolute/frozen-bundle --build /absolute/reference-build --db /absolute/reference.sqlite --production-root /absolute/clean-original --model /absolute/model.gguf --server /absolute/llama-server --parameters /absolute/reference-parameters.json --out /absolute/original-seal-001
python3 scripts/build_product_corpus.py init-ledger --out /absolute/new-release-ledger
```

Fill [holdout-spec.example.json](execution-plan/holdout-spec.example.json) using
actual absolute paths and the actual seal manifest SHA. Every declared arm has
a separate parameter JSON. `fast` is mandatory; enabled reranking adds the
same-candidate `quality` control, and enabled multi-query also adds `multi`.
`quality` differs from fast only in explicit quality/rerank flags. `multi`
differs from quality only in `multi_query=true`. All arms use the identical
candidate source, embedding/index and materialized corpus. At most three fixed
arms are permitted. Disabled experiments add no unexecuted model/arm claim.

```sh
python3 scripts/capture_holdout.py freeze --spec /absolute/reviewed-spec.json --out /absolute/final-freeze.json
```

The freeze binds the full production file map/source hash, observed commit,
evaluator/validator/dependency files, runtime, target bytes, model/native server,
independent original/candidate index SHAs, both build receipts, original private
seal, entire arm registry and initialized-ledger anchor. An unchanged-content Git
commit remains compatible; production/tool/dependency/parameter/asset drift is
rejected. Freeze validates the seal and hashes private report bytes without
parsing its metrics. Review this concrete freeze before the final audit.

The following command is the deliberate one-time exposure. It is not part of a
routine task check or a missing-input remediation command:

```sh
python3 scripts/capture_holdout.py audit --freeze /absolute/final-freeze.json --out /absolute/final-joint-audit-001
```

The ledger consumes `prior_exposures=0` before entering any candidate arm. Each
arm start is appended before its first query; completion binds its raw report
and actual returned quality-execution metadata. All predeclared arms share that
single audit. Failure/crash leaves the exposure spent even with no completed
rows. No retry, new ledger, arm removal, parameter tuning or winner selection
can restore an unseen holdout. A new release-quality claim after exposure needs
independent unseen sources. Replaying the same completed raw artifact is read
only and does not spend another exposure.

Pin the actual joint command `manifest.json` and SHA as
`product.sealed_holdout_audit`. Pin the current `holdout_exposure.jsonl` and SHA
as `product.exposure_ledger`; keep the freeze, private original report, native
assets, indexes, source checkout and build receipts at their recorded paths.
Use the same completed audit in every final release/profile replay.

`release.holdout` verifies the command, seal, full hash chain and all heldout
IDs, recomputes report scores from frozen labels, and reads every claimed span
back from the actual SQLite source. Genuine original schema4 uses a narrow
read-only audit facade; proven unknown original file types remain fixed-denominator
misses. Recognized-but-missing OCR/ASR/source content is MISSING, never exempted.

Fast uses the existing fixed target table. Enabled quality arms retain the
noise threshold, the existing degradation ceiling, and a predeclared pooled
simple-query Top1 comparison over semantic/CJK/lookup/PDF/media classes with
source-group bootstrap seed20261021 and10000 repetitions. A wholly negative
95% interval rejects a drop; fewer than12 independent groups is MISSING. The
reranking quality arm additionally needs nDCG@10 gain of at least+0.02 against
the identical same-candidate fast/off control, using the complete answerable
fold and the existing P1-07 target (`product.rerank_ndcg_gain`). Requested flags
alone prove no execution: usage observations must have exactly one typed row
per heldout query, the reranker must actually execute, and enabled multi-query
must actually expand at least one query with consistent plan counts. The
multi-query arm additionally needs the existing compound complete-fact target
and gain against its identical single-query quality control. These engineering
rules are frozen before exposure; they do not establish population guarantees.

Published/recomputed PASS is available only after complete real captures and
all checks succeed. Synthetic ledger/fixture tests establish no release quality.
Other local and hosted release requirements remain separate checks in
[ACCEPTANCE](execution-plan/ACCEPTANCE.md).
