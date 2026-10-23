# Source-bound public retrieval regression

The existing `eval_public.py --gate` and published BEIR BM25 values remain
advisory. They are not interchangeable with the source-bound raw regression
consumed by `retrieval.public`. Neither public path reads product sealed holdout.

Prepare all three pinned public ZIPs deliberately before evaluation. Licenses
and exact size/SHA pins are in `evals/public_manifest.json`; keep dataset bytes
outside the repository. Acceptance never downloads data or prepares an index.
Use independent cache roots for the clean original production checkout at
`769483752f49fb0bf8241dca778b1895b9a1b70b` and the current candidate. Each root needs
`beir/scifact.zip`, `beir/nfcorpus.zip` and `beir/arguana.zip`.

```sh
python3 scripts/capture_public.py --prepare --role candidate --cache-root /absolute/candidate-cache --production-root /absolute/goldmine --model /absolute/model.gguf --server /absolute/llama-server --out reports/execution/public-candidate-prepare-001
python3 scripts/capture_public.py --role candidate --cache-root /absolute/candidate-cache --production-root /absolute/goldmine --model /absolute/model.gguf --server /absolute/llama-server --out reports/execution/public-candidate-001
```

For the original arm, use `--role reference` for both captures and point
`--production-root` to the separate original checkout. The capture tool adds
the required `--reference-code` to the actual evaluator command. Preparation
refuses existing corpora/indexes; scoring opens
the separately prepared checkpointed index read-only. No model or ZIP download
is implicit. Missing assets produce exit2/MISSING; `--diagnostic --backend mock`
only exercises mechanics and is refused by the formal adapter.

Preparation is a separately captured, actual `eval_public.py --raw --prepare`
command. Its stdout records all three finalized, checkpointed database SHAs,
ZIP member/source identities, model, native server and runtime. Acceptance
requires preparation to finish before scoring, and those finalized database
bytes must match the read-only scoring inputs. Editing a database while keeping
its self-declared metadata cannot substitute for an actual preparation receipt.

The raw/v1 stdout and command receipts bind the evaluator, production modules,
model, native server, runtime dependency versions and each actual index. The
report includes every pinned test query for lexical, semantic, RRF and hybrid,
with fetched doc IDs and the top ten after removing a self-document. Corpus,
queries and qrels are hashed from the actual pinned ZIP members. Missing query
text, omitted queries/arms/datasets, duplicate or unknown result IDs, changed
indexes and inconsistent hashes are errors. Dangling qrels remain in IDCG.

Candidate compatibility uses the validated full production file map and its
source SHA256; an unchanged-content Git commit does not invalidate that index
or raw result. The observed commit remains in provenance. The original reference
still requires the exact clean historical commit. Index metadata has an explicit
identity domain; old ambiguous index metadata requires deliberate preparation.

Set `public.reference_prepare`, `public.candidate_prepare`, `public.reference_run`
and `public.candidate_run` in schema2 acceptance inputs to the four separate
captured `manifest.json` paths and their actual SHA256. Also pin
`public.cache_manifest`: its datasets map contains each original ZIP as a
relative `path`, exact `bytes` and `sha256`. The disk-only adapter reads those
ZIP qrels, recomputes every contribution and aggregate, and applies the fixed
per-dataset hybrid nDCG@10 absolute-drop ceiling of0.02 against the original arm.
An exit0 advisory summary cannot substitute for this evidence.

These public regression checks do not establish product corpus, answer quality,
real card quality or hosted release acceptance. No public quality result is
claimed until the complete real captures have been produced and replayed.
