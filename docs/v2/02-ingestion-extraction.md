# goldmine v2 — Ingestion & Extraction Design

> Mission: **"search anything, local, ACCURATE."** Every byte of a normal Mac corpus becomes
> searchable with the right modality, entirely on this machine. Ingestion coverage and fidelity
> are the foundation everything else in v2 stands on.
>
> Status: design. Companion docs: `docs/v2/06-distill-knowledge.md`.
> Every external claim in this doc is backed by a `file:line` read, or by a probe **run on this
> machine on 2026-10-16** (marked **[measured]**).

---

## 1. Mission & target metrics

v1 can read text documents and, under the experimental foresight backend, raw image/audio
bytes. It cannot hear speech, read screenshots or scans, or see video — which is most of what
a real Mac corpus *is*. v2 closes that with four moves:

1. **ASR** (whisper-cli): speech → transcribed, located, searchable text.
2. **OCR** (Apple Vision via a tiny compiled helper): screenshots and scanned PDFs → text.
3. **Image fidelity** (sips thumbnailing): every image format macOS can decode becomes
   embeddable at a bounded size, with EXIF dates recorded.
4. **Video frames** (ffmpeg + scene/AHash dedup): a visual index beside the soundtrack.

### Target metrics (v2 acceptance)

| Metric | v1 | v2 target | Notes |
|---|---|---|---|
| 1 h podcast transcribed + indexed | not possible | **< 15 min** | ASR measured at **7.2× realtime** [measured]: 21.6 min audio in 3:01 → ~8.4 min ASR; + ffmpeg decode + ~3 min embedding ≈ 12 min |
| Screenshot text (lookup query like "externally-managed-environment") | miss | hit via OCR FTS | DigUp regression case: `Extractors.swift:75-78` |
| Scanned PDF pages with a vector | 0 (`thin-text`, ARCHITECTURE.md:399) | ≥ 95 % | OCR failures stay honest, never faked |
| Images content-indexed (foresight) | only ≤ 20 MB raw bytes (pipeline.py:46-49) | 100 % of decodable images, any size | sips thumbnail removes the raw-byte cap |
| 100-file mixed corpus | — | < 20 min end-to-end | coverage fixture, §9 |
| Relevance eval | v1 text baseline | no regression + audio/image queries answered | `scripts/eval_relevance.py`, 40-query set, §9 |

"Content-indexed" keeps its v1 definition (SPEC.md:327-337): ≥ 1 segment with a vector **or**
content FTS text. The v2 KPI counts **vector coverage per kind**, reported by
`goldmine status --json` (§9).

---

## 2. v1 state (what exists, file:line)

| Area | Where | Behavior |
|---|---|---|
| Classification | `goldmine/ingest/rules.py:136-162` | extension sets + MPEG-TS sniff (`.ts` read 600 B, walk.py:23; DigUp port rules.py:1-7) |
| Secrets/minified/license gates | `rules.py:96-117`, `walk.py:153-165` | **name-based only** (`id_rsa`, `.min.js`, `license*`) |
| Screenshot detection | `rules.py:115-117` | name-prefix only; xattr deferred (DESIGN_DECISIONS.md §10, line 179) |
| Walker | `ingest/walk.py:45-150` | scandir, sorted, hidden dirs skipped (:105-107), size cap, deletions (:176-197) |
| Text extraction | `ingest/extract_text.py:46-84` | BOM-aware decode ladder; docx stdlib zip+xml (:111-150); rtf/doc/odt via textutil (:164-187) |
| HTML | `ingest/extract_html.py:35-107` | HTMLParser strip-all-tags (DigUp `plainText(fromHTML:)` port); **no main-content detection** |
| PDF | `ingest/extract_pdf.py:140-179` | pdftotext, 30 + 50-page batches; **thin pages < 200 chars stay vectorless** (:25, consumed at pipeline.py:583-590) |
| Audio | `ingest/media.py:159-220` | ffmpeg 16 kHz mono WAV, 30 s window / 25 s hop (:31-33), RMS silence gate 0.003 (:33), embeddable only under foresight/mock (module doc :2-10) |
| Video | `ingest/media.py:223-229` | metadata + soundtrack only; **frames deliberately not built** ("documented stretch") |
| Images | `pipeline.py:446-468` | raw bytes to `embed_image`, 20 MB refusal cap (:46-49), excerpt = filename (:467), **no OCR, no thumbnail** |
| Commit | `pipeline.py:649-825` | one transaction per file (`store.replace_segments`, store.py:472); media embedded serially on main thread (:758+) |
| Unchanged skip | `pipeline.py:630-641` | (size, mtime) + sha256 — **extractor_version is NOT compared**, so a new extractor does not auto-upgrade old rows |
| Chunking | `chunker.py:33-97` | ~1800-char paragraph-aware; `est_tokens = len//4` (:135-137); `effective_chunk_size` reserves room assuming **1 char ≈ 1 token** for CJK (:140-150) |
| Config | `config.py:41-48` | `IngestCfg`: max_file_mb 200, max_pages 5000, max_chunks 5000, audio_max_minutes 120, workers 2 |
| Capability truth | SPEC.md:327-348 | `content_indexed / metadata_only / unsupported / failed` + orthogonal `thin-text` (:343) and a **pre-reserved `dedup: n frames dropped` info flag** (:344) |
| Deferred list | DESIGN_DECISIONS.md:173-182 | OCR, ASR hook, watch mode all recorded as v2 work |
| Models dir | `models/` | pinned GGUFs + `SHA256SUMS` + `.sha256` sidecars; llama-server lifecycle ARCHITECTURE.md:354-374 |

Out of scope in v1, verbatim: ARCHITECTURE.md:417-421 ("OCR, … daemon/watch mode, Xattr-based
screenshot detection …").

---

## 3. DigUp reference (what the predecessor learned, file:line)

| Lesson | Where |
|---|---|
| Vision OCR settings: `recognitionLevel = .accurate`, `usesLanguageCorrection = true`, `automaticallyDetectsLanguage = true`; results joined with `\n` | `Sources/DigUpKit/Extractors.swift:79-86` |
| Accurate > fast for screenshots: fast saved only 8 % time, read 9 % fewer chars, **lost an exact lookup** ("externally-managed-environment") | Extractors.swift:75-78 |
| OCR ≈ 0.24 s per Retina screenshot, "about as long as embedding it" | `Sources/DigUpKit/Indexer.swift:215-216` |
| Vision caches ~100 MB of models in whatever process ran OCR → OCR runs in a disposable helper process | `Sources/digup/IndexService.swift:4-8` |
| **Screenshot policy**: image vector at 1536 px PNG budget **plus** OCR text stored as a text segment with **no vector** — "a vector of the words made screenshots outrank better matches (2026-10-08)" | Indexer.swift:371-375, 494-519 (comment at :510-513) |
| Screenshot detection: xattr `com.apple.metadata:kMDItemIsScreenCapture` first, name prefixes fallback | `Sources/DigUpKit/Rules.swift:139-144` |
| Image prep: ImageIO decodes straight to thumbnail size ("a 48 MP photo never gets decoded at full resolution"), HEIC/RAW/WebP all work; EXIF `DateTimeOriginal` captured; too-small images refused | Extractors.swift:35-57 (`prepare`, `minSide` check at :43) |
| Photo vs screenshot encoder sizes: photo 1024 px JPEG, screenshot 1536 px PNG | Indexer.swift:371-373 |
| Video keyframes: sample every `interval`, keep one per shot (8×8 average-hash, Hamming ≤ 4 bits vs last kept), step = `max(interval, duration/maxFrames)`, **maxFrames 1200**, each kept frame stands until the next | `Sources/DigUpKit/MediaExtractors.swift:101-138` (hash at :140-156) |
| Audio windows: 16 kHz mono, ≤ 30 s (model ignores the rest), RMS > 0.003 gate, streaming (one window in memory), 2 s minimum tail | MediaExtractors.swift:28-99 |
| Scanned/garbled PDF pages become rendered page images with **page-image vectors**; glyph-order pages get a meaning vector from garbled text but never keyword text | Indexer.swift:535-582; `PDFExtractor.pages` Extractors.swift:112-151 |
| Download policy: pinned HF revision, per-file bytes+sha256 constants, verify then rename ("only verified files get their final name"), sha256 streamed in 8 MB pieces | `Sources/DigUpKit/ModelFiles.swift:14-45, 68-85` |
| Watch: FSEvents file-level events + NoDefer, hidden-path components filtered; engine debounces | `Sources/DigUpApp/FolderWatcher.swift:11-44` |
| Helper loop: new files first (≤ 50 on any power source), then backfill under the power policy, then long files; linger with a warm worker, then exit | IndexService.swift:14-23, 93-101, 236-251; default linger 60 s (`Sources/digup/main.swift:429`) |
| Power policy: backfill waits for Low Power Mode off, thermal ≤ fair, AC (battery indexing opt-in) | `Sources/DigUpApp/PowerPolicy.swift:54-63` |
| Code rules v1 lacks: generated-file refusal ("DO NOT EDIT", `@generated`…), content minified heuristic (avg > 300 chars/line **and** a > 3000-char line), `PRIVATE KEY-----` content scan, per-file names (Makefile/Dockerfile…), vendored/build folder list, `.git`-as-file (submodule/worktree) skip | `Sources/DigUpKit/CodeExtractor.swift:50-63`; Rules.swift:53-56, 60-64, 98-106 |
| Notebook cells read as cells, outputs never read | CodeExtractor.swift:150-163 |

---

## 4. Gap analysis

Gaps that block "search anything, accurate", ranked:

- **G1 — Speech is invisible.** A 1 h podcast produces only raw-audio vectors under foresight
  (pipeline.py:484-521); under the default llama backend it is `metadata_only`
  (README.md:77). No transcript, no FTS, no verbatim excerpt, no exact lookup.
- **G2 — Pixels with text are invisible.** Screenshots: name-row only (pipeline.py:463-468,
  OCR explicitly out of scope at :463 comment). Scanned PDFs: `thin-text` FTS-only pages with
  no vector (pipeline.py:583-590; ARCHITECTURE.md:399). This is where "accurate" hurts most —
  screenshots are the highest-value local corpus after notes.
- **G3 — Images are capped at 20 MB raw bytes** (pipeline.py:46-49) and HEIC/RAW never
  resize (no PIL), so a photo library largely fails; no EXIF date for filters.
- **G4 — Video visual track punted** (media.py:223-229); only the soundtrack exists, and only
  as opaque audio vectors.
- **G5 — Chunking is char-based**; CJK text overflows the ~512-token input (chunker.py:135-150
  admits the worst case), silently truncating meaning.
- **G6 — HTML boilerplate pollutes chunks** (extract_html.py:35-80 keeps nav/footer/ads).
- **G7 — No email/EPUB support**; `.zip` and friends fall into "unknown type" (walk.py:149-150)
  without an honest reason; `.eml` not even classified (rules.py:27 DOC_EXTS).
- **G8 — Rules parity gaps vs DigUp**: no xattr screenshot check (rules.py:115-117), no
  content-level secret/generated/minified checks (CodeExtractor.swift:50-63 has them; goldmine
  has none — grep confirms), no vendored/build folder list for `--code`, no submodule skip, no
  min-image-side refusal, **`CODE_FILE_NAMES` is exported in `__all__` (rules.py:18) but never
  defined — a latent ImportError**, and `is_project_root` (rules.py:129-133) has no production
  caller (its only caller is the unit test, tests/test_rules.py:103-107).
- **G9 — No incremental mode**; every `ingest` is a full walk (cheap via sha-skip, but nothing
  watches), deferred to v2 by DESIGN_DECISIONS.md:180.
- **G10 — Extractor upgrades don't propagate**: `_skip_unchanged` (pipeline.py:630-641)
  ignores `extractor_version`, so landing OCR in v2 would not upgrade thin pages on a plain
  re-ingest.

---

## 5. v2 design

### 5.1 ASR — whisper-cli integration (v2.0, L)

**Model choice.** Primary: `ggml-large-v3-turbo-q5_0.bin` (574 MB). Rationale: the turbo
decoder (4 layers vs 32) exists precisely for this throughput/quality point; q5_0 is
ASR-lossless in practice; at 574 MB it fits the models dir beside the 865 MB of embedding
GGUFs, and whisper.cpp loads ~1.6 GB of working set — trivial against the M4's 12.7 GB
recommended GPU set (whisper-cli startup log [measured]). Fallback tier for tight
disk/latency budgets: `ggml-small-q5_1.bin` (190 MB). `medium` and `large-v3` are rejected:
turbo dominates them on speed at near-equal WER; `tiny`/`base` fail the accuracy mission.
`.en`-only variants are rejected — the corpus is multilingual (EmbeddingGemma is too).

**Measured on this machine [measured, 2026-10-16]** (M4, Metal, brew whisper-cpp 0.25.3
backend):

- 21.6 min of continuous speech (`say`-synthesized fixture), `--vad -l auto`:
  **3:01 wall** = 7.2× realtime → 1 h ≈ 8.4 min.
- JSON output gives per-segment ms offsets (`{"offsets": {"from": 3450, "to": 10150}, …}`),
  ~1 segment / 3.9 s.
- `-l auto` detected `zh` on a Mandarin clip and transcribed it correctly.

**Model manager.** New command `goldmine models add whisper-turbo|whisper-small|vad|ocr`,
listing via `goldmine models list`. Same discipline as DigUp's `ModelFiles.swift:14-45`:

- Pinned URLs + expected sha256 constants in `goldmine/models_manifest.py` (HF `lfs.oid`
  from the models API is the sha256 — verified for all three files [measured]):
  - `https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin`
    sha256 `394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2`
  - `…/ggml-small-q5_1.bin` sha256 `ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb`
  - `https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin`
    (885 kB) sha256 `29940d98d42b91fbd05ce489f3ecf7c72f0a42f027e4875919a28fb4c04ea2cf`
- Streamed download → verify sha256 (8 MB chunks, ModelFiles.swift:68-85 pattern) → only then
  rename into place + write `<file>.sha256` sidecar (matches the existing `models/`
  convention). Partial files never wear the final name (ModelFiles.swift:77-79 rationale).
- doctor: `whisper-cli` present? model present + sha256 sidecar valid? (SPEC.md:281-282
  already reserves this check.)

**Windowing: VAD replaces the fixed 30/25 s windows for the ASR path.** The v1 30 s/25 s
windows (media.py:31-33) exist because the *embedding model* ignores input past 30 s.
whisper has no such limit — feeding the whole file loses nothing and lets silero VAD do the
cutting:

```
ffmpeg -i X -ac 1 -ar 16000 -f wav  →  one 16 kHz mono WAV (streamed to $TMPDIR)
whisper-cli -m <model> --vad -vm <silero> -l <auto|lang> -oj -of <tmp>/t -np -f <wav>
```

VAD gains: silence/music gaps skipped (no hallucination bait), natural segment bounds,
fewer wasted decode steps. The RMS gate (media.py:33) stays only on the raw-audio
embedding path. Flags: `--vad-min-speech-duration-ms 250` and `--vad-speech-pad-ms 30`
defaults kept; `--vad-threshold 0.5` default kept.

**Segments → index rows.** Parse the JSON (`transcription[].text` + `offsets.from/to` ms):

<!-- amended 2026-10-16: B1 (Reviewer 1) — whisper-cli does NOT emit segment-level
     no_speech_prob/avg_logprob (verified by running it: -ojf adds only tokens[] with
     per-token -p). Guard re-specified on per-segment mean token probability. -->
- Drop junk: whisper-cli emits **no segment-level `no_speech_prob`/`avg_logprob`** —
  with `-ojf` the only confidence data is `tokens[]` with per-token `-p` probabilities.
  Guard re-specified: per segment, compute the **mean token probability** and drop
  segments below the calibrated floor (start at 0.50; calibrate on the real-podcast
  acceptance run), recording `info.asr_dropped: n`. The < 20 chars/min yield gate
  (below) stays the file-level backstop. Alternative if it lands first: pin a
  whisper.cpp build that serializes segment-level probs — **verify before B3 ships**.
- **Merge for embedding, keep for locating:** adjacent segments merge into chunks of
  ≤ ~40 s / ~900 chars (natural speech bounds; a 4 s snippet is too little context for a
  good text vector). One segment row per merged chunk: `kind="asr"`, `modality="text"`,
  `loc=start_sec`, `loc_end=end_sec`, FTS text = verbatim transcript, vector = prompted text
  embedding (`document_prompt`, same as every text chunk — pipeline.py:710). Excerpt =
  first line of the chunk. Word-level timestamps (`--dtw`) are rejected: per-language
  extra models for 40 s→4 s loc resolution nobody asked for.
- Quality gate: if transcript yield < 20 chars/min of audio, the file stays
  `content_indexed` only if ≥ 1 vector landed, else honest `metadata_only`
  (`"no speech detected"`).

**Language.** `-l auto` default; detected language recorded in `info.language`
(JSON `result.language` — verified [measured]). `--asr-language` pins it for known
corpora (auto-detect can misfire on short/noisy clips). **No `--translate`** — translation
destroys fidelity; EmbeddingGemma is multilingual, the FTS is unicode61 + porter
(ARCHITECTURE.md:303-305). Honest caveat (2026-10-16 review, A6): unicode61 has no CJK
segmentation, so Chinese *lexical* retrieval degrades to substring/prefix-style matching
while the embedding leg carries the semantics; the CJK lexical decision (bigram
segmentation vs trigram index vs documented limitation) is tracked in REDESIGN §9 (Q20).

**Budgets.** `ingest.asr_max_minutes = 120` (config.py:41-48 pattern): caps source audio;
over-cap = `partial: true` + `read_end` exactly like audio_max_minutes today
(pipeline.py:505-508). Subprocess timeout: none at the file level (a 2 h transcription is
legitimately long); per-run progress printed; Ctrl-C finishes the current file then stops
(ARCHITECTURE.md:377-380 semantics).

**ASR text vs raw-audio embedding — when each.** Decision table:

| Audio content | v2 behavior |
|---|---|
| Speech (VAD finds ≥ 20 % speech AND ASR yield ≥ threshold) | **transcript only**: text chunks (FTS + text vectors). No raw-audio vectors — DigUp measured that a second vector of the same words hurts ranking (Indexer.swift:510-513), and 120 raw window RPCs + ~450 text chunks per podcast is double cost for a strictly weaker signal on speech |
| Music / non-speech (VAD speech < 20 % or ASR yield ≈ 0) | **raw-audio windows** via the v1 path (media.py:159-220, foresight embed) — this is where the audio encoder actually has signal |
| ASR unavailable (no model / tool) | v1 fallback: raw windows under foresight; `metadata_only` under llama; doctor suggests `goldmine models add whisper-turbo` |
| Video | soundtrack first (same table); frames are §5.3 |

**Perf budget check (1 h podcast, target < 15 min)** [measured components]: decode ~10 s +
ASR ~8.4 min + merge/embed ~450 chunks in batches of 16 ~2-3 min + FTS/commit ~30 s ≈
**11-12 min**. Acceptance test ships with the feature.

### 5.2 OCR — Apple Vision via a compiled helper (v2.0, L)

**Options compared on this machine [measured]:**

| Option | Verdict | Evidence |
|---|---|---|
| **(a) Compiled Swift helper** (DigUp's route) | **ADOPT** | `swiftc` 6.3.3 present via CommandLineTools (`xcode-select -p` = /Library/Developer/CommandLineTools — no Xcode needed); a 60-line probe compiled in 17 s, read rendered text perfectly, 1.3 s cold incl. runtime, OCR ~0.1-1 s; the exact DigUp settings (`Extractors.swift:79-86`) are proven by its eval history (:75-78) |
| (b) pyobjc Vision bindings | reject | **not installed** (probe: `import Vision/Foundation/Quartz` all fail on pyenv 3.12); `pyobjc-framework-Vision` would break the numpy-only dependency stance (README.md:18-20); keeps ~100 MB Vision caches in a long-lived process instead of a disposable one (IndexService.swift:4-8). Re-checked 2026-10-16 (A5): still rejected as a distribution alternative too — the answer for tool-less Macs is a prebuilt helper binary, not pyobjc |
| (c) `shortcuts` CLI | reject | binary exists but requires a shortcut pre-authored in the user's Shortcuts library — cannot be shipped or provisioned from a CLI; per-image spawn; no control of recognition level/language correction |
| (d) tesseract | reject (document as non-macOS fallback) | not installed; measurably worse on small UI text/dark-mode screenshots than Vision; adds brew dep + traineddata downloads |

<!-- amended 2026-10-16: A5 (ChatGPT) + B13 (Reviewer 1) — ship a prebuilt goldmine-ocr
     release asset; compiling is the dev fallback only (a stock Mac without
     CommandLineTools has no fix path otherwise); doctor gains a CLT row -->
**The helper.** `tools/visionocr.swift`, ~80 lines, shipped in-repo and to users as a
**prebuilt release asset** (same GHA release job as llama-server, 05 §4.7) — a stock Mac
without CommandLineTools gets a working `goldmine-ocr` with zero dev tools. Compiling
from source is the documented dev fallback, never the user path:

```
goldmine models add ocr
  → install prebuilt goldmine-ocr release asset → <home>/bin/goldmine-ocr
  → dev fallback: swiftc -O tools/visionocr.swift -o <home>/bin/goldmine-ocr   (needs CLT)
```

Subprocess contract (batch mode amortizes the 1.3 s cold start — screenshots arrive in
bursts):

- stdin: one JSON line per image: `{"id": "…", "path": "/abs/img.png"}`
- stdout: one JSON line per input: `{"id": "…", "text": "…", "chars": n, "ms": 213}` or
  `{"id": "…", "error": "…"}`; exit 0 even with per-image errors (mirrors LlamaEmbedder's
  per-input `Embedding(vector:nil,error:)` pattern, LlamaEmbedder.swift:152-158)
- settings exactly DigUp's: `.accurate`, `usesLanguageCorrection`,
  `automaticallyDetectsLanguage` (Extractors.swift:81-83); full-resolution input —
  "small UI text is what screenshots are full of" (:76-77)
- handshake `goldmine-ocr --version` → `"goldmine-ocr 1"`; doctor treats a stale/failed
  binary as "OCR off" with fix hint `goldmine models add ocr` (recompile after macOS
  major updates)
- per-image timeout 30 s enforced by the caller (util.run_tool pattern)

**Scope A — screenshots (adopt DigUp wholesale).** Detection upgrades to xattr-first:
`getxattr(path, "com.apple.metadata:kMDItemIsScreenCapture")` via stdlib `ctypes` (DigUp
Rules.swift:140-144), name prefixes as fallback (rules.py:77 stays). For each screenshot:

1. OCR the full-resolution image → OCR text.
2. Store **two** segments: `kind="image"` with the foresight image vector (kept! — DigUp
   screenshots do get image vectors, Indexer.swift:496-504) and `kind="ocr"`,
   `text_for_fts=OCR text`, **`vec=None`**. The OCR words are for exact lookup only;
   a text vector of them measurably made screenshots outrank better matches
   (Indexer.swift:510-513) — adopt the no-vector policy with that evidence.
3. `info.ocr_chars = n` (Indexer.swift:517 analog).

<!-- amended 2026-10-16: A5 (ChatGPT, critical) — DigUp's no-vector evidence assumed an
     accompanying image vector; under llama there is none, so keyword-only may be too weak -->
**Eval gate before B4 ships (A5, critical).** DigUp's "a vector of the OCR words made
screenshots outrank better matches" evidence (Indexer.swift:510-513) was measured on
screenshots that ALSO carried an image vector. Under the default llama backend there is
**no** image vector — keyword-only FTS may be the only access path, which is a different
tradeoff than the one DigUp measured. B4's acceptance therefore A/B-tests on the coverage
fixture: OCR text FTS-only vs OCR text + text vector, under llama, on the screenshot +
scanned-PDF query sets. Ship keyword-only only if the vector leg loses or ties; either
way the release notes state which was measured.

Photos are **not** OCR'd in v2.0 (DigUp Indexer.swift:374 OCRs screenshots only); a
`--ocr-images` flag for whiteboards/photos is a v2.2 open question (§8).

**Scope B — scanned PDF thin-page upgrade.** Page text decisions stay in
`_extract_pdf` (pipeline.py:563-626), with one new input: when pdftotext reports a thin
page (< 200 chars, extract_pdf.py:25) and OCR is available, render that page to pixels and
OCR it:

- render: `pdftoppm -f N -l N -r 150 -scale-to-x 1400 -scale-to-y -1 -png` (poppler is
  already the PDF dependency; 1400 px matches DigUp's `renderPixels: 1400`,
  Indexer.swift:384). pdftoppm replaces PDFKit so v1's page-batching semantics survive.
- OCR text ≥ 200 chars → page segment gets full FTS + vector + excerpt;
  `info.ocr_pages=[…]`, extractor_version becomes `"pdftotext+visionocr@1"`, detail
  `"ocr: N page(s)"`. OCR text < 200 chars → keep v1's honest thin-page row (never fake).
- A PDF with **no text layer at all** (pipeline.py:604-613 "page needs OCR (scanned)")
  becomes `content_indexed` instead of `metadata_only` once every page OCRs.
- Reject DigUp's garbled-text/glyph-order subtleties (Extractors.swift:121-143) for v2.0 —
  they need PDFKit; note as non-macOS/late enhancement.

**Re-extraction semantics.** Fix G10: `_skip_unchanged` (pipeline.py:630-641) additionally
returns False when `rec.extractor_version != current_version_for(kind)` — landing a new
extractor upgrades old rows on the next ordinary ingest of their root, no flags. For
targeted repair without a full pass: `goldmine ingest --refresh-thin` re-extracts only rows
with `info.thin_text` or `detail like 'page needs OCR%'` (stored, so it's one indexed
query). `--reindex` semantics unchanged (clears vectors, pipeline.py:1006-1012).

**Download story.** None — Vision ships with macOS and fetches recognition language models
on demand (first Arabic/CJK OCR may take a one-time system download; the probe listed
en/fr/it/de/es/pt/zh-Hans/zh-Hant [measured]). doctor reports
`goldmine-ocr: ok (8 langs) | missing (run goldmine models add ocr)`, plus a
CommandLineTools row for the compile fallback (`swiftc: present | absent — only needed to
build goldmine-ocr from source; the release asset needs no dev tools`, B13).

### 5.3 Video frames — ffmpeg + scene/AHash dedup (v2.1, M/L; foresight-only)

**Worth it? Yes — but second.** Frames are the last blind modality, and the machinery is
verified cheap: `select='gt(scene,0.3)'` fired exactly on the two hard cuts of a synthetic
clip [measured]. It is deferred behind ASR in priority because for the dominant video
kinds on this machine (lectures, podcasts, screen recordings) the **soundtrack ASR path
from §5.1 already makes them searchable**, and frames depend on the experimental foresight
backend (llama `/v1/embeddings` cannot embed images — README.md:76). Cap the investment
accordingly.

**Pipeline** (DigUp semantics, ffmpeg mechanics — no AVFoundation in Python):

```
ffmpeg -i video -an \
  -vf "fps=1/${step},split=2[a][b];[a]scale=1024:-2[j];[b]scale=8x8,format=gray[h]" \
  -map "[j]" -q:v 4 frame-%05d.jpg  -map "[h]" -f gray16be - 2>/dev/null > hashes.raw
```

- One process, one pass: full frames for embedding + 8×8 grayscale rows for hashing come
  from the same decoded stream (split). `step = max(3, duration/1200)` reproduces DigUp's
  sampling + maxFrames-1200 cap (MediaExtractors.swift:113).
- aHash in Python (numpy, 64 bytes/frame read sequentially): mean-threshold hash,
  Hamming distance ≤ 4 bits vs the last kept frame → drop (exact DigUp semantics,
  MediaExtractors.swift:129-130, :140-156). Replaces any scene-detection pass: fixed
  sampling + dedup already collapses static stretches (DigUp's comment: "screen recordings
  and lectures shrink a lot", :102-103), and it avoids a second full decode.
- Each kept frame → `embed_image(frame.jpg)` (foresight) → segment `kind="frame"`,
  `modality="image"`, `loc=start_sec`, `loc_end=next kept frame's start` (DigUp
  MediaExtractors.swift:135-136). Excerpt `"frame 87s"`.
- `info: {duration, frames: kept, frames_dropped: n}` + SPEC.md:344's pre-reserved
  `dedup: n frames dropped` flag finally gets used.
- Frames carry no FTS text in v2.1. OCR-of-frames (slide text) is a v2.2 stretch (§8) —
  note it would make lecture videos keyword-searchable at slide granularity.
- Memory bounds: ffmpeg streams (constant memory); Python holds one 64-byte hash + one
  JPEG path at a time; embeddings serial on the main thread (pipeline.py:758+ unchanged).
- Backend gate: no image capability → frames skipped with v1's honest `metadata_only`
  detail (`"backend lacks image embedding"`, pipeline.py:447-451); soundtrack still runs.

**maxFrames/coverage interplay:** `step` derivation from ffprobe duration (already
fetched, media.py:88-108) means a 2 h lecture yields ≤ 1200 candidate frames *before*
dedup, typically ~100-300 kept. Embed cost ≈ one foresight RPC each — acceptable.

### 5.4 Image pipeline — sips thumbnailing, EXIF, screenshot policy (v2.0, M)

**sips is the PIL replacement** [measured]: `/usr/bin/sips` reads HEIC/HEIF, WebP, AVIF,
JXL, TIFF, JPEG-2000, and RAW (DNG, CR2/CR3, NEF, ARW, ORF, RW2, SRW, PEF, RAF, 3FR…)
(`sips --formats`); `-Z` resample + `-s format jpeg -s formatOptions 90` round-trips a
HEIC→JPG thumbnail correctly. Replaces shipping raw bytes.

- **Photos**: `sips -Z 1024 -s format jpeg -s formatOptions 90 src --out tmp.jpg` →
  `embed_image(tmp.jpg)` (DigUp photo budget, Indexer.swift:371).
- **Screenshots**: `sips -Z 1536 -s format png` (DigUp :371-373 — PNG keeps UI text crisp).
- Byte caps: the 20 MB raw-byte refusal (pipeline.py:453-461) is deleted; thumbnails are
  ≤ ~1.5 MB. New guard: refuse decode attempts above 500 MB source (disk-bomb sanity);
  `minSide` refusal: images with `max(w,h) < 64` are `metadata_only` with detail
  `"small image WxH"` (DigUp Extractors.swift:43 + tooSmall error).
- **EXIF date**: `sips -g creation -g make -g model` returns EXIF-derived values (nil when
  absent) [measured on keys]. `info.taken = "<ISO>"`, `info.camera = make+model`. Downstream
  (search v2 doc) date filters use COALESCE(info.taken, files.mtime) — recorded here as the
  contract, implemented with search.
- **Screenshot vs photo policy**: §5.2 Scope A. Summary: both get image vectors; only
  screenshots get OCR text, and that text is FTS-only, never vectorized (DigUp
  Indexer.swift:510-513 evidence). Rejected alternative — "screenshots keyword-only, no
  image vector": that misreads DigUp; the picture carries layout/branding meaning text
  misses, and DigUp's measurement was against *vectorizing the OCR words*, not against the
  image vector.
- Fingerprint note: preprocessing changes → bump `MEDIA_PREPROCESSING`
  (media.py:41-43) to `"v2:image=jpeg-thumb-1024|png-1536(screenshot); audio=…"` so old
  raw-bytes image vectors force an honest reindex (fingerprints.py gate, ARCHITECTURE.md:206-245).

### 5.5 Extraction fidelity

**Token-accurate chunking for CJK (v2.1, S/M).** The llama backend's own tokenizer is the
truth: the managed llama-server exposes `POST /tokenize` (vendor
`tools/server/server.cpp:290` — `ctx_http.post("/tokenize", …)`; build 11146 [measured]).
Design: `backends/llama.py` gains `count_tokens(texts: list[str]) -> list[int]` (one HTTP
call per batch of candidate chunks; ~1 ms each on this box); `chunker.chunk_text` gains an
optional `token_counter` callback used to validate each emitted chunk against
`effective_chunk_size`'s budget — over-budget chunks are re-split at the deepest whitespace
*or* (pure-CJK runs) at sentence punctuation `。！？…` — never mid-ideograph. Backends
without a counter keep `est_tokens` (chunker.py:135-137). Honest-cost note in doctor when
counter disabled.

**HTML main-content extraction (v2.1, M).** v1's strip-everything (extract_html.py:35-80)
is DigUp-faithful (Extractors.swift:186-202 is the same regex-strip philosophy) but chunks
nav/footers/cookie banners as if they were content. Design a **readability-lite, stdlib**
block filter inside the existing `_TextExtractor`:

- Track a shallow element stack with block-level text length and link-text length.
- After parse, score blocks: drop top-level blocks that are link-dense (link text > 60 %
  of block) or whose class/id/aria matches
  `(nav|menu|sidebar|footer|header|comment|promo|banner|advert|cookie|share|related)` —
  the trafilatura signal set, minus its ML.
- **Never lose content**: if the surviving text < 40 % of raw strip yield, fall back to the
  v1 full strip (`info: {"main_content": false}`) — precision loss must be impossible to
  silence. Eval: 20 saved pages, compare R@5 with/without (TEST_PLAN harness).

**Email — .eml in, .msg documented out (v2.0, S).** `.eml` → kind `doc` via stdlib
`email`: headers Subject (RFC2047-decoded) → title, From/Date → info, `walk()` multipart
preferring `text/plain` (html part run through extract_html when plain missing), charset
via the decode ladder. extractor `"eml-stdlib.v2"`. Add `"eml"` to DOC_EXTS (rules.py:27).
**`.msg` is rejected**: OLE compound binary, needs a third-party parser, textutil can't
read it, DigUp doesn't either — stays "unknown type" honestly.

**Archives — NO content extraction (documented decision).** zip/tar/gz/7z/dmg/pkg stay
unopened: zip-bomb surface (v1 already guards docx at extract_text.py:31-35 — generalizing
that fight to every archive format buys noise: JARs, node_modules tarballs, app payloads),
and the useful contents are usually findable by path. Change: classify them as
`capability="unsupported", reason="archive (contents not indexed)"` instead of the vague
"unknown type" (walk.py:149-150) — honesty, not scope. Revisit only on user demand.

**EPUB — yes, cheap win (v2.0, S).** stdlib zip+xml: `META-INF/container.xml` → OPF →
spine order → each XHTML through `html_to_text` (extract_html.py:97-107) → join with
`\n\n`; `dc:title` → title; total uncompressed size cap reusing the DOCX guard pattern
(DOCX_MAX_XML_BYTES, extract_text.py:31) at 256 MB; entity/DOCTYPE refusal copied verbatim
(:35-41, XXE-safe). extractor `"epub-stdlib.v2"`. Add `"epub"` to DOC_EXTS.

### 5.6 Watch mode / incremental — `goldmine watch` (v2.1, M)

**Event source: adaptive polling first, FSEvents as opt-in.** Stdlib-only reality check on
macOS: pyobjc FSEvents needs a non-installed dependency ([measured] probe); stdlib
`select.kqueue` vnode kevents watch *files*, not trees (one fd per file — useless for a
corpus); `fs_usage` needs sudo (forbidden by house rules). So v2.1 ships polling that is
honest about what it is:

- Loop: `walk.walk(roots)` (unchanged, walk.py:45-150) → sha-skip discards unchanged in
  ~µs each → feed the changed set to `IngestPipeline.run()` **unchanged** — the pipeline
  already is incremental (prefilter walk.py + sha256 pipeline.py:630-641 + per-file
  transactions store.py:472); watch only decides *when*.
- Cadence: first pass immediately; then poll every `watch.interval` (10 s), backing off to
  60 s after 10 quiet polls; a pass that finds changes resets the backoff. Debounce: 2 s
  settle after a change before the pass (editor temp-file churn).
- Cost control: scandir-only walk of ~/git-scale trees is seconds; if a root costs > 30 s
  to scan, doctor flags it and suggests narrower roots (loud, like every other cap).
- **Power awareness** (DigUp PowerPolicy.swift:54-63, ported without IOKit/pyobjc):
  `pmset -g` (`lowpowermode`), `pmset -g therm` (`CPU_Speed_Limit` > 0), and AC status via
  `pmset -g batt` (`AC Power`) — subprocess probes every cycle, same blockers as DigUp:
  Low Power Mode / thermal throttling pause the *backfill of old files*; new files (mtime
  after last cycle, ≤ 50) always run (IndexService.swift:93-96 semantics).
- **Linger**: `--linger 300` default — after the index reaches steady state, stay alive
  with the warm llama-server for `linger` seconds (screenshots come in bursts), then exit
  0 (IndexService.swift:236-251 + main.swift:429 pattern). `--forever` for a
  LaunchAgent-style deployment; the user opts into that (no daemon we install silently).
- **Crash recovery**: nothing to add — per-file atomic transactions + `pending` rows are
  re-attempted next pass (v1 idempotency, ARCHITECTURE.md:377-380). Startup sweeps stale
  `$TMPDIR/goldmine-*` dirs whose owning pid is gone (IndexService.swift:146-154 pattern).
- **Observability**: heartbeat `{"pid", "last_pass", "queued", "blocked_on"}` written to
  `<home>/watch.json` each pass; `goldmine status` surfaces it; SIGTERM → finish current
  file → exit 130 like ingest.

### 5.7 Dataset/code-project rules parity vs DigUp

Checked Rules.swift + Crawler.swift line by line against rules.py/walk.py:

<!-- amended 2026-10-16: B14 (Reviewer 1) — "zero callers" was a miscite: one test caller exists -->
| DigUp mechanism | v1 state | v2 call |
|---|---|---|
| projectMarkers + "inside a code project only screenshots are indexed" (Rules.swift:18-23) | markers defined (rules.py:70-74) but `is_project_root` has **no production caller** (sole caller is its unit test, tests/test_rules.py:103-107) | **intentionally different, keep**: goldmine's code is opt-in `--code`, not auto-included, so the policy has no trigger; delete-or-wire is v2.1 housekeeping (document the decision, don't port the policy) |
| `codeSkippedFolderNames` vendor/third_party/build/dist/target… (Rules.swift:60-64) | missing — SKIP_FOLDERS has only the 8 (rules.py:64-67) | **adopt** for `--code` walks (S) |
| `.git`-as-file → submodule/worktree skip (Rules.swift:98-106) | missing | **adopt** for `--code` (S) |
| `codeFileNames` Makefile/Dockerfile/… (Rules.swift:53-56) | **exported in `__all__` (rules.py:18) but never defined — latent ImportError** | **fix** (S): define the set, use it in `--code` classification |
| Content-level code gates: `PRIVATE KEY-----`, generated headers ("DO NOT EDIT", `@generated`…), content-minified (avg > 300 chars/line AND a > 3000-char line) (CodeExtractor.swift:50-63) | none — v1 gates are name-only (walk.py:153-165) | **adopt** (M): scan in `_extract` code path; verdicts `unsupported` with machine-stable reasons ("holds a private key", "made by a program", "minified (content)") |
| Notebook cells (CodeExtractor.swift:150-195) | .ipynb handled as raw text → chunk_code over JSON soup | **adopt** (S/M): stdlib json cell extraction, cell-aligned locs, outputs never read |
| xattr screenshot detection (Rules.swift:140-144) | name-prefix only (rules.py:115-117) | **adopt** in v2.0 (§5.2, ctypes stdlib) |
| datasetFolders minimum 300, ratio 0.7, roots exempt (Rules.swift:174-188) | ported verbatim (rules.py:179-197), gated `--include-datasets` (pipeline.py:873-899) | parity ✓ |
| camera-prefix protection (Rules.swift:162-170) | ported verbatim (rules.py:81-83, 171-176) | parity ✓ |
| MPEG-TS 0x47 @ 188/192 (Rules.swift:129-137) | ported (walk.py:23 + rules.py:145-148, util.is_mpeg_ts) | parity ✓ (verify util matches the 188/192 double-check) |
| minImageSide / tooSmall refusal (Extractors.swift:43) | missing | **adopt** (S) in §5.4 |
| Bundles skipped ("app or bundle", Crawler.swift:63-65) | hidden dirs skipped, packages not special-cased (walk.py:105-118) | **adopt** (S): skip `.app`/`.bundle` dir-descents with a counted notice |

### 5.8 New/changed config surface (summary)

```toml
[ingest]            # new keys only (config.py:41-48 keeps the rest)
ocr = true          # master switch; auto-off when goldmine-ocr is absent (doctor)
asr_max_minutes = 120

[asr]
model  = "large-v3-turbo-q5_0"   # | "small-q5_1" | absolute path
language = "auto"                # whisper -l

[frames]            # v2.1
enabled = true
interval_s = 3.0                 # step = max(interval, duration/1200)
max_frames = 1200
scene_hamming = 4

[watch]             # v2.1
interval = 10
linger = 300
```

New CLI: `goldmine models add|list` (whisper-turbo, whisper-small, vad, ocr);
`goldmine ingest --refresh-thin`; `goldmine watch`. Env additions mirror config
(`GOLDMINE_ASR_MODEL`, …) per SPEC.md:395-406.

---

## 6. Phasing & effort

| Phase | Item | Effort | Why here |
|---|---|---|---|
| **v2.0 — "search what v1 couldn't"** | ASR: model manager + VAD pipeline + merge/loc + backend matrix (§5.1) | **L** | biggest coverage hole; all risky constants measured |
| | OCR: visionocr helper + screenshot segments + scanned-PDF thin upgrade + `--refresh-thin` + skip-on-extractor-version fix (§5.2, G10) | **L** | second biggest hole; DigUp-proven, machine-validated |
| | Image pipeline: sips thumbs + minSide + EXIF + fingerprint bump (§5.4) | **M** | unlocks HEIC/RAW libraries; small, independent |
| | .eml + .epub + archive honesty rows (§5.5) | **S** | stdlib, high hit-rate formats |
| | Coverage harness + fixture + eval queries (§9) | **M** | the KPI must exist before the features land |
| | Rules quick-fixes: CODE_FILE_NAMES definition (bug), minSide, bundle skip (§5.7) | **S** | one-pass hygiene |
| **v2.1 — "keep up while you work"** | `goldmine watch` polling daemon (§5.6) | **M** | incremental freshness; pipeline untouched by design |
| | Video frames pipeline (§5.3) | **M/L** | last modality; foresight-gated |
| | CJK /tokenize chunking (§5.5) | **S/M** | accuracy for CJK corpora |
| | HTML main-content lite (§5.5) | **M** | chunk quality on web pages |
| | Rules parity batch: vendored folders, submodule skip, content code gates, ipynb cells, is_project_root disposition (§5.7) | **M** | correctness under `--code` |
| **v2.2 — "polish"** | `--ocr-images` (photos/whiteboards) | **S** | needs its own eval (false-positive text) |
| | OCR-of-video-frames (slide text) | **M** | rides on §5.3 + §5.2 |
| | pyobjc-FSEvents optional extra for watch latency | **S** | only if polling disappoints |
| | tesseract fallback for non-macOS packaging | **S** | portable packaging track |

Dependencies: OCR needs nothing from ASR; images are independent; watch consumes whatever
extractors exist (last); frames consume §5.4's thumbnail discipline but ship their own.
The models manager (v2.0) is shared by whisper and goldmine-ocr.

---

## 7. Risks & open questions

- **ASR WER measured on synthetic speech.** The 7.2× realtime and zh auto-detect [measured]
  used `say` fixtures. Acceptance must rerun on a real 1 h podcast (music intro, crosstalk)
  before v2.0 ships; target WER not set — "searchable transcript" (F1 on query-term recall)
  is the bar, not transcription perfection.
- **Whisper hallucination** on silence/music despite VAD → per-segment mean token
  probability filter (§5.1 — re-specified 2026-10-16, B1: whisper-cli emits no
  segment-level no_speech_prob/avg_logprob) + the < 20 chars/min yield gate; verify on
  music-heavy files.
- **goldmine-ocr binary lifecycle**: must recompile after macOS major updates (Vision
  private-frame behavior drifts); doctor handshake catches it, but a LaunchAgent-free
  design means a human runs doctor. Acceptable on this machine (AGENTS.md maintenance
  cadence); revisit if watch mode becomes always-on.
- **sips EXIF orientation**: does `-Z` apply orientation like
  `kCGImageSourceCreateThumbnailWithTransform` (Extractors.swift:44-48)? Unverified —
  first v2.0 task with a rotated-EXIF fixture; fallback is reading orientation via
  `sips -g` and pre-rotating.
- **llama mmproj route still unverified** (DESIGN_DECISIONS.md:176-177): images/frames/OCR-
  free audio remain foresight-dependent. If ForesightHelper disappears, v2 degrades exactly
  like v1 (honest metadata_only) — no new failure modes introduced, but frames (§5.3) would
  be dead code. Open question for the backends doc: vendored llama-server + mmproj for
  embeddings.
- **/tokenize cost**: one HTTP roundtrip per batch of candidate chunks; if measured slow,
  fall back to the len//4 heuristic per file and only tokenize CJK-heavy chunks.
- **HTML main-content false drops**: mitigated by the 40 % yield fallback (§5.5); needs the
  saved-page eval to prove precision gain > recall loss.
- **Polling watch on huge trees**: scan cost is O(tree); guidance + doctor flag only.
  FSEvents extra is the escape hatch (v2.2).
- **Disk budget**: whisper turbo 574 MB + VAD 885 kB + goldmine-ocr binary (~100 kB) in
  `models/` + small tier 190 MB optional. Models downloaded for this design already sit in
  `models/whisper/` (sha256-verified [measured]).
- **Dataset false positives**: dataset_folders threshold (300 files, 70 % machine-named,
  rules.py:179-197) can hit a user's burst-shot folder; unchanged from v1, but watch mode
  makes silent wrongness stickier — the unsupported reasons report must stay loud.
- **Privacy/licensing**: unchanged from v1 — everything local; whisper/Vision models run
  on-device; HF downloads are one-time, sha-pinned.

---

## 8. Open questions for review

1. Photo OCR (`--ocr-images`): whiteboards are photos; is v2.2 right, or should "photo with
   ≥ 200 OCR chars" auto-promote to screenshot-like handling?
2. ASR merged-chunk granularity: is ≤ 40 s / ~900 chars right for FTS, or should whisper's
   own segments also land as FTS-only rows (finer loc, ~4 s) at ~2.3× FTS size?
3. Frames under llama backend: worth a *metadata + timestamps-only* frame row (no vector)
   for the timeline UI, or pure metadata_only as today?
4. `.msg` (Outlook): accept a future optional `olefile` dependency, or hold the stdlib line?
5. Watch defaults: is `--forever` + a suggested user LaunchAgent acceptable to document, or
   must v2 ship its own plist (this machine already runs several goldmine-adjacent agents)?

---

## 9. Coverage KPI — fixture and measurement

**Fixture (2-5 GB, built by `scripts/make_coverage_fixture.py`, all local):**

| Slot | Source on this machine | Kinds exercised |
|---|---|---|
| ~/git/mac | real repo (md, py, sh, log, plist) | doc, code, thin data |
| ~/Desktop + ~/Downloads sample | real screenshots (e.g. "Screenshot 2026-10-11 at 13.34.07.png"), PDFs, installers | screenshot/OCR, pdf |
| Scanned PDF | generate: render 10 pages of a text PDF through sips → images → reassemble PDF (no text layer) + one hybrid (5 text + 5 scanned pages) | pdf OCR |
| Podcast/lecture | one CC-licensed ~1 h mp3 (fetch once, cache in fixture) + a 10 min `say`-synthesized file | ASR |
| Music | a 5 min instrumental (CC) | audio embed / no-speech gate |
| Video | one screen recording + one synthesized ffmpeg clip with hard cuts (scene dedup test) | frames + soundtrack |
| Images | HEIC/JPG/RAW converted via sips from existing files, one > 20 MB | image pipeline |
| Email/EPUB | 5 hand-written .eml (plain+html+attachment, RFC2047 subject) + 1 Gutenberg epub | new formats |
| Web | `goldmine ingest` a small real blog (10 pages) | webpage/main-content |
| Noise | a 400-file synthetic "dataset" folder (machine-named PNGs) | dataset gate |

**Measurement.** `goldmine status --json` gains a per-kind
`content_indexed_vector / content_indexed_fts_only / metadata_only / …` breakdown
(files × segments.modality is already in the DB — store.py counts). KPI =
`content_indexed_vector / scanned` per kind. Published as a table in the v2 release notes
and asserted (non-regression) by `scripts/eval_relevance.py` extended with:

- 10 audio queries (transcript facts, e.g. "what did they say about pod eviction")
- 10 screenshot queries (exact UI strings — the DigUp regression class, Extractors.swift:75-78)
- 10 scanned-PDF queries, 10 image queries, 10 text baseline queries

**v2 targets per kind** (vector coverage): txt/md/html/docx/rtf/doc/odt/eml/epub 100 % ·
pdf-text 100 % · pdf-scanned ≥ 95 % · code 100 % (`--code`) · webpage 100 % · image 100 %
(foresight) with screenshot OCR text on 100 % of screenshots · audio ≥ 98 % (rest: honestly
"no speech" or "ASR failed" — never silent) · video ≥ 95 % via soundtrack, frames where
foresight · secrets/minified/archives: 0 % by design, reasons recorded.
