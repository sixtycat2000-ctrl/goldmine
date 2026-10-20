# Real distill audit inputs

No qualifying frozen real-topic dataset is present yet. The current product.v4
seed contains 12 sources; it cannot establish the required 100-document,
8-topic quality result. Synthetic canary variants remain explicitly synthetic.

Prepare a separately reviewed labels.json before clustering. Its exact schema
is in scripts/distill_eval/labels.py: schema1, suite_id distill.real.v1, state
FROZEN, evidence_mode real, a timezone-aware frozen_at, labeling_protocol
independent-before-clustering, named reviewers, a hash-pinned frozen product
manifest, at least8 distinct topic IDs and document labels.

Each document has exactly path, topics, role, split, case_tags. Paths are
portable paths present in the licensed product manifest. Roles are topic
(one topic), cross_topic (multiple), or noise (none). At least100 base files
must include noise/cross-topic examples; every topic needs8 independent source
groups. Exactly10 separately frozen files form the incremental split. Include
at least2 language:* tags and explicit paraphrase/duplicate cases. Topic
labels must never be inserted into indexed source text.

Ingest the base sources with the real llama backend, copy that index, and ingest
the10 additional files into the copy. Existing segment IDs/text/vectors must
remain intact. The evaluator copies both inputs before doing any work:

```sh
python3 scripts/eval_distill.py --report-real /absolute/base.sqlite --labels /absolute/labels.json --corpus-root /absolute/indexed-root --incremental-db /absolute/plus-ten.sqlite --json --out reports/execution/distill-real.json
```

The strict mode returns0 for complete passing evidence,1 for measured quality
failure,2 for missing/invalid inputs. It retains raw cards, source/citation spans,
fixed first20 card support verdicts, distributions, inherited tau, reclustering
ratio and every membership difference against same-tau full recomputation.
Whitespace folding is declared; case and original character offsets are kept.
Deletion and old-citation checks use disposable copies only. Original input
databases must retain their hashes. Default --report-real without labels stays
diagnostic and labels its quality status MISSING even when it exits0.
