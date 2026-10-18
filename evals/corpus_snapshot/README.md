# goldmine

> Ingest anything, search anything. A local-first macOS CLI: point it at folders and URLs,
> it embeds everything with Google's EmbeddingGemma 2 (768-dim, multimodal) running entirely
> on your Mac via llama.cpp, and gives you hybrid search that combines meaning with exact
> matching — plus `goldmine distill`, which condenses the corpus into short, source-backed
> "golden knowledge" cards.

goldmine is built on two convictions learned from its predecessor [DigUp](../DigUp): a search
result you can't trust is worse than no result, and every capability your machine lacks must
be visible, not silent.

**The one rule — the scope invariant:** goldmine only searches folders you add — never your
whole Mac. `goldmine init` asks you to pick folders explicitly (or creates an empty
`~/Documents/goldmine/` as the zero-thought option); there is no default sweep of
`~/Documents`, `~/Downloads`, or `~/Desktop`, the chosen roots are echoed with file/media
estimates before the first ingest, `status` shows the indexed roots, and config records them
under `[init] roots`.

## Quickstart (3 commands)

```bash
python3 -m pip install -e .        # numpy is the only dependency
goldmine init                      # env check → model → folders → first ingest → demo
goldmine search "pod eviction memory pressure"
```

`init` is a resumable state machine: Ctrl-C any time and re-run `goldmine init` to continue;
`--restart` starts over. `--yes` runs non-interactively (creates `~/Documents/goldmine/`,
never sweeps existing folders; pass `--root PATH` to index your own folders instead, and
`--media` to also install speech transcription + screenshot OCR). Without the model, or
while the embedding server is broken, everything still works as keyword search:
`goldmine search <words> --lexical` / `goldmine ingest <folder> --lexical-only`.

Daily use: `goldmine ingest <folder or URL>` · `goldmine search <words>` (inline
`kind:`/`under:`/`after:`/`before:`/`site:` filters, quoted phrases, `--why`) ·
`goldmine open --hit 1 --query <words>` (audio seek, editor line) · `goldmine status` ·
`goldmine backup`.

## Where things live

| What | Where |
|---|---|
| Index | `~/Library/Application Support/goldmine/<name>.sqlite` |
| Config (incl. `[init] roots`) | `~/.config/goldmine/config.toml` |
| Models | `~/Library/Application Support/goldmine/models/` (repo dev checkout: `models/`) |
| Backups | `~/Library/Application Support/goldmine/backups/` |
| Init progress | `<home>/init_state.json` (deleted when init completes) |

One SQLite file holds everything — text, FTS5, vectors as 768-dim float16 blobs. An embedding
fingerprint (model sha256, prompt scheme, pooling, backend) guards it: change the model and
goldmine refuses to mix vectors, telling you exactly how to reindex. Distill never
synthesizes prose: every card is a verbatim excerpt from your own files.

## What it can read

txt/md/html · docx · rtf/doc/odt (via `textutil`) · PDFs (text layer + OCR for scanned
pages) · source code with `--code` · .eml/.epub · web pages (bounded crawler, robots.txt) ·
images (thumbnails + metadata; screenshots/scans via Vision OCR) · audio/video (whisper
transcription with seekable timestamps, measured ~7.2× realtime). Secrets, minified
bundles, and archives are never read — they get an explicit `unsupported` row with the
reason. The full truth table lives in [docs/USER_GUIDE.md](docs/USER_GUIDE.md); `status` and
`doctor` report it per file, always.

## Docs

| File | Contents |
|---|---|
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | the plain-language guide: daily use, capability table, measured numbers, troubleshooting |
| [ARCHITECTURE.md](ARCHITECTURE.md) | components, data flow, schema DDL, fingerprinting, process model, degradation model |
| [SPEC.md](SPEC.md) | the CLI contract: commands, flags, exit codes, output formats, config, env |
| [docs/v2/REDESIGN.md](docs/v2/REDESIGN.md) | the v2 plan: roadmap tables, resolved decisions, deferrals |
| `goldmine help <topic>` | in-product guides: getting-started, ingest, search-syntax, models, media, maintenance, troubleshoot |

## Honest status (v2.0)

Measured on the hash-pinned real corpus (`evals/`, n=146 queries, llama backend,
2026-10-17): hybrid **success@5 0.747**, **MRR 0.646**, success@1 0.562, nDCG@10 0.652 —
published targets (0.85 / 0.65) not yet met; the regression gate (`evals/baseline.json`,
−0.02 rule) keeps future changes honest. Token-true chunking made the measured 2670-token
embed-failure class structurally impossible; ASR runs at 7.2× realtime (a 1 h podcast in
~8.4 min); scanned-PDF and screenshot text is searchable; 648 offline tests + live suites.

**Shipped in v2.0** (21 items in `docs/v2/REDESIGN.md` §5): `init` onboarding (resumable,
`--yes`, scope invariant, TCC pre-warnings), `model` manager (manifest-pinned, resumable,
sha-verified, live arch probe), ASR + Vision OCR + image thumbnails + .eml/.epub, filters +
`--why` + «highlighted» snippets + `--format fzf`, ingest ticker/ETA, universal 4-line
error shape, `goldmine open`, `goldmine backup` + `doctor --deep`, per-file identity +
staleness visibility, real-corpus eval harness + regression gate, token-true chunking.

**Deferred** (see `docs/v2/REDESIGN.md` §5 v2.1/v2.2): `serve` daemon (warm search),
`find` TUI (killed — revisit with a named user), `reindex plan|run`, distill v2 cards,
`watch` mode, video frames, fusion tuning (G1).

Troubleshooting: `goldmine help troubleshoot` — the failures people actually hit, each with
a copy-pasteable fix. Every failure prints the same 4-line shape (`what / why: / fix: /
more: goldmine doctor`). The whole test suite runs offline against a deterministic mock
backend: `python3 -m pytest tests/ -q -m "not live"`.
