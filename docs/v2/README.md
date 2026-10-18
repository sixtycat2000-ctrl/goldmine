# goldmine v2 — design docs

v2 is the redesign pass over v1 (see `../../README.md`, `../../ARCHITECTURE.md`,
`../../SPEC.md`): make results **accurate on real corpora** (hand-labeled eval, tuned
fusion, ASR/OCR/image coverage) and make the tool **easy** (onboarding, interactive finder,
warm daemon, self-remediating errors) — without giving up the CLI identity, the honest
capability reporting, or the numpy-only, local-first posture.

## The docs

| File | One line |
|---|---|
| [REDESIGN.md](REDESIGN.md) | **Master plan**: falsifiable v2.0 claims + merged acceptance gates, v2 architecture delta + diagram, resolved cross-doc conflicts (§3), review amendments (§4, 2026-10-16), unified roadmap (v2.0 21 / v2.1 27 / v2.2 13 rows after the review rescope), migration path, risks, kill list, open questions. Start here. |
| [01-search-relevance.md](01-search-relevance.md) | Real-corpus eval harness + regression gate, fusion tuning, query routing, MMR/fold, token-true chunking, snippets, filters, `--why`. |
| [02-ingestion-extraction.md](02-ingestion-extraction.md) | Whisper ASR (7.2× realtime measured), Apple Vision OCR helper (prebuilt asset), sips image pipeline, video frames, .eml/.epub, watch mode, rules-parity fixes (incl. the CODE_FILE_NAMES bug). |
| [03-product-ux.md](03-product-ux.md) | `goldmine init` onboarding, `open` in-context opener, output polish, integrations (fzf), universal error shape; find TUI + serve UX kept as design-of-record (v2.1). |
| [04-performance-scale.md](04-performance-scale.md) | Measured baselines (query p50 222 ms @100k, mmap scan ~45 ms @500k, embed 16–18 texts/s GPU-bound, spawn 0.63–1.1 s, RSS 484 MB), scan decision tree, `serve` perf budgets, bench gates. |
| [05-architecture-data.md](05-architecture-data.md) | Migration ladder, per-file reindex identities + planner, model manager, UDS daemon substrate, multi-index registry, config/packaging/backup contracts. |
| [06-distill-knowledge.md](06-distill-knowledge.md) | Otsu-calibrated τ, average-link admission guard, medoid+coverage cards, incremental distill, `--pack` RAG digest, `--llm` polish with provenance rules. |

## Reading order

1. **REDESIGN.md** — the consolidated plan and the conflict resolutions (binding).
2. Then follow your area: relevance → 01, ingestion → 02, UX → 03, perf → 04,
   substrate/migration → 05, distill → 06.
3. v1 grounding when needed: `../../ARCHITECTURE.md` (components, schema, fingerprint),
   `../../SPEC.md` (CLI contract), `../../docs/DESIGN_DECISIONS.md` (v1 decision log).

## Status

**v2.0 IMPLEMENTED and verified** (2026-10-17): all 21 roadmap rows built across 5
implementation waves + a final adversarial review (0 P0 / 2 P1 / 9 P2 / 6 P3 — all 15
findings fixed, incl. the foresight live-identity crash and the migration chunker
backfill). 677 offline tests + 12 live tests green; real-model eval: success@5 0.747 /
MRR 0.646 (n=146, regression gate green); bench + coverage gates PASS. Start at
`../../docs/USER_GUIDE.md` (plain-language), `../../README.md` (quickstart), then
REDESIGN.md for what v2.1/v2.2 hold. v2.1/v2.2 items in these docs remain design-only
(serve daemon, find TUI, distill v2, planner, pack, watch — each re-entry-gated).
