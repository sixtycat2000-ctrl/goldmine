# Frozen real distillation sources

The independently reviewed corpus contains120 licensed original documents:110 base and10 predeclared incremental additions, across8 topics. `labels.json` and `product-sources.json` are FROZEN before any clustering. Assets, normalized source views and full licenses resolve within this folder. The upstream420-question product suite is unchanged.

The corpus includes natural duplicate/paraphrase and multilingual cases. Source groups are provenance units; some topics share an upstream project (notably Vue), so8 groups do not establish8 independent projects or broad statistical independence. English108, Chinese10, Japanese1 and Korean1 are deliberately uneven; the latter two are noise. Source/topic/language confounding must remain visible.

The review snapshots in `reviews/` and `adoption.json` preserve the pre-clustering audit lineage. No real index, embedding, cluster, purity or recovery result is included.

Use `scripts/prepare_distill.py` with an explicitly provisioned NumPy-capable Python environment and pinned existing model/server/OCR paths. It materializes110 sources at one absolute corpus root, ingests one owned working index, saves a standalone base snapshot, adds the exact10 files to that same root/index, and saves the extended snapshot. Both source registries, screenshot flags, native commands, model activity and all prior file/segment/FTS rows are verified. Labels/licenses stay outside the indexed corpus.

Then use `scripts/capture_distill.py` with the generated preparation receipt and both snapshots. Strict evaluation and task acceptance require that receipt; default diagnostic output cannot certify quality. See `docs/execution-plan/P2.md` and `scripts/prepare_distill.py --help`. Required quality remains purity>=0.70, recovery>=6/8, largest-card share<=0.50, complete stored excerpt/member provenance and incremental recluster share<=0.10. Real preparation and quality gates are still MISSING.
