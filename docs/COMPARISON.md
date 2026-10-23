# Search comparison

The current result is a **diagnostic lexical run**, not a product quality ranking
or a passed release gate. Three actual installed systems ran all the same 28
draft questions over frozen text views of the same 12 MIT-licensed project
source/document files. The report preserves every query, including misses and
noise matches. No embedding, expansion or reranking models were loaded.

| System and measured configuration | success@1, answerable n=22 | success@5, answerable n=22 | MRR@10, n=22 | nDCG@10, n=22 | Empty noise results, n=6 |
|---|---:|---:|---:|---:|---:|
| goldmine frozen source, lexical fast, rerank off | 12/22 | 17/22 | 0.638 | 0.668 | 2/6 |
| QMD 2.8.3 (`facd35e`), `search`, BM25 | 1/22 | 3/22 | 0.066 | 0.083 | 6/6 |
| Recoll 1.44.2 + Xapian 2.1.0, default query language | 2/22 | 2/22 | 0.091 | 0.091 | 6/6 |

These numbers come from `reports/execution/P2-08/lexical-seed-002/report.json`.
All systems completed 28/28 requests with no parser or mapping failures in that
run. Goldmine's broader matching retrieved more relevant files and also returned
material for more noise queries. Default lexical query semantics differ: this
does not isolate one ranking algorithm. The two lookup questions had first-rank
success 2/2 for goldmine and Recoll and 0/2 for QMD; those misses remain recorded.
The initial `lexical-seed-001` attempt retains a goldmine output-adapter error,
corrected by reading its actual JSON list output before the complete second run.

The 22 answerable questions cover semantic intent, CJK, lookup and compound
intent, but all three systems here ran **lexical retrieval**. A category called
`semantic` does not mean semantic retrieval was measured. The six noise queries
have no relevant documents and are reported separately. With only project-owned
sources and these draft labels, the results cannot establish general personal
search accuracy, a statistically significant advantage, or the 420-question
product gate. The full real PDF/media corpus and sealed grouped folds remain
missing; no holdout was opened for this comparison.

| Input view / mode | goldmine | QMD | Recoll |
|---|---|---|---|
| Frozen common text (`.md` conversion of 12 sources) | Ran lexical-only ingest and all 28 queries | Ran indexed collection `common` and all 28 queries | Ran isolated index and all 28 queries |
| Native original code/documents | Not measured in this comparison | Not measured; common view explicitly converted source files to Markdown | Not measured; native extraction/filter configuration must be recorded |
| Native PDF, scanned PDF, images, OCR, ASR and metadata | Not measured | Not measured; conversion requirements must be recorded | Not measured; installed filters/dependencies must be checked |
| Semantic / quality mode | Fast semantic and quality rerank/multi-query not measured | `vsearch` and recommended hybrid `query` not measured | Optional semantic capabilities/configurations not measured |
| Common verified page/time/line accuracy | Not comparable in these output adapters | Not comparable | Not comparable |
| Cold/warm latency, peak RSS, incrementals, dependency failures | Not measured | Not measured | Not measured |

QMD's installed help explicitly distinguishes keyword `search`, vector `vsearch`
and hybrid expansion/reranking `query`. Its lexical result is not a measurement
of the recommended hybrid mode. Recoll's unmeasured semantic options must not be
described as absent. Official starting points are [QMD](https://github.com/tobi/qmd)
and the [Recoll manual](https://www.recoll.org/usermanual/usermanual.html). Exact
installed help, package lock, binary/config hashes, versions and the official
Recoll disk-image URL/SHA are pinned by
[`evals/comparison/manifest.json`](../evals/comparison/manifest.json). Recoll emitted
warnings about its missing example `backends` file and optional `aspell`; raw
stderr is retained rather than hidden. Lexical requests nevertheless exited 0.

Run the diagnostic after restoring the explicit private installations and common
view specified by the manifest:

```sh
python3 scripts/compare_search.py --validate --json
python3 scripts/compare_search.py --diagnostic --out reports/execution/P2-08/lexical-seed-003 --json
python3 scripts/compare_search.py --recompute reports/execution/P2-08/lexical-seed-002/report.json --json
python3 -m pytest tests/test_compare_search.py -q
```

The executable uses argv arrays and an explicit argument separator, an owned
working directory, isolated XDG paths and explicit Recoll config/data directories.
Goldmine source is copied and hashed before indexing, then imported in fresh
subprocesses from that frozen copy. Its index is lexical-only with zero vectors;
the mock backend option prevents external model use and does not generate or
replace any retrieved text. Competitor queries use their previously prepared,
explicit private indexes. No user index or global configuration is changed.
Each process has a wall-clock timeout, POSIX CPU/output/file-descriptor limits,
and an owned process group for cleanup. Per-command wall times are operational
receipts, not the repeated benchmark required for performance comparisons.

For every question, `raw/` retains stdout/stderr, the report retains exact argv,
exit status, engine ranks, URI identities, file-rank mappings, qrels and metrics.
QMD URIs and Recoll base64-encoded file URLs must map through the frozen document
list; unknown identities fail that request and stay in its denominator. Duplicate
file hits collapse at first appearance. nDCG uses one maximum relevance grade
per expected file. Missing requests are counted explicitly and contribute zero
to the fixed-denominator lower-bound aggregates. The saved report can recompute
all table values without rerunning the software. Real nonempty outputs are also
checked in as parser fixtures under `tests/fixtures_data/comparison/`.

P2-08 remains incomplete for native views, semantic/quality modes, full audited
data, one final sealed comparison, and repeated performance/resource/update
measurements. Those require separate recorded runs; this diagnostic never
updates the historical project baseline or claims release verification.
