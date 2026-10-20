# product.v4 — audited seed, full quality suite still missing

This is a **DRAFT**, not a completed 420-question benchmark. It currently contains
28 individually annotated questions from 12 MIT-licensed project code/documents
at production commit `769483752f49fb0bf8241dca778b1895b9a1b70b`. Source and labels
were copied/fixed and checked against their exact source spans before any
candidate relevance run. No official product baseline or holdout result exists.

The seed includes 12 semantic, 6 Chinese/mixed/Japanese/Korean, 2 filename lookup,
2 cross-file compound and 6 noise questions. **392 questions remain missing**,
including every real PDF-page and media-text item. The seed has insufficient
independent source groups and no frozen dev/holdout split. Protocol tests use mock
embeddings only and provide no retrieval-quality evidence.

```bash
python3 scripts/build_product_corpus.py validate --bundle evals/product_v4
python3 scripts/eval_product.py --validate --bundle evals/product_v4 --json
```

Both currently exit **2 / MISSING**, listing all fixed-count, language, source
group, media, PDF and split gaps. Do not replace missing material with repeated
templates, relabel canaries as real, shrink denominators, or create a baseline
from the candidate implementation.

## Portable input layout

Only `assets/` contains candidate source bytes. `views/` contains independently
frozen canonical text/metadata oracles; it is never indexed. Questions, expected
facts, label review, environment lock, manifest, and this README stay outside
the built corpus. Original MIT copyright/license is retained under `licenses/`.
Each source records origin revision/path, real/canary distinction, byte hash,
license and generation method. All input references are relative; escaping
symlinks, traversal, case-folding collisions and file/directory collisions fail.

Manifest fields are schema, suite_id, state, evidence_mode,
reference_source_commit, reference_environment, sources, queries, splits and
label_audit. `sources` contains path, asset `{path,sha256}`, kind, source_group,
parents, provenance and views. A view contains id, path, sha256, evidence_type,
location, storage_field and normalization=`nfc-lf-v1`. Source relations, exact
copies, near duplicates and shared compound sources are merged before splitting.

Questions use the plan's id/primary_class/tags/q/expect/required_facts/needs/
source_group schema. Each expected evidence additionally binds **view_id**, so
character offsets identify one exact frozen oracle rather than an ambiguous
page/transcript/metadata field. Span offsets are zero-based, half-open Unicode
character positions; text and SHA must match that slice. Typed page/line/second
locations reject unknown fields, wrong units and impossible ranges. Plain text
oracle views are checked against actual pinned source lines.

Facts use AND/OR alternatives of evidence IDs. A pack covers a fact only if a
complete sufficient alternative is present in material body text and can be
read back from its actual indexed segment. Source/filename hits, query headers,
source tables, wrong times/pages, and undeclared supporting citations receive no
fact credit. Rankings remain one file per hit; duplicate files fail and multiple
locations in one file cannot inflate document nDCG above 1.

The exact-lookup leakage exception is narrow: a single lookup identifier/path or
an exact annotated metadata name may naturally occur in sources. Natural-language
semantic/CJK/PDF questions and answer/compiler/grading files remain forbidden.

## Explicit mechanical use of the incomplete seed

Choose new paths outside this bundle. These commands never create quality evidence:

```bash
python3 scripts/build_product_corpus.py build --bundle evals/product_v4 --allow-draft --out /tmp/product-seed-build
python3 scripts/eval_product.py --prepare --diagnostic --backend mock --build /tmp/product-seed-build --db /tmp/product-seed.sqlite --json
python3 scripts/eval_product.py --diagnostic --backend mock --build /tmp/product-seed-build --db /tmp/product-seed.sqlite --split dev --json
```

Output says diagnostic_only=true, quality_evidence=false and retains all fixed
420/360/60 requirements and missing assets. `--gate` refuses mock/draft reports.
No script downloads sources/models, invents captions or transcripts, scans user
folders, silently imports during evaluation, or edits the historical baselines.

## Complete the real suite before quality verification

1. Curate the remaining real permitted sources and independently written queries;
   keep each source's license/revision and explicit synthetic-canary labels. Meet
   all category/language/media counts, per-file maximum 8 and source-group minimum
   12. Scanned/mixed-PDF cases must exercise actual OCR; ASR requires actual audio
   with time-labeled text and photographs require real named metadata fields.
2. Freeze canonical views, question positions and fact alternatives before looking
   at candidate ranks. Audit the deterministic eight-per-class sample from the
   real source and preserve review notes. The existing seed-only review does not
   substitute for review of new questions.
3. Generate a new grouped split with `build_product_corpus.py split --out NEW_FILE`.
   It uses seed 20261021 and no retrieval scores. Both folds need >=25 per class
   and >=8 per media subtype. Commit its identity and complete input hashes;
   change the manifest to FROZEN only after all checks pass. The validator never
   selects a favorable split from observed rankings.
4. Build source assets into a new directory, then explicitly prepare separate
   reference/candidate databases. Formal llama runs require explicit existing
   `--model` and `--server` paths and the frozen Python/numpy environment. Missing
   real OCR/ASR/metadata content is MISSING, not a skipped question.
5. The first reference must run with `--production-root` selecting an independent
   **clean original-commit checkout** and `--reference-code`. Imports and every
   production source file are checked; current candidate modules cannot shadow it.
   Ordinary development sees dev only. `--reference-code --seal-reference --split
   holdout --out NEW_PRIVATE_DIR` stores original holdout results privately and
   prints only the seal hash/identity, with no scores or individual results.
6. For the final joint release audit, initialize one new exposure ledger using
   `build_product_corpus.py init-ledger --out NEW_LEDGER_DIR`. Freeze JSON fields:
   schema=1, purpose=final_joint_release_audit, frozen_at, bundle_identity,
   candidate_code_sha256, parameters_sha256, dependency_lock_sha256 and
   ledger_anchor_sha256. The code and parameter hashes use the runner's recorded
   identities; the dependency hash is the actual pinned environment file SHA.
7. Only then use `--split holdout --freeze FREEZE.json --ledger LEDGER_DIR`.
   Preflight checks run first; exposure is appended before any ranked query.
   Failure/crash after exposure consumes the attempt. A changed candidate cannot
   reuse the set as unseen. `--deterministic-recheck` permits only the identical
   frozen candidate and appends a recheck receipt. Clearing/editing a ledger or
   replacing its initialization breaks validation; this local hash chain is an
   audit mechanism, not an independently signed authorization service.

`--out` creates a new report directory and refuses replacement. Reports retain
actual context_text, pack record bodies, read-back source spans, per-query scores,
input/model/index/code identities and grouped summaries. Saved results are
recomputed before comparison: edited counts/scores, missing rows, invented spans
and sidecar text absent from actual bodies fail. Paired uncertainty resamples
whole source groups; small source counts cannot establish significant improvement.

The current draft intentionally has no `splits.json` or product reference run.
The missing report is the authoritative remaining-data checklist.
