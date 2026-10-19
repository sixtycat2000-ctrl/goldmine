# goldmine — CLI Specification

Normative contract for the `goldmine` command. Implementation must match this document or
update it in the same change. Design rationale lives in [ARCHITECTURE.md](ARCHITECTURE.md) and
[docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md); wave designs of record:
docs/v2.1-r6-distill.md (distill), docs/v2.1-pack-eval.md (pack/context), docs/v2.1-r8-serve.md (serve).

**Aligned to build 2026-10-18** (v2.1 R6 distill + R8 serve). Command set (14):
`init` · `ingest` · `search` · `serve` (`status`/`stop`) · `status` · `version` ·
`model` (`install`/`list`/`verify`/`use`/`adopt`/`check`) · `distill` · `doctor` ·
`backup` · `backends` · `reset` · `open` · `help`. (`goldmine models …` is a hidden
argv alias of `goldmine model …`, `models add X` == `model install X`.)

Python 3.12, stdlib-first. Dependencies: `numpy` (required), `grpcio`+`protobuf` (optional
`foresight` extra), `pytest` (dev). External tools (`pdftotext`, `textutil`, `ffmpeg`,
`llama-server`, `whisper-cli`, the `goldmine-ocr` helper) are optional at runtime and always
capability-reported.

---

## 1. Global behavior

```
goldmine [GLOBAL OPTIONS] <command> [args]
```

Global options (accepted before or after the command):

| Option | Default | Meaning |
|---|---|---|
| `--index NAME` | `default` | Use `<home>/<NAME>.sqlite` (named index) |
| `--db PATH` | — | Explicit index file path (overrides `--index`) |
| `--backend NAME` | config `backend.default` = `llama` | `llama` \| `foresight` \| `mock` (validated at parse time; unknown = usage error) |
| `--config PATH` | `~/.config/goldmine/config.toml` | Config file location |
| `-v / -vv` | off | Log INFO / DEBUG to stderr |
| `-q` | off | Errors only |
| `--log-file PATH` | off | Also append full-level logs to PATH |
| `--json` | off | Machine-readable stdout (per-command schema below). Errors stay on stderr (the error object goes to stdout) |
| `--no-color` | auto | Disable ANSI (auto-disables when stdout is not a tty) |

**Index location (decided):** data directory default
`~/Library/Application Support/goldmine/` (macOS-native; this is a macOS-first tool), the
index itself `<home>/index.sqlite` (or `<home>/<NAME>.sqlite`). Override with `GOLDMINE_HOME`
or `--db`. Configuration stays at the XDG path `~/.config/goldmine/config.toml` — config is
portable convention, data is platform convention. Logs: `<home>/logs/`.

**Config resolution order:** CLI flag > environment variable > config.toml > built-in default.

### 1.1 Exit codes

| Code | Meaning | Examples |
|---|---|---|
| 0 | success (an honest zero — empty hits, `serve status` not running — is also 0) | |
| 1 | runtime error | backend crash mid-run, DB corrupt, `distill` corpus < 2 vectors or `--max-chunks` refusal |
| 2 | usage error (argparse) **and** backend-startup failure | unknown flag, missing required arg, missing arg value; **deviation:** a backend that fails to *start* (spawn/`/health` timeout) also exits 2 (`kind=backend_startup_failed`) — ARCHITECTURE §8 wins over the v1 "2 = usage only" table; distinct from a mid-run backend crash (1) and per-file failures (5) |
| 3 | fingerprint mismatch / refusal | index built with a different model or backend (`search`, `distill`); suppressed by `search --lexical` |
| 4 | prerequisite missing | no index file (`search`/`status`/`distill`/`open`/`serve` clients), no backend available at ingest, `doctor` found FAIL items, `model check` arch probe failed |
| 5 | partial success | ingest completed with ≥1 failed file or inaccessible root (summary still printed); `reset --reindex` whose re-ingest had failures |
| 130 | interrupted (SIGINT) | cancel flag honored; partial summary printed; `init` resumes from saved state |

### 1.2 Universal error shape (normative)

Every user-facing failure prints on stderr:

```
<what happened, one line, lower-case, no stack>
why: <root cause as data — path, exit code, missing thing>
fix: <exact copy-pasteable command>
or: <second remedy, when one exists>
more: goldmine doctor                                  <- when the cause may be broader
```

`fix:` is a real command, never advice ("install the tool"); an error without at least one
mechanical fix cannot be constructed (enforced at build time). `--json` mode additionally
prints the stable error object on **stdout** (a script never sees empty stdout):

```json
{"error": {"kind": "fingerprint_mismatch", "message": "…", "why": "…",
           "fix": ["goldmine reset --reindex", "goldmine search <query> --lexical"],
           "doctor": true}}
```

The documented exception: main()'s catch-all for truly unexpected bugs prints a deliberately
fix-less partial shape (error + `more:` + report) — there is no honest mechanical fix for a
bug. Usage errors raised by argparse itself emit the same JSON object (`kind=usage_error`).

### 1.3 Logging

stderr, level from `-v/-vv/-q` or `GOLDMINE_LOG_LEVEL` (`DEBUG|INFO|WARNING|ERROR`).
Format: `%(asctime)s %(levelname)s %(name)s: %(message)s`. stdout carries **data only** —
progress and diagnostics never touch stdout, so `goldmine search … --json | jq` is always
clean. Ingest progress lines (INFO): `ingest: 42/120 files, 1 failed`. Search prints one
stderr footer per semantic search: `via: serve|cold (0.42s)` — which backend path served the
query and the backend-acquisition time (§11).

### 1.4 `--json` envelopes

- Informational payloads (`version`, `backends`, `backup`, `open`, `init`, `serve status`)
  open with the frozen envelope `{"schema": "goldmine/1", "command": <name>, …}`.
- `search --json` is a bare array of hit objects (historical contract, §4); the pack meta
  object carries `schema: goldmine.pack/v1`; `distill --json` carries
  `schema: goldmine.cards/v2`.
- Errors use the §1.2 object; usage errors from argparse emit it too.

---

## 2. `goldmine init`

```
goldmine init [--yes] [--root PATH]... [--media] [--model-path PATH] [--skip-demo] [--restart]
```

Guided first run, a resumable 8-state machine (`<home>/init_state.json`): `env → model →
probe → folders → media → ingest → demo → done`. Each state is idempotent and saved on
completion; Ctrl-C (130) or a crash resumes at the saved state; `--restart` wipes it.

- `env`: python/numpy/tool probes (doctor's checks, quiet).
- `model`: detect / install (`goldmine model install`) / adopt (`--model-path`, sha-verified)
  the embedding model — or skip to a lexical-only index.
- `probe`: `goldmine model check` live arch probe (non-fatal WARN on failure).
- `folders`: **the scope step.** SCOPE INVARIANT: goldmine only searches folders the user
  explicitly chose — never a sweep of `~/Documents`/`~/Downloads`/`~/Desktop`. The step
  offers to create a dedicated `~/Documents/goldmine/` or takes typed paths one by one; every
  chosen root is echoed with a file/media estimate before ingest; the choice is recorded in
  `[init] roots` (config.toml) and `status` shows the indexed roots.
- `media`: offer speech transcription + screenshot OCR setup (`--media` = yes without
  prompting; installs whisper, ~574 MB).
- `ingest`: first ingest over the chosen roots (standard ticker, exit-5 semantics).
- `demo`: one real search for the corpus's most common word (`--skip-demo` skips).
- `done`: summary + next commands.

`--yes` is non-interactive: it creates and uses `~/Documents/goldmine` (never sweeps
existing folders), installs the model if missing, and skips the media step (add `--media`
to include it). Explicit `--root PATH` flags are the user's own choice and the only roots
`--yes` indexes besides the dedicated folder.

---

## 3. `goldmine ingest`

```
goldmine ingest PATH... [URL...] [flags]

  --code                    include code/data files (one index; line-aligned chunks).
                            Default off — matches DigUp: ripgrep searches code better and
                            code floods an index.
  --dry-run                 classify only: print per-kind counts, top skip reasons, cap
                            impact; no DB writes, no backend, no tool spawns. Exit 0.
  --lexical-only            explicit opt-in to ingest without embeddings (all content becomes
                            FTS-only). Without it, an unavailable backend is a refusal (exit 4).
  --reindex                 clear all segments + fingerprint, re-extract and re-embed
                            everything under the stored/current roots (fingerprint-mismatch
                            remediation; clears only AFTER backend startup + gate pass).
  --refresh-thin            re-extract files carrying thin/scanned PDF pages even when
                            unchanged (targeted OCR-upgrade repair; §16).
  --exclude GLOB            repeatable; glob relative to any root (also matched against the
                            absolute path). Adds to the built-in skip rules.
  --include-datasets        do not apply dataset-folder detection (DigUp heuristic).
  --max-file-mb N           per-file size cap [default 200; code files 1 MB hard cap]
  --max-pages N             PDF page cap [default 5000; first-pass step 30 pages]
  --max-chunks N            per-file chunk cap [default 5000; first-pass step 40 chunks]
  --audio-max-minutes N     audio/video soundtrack duration cap [default 120]
  --max-depth N             crawler depth [default 2] (0 = the URL itself only)
  --crawl-max-pages N       crawler page cap per ingest [default 50]
  --crawl-max-mb N          crawler per-page byte cap [default 5]
  --crawl-no-robots         ignore robots.txt (default: respect it)
  --politeness SECONDS      crawler delay between same-host requests [default 1.0]
  --workers N               extraction thread pool [default 2]
  --batch N                 embedding batch size [default 16]
  --encoding CHARSET        text decode override for this run
  --backend NAME            override global backend for this run
```

Behavior:

- At least one PATH or URL required (exit 2 otherwise). URLs (`http(s)://`) are crawled within
  the bounds above, same-host only, robots.txt respected (off-host and cross-port redirects
  are reported, not followed); each fetched page is stored as `kind=webpage`,
  `path=<canonical URL>`, `source='url'`, committed through the same text-commit path as file
  chunks (ctx guard, batched prompted vectors) with `loc=NULL` segments.
- Walk order deterministic (sorted); skip decisions recorded with reasons
  (secrets, minified, license files, developer folders, dataset folders, size caps, unknown
  type, **archives are unsupported** — zip/tar/etc. are never unpacked).
- A PATH root that cannot be stat'ed ("phantom root") becomes a per-root failure and is NOT
  recorded among the index roots (a typo must not silently become index scope).
- Unchanged files (size+mtime match, sha256 confirms) skipped; changed files re-extracted with
  atomic segment replacement (one transaction per file); deleted files pruned; all counted in
  the summary. Staleness re-do rules extend the content-hash skip — see §16.
- First ingest records `meta.roots_json`; later runs merge roots unless `--reindex`.

Media pipeline (details in §15 capability matrix):

- **Docs**: txt/text, md/markdown, html/htm, docx, **.eml** (mail headers + body),
  **.epub**, and every `textutil` format (rtf/doc/docx/pages/… — `textutil` missing is an
  honest metadata_only row). All prose goes through the token-budget chunker.

- **Images**: sips thumbnail first (photos 1024 JPEG q90 / screenshots 1536 PNG) — there is
  no raw-byte image cap; minSide 256 refusal (machine-stable unsupported reason); EXIF taken
  date becomes `files.content_date`. Screenshots (xattr-detected) additionally get Vision OCR
  text (`goldmine-ocr` helper, `--media`/`[ocr] enabled`): FTS text always; a TEXT vector
  under a text-only backend when `ingest.ocr_text_vector` is on (default). Same-wave
  screenshots share ONE helper spawn.
- **Audio/video**: whisper ASR first when `ingest.asr` = `auto` (default) and whisper-cli +
  models are present — a transcript becomes text chunks (FTS + prompted vectors under any
  text-capable backend); `always` turns every ASR gap into an honest row; `off` restores the
  raw path: 30 s WAV windows (25 s hop, RMS silence gate) via `embed_audio_wav` on
  audio-capable backends, metadata-only otherwise.
- **PDF**: pdftotext; pages < 200 chars (including scanned/empty) are rendered (pdftoppm) and
  OCR'd when the helper is available — upgraded pages become normal chunks
  (`pdftotext+visionocr@…` extractor version), pages that stay thin keep honest FTS-only rows.

stdout (human):

```
ingest: 3 roots, 214 files, 4 URLs
  content-indexed  182   (pdf 40, doc 96, webpage 4)
  metadata-only    21   (no image backend 14 · pdftotext not found 3)
  unsupported       9   (secrets 2 · minified 4 · unknown type 3)
  failed            2   (/a/b.pdf: timeout after 60s · /a/c.rtf: textutil failed rc=1)
  skipped-unchanged 89 · deleted 3 · partial reads 5
vectors: 4,231 · segments: 5,102 · fingerprint llama/embeddinggemma-2-Q8_0/768/a1b2c3d4e5f6a7b8
notice: [build_changed] llama.cpp build changed (recorded: b11146, installed: b11202) — reindex recommended
```

stdout (`--json`) — one object, `render_ingest_summary`'s schema:

```json
{"roots": ["/a", "https://x.dev"], "files": {"scanned": 214, "content_indexed": 182,
 "metadata_only": 21, "unsupported": 9, "failed": 2, "unchanged": 89, "deleted": 3,
 "partial": 5},
 "by_kind": {"pdf": {"content_indexed": 40, "metadata_only": 3}},
 "failures": [{"path": "/a/b.pdf", "detail": "timeout after 60s"}],
 "metadata_reasons": [{"reason": "no image backend", "count": 14}],
 "unsupported_reasons": [{"reason": "secrets", "count": 2}],
 "vectors": 4231, "segments": 5102,
 "fingerprint": {"id": "a1b2c3d4e5f6a7b8", "backend": "llama", "model_id": "embeddinggemma-2-Q8_0", "dim": 768},
 "notices": [{"code": "build_changed", "message": "..."}],
 "dry_run": false}
```

`--dry-run` prints the same shape with `"dry_run": true` and classification only (media kinds
follow the backend's declared capability, speech-aware for audio).

---

## 4. `goldmine search`

```
goldmine search QUERY [flags]

  --limit N            max hits [default from config: 20; 0 is a valid answer]
  --scorer NAME        hybrid (default) | lexical | semantic | rrf
                       (ablation baselines kept per design verdict 3; rrf = k=60
                       reciprocal-rank fusion of the two ranked lists)
  --kinds KINDS        comma list filter: doc,pdf,code,image,audio,video,webpage
  --z-gate X           z-score gate for vector candidates [default from config: 3.0]
  --lexical            force lexical-only AND suppress the fingerprint refusal (results
                       labeled `lexical (stale-vectors)` when the index has vectors)
  --excerpt-chars N    excerpt width [default 240; 0 disables excerpts and «» snippets]
  --why                per-hit score decomposition (human line; `why` object in --json)
  --raw                --json: keep the «» query-term markers (field `excerpt_marked`);
                       the `excerpt` field stays clean
  --format FMT         plain (default) | fzf: one TSV line per hit
                       path<TAB>kind<TAB>loc<TAB>excerpt<TAB>score (shell widget)
  --context N          attach N neighbor segments each side (0-3, clamped at 3;
                       default 0 — 1 in --pack mode; stderr notice on clamp)
  --pack               emit an answer-material pack instead of the hit list: `#` header,
                       `[n]` verbatim records, `# sources` footer — budgeted for pasting
                       straight to an LLM (schema goldmine.pack/v1)
  --budget N           pack token budget [default 4000, clamped to 512-32768; needs --pack;
                       --pack and --format fzf conflict (exit 2)]
  --no-serve           skip the serve daemon for THIS search (cold llama-server spawn,
                       exactly the pre-serve path)
```

Inline query filters (anywhere in the query, case-insensitive prefixes — unknown prefixes
are NOT filters, they stay literal search terms):

| filter | meaning |
|---|---|
| `kind:pdf\|doc\|…` | kinds; repeatable entries merge via `\|` |
| `under:PATH` | file paths under a prefix (`~` expanded; repeatable) |
| `after:DATE` / `before:DATE` | `YYYY-MM-DD` or relative `7d`/`2w`/`3m`; the file's **content date when known** (EXIF taken), else its mtime |
| `site:HOST` | web pages from one host |
| `"exact phrase"` | quoted = exact phrase match |

A filter that matches nothing yields zero hits, honestly (exit 0). Parsed filters are echoed
back (`filters_applied` in --json; the human footer lists them).

Query semantics (normative):

- Document/query prompts per EmbeddingGemma 2 model card, verbatim:
  `task: search result | query: {text}` for the query; `title: {title} | text: {text}` for
  documents (title = file stem / page number label); media vectors carry no prompt.
- Terms: lowercased alphanumeric tokens, deduped, stopwords and kind-words dropped (DigUp
  `Searcher.terms` list, English subset). Kind words that survive as hints ("screenshots of…")
  give a flat, capped +1.0 kind boost — never a filter, never stacked.
- Lexical: `fts MATCH "t1"* OR "t2" ... ORDER BY bm25(fts,3.0,1.0) LIMIT 1000` —
  **SQLite bm25 is lower-is-better; ascending order = best first; the raw value is never
  added to a higher-is-better score** (verdict 3 fix). Coverage = share of query terms found
  anywhere in the file's rows.
- Semantic: z-scores with per-modality centering; modality groups < 20 vectors use the global
  mean; std floor 0.02 (verdict 3 tiny-group fix, DigUp-measured).
- **The z gate is the SINGLE qualification rule**: candidates = {file: best z ≥ z_gate} ∪
  {file: coverage ≥ 0.5}; default gate **3.0** (v2.1 calibration A/B, n=146 llama eval: gate
  2.0+floor → 3.0 moved s@5 0.747 → 0.781 with zero per-tag regressions; the v2.0 two-bar
  spelling is subsumed — at gate = floor the second bar is algebraically absorbed and the A/B
  measured both spellings byte-identical on every query). There is no second z floor.
- Fusion: `score = w_m·max(z,0) + w_c·coverage^1.5 + kind_hint·1.0 (flat, capped — never
  stacks)`; `(w_m, w_c) = (1.0, 2.0)`. **Lookup queries** invert to `(0.5, 4.0)`: a query is
  lookup when ANY term is digit-bearing and alphanumeric-mixed or ≥ 4 digits (v2.1: the
  every-term rule never fired on mixed code+word queries like `LH-2209` → `lh 2209`).
- Missing-vector behavior (verdict 3 fix): a chunk without a vector (digits-only text, thin
  PDF page) competes lexically only; a file with zero vectors can still qualify on coverage;
  its `z`/`cosine` fields are `null`.
- Hits are file-level: a file's overlapping chunk matches collapse to its best segment
  (meaning segment when z ≥ gate, else keyword segment).

stdout (human), one block per hit:

```
1. score 3.41  z 3.12  cov 0.67   doc    /notes/k8s.md  L120-128
   "the scheduler «evicts» a pod when a node is under pressure and the pod has no «toleration»…"
2. score 2.90  z —     cov 1.00   pdf    /papers/trx50-datasheet.pdf  p.4
   "TRX50 chipset requires BIOS version 1.0.0.4a or later…"
```

- Snippets are «»-marked term-density windows (DigUp marking, density-window anchor; a
  pure-semantic hit with no term match shows the stored excerpt unchanged). `--raw` keeps the
  markers in the `--json` `excerpt_marked` field only.
- Near-duplicate fold: a hit whose segment is cosine ≥ 0.97 to a higher-ranked kept hit folds
  under it — `+1 duplicate` under the primary (--json keeps every hit with `duplicate_of`).
- Weaker-matches fold: human output cuts at the first hit below 0.5× the top score into a
  trailing `… +N weaker matches` line (--json keeps the full list).
- `--why` adds one decomposition line per hit (meaning/words parts, kind hint, lookup flag,
  gate); `--context N` adds a neighbor note line (or `context`/`context_note` in --json).
- metadata-only files show `no-vector` in place of z; a vectorless content file shows `—`.
- Zero hits: `nothing found for '…'` on stdout plus a stderr next-step hint (`try: fewer/
  shorter words · drop kind:/under: filters · goldmine help search-syntax`) — an honest empty
  is a feature, exit 0. `--pack` renders an honest empty pack the same way.
- Every semantic search prints the stderr footer `via: serve|cold (0.42s)` (§11); lexical
  searches print no footer.

stdout (`--json`): array of

```json
{"rank": 1, "score": 3.41, "z": 3.12, "cosine": 0.812, "coverage": 0.67,
 "kind": "doc", "path": "/notes/k8s.md", "loc": 120, "loc_end": 128,
 "segment": "chunk", "excerpt": "...", "file_status": "content_indexed",
 "scorer": "hybrid"}
```

Additive keys appear only when they carry data: `why` (`--why`), `excerpt_marked` (`--raw`),
`duplicate_of` (folded), `filters_applied` (filters parsed), `context` + `context_note`
(`--context N`).

Exit 0 even with zero hits. Exit 3 on fingerprint mismatch (unless `--lexical`), 4 if a
semantic scorer is requested and no backend is available, 2 for the pack/context flag
conflicts.

---

## 5. `goldmine distill` (v2)

```
goldmine distill [flags]

  --preview            dry run: corpus size, calibrated τ (+ raw + source + fallback
                       reason), pairwise quartiles, size histogram (kept + dropped),
                       largest dropped clusters — writes NOTHING
  --threshold X        manual τ override (tau_source='manual'); UNSET means CALIBRATED
                       from the corpus (v2 — the v1 fixed 0.70 default was the
                       mega-cluster bug; design of record: docs/v2.1-r6-distill.md)
  --min-members N      drop clusters smaller than N [default 3; 0 keeps singletons]
  --max-chunks N       corpus bound; refuse (never truncate) beyond it [default 50000]
  --kinds KINDS        restrict corpus by kind (default: all vectorized kinds)
  --under PATH         restrict the corpus to files under PATH (repeatable)
  --after DATE         only files with mtime >= DATE (YYYY-MM-DD; file mtime,
                       NOT content date)
  --as NAME            save scope + params as a named view, then run it
  --rerun NAME         run a saved view (scope + params reapplied; incremental
                       when eligible)
  --refresh            forget incremental state, force a full recompute (history kept)
  --limit-cards N      cards in output, by member count [default 20; 0 shows none —
                       every card still saves]
  --out FILE           write markdown to FILE; a directory or extension-less path
                       writes cards.md + cards.json
  --json               JSON cards to stdout instead of markdown
```

Algorithm (normative, deterministic — no RNG anywhere; algo string `leader-avglink.v2`):

1. Corpus = all vectorized segments in scope, ordered by `(file_id, segment_id)`.
2. Dedupe: exact duplicates by sha256 of normalized text; then near-duplicates, greedy in
   order, cosine ≥ 0.97 to an already-kept chunk.
3. **τ calibration** (`measure_tau`): sampled pairwise cosines (all pairs when N ≤ 300, else
   a 300-row stride gram), 256-bin histogram, Otsu t\* = bin center of argmax between-class
   variance; hump/valley + mode-proximity fallback to the **P90 percentile** path (real
   corpora are unimodal and expected to land on `percentile`); clamp [0.55, 0.85].
   `tau_source ∈ otsu | floor | ceil | percentile | manual | inherited`; τ + source + raw +
   fallback reason are reported in `--preview` and the output header.
4. Greedy leader clustering with the **average-link admission guard**: a chunk joins the
   first cluster (lowest index) where cos-to-centroid ≥ τ **and** mean-cos-to-members =
   `(S_c·x̂)/n_c` ≥ τ_lo = max(τ−0.10, 0.35); else it founds a new cluster. The guard reads
   the running member sum, never a centroid shortcut.
5. Singleton/small-cluster handling: clusters < `min_members` are dropped — never silently
   (the full kept+dropped size histogram is reported in stats and `--preview`).
6. Card content: **medoid** primary excerpt (min total cosine distance to members; ties →
   lowest segment id) + up to 2 **coverage excerpts** (greedy max-min cosine distance, stop
   below 0.25) + filtered TF-IDF terms (df ≥ 2 distinct members, bigram competition) +
   extractive title (first sentence of the medoid). Citations: up to 10
   `(path, kind, loc, loc_end)` plus distinct-file count. **Never synthesized prose** —
   extractive only. Cards may carry a parent header row (two-level topics, `parent_id`).

Incremental (per named view, `--as`/`--rerun`): a run is incremental when a previous run for
the view exists, τ is equal (an explicit `--threshold` must match), the embed_id matches,
vanished member files ≤ 20% (a modified file is updated in place, never deleted), and
`--refresh` is absent. `corpus_hash` never gates eligibility — audit identity only. Cards,
run stats and history persist in the index (`cards`, `distill_runs` tables).

stdout (human, markdown) — H1, then the run header carrying algo, calibrated τ + source,
corpus size/hash and scope; then a TOC and the cards:

```
# goldmine cards v2

leader-avglink.v2 · τ=0.7231 (percentile) · 12 cards from 4,231 chunks · corpus d41f2a996b8e

## Contents

1. [k8s pod eviction node scheduler kubelet taint toleration pressure](#1-…)

### 1. k8s pod eviction node scheduler kubelet taint toleration pressure   [14 chunks · 5 files]
> the scheduler evicts a pod when a node is under pressure and the pod has no toleration…
  — /notes/k8s.md (doc, L120-128)
  — /git/ops/runbook.md (doc, L400-411)   [+3 more files]
```

stdout (`--json`): `{"schema": "goldmine.cards/v2", "stats": {…}, "cards": [{id, parent_id,
title, title_kind, terms, excerpts, size, n_files, status, citations, distill_algo, run_id,
llm}]}` — no wall-clock fields; the `llm` key is present and always null in this build
(additive-only schema).

Exit 3 on fingerprint mismatch (same gate as search); exit 1 if the corpus has < 2 vectorized
chunks or exceeds `--max-chunks` (`DistillRefused` — refuse, never truncate).

---

## 6. `goldmine status`

```
goldmine status [--json] [--failures N]     (N = how many failure/metadata rows to list, default 10; 0 shows none)
```

Human output: index path + size, schema/fingerprint summary, backend availability one-liner,
counts table by kind × capability, vector/segment counts, pending/failed lists with detail,
the **indexed roots** (scope invariant — `roots:` line from `meta.roots_json`), extractor
drift notices, `llama_build` change notice, and a staleness hint (`re-run goldmine ingest
<roots> to refresh N stale files`) when per-file identity drift exists — staleness never
refuses (§16). Exit 0; exit 4 if the index file does not exist (with the `no_index` error
shape).

`--json`: one object with the same fields (capability counts keyed `content_indexed`,
`metadata_only`, `unsupported`, `failed`).

---

## 7. `goldmine doctor`

```
goldmine doctor [--deep] [--json]
```

Checks (each yields OK / WARN / FAIL + detail + fix hint):

- python 3.12+, numpy importable
- backends: `llama` (binary found? `--version` parses? model file exists? sidecar sha256
  valid? live probe when `GOLDMINE_LIVE=1`: spawn + embed `"ping"` → expect dim 768),
  `foresight` (grpcio importable? socket exists? `Ping` → version), `mock` (always OK)
- tools: `pdftotext -v`, `textutil` (macOS), `ffmpeg -version`, `ffprobe`; speech:
  `whisper-cli` + VAD/model files; OCR: `goldmine-ocr` helper present/enabled
- index: exists? schema_version current (migration ladder §17)? fingerprint present and
  matching the live backend? extractor_version drift counts; orphaned rows (segments without
  files — should be none)
- config file parses; env vars valid; disk free on `<home>`

`--deep` adds (read-only): `PRAGMA integrity_check` + foreign-key check over the index, a
staleness summary row, cards-vs-index fingerprint consistency, and backup-directory health.

Exit 0 if no FAIL; exit 4 if any FAIL. `--json`: `{"checks": [{"name", "status", "detail",
"fix_hint"}], "summary": {...}}`.

---

## 8. `goldmine backends`

```
goldmine backends [--json] [--probe]
```

Lists the three backends with: id, available, dim, capability (text/image/audio), source of
truth (binary path / socket path / in-process), and — with `--probe` — a live round trip per
available backend. This is where the media truth is visible at a glance:

| backend | text | image | audio | note |
|---|---|---|---|---|
| llama | yes | no | no | `/v1/embeddings` is text-only (llama.cpp); audio/video reach it as ASR text |
| foresight | yes | yes | yes | experimental; helper must be running |
| mock | yes | yes | yes | deterministic hash vectors, tests only |

Exit 0 always (unavailability is data, not an error). `--probe` failures mark the backend
unavailable with the error tail.

---

## 9. `goldmine model`

```
goldmine model <verb>            (a bare `goldmine model` is a usage error, exit 2)

  model install KEY [--from PATH] [--as ALIAS]
      download a manifest-pinned model into <home>/models (resumable, sha256-verified);
      --from registers an existing local file by path (no copy)
  model list
      every pinned key and its install state
  model verify [KEY]
      recompute bytes + sha256 vs the pin (omit KEY to verify every resolvable entry)
  model use KEY
      set the default embedding model (writes config llama.model)
  model adopt PATH KEY
      register an existing local .gguf under a manifest key (sha-verified)
  model check
      live arch probe: spawn llama-server, embed "ping", assert the dim
      (catches the brew llama.cpp b11146 arch trap). Exit 0 ok / 4 failed.
```

Model errors use the universal error shape (`kind=model_error`); caller errors are usage
(exit 2), runtime failures exit 1.

---

## 10. `goldmine open`

```
goldmine open (--hit N --query QUERY... | --path PATH) [--loc N] [--kind KIND] [--dry-run]
```

Open a file at the moment that matters: audio/video at a second (`--loc` seconds; QuickTime/
IINA jump when that is the default player), code at a line (`$EDITOR +LINE`), everything else
with the default app. `--hit N --query …` re-runs the search deterministically and opens hit
N (1-based) without shell-quoting paths. `--kind` overrides the kind guess. Every degrade
prints what happened and ends at a plain `open` — exit stays 0. `--dry-run` prints what would
run; execute nothing.

---

## 11. `goldmine serve` — warm-search daemon (opt-in)

```
goldmine serve [--linger S]        start the daemon (foreground)
goldmine serve status [--json]     daemon status ("serve: not running" is an honest 0)
goldmine serve stop                stop the daemon (verified SIGTERM fallback)
```

One daemon, no index state: it keeps ONE llama-server resident (spawned lazily on the first
embed) and serves embed/count_tokens RPCs to search clients over a line-JSON UDS. It never
opens SQLite; vectors stay in client memory; search results are identical served or cold.
Single instance (flock); lifecycle: stale-state sweep (verify-before-kill orphan receipts) →
fresh model sha256 → bind the 0600 socket in a 0700 per-uid dir → discovery file → accept
loop with a 1 s tick → SIGTERM/SIGINT/`serve stop`/linger-expiry drain → close backend →
unlink socket, exit 0. SIGKILL at any point leaves stale files + an orphaned child, all
handled by the next `serve start` (crash-only statelessness).

- `--linger S` idle seconds before exit [default from config: 300; only model-touching
  searches refresh the timer — `status`/`stop` do not].
- Memory guard: two warn-or-worse `kern.memorystatus_vm_pressure_level` readings (≥ confirm
  window apart) UNLOAD the model — it steps aside and stays aside until the system recovers;
  two critical readings exit gracefully 0. A search that hits an unloaded/absent daemon
  retries once, then falls back to a cold spawn — searches never fail because of serve.
- Clients: every `search` with a semantic scorer tries the daemon first (`GOLDMINE_SERVE` env
  > `[serve] socket` > discovery file); `--no-serve` opts out for one search; non-llama
  backends never consult it. The stderr footer `via: serve|cold (0.42s)` reports which path
  served (§1.3).

---

## 12. `goldmine backup`

```
goldmine backup [--out PATH] [--keep N]
```

`VACUUM INTO` a consistent single-file copy (WAL content included) taken while the index
stays usable; the copy is verified (opens + `PRAGMA integrity_check`) before it counts.
Default output `<home>/backups/<index>-<timestamp>.sqlite`; older backups pruned beyond
`--keep` [default 5]. `--out PATH` writes an explicit file and disables default-dir pruning.
Nothing-to-back-up keeps its own honest shape (not the generic no_index one). `--json` uses
the standard envelope. Exit 0; 4 when there is no index (the nothing-to-back-up shape);
1 when the backup itself fails verification.

---

## 13. `goldmine version` · `goldmine help`

```
goldmine version [--full]      version line; --full adds git describe, llama-server
                               build/source, model pin (sidecar sha), schema support,
                               backends, home
goldmine help [TOPIC]          topic guides; bare `help` lists topics; unknown topic = exit 2
```

Topics: `getting-started` · `ingest` · `search-syntax` · `models` · `media` · `maintenance` ·
`serve` · `troubleshoot`.

---

## 14. `goldmine reset`

```
goldmine reset [--yes] [--reindex]
```

- `goldmine reset` (no flags): interactive confirm (`--yes` required when stdin is not a TTY)
  → **delete** the index file (and its WAL/SHM). Nothing else is touched.
- `--reindex`: keep files/roots, delete segments + cards + fingerprint, then run ingest
  immediately (same backend/config). Fingerprint-mismatch remediation for "model changed on
  purpose". Exit 5 if the subsequent ingest had failures.

---

## 15. Capability matrix (normative definitions — verdict 5a)

Every scanned file (and crawled URL) ends in exactly one capability, stored in
`files.capability`, visible in `status`/`doctor`, and per-hit in search output:

| capability | Definition | Typical causes |
|---|---|---|
| `content_indexed` | ≥1 segment carries a vector **or** FTS text extracted from the file's content (an OCR'd screenshot under a text-only backend counts) | normal doc/pdf/code/webpage; ASR transcript; image thumbnail vector; OCR text |
| `metadata_only` | File recognized and recorded (name-row FTS), but zero content extracted or zero content vectors possible with current backend | image under llama without OCR text; ffmpeg/ffprobe/sips missing; ASR off + no audio backend; rtf without textutil |
| `unsupported` | Intentionally not read, machine-stable `reason` stored | secrets (pem/key/.env/id_rsa…), minified bundles, license files, unknown binary type, **archives (never unpacked)**, size caps, image below minSide 256, dataset folders |
| `failed` | Extraction attempted and errored | subprocess timeout, decode crash, ASR failure under `asr=always` |

Orthogonal flags in `info`/`detail` (a file can be `content_indexed` **and** degraded):

- `partial: true` + `read_to/read_end` — caps truncated the read (pages/chunks/duration/ASR)
- `lossy-decode: <assumed charset>` — bytes decoded with a fallback encoding
- `thin_text: true` + `thin_pages: [n…]` — PDF page(s) below 200 chars kept for FTS but
  vectorless; `ocr_pages: [n…]` marks pages the OCR pass upgraded (§16 re-do rules)
- `screenshot: true`, `ocr_chars: N` / `ocr: "no text"|"unavailable"`, `thumb: "WxH"`,
  `taken: <EXIF datetime>` — image pipeline state (`ocr: unavailable` rows are re-done once
  OCR becomes available)
- `duration`, `windows`, `asr_segments`, `language`, `asr_dropped` — audio/video facts
- `fetched_at`, `final_url` — crawled-page provenance

Honesty rules: `metadata_only` must never render as a success equal to `content_indexed`;
`unsupported` always carries a machine-stable `reason` code; the ingest summary and `status`
always break out all four capabilities by kind.

---

## 16. Staleness, fingerprints & per-file identity

**Index fingerprint (refusal gate).** The live backend's identity — backend, model_id,
model_sha256, dim, normalization, pooling, prompt scheme, media preprocessing — is
canonicalized to `fingerprint_id` and stored in meta (`fingerprint_id`, `fingerprint_json`)
on every ingest. `search`/`distill` compare stored vs live and REFUSE on divergence (exit 3,
§1.2 shape) — never mix vectors across embedding spaces. `search --lexical` suppresses the
gate and labels results `lexical (stale-vectors)`; `reset --reindex` is the mechanical
rebuild.

**Per-file embed identity.** Every vector-bearing row also stores `files.embed_id` (the
identity id of the embedding space that produced ITS vectors; NULL on vectorless files —
they cannot go embed-stale). The live id comes from the started backend's own fields
(`meta.embed_id`, `embed_identity_json`); it is field-compatible with the value derived from
the stored fingerprint.

**Staleness = re-do, never a refusal.** The content-hash skip (size+mtime prefilter, sha256
confirm) re-extracts a file when, in addition to content change: its stored
`chunker_version` differs from the running chunker (D3); its `extractor_version` is outside
the current registry (G10 — landing OCR/sips upgrades quietly upgrade old rows on the next
ordinary ingest); it carries thin-page markers and `--refresh-thin` was given; or it is a
screenshot indexed metadata-only before OCR was available and OCR is ready now (A5 upgrade,
one-shot — rows with `ocr: "no text"`/`ocr_chars` never churn). `status`/`doctor --deep`
surface a staleness summary and a re-run hint; nothing stale ever blocks a search.

**Migration ladder (schema v4).** `TARGET = 4` (`store.SCHEMA_VERSION` == `migrations.TARGET`).
One function per version (`MIGRATIONS[n]`: n−1 → n), the whole ladder in ONE transaction
(`BEGIN IMMEDIATE`), each step stamped `PRAGMA user_version = n` and recorded in
`meta.migration_history_json`. Migrations transform stored rows only — never re-embed, never
read source files; the v1→v2 backfill recomputes bookkeeping (per-file embed ids) from the
KEPT legacy fingerprint (zero-stale invariant, fixture-tested). Before migrating, files
larger than 1 MB get a `VACUUM INTO` snapshot (`<index>.pre-v<N>.sqlite`). Fresh databases
are created directly at v4. Unknown FUTURE schemas are refused (upgrade goldmine). SIGKILL
mid-migration is verified to roll back cleanly; re-running migrate() always reaches TARGET.

---

## 17. Config file

`~/.config/goldmine/config.toml` (stdlib `tomllib`; missing file = all defaults; unknown keys
WARN). Every value here is overridable by env/flag per the resolution order.

```toml
[core]
home = "~/Library/Application Support/goldmine"   # data dir (index + logs + backups)

[backend]
default = "llama"

[llama]
server   = "/opt/homebrew/opt/llama.cpp/bin/llama-server"
model    = "~/git/goldmine/models/embeddinggemma-2-Q8_0.gguf"
ctx      = 2048
batch    = 16
startup_timeout  = 120     # seconds to /health
request_timeout  = 60      # per embeddings call

[foresight]
socket = ""                # empty = $TMPDIR/foresight_helper.sock
expect_version = ""        # optional pin; mismatch ⇒ WARN (fingerprint uses live version)

[models]
dir = ""                   # empty = <home>/models (model-manager canonical location)

[ingest]
max_file_mb = 200
max_pages = 5000
max_chunks = 5000
audio_max_minutes = 120
workers = 2
politeness = 1.0
ocr_text_vector = true     # screenshot OCR text gets a TEXT vector under text-only backends
asr = "auto"               # auto | always | off  (whisper speech transcription)
asr_max_minutes = 120      # source-audio cap; over -> partial rows
asr_language = "auto"      # whisper -l value; 'auto' = detect per file
asr_whisper = ""           # optional whisper-cli path override

[ocr]
enabled = true             # Vision OCR master switch (auto-off when the helper is absent)

[search]
limit  = 20
z_gate = 3.0               # single qualification rule (§4)
scorer = "hybrid"

[init]
roots = []                 # the folders the user explicitly chose (scope invariant)

[distill]
# threshold unset/absent = CALIBRATED (v2). A value here is a manual
# override (tau_source='manual').
min_members = 3
max_chunks  = 50000

[serve]
linger = 300               # idle seconds before the daemon exits
timeout_s = 10.0           # client per-request deadline
socket = ""                # explicit socket path (GOLDMINE_SERVE env wins)
```

## 18. Environment variables

| Var | Overrides | Notes |
|---|---|---|
| `GOLDMINE_HOME` | `core.home` | data directory root |
| `GOLDMINE_CONFIG` | `--config` | config file path |
| `GOLDMINE_BACKEND` | `backend.default` | `llama`/`foresight`/`mock` |
| `GOLDMINE_LLAMA_SERVER` | `llama.server` | binary path |
| `GOLDMINE_MODEL` | `llama.model` | GGUF path |
| `GOLDMINE_MODELS` | `models.dir` | model-manager directory |
| `GOLDMINE_FORESIGHT_SOCKET` | `foresight.socket` | UDS path |
| `GOLDMINE_WHISPER` | `ingest.asr_whisper` | whisper-cli path override |
| `GOLDMINE_ASR` | `ingest.asr` | `auto`/`always`/`off` |
| `GOLDMINE_OCR` | `ocr.enabled` | truthy/falsy |
| `GOLDMINE_OCR_TEXT_VECTOR` | `ingest.ocr_text_vector` | truthy/falsy |
| `GOLDMINE_SERVE` | `serve.socket` | serve daemon socket target for search clients |
| `GOLDMINE_SERVE_PRESSURE_LEVEL` | — | memory-guard test hook: forces the pressure level |
| `GOLDMINE_LOG_LEVEL` | `-v/-q` | `DEBUG/INFO/WARNING/ERROR` |
| `GOLDMINE_LIVE` | — | test gate only: enables live-backend tests and doctor's live probe |

## 19. Pinned runtime facts (this machine, recorded for reproducibility)

- llama.cpp `0.5.0 (build 11146, commit 7fe450e19)` at `/opt/homebrew/opt/llama.cpp/bin/llama-server`
- Model `models/embeddinggemma-2-Q8_0.gguf` (310 MB) + `mmproj-embeddinggemma-2-Q8_0.gguf`
  (555 MB), pinned ggml-org rev `bfcd298762cc34d0357ece5ebdd31791a3a374d8` (DigUp's exact pin)
- ForesightHelper live at `$TMPDIR/foresight_helper.sock`; grpcio 1.84.0 (pip --user);
  `python3` = pyenv 3.12.0; poppler + ffmpeg + whisper-cpp via brew
