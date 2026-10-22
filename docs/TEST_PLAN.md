# goldmine — Test Plan

> Implementation update (2026-10-21): see [the current feature status](FEATURE_STATUS.md) and the [P0–P2 execution record](execution-plan/PROGRESS.md). Older design and benchmark sections below retain their historical scope; they are not final candidate verification.

Principle: **offline-first**. The default `pytest` run needs no network, no llama-server, no
model files — everything goes through the deterministic `mock` backend.
Live tests exist as a separate, explicitly-gated tier. `GOLDMINE_HOME`/`GOLDMINE_CONFIG` are
redirected to tmp paths in every tier so tests never touch real user data.

---

## 1. Tiers

| Tier | Name | Gate | What runs | What must be installed |
|---|---|---|---|---|
| 1 | unit | none (default) | pure functions: rules, decode, chunker, z-scores, RRF, TF-IDF, clustering determinism, fingerprints, fp16 round-trip | numpy, pytest |
| 2 | integration (offline) | none (default) | Store+Pipeline+Searcher+Distill end-to-end on fixture corpora with `MockBackend`; `backends/llama.py` client logic against `tests/fake_llama_server.py`; crawl bounds against a local `http.server` fixture | numpy, pytest |
| 3 | live | `GOLDMINE_LIVE=1` | real `llama-server` spawn + real embeddings (dim=768 asserted); real pdftotext/ffmpeg round-trips | brew llama.cpp, model GGUFs, poppler, ffmpeg |

Tier 3 marker: `@pytest.mark.live` registered in `conftest.py`;
`pytest -m "not live"` is the CI default; `GOLDMINE_LIVE=1 pytest -m live` runs tier 3.
Live tests skip (never fail) when a prerequisite is absent, printing the doctor-style fix
hint — consistent with the project's honesty rule.

---

## 2. Fixtures

Committed or built-at-test-time, all tiny (KB scale):

| Fixture | How | Used by |
|---|---|---|
| `tests/fixtures/tiny.pdf` | committed hand-written 1-page PDF with a known sentence + a known rare code (`K7Q2LM`) | pdf extraction, thin-text, eval corpus |
| `tests/fixtures/scan_like.pdf` | committed 1-page PDF whose "text layer" is < 200 chars | thin-text/no-vector behavior |
| `tiny.docx` | **built in conftest** with `zipfile` + minimal `word/document.xml` (`w:p`/`w:t`) — no binary blob in repo | docx stdlib extraction |
| `page.html` | committed: `<meta charset="windows-1252">`, entities, script/style blocks, nested tags | HTML strip, charset handling |
| `page_utf16.html`, `notes_latin1.txt`, `bom.txt` | built in conftest from Python strings | decode matrix (V5b) |
| `sine.wav` | built in conftest: 1 s 16 kHz mono PCM16 sine via `wave`+`math` | media windowing, mock audio embed |
| `secret.pem`, `.env`, `app.min.js`, `LICENSE` | built in conftest (dummy content) | rules: secrets/minified/license |
| `node_modules/`, `__pycache__/` dirs | built in conftest | walker exclusions |
| synthetic corpus (eval) | built in conftest — see §5 | relevance harness |
| `tests/fake_llama_server.py` | stdlib `http.server`: `GET /health` → 200 (after `/ready-at` delay file check), `POST /v1/embeddings` → deterministic vectors from a sha-based hash of input text; flags `--delay`, `--fail-first N`, `--wrong-dim` to exercise probe/retry/error paths | tier 2 llama-client tests |
| crawl fixture server | `http.server` in-thread serving a 3-page chain (a→b→c), an off-host link, a robots.txt disallow, a 429 endpoint (first 2 hits), a 10 MB file | crawl bounds (V5e) |

Fixture rule: no fixture may require network, sudo, or a tool that isn't in tier 1–2's
install list. Where a test needs "pdftotext missing", it monkeypatches `shutil.which`.

---

## 3. Unit tests (tier 1) — what each pins down

- `test_rules.py`: every extension set decision (doc/code/skip/ignore), secrets (pem, key,
  `.env`, `id_rsa`, `.netrc`), minified (`x.min.js`), license prefixes, `.ts` vs
  MPEG-TS sniffing (crafted 600-byte heads), folder skip names, `--code` on/off behavior.
- `test_decode.py`: BOM trio, pure-UTF8, declared-charset HTML, invalid-UTF8 →
  windows-1252 lossy **with detail string**, `--encoding` override wins.
- `test_chunker.py`: paragraph boundary preferred; overlap 200; whitespace-break fallback;
  max_chunks cap; `chunk_code` line alignment and `loc/loc_end`; `est_tokens` guard warns.
- `test_search_offline.py` (scoring math with synthetic score arrays): z-score centering;
  modality group < 20 falls back to global mean; std floor 0.02; lookup inversion (0.5/4.0);
  coverage^1.5; kind-hint flat +1 (no stacking); candidates = z≥gate OR coverage≥0.5;
  file-level collapse picks meaning segment iff z≥gate; **bm25 ordering direction**
  (fake FtsRows with negative bm25 values sort best-first); RRF k=60 reference values.
- `test_distill.py`: exact-dup removal; near-dup 0.97 greedy; clustering is **byte-stable
  across runs and input-order-independent given the (fid,sid) canonical order** (run twice,
  compare); centroid = normalized running mean (hand-computed 3-vector case); singleton
  dropping at min_members; refusal beyond max_chunks; TF-IDF hand case; card content
  (verbatim rep text, citations, term list).
- `test_fingerprints.py`: id stability across key order; diff_fields names the changed key;
  build-string change does **not** change the id (D2 refinement pinned by test).
- `test_util.py`: fp16 pack/unpack round-trip vs hand-packed bytes; excerpt; MPEG-TS heads;
  magic sniffing table; `run_tool` timeout kill and output cap.

## 4. Integration tests (tier 2)

- `test_store.py`: DDL creates cleanly; migration from empty; `replace_segments` atomicity
  (crash-injected via exception mid-transaction leaves old segments intact); name-row FTS;
  unchanged detection; prune; counts grid.
- `test_cli.py` (end-to-end via `main(argv)`, tmp `GOLDMINE_HOME`, mock backend):
  - ingest fixture tree → exit 0, summary grid correct, `capability` values as expected per
    file (pdf=content_indexed, image=metadata_only, pem=unsupported, broken.pdf=failed);
  - `ingest --dry-run` writes nothing (DB absent afterwards);
  - backend unavailable + no `--lexical-only` → exit 4; with flag → lexical-only index,
    `status` shows vectors=0;
  - second ingest: unchanged skipped, modified file re-extracted (segment ids change), deleted
    file pruned and counted;
  - fingerprint flip (mock v1 → mock v2 via monkeypatched fingerprint) → search/distill exit 3
    with remediation text; `search --lexical` still exits 0 and labels stale;
  - search human + `--json` shapes (SPEC §3) incl. zero-hit honesty;
  - distill markdown/JSON output shapes; cards stored with fingerprint;
  - `status`/`doctor`/`backends` exit codes (doctor FAIL → 4);
  - `reset --yes` deletes; `reset --reindex` rebuilds.
- `test_llama_client.py` (against `fake_llama_server.py`): readiness wait, port retry,
  batching (N texts → ceil(N/batch) HTTP calls), request deadline → EmbeddingError with
  stderr tail, `--fail-first` restart-once path, `close()` SIGTERM to **only** the fake child,
  bounded log ring; `--wrong-dim` → fingerprint/dim assertion error at start (768 enforced).
- `test_crawl_bounds.py` (fixture server): max_pages, max_depth, off-host skip, robots
  respect + `--crawl-no-robots`, 429 backoff then success, per-page byte cap, politeness
  delay ≥ configured, canonicalization dedupe (trailing slash, fragment).

## 5. Relevance eval harness (small, deterministic, part of `pytest`)

`tests/eval/test_relevance.py` + `tests/eval/corpus.py`.

**Corpus** (built in conftest, no randomness without a fixed seed): 30 synthetic documents
across 6 topics (k8s ops, sourdough baking, TRX50 PC build, Roman history, pandas idioms,
apartment lease law) — 4–6 docs each, 3–10 paragraphs; every doc contains its topic terms;
5 docs deliberately contain a **lookup token** (e.g. `K7Q2LM`, `TRX50`) whose answer lives in
exactly one doc; 3 near-duplicate pairs (one sentence changed) to exercise dedupe; 2 docs that
share vocabulary across topics (hard negatives). 12 queries, each with a known target doc
(plus known topic-sibling set for R@5 credit).

**Protocol**: ingest corpus into a tmp index with `MockBackend` (lexical realism comes from
FTS, not from the mock vectors; the mock is lexically aligned by construction — bag of tokens —
so hybrid/semantic differences remain measurable); run `Searcher` with each scorer:

| scorer | expectation recorded |
|---|---|
| `lexical` | should win lookup queries; reported per-query |
| `hybrid` (default) | primary configuration under test |
| `semantic` | mock-vectors only: sanity floor, not a quality claim |
| `rrf` | reference fusion baseline |

**Reported**: R@1, R@5, MRR per scorer + per-subset (lookup vs non-lookup), printed as a
table in the test log. **Asserted** (guardrails, not quality claims): hybrid R@1 ≥ lexical
R@1 on lookup subset − 0 (lexical may win lookups); hybrid overall R@5 ≥ 0.9 with mock;
no scorer crashes on the zero-hit query ("zzzq" → empty results, exit-0 path). Quality
judgments between scorers are **reported, not asserted** — the harness exists to make the
near-dup 0.97 default falsifiable (DESIGN_DECISIONS §11): the distill clustering
sensitivity run (manual τ ∈ {0.60, 0.70, 0.80} + the calibrated baseline) is a
`@pytest.mark.eval` test that prints cluster count/purity and always passes unless the
pipeline errors. τ itself is no longer a default to falsify — v2.1 R6 CALIBRATES it per
run (docs/v2.1-r6-distill.md); the distill canary (`scripts/eval_distill.py`, wrapped by
`tests/test_eval_distill.py`) enforces purity ≥ 0.70 / ARI ≥ 0.50 / recovery ≥ 6-of-8 /
excerpt fidelity 100% offline on mock, with the F1 `hard` differential (v1 rules FAIL,
v2 PASS) as the real offline algorithm gate; the `--backend llama` run is the
calibration-quality report recorded in the release checklist.

**Distill canary — llama record (release checklist, R6 M3 2026-10-19).** The live
`eval_distill.py --variant main --backend llama` run over the 100-doc fixture
(real EmbeddingGemma vectors, schema v4): tau 0.8500 (raw 0.8903, `tau_source=percentile`,
fallback "no real valley"; pairwise quartiles 0.717/0.757/0.783 — the real high band) →
purity 0.934, ARI 0.734, excerpt fidelity 1.0, determinism PASS; recovery 5/8 FAIL —
six topics render as exactly one card holding 100% of their docs; locksmith + typewriters
(the most lexically adjacent pair) over-merge at the τ ceiling (one 16-doc card at
dominant-share 0.62, typewriters' remainder split off at 5/11 docs). This is the B10/F2
mechanism the design predicted: pairwise-calibrated τ applied to cos-to-centroid (which
reads ~1.1-1.6x higher) over-merges real embeddings. The pre-approved contingency
(D3/F2c) — recalibrate on the centroid scale, `tau_source='centroid'`, one-function
change — is documented and NOT implemented in R6; this record is its trigger evidence.

**Live variant** (`GOLDMINE_LIVE=1`): same corpus re-indexed with the llama backend; the same
table is printed with real EmbeddingGemma vectors; additionally asserts dim=768, vectors
L2-normalized (‖v‖−1 < 1e-3), and query≠document prompt asymmetry (query-prompt vector ≠
raw-text vector, cosine < 0.999). Auto-skips if the binary/model are absent.

## 6. Live tier (tier 3) specifics

- `GOLDMINE_LIVE=1` required; each test double-checks the env var itself (belt and braces).
- `test_live_llama.py`: real spawn (health ≤ 120 s), one text batch embed, build string
  recorded, fingerprint stable across two spawns, close() leaves no `llama-server` child
  (assert via Popen.poll()).
- `test_live_tools.py`: real `pdftotext` on tiny.pdf (exact known sentence), real ffmpeg on
  sine.wav (1 window, ~30 s cap not hit), video frames on a 3 s generated color video
  (ffmpeg-generated fixture), each skipping with fix hints if tools absent.

## 7. Acceptance criteria (v1 exit checklist)

1. `pytest -m "not live"` passes on a clean machine with only numpy + pytest installed.
2. With **zero** external tools and no backend, `goldmine ingest --lexical-only` produces a
   searchable index and `goldmine status` reports the exact per-file capability grid.
3. With brew llama.cpp + the pinned GGUF, `GOLDMINE_LIVE=1` tier passes: 768-dim, normalized,
   prompt-scheme recorded; `search` returns the eval corpus targets in top 5 for ≥ 10/12
   queries (hybrid, live embeddings).
4. Fingerprint refusal demonstrated end-to-end (mock→llama flip ⇒ exit 3, remediation shown,
   `--lexical` escape hatch works and is labeled).
5. Every external-tool absence produces a `metadata_only` row + doctor fix hint — verified by
   monkeypatched `shutil.which` per tool.
6. Crawler cannot exceed any of its bounds on the fixture server; robots default respected.
7. `distill` output contains zero synthesized sentences: every card line is traceable to a
   stored `segments.excerpt` (asserted by string membership in tier 2).
8. Interrupting ingest (SIGINT in test via cancel flag) exits 130, leaves a consistent DB
   (pending rows), and a resumed run completes without duplicates.
9. `goldmine doctor` on this Mac (live) reports: llama OK, mock OK, poppler OK, ffmpeg OK,
   index state honest — no check invents success.
10. Docs stay true: any behavior change in implementation updates SPEC.md in the same commit
    (checklist item for the implementer, enforced by review).
