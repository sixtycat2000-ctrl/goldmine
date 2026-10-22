# goldmine — User Guide

> Implementation update (2026-10-21): see [the current feature status](FEATURE_STATUS.md) and the [P0–P2 execution record](execution-plan/PROGRESS.md). Older design and benchmark sections below retain their historical scope; they are not final candidate verification.

This guide explains setup, daily use and recovery. The terminal transcript below
is a historical scratch run with trimmed paths. Current behavior and evidence are
listed in [FEATURE_STATUS](FEATURE_STATUS.md); historical timings are not promises
for a fresh installation.

---

## What goldmine is

goldmine is a Mac command-line tool that indexes the folders you point it at — documents,
PDFs, notes, code, web pages, screenshots and audio. Text embeddings support meaning
search; names, metadata, actual OCR and timestamped speech are distinct evidence.
The default llama backend does not search photographs by visual contents. Local
indexing and search stay on your Mac. Explicit model preparation downloads models,
and choosing a URL for ingest fetches that URL. The index keeps its behavior
auditable: for every file it tells you exactly what happened (fully searchable, name-only,
skipped on purpose, or failed — with the reason).

## The one rule

**goldmine only searches folders you add — never your whole Mac.** There is no default sweep
of `~/Documents`, `~/Downloads`, or `~/Desktop`. Setup asks you to pick folders explicitly
(or offers to create an empty `~/Documents/goldmine/` for you). The folders you chose are
echoed before the first index run, recorded in config (`[init] roots`), and shown by
`goldmine status` — so "what does it actually look at?" is always one command away.

## Setup from a local checkout

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
```

Python3.12+ is required; numpy is the only base runtime dependency. Use
`.venv/bin/goldmine` for subsequent commands, or activate this environment first.
The [README keyword quickstart](../README.md#start-with-local-keyword-search)
works without a model or native server. No public package release is configured.

For semantic search, prepare the pinned embedding model and a compatible
llama-server explicitly; a model download does not supply the server binary.
Set `GOLDMINE_LLAMA_SERVER=/absolute/path/llama-server` to the compatible binary.
See [local native builds and installation](BUILDING.md). Then
`.venv/bin/goldmine init` offers resumable guided setup and scope selection.

`goldmine init` walks through eight steps: environment check, embedding model (downloads
once, resumable, checksum-verified), a live model test, **folder choice**, optional speech
transcription + screenshot reading (574 MB, off unless you ask), the first index run, a demo
search, and a summary. It is safe to interrupt: Ctrl-C any time, re-run `goldmine init`, and
it continues where it stopped. Real run:

```
step 2/8 · embedding model
  found embeddinggemma-2-Q8_0 (repo: …/models/embeddinggemma-2-Q8_0.gguf)
step 3/8 · live model probe
  live embed ok — ok (embedded 'ping' -> dim 768)
step 4/8 · folders
  goldmine only searches folders you add — never your whole Mac.
  searching:
    /tmp/gm_guide/docs
    /tmp/gm_guide/notes
  (recorded in …/config.toml — only these folders are ever indexed)
step 6/8 · first ingest
  root /tmp/gm_guide/docs: 2 files…
  root /tmp/gm_guide/notes: 2 files…
  ingest: 2 roots, 4 files
    content-indexed  4   (doc 4)
  vectors: 4 · segments: 8 · fingerprint llama/embeddinggemma-2-Q8_0/768/ff3dfbc47659dccb
step 7/8 · demo
  search "first" →
    1. score 2.02  z 0.02  cov 1.00   doc    …/docs/projects/k8s.md  L1-5
       "…The scheduler evicts a pod when a node is under memory pressure and the pod has
        no toleration for the taint. Best-effort pods go «first»; pods with a…"
init complete — 3 commands from zero to a result.
```

No time for questions? `goldmine init --yes` does the whole thing non-interactively — it
creates the empty `~/Documents/goldmine/` folder and uses that; it never sweeps existing
folders silently. To index your own folders unattended: `goldmine init --yes --root
~/notes --root ~/projects`. Add `--media` to include speech transcription + screenshot OCR.

## Daily use

**Add more folders later:** `goldmine ingest <folder>` — unchanged files are skipped by
content hash, so re-running is cheap. URLs work too: `goldmine ingest https://example.dev/blog`.

**Search** — type words; you get the files where those ideas live, best match first:

```
$ goldmine search "pod eviction memory pressure"
1. score 3.68  z 1.68  cov 1.00   doc    /tmp/gm_guide/docs/projects/k8s.md  L1-5
   "# Cluster notes — «eviction» The scheduler evicts a «pod» when a node is under
    «memory» «pressure» and the «pod»…"
```

`«guillemets»` mark where your words matched. Identifiers such as `ZT-84`, paths,
API names and quantities require a complete match in stored source evidence.
For example, `5mA` and `5MA` are different quantities for this check. A filename
can identify a file; the remaining question still needs evidence in that same
file's body. Quoted phrases must occur contiguously in one stored segment.

Useful extras:

- Inline filters: `kind:pdf kind:doc`, `under:~/notes`, `after:2026-01-01`, `before:2w`,
  `site:example.com`, and `"exact phrases"` in quotes. Unknown words are never mistaken for
  filters.
- `--why` shows the score arithmetic per hit (`why: 1·z 1.71 + 2·cov^1.5 1.00 = 3.71 · bm25 #1`).
- A successful search can return zero hits:
  `nothing found for 'quantum entanglement'` (exit 0 — try fewer words, or drop a filter).
  This means the request found no admitted evidence in the indexed snapshot;
  it does not establish that the information is absent from all your files.

**Open what you found** at the moment that matters:

```
$ goldmine open --hit 1 --query "restic backup"
opened /tmp/gm_guide/docs/projects/backup.md (doc)
```

Audio and video open at the matched second (QuickTime/IINA jump), code opens at the line in
`$EDITOR`, everything else opens with its default app. If a jump isn't possible it says so
and opens plainly — it never pretends.

**Check state:** `goldmine status` — what's indexed, from which roots, and what isn't:

```
index: /tmp/gm_guide/gmhome/default.sqlite (76.0 KB)
schema v2 · fingerprint ff3dfbc47659dccb (llama/embeddinggemma-2-Q8_0/768)
roots: /private/tmp/gm_guide/docs, /private/tmp/gm_guide/notes
backend llama: available (dim 768)
files: 4 · segments: 8 · vectors: 4 · bytes: 907 B
stale: 0 of 4 files (embed 0 · chunker 0 · extractor 0)
capability grid (kind × capability):
  doc      content_indexed 4
```

**When something fails**, every failure prints the same four-line shape — what happened, the
root cause, a command you can copy-paste, and where to look deeper:

```
no index at /tmp/gm_guide/none.sqlite — ingest something first
why: no index file at /tmp/gm_guide/none.sqlite
fix: goldmine init
or: goldmine ingest <roots>
more: goldmine doctor
```

Do the `fix:` line. If it recurs, run the `more:` line.

## What it can read

Plain text, Markdown, HTML, and web pages; Word (docx), RTF/DOC/ODT (via macOS `textutil`);
PDFs — text pages directly, **scanned pages via OCR**; source code (with `--code`);
`.eml` and `.epub`; `.rst` is indexed as plain text with original markup and lines,
without rendering or executing directives; images (metadata + thumbnails, HEIC included); **screenshots and scans
via Vision OCR**; audio and video **via whisper transcription** (searchable with
`0:06s`-style positions). Deliberately *not* read: secrets (`.env`, keys), minified bundles,
archives — each gets an explicit `unsupported` row with the reason, so silence never hides a
file. Missing optional tools (poppler, ffmpeg) downgrade capability for the affected kinds
and are reported by `goldmine doctor` — they never break a run.

Photo capture metadata uses actual EXIF `DateTimeOriginal`; the IFD0 edit time
and `sips creation` do not substitute for it. JPEG metadata is read without
native tools. Other image formats use macOS ImageIO through the built-in
`osascript` with a bounded timeout. Missing/unsupported capture time is `None`.
The stored `taken` string preserves the camera's wall clock. An explicit EXIF
offset determines its epoch; without an offset, date filters retain the
host-local time convention, without claiming the photographer's timezone.
Ordinary ingest repairs legacy photo metadata once and records that version.
Transient native failures retain an error marker and retry on the next ordinary
ingest. A previously verified capture date survives a failed refresh only when
the source bytes are unchanged; legacy edit dates receive no such exemption.

## What v2.0 added vs v1

Measured on a hand-labeled real corpus (146 queries, `evals/`, 2026-10-17):

- **Search accuracy measured and gated**: success@5 **0.747**, MRR **0.646** (v1 was never
  measured on real data). A regression gate freezes this baseline — any tracked metric
  dropping > 0.02 fails the eval.
- **Speech transcription**: whisper runs at **7.2× realtime** — a 1-hour podcast is
  searchable in ~8.4 minutes, with clickable timestamps.
- **Screenshot/scanned-text OCR**: Apple Vision reads screenshots and thin PDF pages (v1
  scored zero on such queries — they are now solvable; the coverage fixture tracks it).
- **Zero token-overflow embed failures**: v1's measured 2670-token overflow (an HTTP 400
  that failed whole files) is now structurally impossible — chunks are cut by the model's
  real token count.
- **Filters** (`kind:` / `under:` / `after:` / `before:` / `site:`), quoted phrases,
  `--why`, «highlighted» snippets, `--format fzf`.
- **`goldmine init`** (this guide's Setup), **`goldmine open`**, **`goldmine backup`**
  (one-command snapshot — transcripts are worth protecting), and **`goldmine help`** topics.

## How it stays honest

- **Per-file capability rows.** Every scanned file ends as `content-indexed`, `metadata-only`
  (name-searchable only, with the reason), `unsupported` (never read, with the reason), or
  `failed` (with the reason). `status` shows the grid; nothing degrades silently.
- **`goldmine doctor`** checks the environment and prints a copy-pasteable fix for anything
  missing; `--deep` also verifies index integrity.
- **Staleness is visible, never a refusal.** After an upgrade changes how text is cut,
  `status` reports "N files stale — re-run `goldmine ingest <roots>`"; plain ingest re-does
  them. The one hard gate: vectors from a *different embedding model* are never mixed
  (search refuses with the exact rebuild command, exit 3).
- **The scope invariant itself**: indexed roots in `status`, the choice recorded in config,
  and every root echoed with its file/audio estimate before the first ingest.

## Where things live

| What | Where |
|---|---|
| The index (one SQLite file) | `~/Library/Application Support/goldmine/default.sqlite` |
| Config — incl. your chosen roots | `~/.config/goldmine/config.toml` |
| Downloaded models | `~/Library/Application Support/goldmine/models/` |
| Backups (`goldmine backup`) | `~/Library/Application Support/goldmine/backups/` |
| Interrupted setup state | `~/Library/Application Support/goldmine/init_state.json` |

Backup = `goldmine backup` (verified single-file copy, keeps 5). Restore = copy the file
back. The folder above is covered by Time Machine; backups are the fast second copy.

## Troubleshooting — the top 5

1. **"no index at … — ingest something first"** → run `goldmine init`, or
   `goldmine ingest <folder>`.
2. **"no embedding backend available"** → run `goldmine model check` first (a broken model
   server masquerades as this). Keyword-only meanwhile: `goldmine search <words> --lexical`.
3. **"refused: … different embedding fingerprint" (exit 3)** — you switched models and old
   vectors remain. Rebuild: `goldmine reset --reindex`; search meanwhile with `--lexical`.
4. **"protected by macOS (TCC)"** — a folder the system won't let your terminal read.
   Grant Full Disk Access to your terminal (System Settings → Privacy & Security), or index
   a copy of the folder from somewhere else.
5. **"N file(s) failed to extract" (exit 5)** — those specific files failed; everything
   else was indexed. The first failure's path + reason are printed; re-run the same
   `goldmine ingest` command to retry just those.

More: `goldmine help troubleshoot` (each with its fix), `goldmine help` for all topics.

## Topic cards (`goldmine distill`)

`goldmine distill` condenses the index into topic cards: short extractive titles, keyword
lines, and up to three verbatim excerpts per card, each with a citation back to the file.
v2.1 calibrates the similarity threshold from your corpus every run (the preview tells you
what it measured and why — real corpora normally run the percentile fallback; that is
expected, not a bug), and an admission guard keeps one broad topic from absorbing
everything (the v1 mega-cluster bug). Typical loop:

    goldmine distill --preview                      # look before writing
    goldmine distill --as notes --under ~/notes     # save a named view + build cards
    goldmine distill --rerun notes                  # later: only NEW chunks get worked

Re-runs of a saved view are incremental: existing card memberships are frozen, deleted
content is swept (cards that fall below the minimum dissolve), and the threshold is
inherited so labels stay stable. `--refresh` forces a full rebuild; `status` tells you when
cards are empty after the v4 upgrade. Design of record: `docs/v2.1-r6-distill.md`.

## Explicit background commands and remaining experiments

`goldmine serve --db PATH` keeps its search model resident subject to the existing
power, thermal and memory guards. `goldmine watch --roots FOLDER --db PATH` watches
only chosen roots and refreshes their index. Both commands are implemented and
must be explicitly started; installation does not register a background service.
The required controlled/real ten-minute watch verification is separate from the
measured search latency evidence. See [current status](FEATURE_STATUS.md).

Multi-query and local reranking are optional experiments, off by default. Their
quality benefit and the remaining real corpus/answer/card checks still need
acceptance. Whole-Mac default scans, telemetry and a GUI remain outside scope.
