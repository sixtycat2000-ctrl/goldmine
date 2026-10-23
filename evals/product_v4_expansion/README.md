# Product v4 expansion: source-curated 420-question corpus

This is a distinct corpus from the exposed 28-question diagnostic seed in
`../product_v4`. The seed and its historical comparison results are preserved.
This corpus does not assert any retrieval quality result merely by existing.

The 420 questions cover 60 each of semantic, CJK, exact lookup, PDF pages,
media text, compound evidence, and unsupported/noise requests. All 122 indexed
source files are real published material with pinned bytes and licence notices.
They include original code/documentation, unmodified manual page excerpts,
faithful historical scanned pages, real browser captures, publisher audio, and
original photographs with real capture metadata. No generated question cards,
TTS, fake EXIF, or synthetic documents are counted as real coverage.

`manifest.json` binds source assets, independent oracle views, labels, dependency
lock, grouped folds, and the fixed independent label audit by SHA-256. It stays
DRAFT until all requirements pass. `curation-summary.json` records mechanical
counts and known source discoveries. Authors and reviewers are AI agents; no
human review or professional credential is claimed.

- Only `assets/` entries named by the manifest become indexed corpus files.
  Queries, answer labels, source transcripts, licence files, original full PDF
  archives, and curator code never enter the index.
- Build receipt v2 also transports the already declared `kind=screenshot`:
  on macOS it materializes the fixed true screen-capture xattr for all 20 real
  browser captures, never for ordinary photos. It verifies the exact native
  attribute and unchanged asset SHA, and binds that receipt to each index.
  Unsupported filesystems/platforms fail explicitly. Both versions use the
  same materialized corpus. This tests declared screenshots, rather than
  automatic detection of arbitrary unmarked PNGs. Initial byte-only builds
  lost this classification and produced missing OCR; those failed capability
  checks are retained. The protocol correction preceded every relevance run.
- `views/` contains exact source-grounded text/metadata. Audio preserves the
  publisher's uppercase transcript and verified whole-utterance duration;
  no fabricated word timings. Scanned-page and screenshot facts were checked
  visually against actual pixels. Raw OCR and transcription corrections remain
  available beside the authoring notes.
- Related manual pages, screenshots, source files, duplicate views, and compound
  evidence stay in one source group. `splits.json` uses seed 20261021 and source
  groups only; it is computed before any relevance score. No group is split to
  meet a quota. The fixed audit samples eight questions per class.
- `text/`, `compound/`, `media/`, `root/`, and `noise/` retain the original curator
  shards, source acquisition receipts, attribution, and authoring records.
  The assembled manifest paths are portable; absolute acquisition-machine paths
  in raw receipts are historical context, not runtime dependencies.
- `provenance/original-pdfs/` preserves the exact original publisher PDFs even
  when the live stable download URL later changes. The asset-to-original page
  maps and exact scanned-pixel proofs are in `root/provenance/pdf-pages.json`.

The source audit exposed capture/modification-time confusion in seven photos,
unsupported `.rst` ingestion, historical two-column OCR errors, and an ASR
casing/punctuation proof gap. Generic metadata/format fixes and the explicit
pre-ranking evidence protocol were reviewed before scoring. These source-aware
engineering discoveries are disclosed; this is not a blind human evaluation.
Actual OCR mistakes remain in extraction output and must not be rewritten to
match the oracle.

Validate without running retrieval:

```sh
python3 scripts/eval_product.py --bundle evals/product_v4_expansion --validate --json
```

Build only the manifest's source bytes in a new directory outside this bundle:

```sh
python3 scripts/build_product_corpus.py build \
  --bundle evals/product_v4_expansion --out /absolute/new/product-corpus
```

Run normal development only on `dev`. Original reference preparation must use
an independent clean checkout at `769483752f49fb0bf8241dca778b1895b9a1b70b`
and the pinned environment. Unsupported original formats count as misses at the
fixed denominator after verified original preparation; missing tools and corrupt
indexes remain MISSING. Holdout stays reserved for the single final frozen
candidate audit under the exposure-ledger contract.

`python3 evals/product_v4_expansion/compile.py` only assembles a DRAFT from already
authored shards and refuses to overwrite a FROZEN corpus. It never invokes a
model, indexes source files, chooses labels from retrieval success, or scores a
query. Treat a frozen manifest and its inputs as immutable.
