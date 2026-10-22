# goldmine v2 — Architecture & Data Evolution (05)

> The substrate the other v2 domains stand on: the migration ladder, per-dimension
> reindex identity, the model manager, the daemon/process model, multi-index
> scoping, config evolution, packaging, and crash-only integrity. Every other
> domain doc (02 ingestion, 04 performance, 06 distill) inherits the contracts
> defined here; cross-domain assumptions are marked **[pending-02/04/06]** where
> the owning doc must confirm, and **[confirmed-0X]** where it already has.

---

## 1. Mission

v2's goal is **accuracy + easy to use**. Architecturally that decomposes into one
requirement and one enabler:

- **Requirement — improvement must not punish users.** Every domain wants to
  evolve (better extraction, OCR/ASR, token chunker, new prompts, a v2 card
  algorithm, a new embedding model). In v1, *any* identity change hits the global
  fingerprint gate (`fingerprints.py:121-137`) and the only remediation is a full
  `--reindex` that clears all segments (`pipeline.py:1006-1012`,
  `cli.py:888-894`). At measured re-embed rates (17.5 texts/s prose, 04 §3.2) a
  100k-chunk corpus costs 95–150 min (04 §6.5) — that is a punishment that
  would freeze the corpus against every improvement. v2 must make "this file's
  stored data was produced by older logic X" a *queryable fact per file*, so a
  planner can re-do exactly the stale files.
- **Enabler — a warm process.** Cold spawn is 0.6–1.1 s of llama-server startup
  plus up to 2 s of vector loading per command (04 §3.7, §3.1). v2 adds
  `goldmine serve` owning one llama-server and the vector cache; one-shot
  commands use it when present and behave exactly like v1 when not.

Everything below serves those two. Anything that does not is on the kill list (§4.9).

---

## 2. v1 state (what exists, file:line)

| Area | Fact | Evidence |
|---|---|---|
| Schema | One SQLite file per index; `meta`/`files`/`segments`/`fts`/`cards`/`card_chunks`; `PRAGMA user_version = 1` set by the open-time stamp, not the DDL | `store.py:25-82` (DDL) + `store.py:200-203` (user_version stamp), `ARCHITECTURE.md:254-326` |
| Version handling | `Store.SCHEMA_VERSION = 1`; refuses newer (`version > SCHEMA_VERSION` → error), and for older **just stamps the current version with no migration** | `store.py:181`, `store.py:194-203` |
| Fingerprint | 8 canonical fields (backend, model_id, model_sha256, dim, prompts, pooling, normalization, preprocessing) → `sha256(canonical_json)[:16]` stored in meta | `fingerprints.py:44-66`, `:76-79` |
| Preprocessing version | `PREPROCESSING_VERSION = "pipeline.v1"` is a *global* fingerprint field — one chunker change invalidates every index | `fingerprints.py:29`, `:58-61` |
| Gate | `gate()` refuses every embed/semantic-search/distill path on mismatch (exit 3); `--lexical` escapes for search only | `fingerprints.py:121-137`, `cli.py:991-994` |
| Build string | `llama_build` recorded in meta, **not** in fingerprint (deliberate D2 deviation); change → `build_changed` notice, not refusal | `pipeline.py:224-234`, `DESIGN_DECISIONS.md:76-84` |
| Per-file identity | `files.extractor_version` exists; drift is a *notice* (CURRENT_EXTRACTORS set), not an actionable plan | `store.py:43`, `cli.py:44-47`, `ARCHITECTURE.md:234-237` |
| Incremental skip | `(size, mtime)` prefilter → sha256 confirm, `state='done'` required | `pipeline.py:630-641`, `store.py:308-314` |
| Atomicity | Per-file `replace_segments` in one transaction; FTS rows deleted explicitly (no FK cascade) | `store.py:472-487`, `:316-325` |
| Search math | z-scores centered **over the whole vector table** (per-modality groups, global mean, std floor 0.02) — this is what forbids mixed-vector sets | `search.py:294-309` |
| Process model | One OS process per command; llama-server spawned per command, never adopted, killed in `finally` | `ARCHITECTURE.md:17`, `llama.py:235-244`, `pipeline.py:325-327` |
| Server resolution | env/config > `vendor/llama.cpp/build/bin` (repo-relative!) > brew; brew b11146 **cannot load gemma-embedding2** (arch unknown) | `llama.py:56-68`, `:84-101`, `doctor.py:101-102` |
| Models on disk | repo `models/` + `SHA256SUMS` + `<file>.sha256` sidecars; whisper models already side-by-side | `models/SHA256SUMS`, `doctor.py:145` |
| Cards | Full replace each distill run (`DELETE FROM cards`), pinned to fingerprint_id + algo | `store.py:574-601` |
| CLI surface | `ingest search status distill doctor backends reset` — no serve/model/index/reindex commands; `--index`/`--db` exist | `cli.py:967-975`, `:192-196` |
| Config | Single global config.toml, flag > env > file > default, unknown keys WARN; no version key | `config.py:170-209`, `:164-167` |
| Exit codes | 0/1/2/3/4/5/130, 3 = fingerprint mismatch | `cli.py:33-39` |
| Packaging | console script `goldmine = goldmine.cli:main`, deps numpy only (+optional grpcio); **repo-relative vendor path breaks for pip-installed copies** | `pyproject.toml` `[project.scripts]`, `llama.py:56-57` |

Key structural facts for v2: vectors are fp16 BLOBs beside their rows
(`store.py:489-519`); the whole table is loaded once per command; there is no
jobs/queue table, no registry of named indexes, no backup command, and no
provenance beyond `meta.app_version`/`llama_build` (`pipeline.py:1051-1061`).

---

## 3. DigUp reference (what to copy, file:line)

| Mechanism | What it does | Evidence |
|---|---|---|
| Migration ladder | `schemaVersion = 5`; `migrate()` reads user_version, refuses newer, **one private function per version**, each guarded by a version *range* (`(1...3).contains(version)`), all inside one transaction with `busy_timeout 60000`; fresh DBs `create()` straight at the latest | `IndexStore.swift:23`, `:73-92`, `:84-91` |
| Migrations that don't re-read files | 1→2 rebuilds FTS *from stored text* and re-queues only garbled PDFs; 2→3 is one UPDATE nulling vectors; 4→5 fixes states/derives info from stored JSON — "nothing is read or embedded here" | `IndexStore.swift:131-148`, `:152-154`, `:172-192` |
| `migrateVectorNames` (the key precedent) | 3→4 **renames the vector identity in meta** (`llama.cpp-b11461-metal` → `llama.cpp-vectors1`) so a llama.cpp upgrade that doesn't change outputs keeps the index — "Nothing is embedded again" | `IndexStore.swift:156-165` |
| Identity bookkeeping | `IndexOptions.fingerprint` string carries only vector-affecting knobs (budgets, windows, stretch); "an index never mixes settings" | `Model.swift:36`, `:80-86` |
| Re-extract unit | `requeue(_ id)` = drop segments (keep name row), reset to pending: "Extract and embed the file again, as it is (after a better way to read it arrived)" — always re-extract + re-embed together, never re-embed alone | `IndexStore.swift:254-259` |
| Move without re-embed | rename/move updates only `path` + the name row's FTS text | `IndexStore.swift:261-269` |
| Model pinning | `ModelFiles`: repo + revision + per-file bytes + sha256 constants; URL built from the pinned revision; media (mmproj) is a separate pinned file | `ModelFiles.swift:10-33` |
| Model storage | `~/Library/Application Support/DigUp/Models`; folder excluded from Time Machine; HF-hub cache shortcut (copy + verify) before downloading | `ModelFiles.swift:36-47`, `ModelDownload.swift:102-105`, `:117-137` |
| Download discipline | `.partial` + HTTP Range resume (range re-sent across HF CDN redirects), rename into final name **only after** size+sha256 verify; retry 2/5/15/30 s; disk-space precheck with slack; a file under its real name is always whole | `ModelDownload.swift:7-13`, `:111-116`, `:197-200`, `:229-239`, `:336-344` |
| Warm-encoder precedent | query encoder (~0.25 GB) exits after `encoderIdleSeconds` (600 default) without a query | `AppSettings.swift:28-29`, `:70-72` |
| Linger pattern | worker kept for `linger` seconds after work ends, then exit (`IndexService.swift:236-251`; cited in 04 §4) | 04-performance-scale.md:179 |
| Release engineering | signed → notarized → stapled app, then DMG, then appcast; publishing is a separate step. (Reference only — a Python CLI needs none of the notarization) | `release.sh:19-45` |

Not copied: UserDefaults/settings domain (we have config.toml), the DMG pipeline,
the helper-app split (our daemon is a plain process), `scope`-by-prefix SQL
(IndexStore.swift:34-46 — our `--kinds`/`--under` filters cover the need).

---

## 4. v2 design per area

### 4.1 Migration framework — the tested one-per-version ladder

**Problem.** `store.py:201-203` is not a migration system: it stamps
`user_version` to current, so the day schema v2 ships, v1 indexes would be
silently mis-stamped (missing tables → hard crashes later). DigUp's ladder
(IndexStore.swift:73-92) is the model.

**Design — `goldmine/migrations.py` (new, the only place that writes schema):**

```python
# migrations.py — one entry per version. MIGRATIONS[n] upgrades n-1 -> n.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {2: _v1_to_v2}

TARGET = 2  # == Store.SCHEMA_VERSION; single source, imported by store.py

def migrate(db: sqlite3.Connection) -> None:
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > TARGET:
        raise NewerIndex(version)          # keep store.py:194-200 behavior
    if version == TARGET:
        return
    db.execute("PRAGMA busy_timeout = 60000")   # a concurrent daemon waits (DigUp :82-83)
    try:
        with db:                                # ONE transaction per open, all steps
            version = db.execute("PRAGMA user_version").fetchone()[0]  # re-read inside lock
            for v in range(version, TARGET):
                _record_start(db, v + 1)
                MIGRATIONS[v + 1](db)           # each migration ends: PRAGMA user_version = v+1
                _record_done(db, v + 1)
    finally:
        db.execute("PRAGMA busy_timeout = 60000")
```

Rules (each is a test):

1. **One function per version**, ending with `PRAGMA user_version = n` — never a
   combined "upgrade to latest" (DigUp :86-91).
2. **Migrations never read source files, never embed, never call tools.** They
   transform stored rows only (DigUp migrateKeywordSearch rebuilds FTS from
   stored text, :134-138). Anything needing extraction is expressed as planner
   work (§4.2), not migration work.
3. **Fresh databases `create()` directly at TARGET** with the full v2 DDL; the
   ladder only runs for existing files (DigUp :86).
4. **Tested from checked-in fixtures**: `tests/migrations/v1_index.sqlite` (a
   real small v1 index, ~10 files with vectors, cards, thin pages) is migrated
   in every CI run and the result asserted (row counts preserved, new columns
   backfilled, cards-v2 tables present-empty). One fixture per historical
   version from v2 onward — this is the discipline that keeps the ladder honest.
5. **History in meta**: `migration_history_json` appends
   `{to, at, app_version, ms, notes}`; `goldmine version --full` shows it.
6. **Backups before migrating**: `Store.__init__` runs `backup_checkpoint()` —
   if `user_version < TARGET` and the file is larger than 1 MB, copy
   `<index>.sqlite` → `<index>.pre-v<N>.sqlite` (atomic rename) before the
   ladder. Cheap insurance; user deletes it later.

**The v1 → v2 migration, concretely** (this is the payload of `_v1_to_v2`;
each item marked confirmed = designed here against code I read, pending = the
owning domain doc must confirm details):

```sql
-- (a) Per-file pipeline identity (confirmed — §4.2). NULL-safe, instant on any size.
ALTER TABLE files ADD COLUMN chunker_version TEXT;   -- backfill 'chunk.v1'
ALTER TABLE files ADD COLUMN embed_id     TEXT;      -- backfill per the binding rule below
                                                 -- (NOT meta.fingerprint_id — B2)
-- extractor_version already exists (store.py:43) — no change.

-- (b) Identity meta (confirmed). The v1 fingerprint_json is KEPT verbatim for
--     downgrade/audit; new component keys are derived from it:
--   meta.embed_identity_json / meta.embed_id      — fingerprint minus `preprocessing`,
--     with `prompts` taken verbatim as `prompts_doc` (binding rule below the block)
--   meta.prep_identity_json                        ({chunker: "chunk.v1", decode: "decode.v1"})
--   meta.fingerprint_legacy_json = old fingerprint_json

-- (c) Date field (column confirmed here; semantics [pending-06 §"Open questions"]).
ALTER TABLE files ADD COLUMN content_date REAL;      -- NULL until an extractor provides it

-- (d) Cards v2 — DEFERRED to v2.1 (2026-10-16 review, C2/C6): the cards-v2 DDL moves
--     with the E1 distill redesign as its own announced migration. v1→v2 ships PURELY
--     ADDITIVE: v1 cards/card_chunks stay untouched and v1 distill keeps working.
--     (06 §4.4's "recreate empty + re-run distill" behavior applies when that lands.)
--     Placeholder for the v2.1 migration: DROP card tables, CREATE distill_runs/cards/
--     card_chunks/distill_views per 06 §4.4 DDL, one-line stdout notice.

-- (e) user_version = 2.
PRAGMA user_version = 2;
```

<!-- amended 2026-10-16: B2 (Reviewer 1) — the (a)-vs-(b) backfill seam resolved with an
     explicit binding rule + a zero-stale fixture test -->
**Backfill binding rule (resolves the (a)-vs-(b) seam).** `embed_id` is never copied from
`meta.fingerprint_id`: that hash still covers `preprocessing`, so a fresh v2 index would
compute a different id and every file would look stale. The binding rule: for migrated
indexes, the live `prompts_doc` is derived by parsing the legacy combined `PROMPT_SCHEME`
string's `d='…'` part (base.py:21-23) and used **verbatim** — byte-identical to what a
fresh index computes, because v1's combined prompt string carried the doc prompt in
exactly that slot. `embed_id` is then recomputed over {backend, model_id, model_sha256,
dim, prompts_doc, pooling, normalization, backend extras} with `preprocessing` excluded,
so **migrated embed ids equal fresh-index embed ids** until a real identity change (model
switch, or the v2.1 prompts split) rewrites identity through the planner. Fixture test
(normative): migrate the checked-in v1 index with the live model unchanged → the planner
reports ZERO stale embed/chunker files.

Items that need **no DDL** (deliberately — the schema is already open enough):

- **Transcript segments** [confirmed-02 §5.1]: ASR chunks land as ordinary
  `segments` rows with `kind='asr'`, `modality='text'`, loc/loc_end seconds;
  `files.extractor_version='asr-whisper.v1'`. `SegmentRec.kind` is free TEXT
  (`store.py:103-111`); DigUp likewise only enumerates kinds in app code
  (Model.swift:17-28).
- **Video frames / OCR rows** [pending-02 §5.2/5.3 confirm final shapes]:
  same trick — `kind='frame'` (modality image) and `kind='ocr'` (modality none,
  FTS only — the exact shape DigUp *removed* vectors from in migration 2→3,
  IndexStore.swift:152-154; learn from their eval: OCR text vectors hurt ranking).
- **Derived vector pack** [confirmed-04 §6.1]: `<index>.vecs` is a file, not a
  table; its header carries (fingerprint_id, **pack_epoch**, vector count, max segment
  id, pack schema version) — the epoch is bumped in the same transaction as every
  `replace_segments` (rowids are reusable; count/max-id alone can't see content swaps).
  Desync = rebuild, never trust. Only meta key added:
  `meta.pack_digest` (written by the pack builder, advisory).
- **Jobs queue** — deliberately absent, see §4.8.
- **Index registry** — deliberately a *file*, not a table, see §4.5.

### 4.2 Selective reindex — per-dimension identity and the planner (the core)

**Problem.** The v1 fingerprint folds four independent axes into one id
(`fingerprints.py:53-62`): the *embedding* identity (backend/model/prompts/
pooling/normalization), the *chunker* version, the *decoder/extraction
semantics* (preprocessing), and (via per-file extractor drift) the tool set.
Changing any one nukes every segment (`pipeline.py:1006-1012`). Meanwhile the
information to do better already half-exists: `files.extractor_version`
per file, `CURRENT_EXTRACTORS` in the CLI (`cli.py:44-47`), and the DigUp
precedent that identity bookkeeping can change without touching vectors
(IndexStore.swift:159-165).

**Design — split identity into dimensions, record each file's dimensions.**

Dimension 1 — **embed identity** (what makes vectors comparable):

```
embed_identity = {backend, model_id, model_sha256, dim,
                  prompts, pooling, normalization, + backend extras}
embed_id = sha256(canonical_json)[:16]
```

This is the v1 fingerprint **minus `preprocessing`**. Prompts stay in it
(v1 already had that right: base.py docstring :3-7 and :17-18 — a prompt change
changes every vector). The llama.cpp build string stays *out* (D2 refinement,
DESIGN_DECISIONS.md:76-84; DigUp paid for that mistake once,
IndexStore.swift:156-158).

Dimension 2 — **prep identity** (what makes *text* comparable, per file):

- `files.chunker_version` — chunker semantics id (`chunk.v1`; bumps to
  `chunk.v2` when 02's token-chunker lands [pending-02]). Chunker constants are
  global code (`chunker.py:14-15`), so this is one comparison per file.
- `files.extractor_version` — already exists per file (`store.py:43`); v2
  formalizes the "current" side into `goldmine/extractors.py`:
  `EXTRACTOR_VERSIONS: dict[str, str]` (the current version *per extractor
  family*, seeded from `cli.py:44-47` + `pdftotext@<ver>` / `textutil@<os>`
  / `asr-whisper.v1` / `ocr-vision.v1`), plus
  `is_current(extractor_version) -> bool` that compares family prefix and
  treats unknown families as stale-with-reason.

Dimension 3 — **content identity**: `sha256` + size + mtime, unchanged
(`pipeline.py:630-641`).

Not dimensions (never invalidate vectors): llama build string (provenance
notice only, `pipeline.py:224-234`), search weights/z-gate, distill algo,
pack file, config knobs that don't feed extraction/embedding.

<!-- amended 2026-10-16: B2 (Reviewer 1) — backfill recomputes embed_id per §4.1's binding
     rule; the old "embed_id = meta.fingerprint_id" contradicted the identity definition -->
**Backfill (the migrateVectorNames move).** The v1→v2 migration sets
`chunker_version='chunk.v1'` and recomputes `embed_id` from the kept-verbatim legacy
fingerprint per §4.1's binding rule (`prompts_doc` = the legacy scheme's `d='…'` part
verbatim; `preprocessing` excluded — never a raw copy of `meta.fingerprint_id`) on every
row **without re-embedding anything** — v1 vectors were, in fact, all produced by
one embed identity (the gate guaranteed it, `fingerprints.py:121-137`) and one
chunker. Bookkeeping catches up to reality; the index keeps working; if the
current live embed identity still equals the recorded one, the planner reports
zero stale. Exactly DigUp migration 3→4 ("Nothing is embedded again",
IndexStore.swift:159-165).

<!-- amended 2026-10-16: Reviewer 2 C2 — the planner commands are v2.1; in v2.0 the
     re-do path is a plain ingest re-run (the extractor-version skip fix, 02 G10),
     so nothing below changes design — only the release that ships it -->
**The planner — `goldmine reindex plan` (dry-run by default) and
`goldmine reindex run`.**

```
plan (pure reads; no backend startup needed unless embed identity is stale):
  for each file: compute stale reasons
    content   : sha/size/mtime vs disk          (existing walk prefilter + hash)
    extractor : files.extractor_version not current for its family
    chunker   : files.chunker_version != chunker.VERSION
    embed     : files.vec-bearing AND files.embed_id != live embed_id
    pending   : state='pending' (crashed/interrupted earlier runs)
  action(f) = re-extract+re-embed (any of content/extractor/chunker) — see below
              embed-only is NOT a path (justified below)
output (human + --json):
  stale: 1,203 of 4,500 files
    by reason: extractor 610 (pdftotext 24.08 → 25.02), chunker 0,
               embed 4,500-under-old-model 593, content 40, pending 8
    by kind:   pdf 700 · doc 320 · code 183
  est: ~9,840 texts → 9.4 min @ 17.5 texts/s (rate: last run, self-calibrating 04 §6.6)
  plan id: <hash> — `goldmine reindex run --plan <id>` executes it
run:
  identical loop to IngestPipeline.run() over the stale file set, with two changes:
    - the sha-skip test (pipeline.py:630-641) additionally consults the
      per-file dimensions, so `run` never re-does a file that became current;
    - per-file atomic commit as today (store.py:472-487), so Ctrl-C/Crash
      resumes by re-planning (pending rows + sha skip — the v1 mechanism, unchanged).
  on completion: rewrite meta.embed_id/embed_identity_json = live; delete plan.
```

Estimate honesty: rates come from `meta.rates_json` recorded by the previous
real run (prose/code/pdf texts-per-second, 04 §6.6 measured-cost model);
first-ever plan uses the hardcoded measured constants and says so.

**Why re-embed-only is not a path (opinionated).** Re-embedding without
re-extracting requires the exact prompted chunk text, which v1 does not store
(FTS `body` is NFC-normalized whole-text; chunk boundaries are not recoverable).
Storing `segments.embed_text` would double text storage to save re-extraction —
but extraction is ≤1% of wall clock against embedding's 99% for text corpora
(04 §3.3, §6.2). So the planner's unit of work is the file, and every stale
file is re-extracted + re-chunked + re-embedded through the normal pipeline.
This also matches DigUp's `requeue` semantics (IndexStore.swift:254-259) and
keeps exactly one ingest code path (02's watch mode reuses the same one,
02 §5.6).

**Correctness invariant — no mixed-vector queryable sets (decided).**
Per-*segment* fingerprinting is rejected. The z-score math centers scores over
the entire vector table (`search.py:294-309`): vectors from two embed identities
have incomparable geometries, so a mixed set doesn't just mis-rank — the mean/std
normalization itself becomes meaningless, and there is no user-meaningful way to
"query the subset" (the subset is defined by which files happened to be stale).
Therefore: the invariant is **per-file bookkeeping + a whole-index rule**:

- While any file has a stale `embed_id` (or meta.embed_id is unset), *semantic*
  search and distill refuse with exit 3 and an actionable message:
  `N files still carry vectors from <old embed_id> — run goldmine reindex run
  (est 4 min) or search --lexical`. This is the v1 gate (`fingerprints.py:121-137`)
  re-expressed over data instead of a single meta row — same honesty, far
  smaller blast radius.
- Lexical search always works (extraction identity does not gate FTS — v1
  already took this position, ARCHITECTURE.md:238-243).
- `plan` is always allowed; `run` computes the live embed identity once at
  start (backend.startup() first, as v1 does before clearing,
  `pipeline.py:208-222` — same ordering discipline: never destroy before the
  replacement identity is proven to exist).

`ingest --reindex` remains as a deprecated alias for "plan everything, run it"
(compat), and `reset --reindex` stays the scorched-earth option
(`cli.py:870-902`).

### 4.3 Model manager — `goldmine model install|list|verify|use|check`

**Manifest (`goldmine/model_manifest.py`)** — pinned constants in code, the
DigUp ModelFiles pattern (ModelFiles.swift:10-33):

```python
@dataclass(frozen=True)
class ModelFile:
    key: str; role: str          # role: embedding|mmproj|asr|vad|ocr|server
    repo: str; revision: str; name: str
    bytes: int; sha256: str
    def url(self) -> str: ...    # https://huggingface.co/<repo>/resolve/<rev>/<name>

MANIFEST = [
  ModelFile("embeddinggemma-2", "embedding", "ggml-org/embeddinggemma-2-GGUF",
            "bfcd298762cc34d0357ece5ebdd31791a3a374d8", "embeddinggemma-2-Q8_0.gguf",
            309_855_456, "2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135"),
  ModelFile("embeddinggemma-2-mmproj", "mmproj", ..., 554_821_024, "c4a8a52691ec…"),
  ModelFile("whisper-turbo", "asr", "ggerganov/whisper.cpp", "main",
            "ggml-large-v3-turbo-q5_0.bin", 574MB-ish, "394221709cd5…"),   # [confirmed-02 §5.1]
  ModelFile("whisper-vad", "vad", "ggml-org/whisper-vad", "main",
            "ggml-silero-v5.1.2.bin", 885_098, "29940d98d42b…"),
  # future: whisper-small (190 MB fallback tier), llama-server release asset (§4.7)
]
```

The embedding pin's sha256 equals what `models/SHA256SUMS` and
`llama.py:62-63` already enforce (verified identical), and matches DigUp's
byte-level pin (ModelFiles.swift:26-30). Manifest values are *constants*;
`model verify` recomputes and compares (streaming 8 MB chunks,
ModelFiles.swift:62-75 pattern).
<!-- amended 2026-10-16: B7 (Reviewer 1) — VAD byte pin corrected to 885,098 (the sha was
     already right); manifest-vs-file test made normative -->
Manifest-vs-file test (normative): CI asserts every manifest entry's `bytes` AND `sha256`
against the real downloaded file — the VAD pin above shipped as 885,000 in this doc's
first draft, which is exactly the typo class the test exists to catch.

**Storage layout — decided: app-support, not repo.**

```
<home>/models/<name>.gguf          # final name only after verify
<home>/models/<name>.gguf.partial  # resumable download
<home>/models/<name>.gguf.sha256   # sidecar (existing repo convention, doctor.py:145)
<home>/bin/llama-server            # §4.7 release asset
```

`<home>` = `~/Library/Application Support/goldmine/` (SPEC.md:33-37). Reasons:
(a) pipx/uv installs have no repo — `llama.py:56-57`'s repo-relative vendor path
is already broken for installed copies; (b) Time-Machine exclusion on the
models dir (re-downloadable, ModelDownload.swift:102-105); (c) DigUp ships
exactly this layout (ModelFiles.swift:36-37). The repo `models/` keeps working
as a dev override: resolution order `GOLDMINE_MODELS` env / `models.dir` config
→ `<home>/models` → repo `models/` (dev convenience, last).

**Download protocol** (stdlib `urllib`, one file at a time; DigUp
ModelDownload.swift discipline): `.partial` + `Range: bytes=<n>-` resume
(re-sent across HF CDN redirects, ModelDownload.swift:336-344) → size check →
streaming sha256 → **rename into the final name only when correct**
(ModelDownload.swift:197-200) → write sidecar. Retry backoff 2/5/15/30 s
(ModelDownload.swift:229). Disk-space precheck `needed + 200 MB` slack
(ModelDownload.swift:111-116), error names the missing bytes. Progress: one
stderr line every 2 s (`42% · 12.3 MB/s · eta 0:38`) — stderr only, stdout
stays data-only (SPEC §1.2). HF-hub cache shortcut (copy + verify from
`~/.cache/huggingface/hub` when the pinned snapshot is present,
ModelFiles.swift:41-54) — include, it is ~30 lines and the AGENTS-machine has
models cached there. [pending-02: their `models add` names must map to manifest
keys — proposed mapping: `whisper-turbo`→asr, `whisper-small`→asr-small,
`vad`→vad, `ocr`→*not a download* (it compiles the Swift helper, 02 §5.2),
`embeddinggemma`→embedding+mmproj. **Conflict flagged**: 02 says
`goldmine models add|list`; this doc owns the command and settles on
`goldmine model install|list|verify|use|check` (singular `model`, matching the
brief), with `goldmine models` accepted as a hidden alias so 02's examples work.]

**`model use NAME [--index NAME]`** — binds a manifest model to an index by
writing `meta.embed_identity` *intent*? No: `use` writes nothing it cannot
verify. It records `meta.model_binding = <manifest key>` and *warns* if the
index's live embed_id would change (the planner will show the cost). The
fingerprint itself is only ever written by ingest after a successful embed
(`pipeline.py:1014-1029`) — same as v1. Model binding is **per-index by
necessity** (the fingerprint is per-index); the daemon keys its resident model
off it (§4.4).

**`model check` — the arch-support probe (catches b11146-class failures early).**
The known failure mode: brew llama.cpp 0.5.0 build 11146 cannot load the
`gemma-embedding2` GGUF arch — it dies at model *load*, not at `--version`
(`llama.py:14-16`, `:64-68`, `doctor.py:101-102`). So a version probe is
insufficient. `model check` runs the real thing, once, on demand:

1. resolve server binary (§4.7 order) + model file (manifest/sha verify);
2. spawn llama-server exactly as `LlamaServerBackend.startup()` does
   (`llama.py:298-372`: free port, no foreign adoption
   `:235-244`, `/health` poll `:283-296`, stderr ring buffer);
3. POST one embedding of `"ping"`, assert dim == manifest dim (768);
4. kill child (SIGTERM→5s→SIGKILL, only our own);
5. findings: `{server_source: vendor|home|brew|env, build_string, model_key,
   model_sha_ok, live_embed: ok|<error tail with the server's own
   'unknown arch' line>}` — exit 0 green, exit 4 red, JSON with `--json`.

`goldmine ingest` calls the same probe automatically when it is about to
*first fingerprint* an index (cheap: skip if `meta.embed_id` already matches —
the common case). `doctor` gains `model check` as one row
(`doctor.py:67-143` llama check subsumed).

**Build-from-source vs prebuilt — decided (§4.7 for mechanics):** the repo
keeps the full vendored checkout + build instructions (`vendor/llama.cpp`,
tag b11461 — this is how the current binary got built:
`vendor/llama.cpp/build/bin/llama-server` reports
`0.6.0-dev (build 1, commit 4d756bc)`), but **users get a prebuilt release
asset**; building from source is the documented fallback, not the default path.

### 4.4 Daemon / process substrate — `goldmine serve`

<!-- amended 2026-10-16: Reviewer 2 C2 — the daemon is v2.1 (this section stays the
     design of record); B3 — the memory guard is pressure-aware, never raw free pages -->
**Decision: UDS, not localhost-HTTP+token.** (04 §6.3 already specced UDS
line-JSON; this doc ratifies it as the substrate contract.) Justification:
(a) auth is the filesystem — socket mode 0600 in a user-owned dir; a token file
is a *secret on disk* that leaks into backups and must be rotated, for a threat
model (other local users) that single-user macOS doesn't have; (b) no port
squatting, no ephemeral-port race class at all (v1 needs `_free_port()` +
retry-×3, `llama.py:230-243` — the daemon deletes that whole bug family).
Rejected HTTP upside (curl-ability) is a
non-goal: the CLI is the interface (kill list §4.9). **Placement: the socket
lives at `$TMPDIR/goldmine-serve-<uid>/serve.sock`, NOT under `<home>`** — macOS
enforces a 104-byte sun_path limit and `~/Library/Application Support/goldmine/`
+ long usernames can exceed it; the discovery file records the real path
(the same `$TMPDIR` pattern v1 used for out-of-process helper sockets).

**Lifecycle:**

```
goldmine serve [--linger S] [--foreground]
  1. flock <home>/serve.lock          — single instance (blocking try; second
                                        serve exits 0 with "already serving")
  2. spawn llama-server on demand     — exactly LlamaServerBackend's rules:
                                        never adopt a foreign server
                                        (llama.py:235-244), SIGTERM→5s→SIGKILL
                                        our own children only (llama.py:377-397)
  3. write <home>/serve.json (discovery file, atomic rename):
       {pid, socket, proto_version, build, goldmine_version, started_at,
        indexes: {name: {index_uuid, schema_version}}}
  4. accept loop over line-JSON requests (04 §6.3 protocol):
       hello {index}        → handshake: index_uuid + schema + proto check
       embed {texts}        → vectors via the resident server
       search {query, ...}  → full query path, cache-hot
       status / reload / checkpoint / shutdown
  5. idle: linger (default 300 s, config serve.linger) with no requests →
     shutdown: close stores, kill llama-server, unlink socket, delete serve.json
  6. memory guard (04 §6.3, pressure-aware): sample the kernel memory-pressure
     percentage every 60 s idle + before each search; sustained warn → drop vector
     caches; sustained critical (hysteresis: 2 readings 30 s apart) → exit 0.
     NEVER raw free pages — vm_stat idle-free sits ≈ 1.5 GB on a healthy 16GB box.
```

**Statelessness (crash-only).** The daemon owns *nothing* that is not
reconstructible: every answer is computed from the SQLite files; the vector
cache self-invalidates via `PRAGMA data_version` (bumped by any other
connection's commit — one-shot ingest against the same DB is seen instantly)
with a COUNT fallback (04 §6.3). `kill -9` at any instant leaves: a stale
discovery file (detected by `pid` liveness via `kill(pid,0)` + connect check,
then ignored/deleted by the next `serve` or one-shot), a stale socket (same),
and a llama-server orphan (prevented: the daemon spawns it with a pipe it
holds — SIGKILL of the daemon closes the pipe; llama-server exits on stdin EOF
fallback plus a watchdog thread that kills the child if the parent dies; if
even that fails, `goldmine serve --recover` and `doctor` find llama-servers
whose parent is dead **by our recorded pid**, kill by exact pid, never by name
matching — v1's kill-only-what-we-spawned rule extended with the recorded pid
as the spawn receipt).

**One llama-server across N named indexes.** The model file is
index-independent; what differs per index is the *fingerprint metadata*
(prompts, preprocessing), which is client-side state. Therefore:

- Each open index acquires a **lease** on the resident model keyed by
  `model_sha256`: `{index_uuid, last_used_ms}` in daemon memory (and mirrored
  into serve.json for observability). Requests refresh `last_used`.
- Same model_sha256 → shared server (the common case: N indexes, one model).
- Different model requested → **v2.0: not resident.** The daemon answers
  `embed` for a non-resident model by returning `model_not_resident`; the
  one-shot command falls back to its own cold spawn (v1 behavior). This makes
  the daemon immune to model-thrash (load A/unload B ping-pong) until there is
  evidence anyone runs two models.
- v2.1: `serve.models_resident = 1|2` LRU — idle model unloaded after
  `serve.model_idle` (default 600 s, the AppSettings encoderIdleSeconds
  precedent, AppSettings.swift:28-29).
- Idle semantics: the *server* has no idle timer of its own; it dies when the
  daemon's global linger expires (zero leases across all indexes), or with the
  daemon. Linger refreshes on any request to any index.

**One-shot discovery & fallback.** Every semantic-path command checks, in
order: `GOLDMINE_SERVE` env (explicit socket path; set by `serve install`),
`<home>/serve.json` (path + pid liveness + connect + `hello` handshake on
proto_version + index_uuid match). Any failure → v1 cold path, silently. The
search summary reports which path served it (`via: serve|cold`) — visible, not
silent (v1's honesty rule).

**Auto-start: decided — on, for search only, with two opt-outs.** v2.0
`search` (semantic scorer) auto-starts the daemon when none is running unless
`--no-serve` or config `serve.auto_start=false`. Rationale: the daemon's first
benefit is exactly the second search; cost is a 0.7 s background start (04
§3.7) and 484 MB resident with a 300 s idle exit; `doctor` and `status` show
daemon state honestly (04 §6.3). Ingest does **not** auto-start in v2.0 (it
spawns its own server; 1.1 s on a multi-minute run is noise) and moves onto the
daemon's server in v2.1 (04 item 7 — a straight M once the lease API exists).
`distill` behaves like search in v2.1 (it needs vectors, not the server).

**`serve install` — opt-in launchd, never auto.**
`goldmine serve install` writes
`~/Library/LaunchAgents/dev.goldmine.serve.plist` (RunAtLoad, KeepAlive=false —
linger makes KeepAlive a lie; if the daemon exits on the memory guard it should
*stay* exited), `StandardOutPath/StandardErrPath` under `<home>/logs/`;
`serve uninstall` removes both. `serve status` reports pid/uptime/RSS/indexes/
resident model; `goldmine doctor` includes one daemon row. This mirrors the
house pattern of opt-in user LaunchAgents (mac AGENTS.md autostart precedent).

**Comparison (decided: daemon, auto-started on search):**

| Option | Search latency | Ingest start | Memory at idle | Failure modes |
|---|---|---|---|---|
| Status quo (per-command spawn) | 1.9 s @100k (04 T1) | +1.1 s every run | 0 | port races, spawn variance |
| Spawn-with-linger (each command leaves server + reaper) | warm after 1st | +1.1 s | 484 MB, ownership *handoff* between processes | orphan chains, two owners of one child, reaper bugs |
| **`goldmine serve` (chosen)** | 40 ms warm (04 §6.3) | v2.0: +1.1 s; v2.1: ~0 | 484 MB ≤ 300 s idle, then 0 | one owner, one flock, crash-only by construction |

Spawn-with-linger is rejected despite matching DigUp's per-request encoder:
DigUp owns the whole app process tree; a CLI has no natural owner to hand a
child to, and handoff is precisely where orphan/adopt bugs live.

### 4.5 Multi-index & scoping

**Registry — decided: a file, not a table.** `<home>/registry.json`
(2026-10-16 review: ships **v2.1**, not v2.0 — deferred with a re-entry gate:
it lands when a second real index exists on this machine; see REDESIGN §5):

```json
{"version": 1,
 "indexes": {"default": {"path": "default.sqlite", "index_uuid": "…",
                          "created_at": 1760000000.0, "last_opened": 1760000000.0,
                          "model": "embeddinggemma-2", "schema_version": 2}}}
```

Written by `Store.__init__` (open = refresh last_opened; create = add) via
temp+atomic-rename; read by `goldmine status --all`, `serve` (lazy per-index
open), `doctor --all`. Rationale for file-over-table: the registry must be
readable when an index file is corrupt (to tell the user *which* file), must be
writable when a *new* index doesn't exist yet (bootstrapping: "which DB do I
create?" cannot be answered from inside a DB), and must survive `reset` of any
one index. Explicit `--db PATH` bypasses the registry entirely (it stays a
first-class escape hatch, `cli.py:192-196`); registered names and ad-hoc paths
never mix in one command.

**Daemon sharing** — per-index leases (§4.4); the daemon opens each named index
lazily on first request and holds one `Store` per index.

**Cross-index search — decided: forbid z-merge; ship rank-fusion with honest
labels (v2.1).** The blocker is quantitative, not aesthetic: z-scores are
calibrated per index against *that corpus's* score distribution
(`search.py:294-309` — centering + std over the loaded table). The same file
querying into a 100-file index and a 100k-file index gets systematically
different z; averaging or maxing z across indexes silently favors small
indexes. Rank-based fusion does not have this problem (ranks are ordinal), and
the code already has RRF (k=60) for the scorer baseline (`search.py:312-…`).

- v2.0: `--index a,b` → error suggesting per-index runs (honest, cheap).
- v2.1: `--index a,b` → per-index top-k (k = 3×limit) → RRF-of-rankings merge →
  final list; JSON hits carry `per_index: {a: {score, z, rank}, b: …}` and the
  header states `fused: rrf(k=60) across 2 indexes (scores not comparable
  across indexes)`. Guard: all queried indexes must share the same
  `model_sha256` (registry + meta check); mismatch = exit 3 with the offending
  index named (different model = different geometry, no fusion pretense).
- Presentation dedupe by path when the same file is indexed in both (flag
  `--dedupe`, default on): keep the hit from the index where it ranked highest.
- Never: merged z, merged bm25, or a "universal" score.

### 4.6 Config evolution

- **`config_version` key (top-level, integer, default 1).** The loader reads it
  before applying tables and runs `upgrade_config(parsed, to=N)` migrations
  (rename/move keys with WARN; never silently drop). v1→v2 rename: none needed
  (all v1 keys remain valid); v2 *adds* `[models]`, `[serve]`, `[watch]`
  (02 §5.8), `[asr]`. Unknown keys already WARN, never fail (`config.py:164-167`)
  — keep that, it is what makes forward/backward config compat cheap.
- **Per-index overrides — decided: no per-index config files in v2.0.** What is
  genuinely per-index *already lives in the index* (roots_json
  `pipeline.py:1043-1050`, fingerprint, model binding) — that is where
  per-index state belongs, because the planner reads the index, and a second
  config layer would create identity state the planner can't see. Config stays
  machine/user-level (paths, budgets, serve/linger). If a real need shows up,
  the shape is `[index.work]` tables merged only when `--index work` is active
  — designed, deferred.
- **Env audit.** One function `config.env_audit() -> dict[str, str|None]`
  enumerating every `GOLDMINE_*` var with its target key; `goldmine doctor`
  prints it (SPEC §11 table + new: `GOLDMINE_SERVE`, `GOLDMINE_MODELS`,
  `GOLDMINE_ASR_MODEL` [pending-02 §5.8]). Rule going forward: **only
  `config.load()` reads `GOLDMINE_*`** (already true today,
  `config.py:198-208`) — a var that appears anywhere else is a review-blocking
  bug.
- **Secrets-free guarantee.** (a) config.toml must never need credentials:
  models are public HF files, the daemon has no auth secret (UDS §4.4), crawl
  keeps stdlib-no-auth in v2 (Digest auth for crawl = kill-listed).
  (b) enforcement — **descoped 2026-10-16** (review C2 killed the secrets-pattern
  scan as scope creep): the control is the design rule in (a) plus review-time grep;
  `doctor` gains no secret scanning. The env audit (above) stays.

### 4.7 Packaging & distribution

- **Install**: `uv tool install goldmine` / `pipx install goldmine`;
  console script already declared (`pyproject.toml [project.scripts]`).
  numpy the only hard dep.
- **The two non-pip artifacts** (llama-server binary ~ tens of MB; models 865 MB
  + 574 MB ASR) ship via **GitHub release assets**, installed by
  `goldmine model install`:
  - `llama-server-macos-arm64-<llama-tag>.tar.xz` — built by GHA from our
    pinned llama.cpp tag (b11461 lineage; the vendored tree proves the recipe),
    ad-hoc signed. Python's `urllib` does **not** set the quarantine xattr
    (only LaunchServices does), so no Gatekeeper friction for a binary fetched
    by the CLI — document this; do not codesign-with-Identity (we have none).
  - Model GGUFs from the pinned HF revisions (§4.3).
  - `build-from-source` stays documented (vendor/llama.cpp + cmake preset) as
    the offline/paranoid path. DigUp's release.sh flow (sign→notarize→DMG,
    release.sh:19-45) is referenced as the pattern for a future GUI; a CLI
    needs none of it.
- **Resolution order (v2, supersedes `llama.py:84-101`)**:
  `env/config explicit → <home>/bin/llama-server → vendor (repo dev) → brew
  (with the arch warning, llama.py:64-68)`. The repo-relative vendor check must
  activate only when running from a checkout (pyproject.toml presence at
  REPO_ROOT), fixing the installed-package breakage.
- **Upgrade path**: `uv tool upgrade goldmine` replaces code only. Indexes
  survive: older binaries refuse newer schemas
  (`store.py:194-200`, kept); newer binaries migrate forward (§4.1, with the
  pre-migration snapshot). Models/bin live in `<home>` and are untouched by
  upgrades; `models list --outdated` diffs manifest revisions vs installed
  sidecars.
- **`goldmine version --full`** (provenance, one JSON blob):
  goldmine version+commit, python, platform, numpy, schema_version,
  config_version, server binary path+source+build string (`llama.py:103-129`
  `probe_version`), manifest revisions installed (name, sha256, bytes), per `--index`:
  index_uuid, schema, embed_id, migration history. Mirrors what
  `meta.app_version`/`llama_build`/`fingerprint_json` already record
  (`pipeline.py:1051-1061`, `:224-234`) but never requires opening an index
  (index section only when `--index` given).

### 4.8 Crash-only & integrity

- **WAL policy** (decided): keep WAL default (`store.py:190`). One-shot
  commands do nothing special (SQLite recovers on next open). The daemon runs
  `PRAGMA wal_checkpoint(PASSIVE)` when idle every 60 s and on `checkpoint`
  request; ingest runs one TRUNCATE checkpoint at successful end of run (makes
  backups single-file-honest). No checkpoint inside the per-file hot loop —
  it would fight the writer.
- **`goldmine backup [--out PATH]`** — `VACUUM INTO <tmp> → atomic rename`:
  single-file, consistent snapshot including WAL content, runs against the live
  index (readers keep working). Default output
  `<home>/backups/<index>-<ts>.sqlite`, keep last 3, print sizes. `--out -`
  streams nothing (VACUUM INTO is file-only — document; use sqlite3 CLI for
  exotic needs). Restore = stop daemon for that index, copy back. The registry
  (file) is copied alongside.
- **Deep integrity** — `goldmine doctor --deep` (and `goldmine check` alias):
  `PRAGMA integrity_check`, `PRAGMA fts_integrity_check`,
  `PRAGMA foreign_key_check`, plus the semantic orphans v1's doctor already
  hunts (segments without files, fts rows without segments,
  `doctor.py:252-307`) and new v2 invariants: files with `vec IS NOT NULL`
  segments but NULL `embed_id` (must be zero post-migration), pack header vs
  DB digest [confirmed-04 §6.1], registry entries pointing at missing files.
  Default `doctor` stays fast (no full integrity_check above 50 MB — say so in
  output).
- **Jobs table — decided: NO (v2.0), with an explicit trigger to add one.**
  v1's resume mechanism (pending rows + sha-skip, `pipeline.py:630-641` +
  `ARCHITECTURE.md:377-380`) is already crash-resumable and needs no queue:
  interrupted ingest = pending files re-attempted next run; reindex run =
  re-plan from the per-file dimensions (§4.2). A `jobs` table pays off only
  when there are **multiple producers contending** (watch loop + manual ingest
  + serve-triggered refresh). Trigger: when `goldmine watch` lands (v2.2,
  02 §5.6), add a minimal `operations` table
  `(id, kind, target_index, state, claimed_by, heartbeat_at, payload_json)` —
  not a task queue, just sweep state so two entry points can't double-work;
  until then, single-writer + flock between watch/ingest is the whole story.

### 4.9 Kill list (architecture/data; 06 §4.9 owns the distill-side list)

| Killed | Why |
|---|---|
| Postgres / DuckDB / any second storage engine | Two engines = two consistency domains + a sync problem, to fix a bottleneck that doesn't exist (brute-force scan is 9–26 ms @100–500k, 04 §3.1/§3.4). DuckDB-as-distill-sidecar also dies: calibration is a 300²·768 sampled gram (06 §4.4) |
| sqlite-vec / hnswlib / ANN anything | Measured and rejected (04 §3.5/§3.6): broken macOS arm64 wheels; exact scan wins the eval at our scale. Re-entry only via the 04 item-10 measured-p95 gate |
| Cross-machine client/server (TCP serve, remote indexes) | Local-first mission; needs TLS+auth+wire-versioning for a one-user LAN case that `ssh + goldmine on the mini` already covers. The UDS decision (§4.4) is deliberately un-extendable to TCP |
| Public HTTP API / GraphQL on the daemon | The line-JSON UDS protocol is an *internal* contract; freezing it as public API before 3.x is a stability promise we can't keep. Same for an OpenAI-compatible endpoint (llama-server's job, not ours) |
| Per-segment / per-chunk fingerprint granularity | §4.2: z-score math makes mixed sets unqueryable, so finer granularity buys nothing; per-file is the finest honest unit |
| Plugin marketplace / third-party backend registry | The backend ABC (`backends/__init__.py:77-96`) + pip extras *is* the extension mechanism; a marketplace adds supply-chain review + API freeze with zero user base |
| Multi-process parallel writers to one index | Single-writer is load-bearing (ARCHITECTURE.md:48-58, §7.1); contention is resolved by flock between watch/ingest/serve, never by concurrent writers |
| asyncio rewrite of the pipeline | Threads + subprocesses measured sufficient (04 §6.2: extraction ≤1%, embedding serial-GPU-bound); asyncio would make the single-writer story harder for zero measured gain |
| FSEvents/pyobjc watcher | Measured blocker (02 §5.6): pyobjc not installed, kqueue watches files not trees; stdlib polling reusing the incremental pipeline is enough |
| Per-index config.toml files | §4.6: identity state belongs in the index; a second config layer is invisible to the planner |
| Durable job queue / work daemon before `watch` exists | §4.8 trigger rule — don't build queue machinery for one producer |

---

## 5. Migration & compatibility plan

Ordered, each step independently shippable:

1. **Ship the ladder before schema 2 exists** (S): refactor `store.py:201-203`
   to call `migrations.migrate()`; v1 behavior identical (TARGET=1, empty
   ladder). Test: v1 fixture opens unchanged.
2. **v1→v2 migration + per-file identity backfill** (S): §4.1 DDL; zero
   re-embedding (the migrateVectorNames move). Old binaries on a v2 index
   refuse cleanly (`store.py:194-200`) — release note says "upgrade goldmine".
   <!-- amended 2026-10-16: B11 (Reviewer 1) — the old "v1.2 binary keeps gating"
        sentence was meaningless: a v1 binary never reaches the gate on a migrated
        index; it refuses at open on user_version. Only v2 binaries evaluate the
        refusal rule, and that rule is per-file (refuse only on incompatible embed
        identity — stale chunker/extractor versions never refuse, A2). -->
   There is no transition window in which two binaries disagree about *whether* to
   refuse: cross-version behavior is exactly "v1 binary → refuse at open
   (`store.py:194-200`) with 'upgrade goldmine'; v2 binary → per-file rule".
3. **Planner** (M): `goldmine reindex plan|run`; `ingest --reindex` aliased.
   Invariant flip: exit-3 message changes from "rebuild everything" to
   "N files stale, est T — run goldmine reindex run".
4. **Model manager** (M): manifest, app-support layout, download/verify,
   `model check`; auto-migrate an existing repo `models/` via
   `model adopt --from ~/git/goldmine/models` (copy or link + sidecars).
5. **Serve** (L, 04 item 4): UDS + discovery + linger + memory guard; search
   auto-start (§4.4); `serve install/uninstall`.
6. **Backup/deep-check/registry** (S each): can land in any order.
7. **Cross-index RRF search** (M, v2.1) once the registry + model_sha guard
   exist. **Shared-embedder ingest** (M, v2.1, 04 item 7) once leases exist.

Compatibility promises (normative):

- A v1 index opened by v2 **searches immediately** (lexical + semantic) after
  migration; nothing is re-embedded unless the user asks.
- Any v2 binary can be downgraded to the previous goldmine release *only if*
  `migration_history_json`'s latest entry is ≤ that release's TARGET —
  `version --full` prints this; the ladder never *needs* downgrades because
  migrations are additive (amended 2026-10-16, C6: the cards drop-and-recreate
  moved to the v2.1 E1 migration — the v1→v2 migration ships purely additive,
  no destructive step in v2.0).
- `--db PATH` and `--index NAME` behave exactly as v1 (`cli.py:192-196`).
- Exit code 3 remains "semantic work refused"; its *message* now names the
  planner remedy instead of full reindex.

---

## 6. Phasing & effort

(S ≤ 1 day, M ≤ 3 days, L > 3 days — one engineer; overlaps 04 §8 items,
cross-referenced)

<!-- amended 2026-10-16: Reviewer 2 C1/C2 — v2.0 shrunk: planner (4,5), daemon (8),
     registry (9) and config hygiene (11) move to v2.1; the secrets scan is killed;
     PyPI publish pulls INTO v2.0 (item 12); cards-v2 DDL (item 2) defers with E1.
     Phasing is owned by REDESIGN.md §5. -->

| # | Item | Size | Release | Notes |
|---|---|---|---|---|
| 1 | `migrations.py` ladder + fixture tests (pre-schema-2, behavior-neutral) | S | v2.0 | §4.1 rules 1-6 |
| 2 | v1→v2 migration: identity backfill (B2 binding rule) + `content_date` + pre-migration snapshot; purely additive — cards-v2 tables deferred to v2.1 (E1) | S | v2.0 | the migrateVectorNames move; no re-embed |
| 3 | Per-file identity plumbing in pipeline (`chunker_version`, `embed_id` written at commit) + `EXTRACTOR_VERSIONS` registry | S | v2.0 | §4.2 |
| 4 | `goldmine reindex plan` (dry-run, estimates, plan id) | M | v2.1 (was v2.0) | §4.2; uses 04 §6.6 rates |
| 5 | `goldmine reindex run` (stale-set ingest; gate re-expressed) | M | v2.1 (was v2.0) | §4.2 invariants |
| 6 | Model manager: manifest, install/list/verify/use, download discipline, disk checks, HF-cache shortcut, `model adopt` | M | v2.0 | §4.3 |
| 7 | `model check` live arch probe + ingest first-fingerprint probe + doctor row | S | v2.0 | §4.3; reuses llama.py startup |
| 8 | `goldmine serve` daemon (UDS, discovery file, flock, leases, linger, pressure-aware memory guard) | L | v2.1 (was v2.0) | = 04 item 4; §4.4 is the contract |
| 9 | `registry.json` + `status --all` + doctor registry checks | S | v2.1 (was v2.0) | §4.5; re-entry-gated |
| 10 | `goldmine backup` (VACUUM INTO) + `doctor --deep` | S | v2.0 | §4.8 |
| 11 | `config_version` + env audit (secrets scan killed, C2) | S | v2.1 (was v2.0) | §4.6 |
| 12 | `version --full` + packaging resolution fix (installed-copy vendor bug) + release asset workflow (llama-server tar) + **PyPI publish (C1)** | M | v2.0 | §4.7 |
| 13 | Shared embedder: ingest embeds via daemon leases | M | v2.1 | = 04 item 7 |
| 14 | Cross-index `--index a,b` RRF search + model_sha guard | M | v2.1 | §4.5 |
| 15 | `serve install/uninstall` launchd + `serve status` polish | S | v2.1 | §4.4 (PyPI moved to v2.0, item 12) |
| 16 | Multi-model LRU resident (models_resident=2) + `model use` UX | M | v2.1 | §4.4 |
| 17 | `operations` table + watch/serve flock arbitration | M | v2.2 | only if `goldmine watch` ships (02 §5.6); §4.8 trigger |
| 18 | Pack per-modality row ranges + registry-driven bulk ops (`status --all --json` streaming) | M | v2.2 | = 04 item 9 |

Critical path: 1→2→(3,4,5) is the identity spine; 8 is independent of it and
can start in parallel (04's bench harness gates it).

---

## 7. Risks & open questions

**Risks**

| Risk | Likelihood | Mitigation |
|---|---|---|
| Backfill mislabels a v1 index (e.g. built with `--lexical-only`, vectors NULL) | low | backfill rule: `embed_id` set only where `vec IS NOT NULL`; vectorless files can't be stale-embed. Fixture includes a lexical-only index |
| Planner estimates lie (rates drift per corpus) | medium | rates self-calibrate from the last real run (`meta.rates_json`); first-run estimate labeled "using defaults"; plan vs actual printed after run |
| Daemon/CLI version skew (old CLI, new daemon) | medium | proto_version + goldmine_version in `hello`; mismatch → one-shot falls back to cold path with a WARN, never fails |
| UDS path length / TMPDIR cleanup | low | socket in `$TMPDIR/goldmine-serve-<uid>/` (§4.4); discovery file re-resolves; startup sweeps stale dirs with dead pids (02 §5.6 pattern) |
| Orphaned llama-server after daemon SIGKILL | low | child holds our pipe (dies on EOF) + watchdog thread; `serve --recover`/doctor kill by recorded pid only, never name-match |
| Cards lost at migration annoy users | certain, bounded | announced pre-migration + one-command rebuild (`goldmine distill`); distill_runs keeps future history (06 §4.4) |
| Two identities in flight during a long `reindex run` (user runs search mid-sweep) | certain | the §4.2 gate refuses semantic search while stale_embed > 0, message shows remaining count/ETA; lexical unaffected |
| Registry.json vs manual file copies (user cp's an index) | low | index_uuid mismatch detected on open (registry uuid ≠ meta uuid) → registry self-heals to the file; WARN |
| Model manifest rot (HF revision disappears) | low | pins are immutable revs (DigUp's exact pin has been stable, ModelFiles.swift:12); verify failure says "pin dead — bump manifest" |

**Open questions (owner in brackets)**

1. Auto-start-on-search default (§4.4): I chose ON with `--no-serve` opt-out;
   confirm no one expects `search` to never leave a process behind. [04/owner]
   **Resolved 2026-10-16 (review C2): moot in v2.0 — the daemon is v2.1, so no
   auto-start and no init consent step ship in v2.0. Re-ratify ON-with-opt-outs at
   C4 acceptance.**
2. Should `content_date` (§4.1c) be populated by which extractors first — PDF
   creation date vs transcript date vs EXIF — and does `--after` prefer it over
   mtime? Column ships NULL-safe regardless. [06 §6 open question]
3. Command-name reconciliation: this doc's `goldmine model …` (singular) vs
   02 §5.8's `goldmine models add|list`. Alias proposed; 02's doc examples
   should converge on the singular form. [02]
4. `model use` per-index binding UX: is `goldmine model use whisper-turbo`
   (global default) plus implicit per-index pinning on first use acceptable, or
   must binding always be explicit? [02]
5. Does the daemon ever need to serve `distill` (vector loads) in v2.0 or is
   distill fine cold? (Loading 100k fp16 = ~0.5 s; probably cold.) [06/04]
6. Migration fixture provenance: build `tests/migrations/v1_index.sqlite` from
   the current test suite's synthetic corpora (deterministic mock backend) so
   it can be regenerated, or check in a frozen binary? Leaning: generated at
   test time from a checked-in script + frozen sha of the result. [tests]
