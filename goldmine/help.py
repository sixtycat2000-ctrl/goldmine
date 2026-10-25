"""`goldmine help <topic>` — static topic guides (v2 item 21 / F7, 03 §5.8).

Plain text embedded here, not a man page (pip installs have no mandir wiring;
03 §5.8 decided: --help per command + these topics + the README). Each topic is
self-contained and names only commands/flags that actually exist. Adding a
topic = one dict entry; tests assert every topic renders and unknown topics
exit 2 with the topic list.
"""

from __future__ import annotations

TOPICS: dict[str, str] = {
    "getting-started": """\
getting-started — from zero to a result in 3 commands

  1. python3 -m pip install -e .          (in a goldmine checkout)
  2. goldmine init                         guided setup: env check, model,
                                           folders, first ingest, demo search
  3. goldmine search "kubernetes pod eviction"

The one rule: goldmine only searches folders you add — never your whole Mac.
`init` asks you to pick folders explicitly (or creates an empty
~/Documents/goldmine/ for you); nothing is swept by default.

init is resumable: Ctrl-C any time and re-run `goldmine init` — it continues
where it stopped. `goldmine init --restart` starts over. `--yes` runs
non-interactive (creates the dedicated folder, installs the model, skips the
optional speech/OCR setup; add `--media` to include it).

No embedding model yet (or no llama-server)? Everything still works as a
keyword search: `goldmine search <words> --lexical`, and
`goldmine ingest <folder> --lexical-only` builds a keyword-only index you can
upgrade later by re-running ingest.

See also: goldmine help ingest · search-syntax · models · media · maintenance
""",
    "ingest": """\
ingest — index files and URLs

  goldmine ingest <PATH-or-URL> [more paths…]

Folders are walked recursively (skips secrets, minified bundles, dataset
folders, node_modules-class noise). URLs are crawled with a page/depth budget,
respecting robots.txt.

Common flags:
  --code              include source code / data files (off by default)
  --dry-run           classify only — nothing written, no backend needed
  --lexical-only      index text without embeddings (keyword-only index)
  --reindex           clear extracted content and re-extract everything
  --refresh-thin      re-extract scanned/thin PDF pages (OCR upgrade)
  --exclude GLOB      repeatable; relative or absolute patterns

Every file gets an honest capability row (visible in `goldmine status`):
  content-indexed   fully searchable (text + vector)
  metadata-only     name-searchable (e.g. images while the OCR/embedding path
                    for that kind is off)
  unsupported       never read (secrets, archives) — with the reason
  failed            tried and failed — with the reason

Ctrl-C is safe: every file commits atomically; re-run the same command and
unchanged files are skipped by content hash. Unreadable (macOS-protected)
folders are reported as "protected by macOS (TCC)" — grant Full Disk Access
to your terminal, or index a copy elsewhere.

See also: goldmine help media (audio transcription, screenshot OCR)
""",
    "search-syntax": """\
search-syntax — queries, filters, honesty knobs

  goldmine search "kubernetes pod eviction"     hybrid (meaning + words)
  goldmine search "TRX50"                       rare tokens auto-invert to a
                                                lookup-style exact match
  goldmine search "\"exact phrase\""                quoted = exact phrase

Inline filters (anywhere in the query, case-insensitive prefixes):
  kind:pdf|doc|code|image|audio|video|webpage   repeatable via |
  under:~/notes/projects                        path prefix (~ expanded)
  after:2026-01-01   before:2w                  content date when known, else
                                                mtime (7d/2w/3m work)
  site:example.com                              web pages from one host

Unknown prefixes are NOT filters — they stay literal search words. A filter
that matches nothing yields zero hits, honestly (exit 0).

Useful flags:
  --why            per-hit score decomposition (score parts, bm25 rank)
  --scorer lexical force keyword-only ranking (also suppresses the
                   fingerprint refusal for stale-model indexes)
  --kinds doc,pdf  same as kind: filters, as a flag
  --limit N        default from config (20)
  --format fzf     one TSV line per hit (path kind loc excerpt score) for
                   the fzf shell widget (scripts/shell/)

Zero hits always prints `nothing found for '…'` plus a next step — never an
error. Exit codes: 0 found-or-honest-zero · 3 index/model fingerprint mismatch
(`goldmine reset --reindex` rebuilds; `--lexical` searches anyway).
""",
    "models": """\
models — the pinned model manager

  goldmine model list                  every pinned model + install state
  goldmine model install KEY           download (resumable, sha256-verified)
  goldmine model install KEY --from FILE
                                       register a local file (no copy)
  goldmine model adopt FILE KEY        same, explicit
  goldmine model verify [KEY]          recompute bytes + sha256 vs the pin
  goldmine model use KEY               set the default embedding model
  goldmine model check                 LIVE probe: spawn llama-server, embed
                                       "ping", assert the vector dimension

Pinned keys (manifest = code, in goldmine/model_manifest.py):
  embeddinggemma-2-Q8_0     310 MB embedding model (default)
  whisper-large-v3-turbo-q5_0  574 MB speech transcription
  silero-vad-v5.1.2         0.9 MB voice-activity detection (ASR needs it)

The brew llama.cpp build cannot load the embedding arch (b11146-class trap):
`model check` catches it BEFORE your first ingest and names the fix (use the
release asset at <home>/bin/llama-server or the vendor build). A model switch
changes the index fingerprint: semantic search refuses (exit 3) while vectors
from the old model remain — `goldmine status` shows the stale set, and plain
`goldmine ingest <roots>` re-embeds it.

Downloads are .partial-resumable and retried automatically; a failed transfer
continues from where it stopped.
""",
    "media": """\
media — speech transcription (ASR), screenshot OCR, image/audio vectors

Enable during setup: `goldmine init --media` (or answer yes at init's media
step). Later: `goldmine model install whisper-large-v3-turbo-q5_0` (installs
the small VAD model too) — plus the goldmine-ocr helper (`goldmine doctor`
names the fix when missing).

Image/audio/video vectors (visual search): install the mmproj model
(`goldmine model install mmproj-embeddinggemma-2-Q8_0`) and the
goldmine-embed helper (the release asset into <home>/bin, or
scripts/build_embed_helper.sh in a checkout). When both resolve, photos/
screenshots get image vectors (token budgets photo 140 / screenshot 280),
thin PDF pages get page-image vectors, glyph-order pages keep a meaning-only
text vector, audio WITHOUT a transcript gets raw-audio vectors —
transcribed speech never gets both (measured DigUp lesson) — and videos get
keyframe image vectors (budget 70: one sample every ~3 s, near-duplicate
frames merged by an 8×8 aHash within 4 bits, JPEG ≤1024 px, at most 1200
frames per video). Enabling this
joins the media fields into the embedding identity: that index re-embeds
once (`goldmine ingest --reindex`).

Audio/video: whisper transcribes (measured ~7x realtime — a 1 h podcast in
~8 min), segments become searchable with `0:06s`-style locations; `goldmine
open --hit N --query …` jumps QuickTime/IINA to the moment. Config: [ingest]
asr = auto | always | off (auto = transcribe when whisper + models resolve).

Screenshots/scans: the Vision OCR helper reads text on images and thin PDF
pages; the OCR text is keyword-searchable always, and becomes a text vector
ONLY under text-only installs (where no image vector carries the meaning).

Everything media is honest per file in `goldmine status`: metadata-only rows
carry the reason (e.g. "asr unavailable"), failures are never silent, and
transcripts are derived data worth protecting — `goldmine backup` exists
because re-running ASR costs real time.

See also: goldmine help maintenance (backup), models (install keys)
""",
    "maintenance": """\
maintenance — status, doctor, backup, reset

  goldmine status            the honest state: indexed roots, per-kind
                             capability grid, staleness ("N files stale since
                             … — re-run ingest"), pending/failed rows
  goldmine status --json     the same as versioned machine output
  goldmine doctor            environment checks with copy-pasteable fix lines
  goldmine doctor --deep     + index integrity/FK checks, staleness, cards,
                             backup health (slower on big indexes)
  goldmine backup            single-file snapshot (VACUUM INTO) while the index
                             stays usable; verified before it counts;
                             <home>/backups/<index>-<timestamp>.sqlite,
                             --keep N (default 5), --out PATH for one-shot
  goldmine reset --yes       delete the index (asks when interactive)
  goldmine reset --reindex   clear segments + re-ingest the stored roots

Restore = stop anything using the index, copy a backup file back over it.
The goldmine home lives under ~/Library (Time-Machine covered); backups are
the second copy that makes ASR/OCR transcripts restorable in minutes.

Staleness never refuses: after a chunker/extractor upgrade, `status` says how
many files are stale and plain `goldmine ingest <roots>` re-does them. Only a
model/fingerprint change gates (exit 3) — vectors from different embedding
models must never mix.
""",
    "serve": """\
serve — the warm-search daemon (opt-in)

  goldmine serve            start it (foreground; Ctrl-C stops cleanly)
  goldmine serve status     what it is doing (human, or --json)
  goldmine serve stop       stop it
  goldmine search "…" --no-serve   one search without the daemon

`goldmine serve` keeps ONE llama-server resident so semantic searches skip the
~1s model load: warm searches typically land well under 300ms end-to-end (a
cold search is ~2s). The model spawns lazily on the first search — an idle
daemon holds ~0 MB. The daemon holds NO index state: it only embeds; your
index, vectors and search math stay where they were. Clients find it via
<home>/serve.json (or a GOLDMINE_SERVE=socket-path override); config:
[serve] linger = 300 (idle seconds before exit — only searches refresh the
timer, not status/stop), timeout_s = 10.

Memory honesty: the resident model server costs ~484 MB; index pages live in
reclaimable file cache. Under memory pressure the model steps aside and stays
aside until the system recovers (the daemon watches the kernel's pressure
level: sustained warn unloads the model, sustained critical exits the daemon
cleanly). On a 16 GB Mac hitting level 2 during a heavy build is ordinary —
the daemon yields, searches fall back to cold (~2s), and residency resumes
once the level returns to normal. `serve status` and `goldmine doctor` always
say where you stand (unloads, last unload time, current level).

The cold-fallback promise: serve problems can never break search — worst case
a search is slow once. A dead daemon, a stale discovery file, a model switch,
or a mid-search failure all degrade to the exact pre-serve cold path (the
`via: serve/cold` line on stderr tells you which ran; search output itself is
unchanged). `serve stop` and searches never kill processes they did not
spawn: the daemon records a verified receipt for its llama-server child, and
the next `serve start` sweeps an orphaned child only after checking that
receipt (SIGKILL a daemon and the child is still cleaned up).

Security: the socket is a 0600 Unix-domain socket under a 0700 per-user
directory ($TMPDIR/goldmine-serve-<uid>), so filesystem permissions are the
whole boundary — same user only. The llama-server itself binds 127.0.0.1 on
an ephemeral port exactly as a cold search does; a UDS has no network
exposure at all (Tailscale/VPN irrelevant). `serve stop` is callable by any
process of your user — accepted (worst case the next search pays a ~0.7s
respawn). Request caps: 64 texts and 256 KiB per text per request (when both
are exceeded the count error wins).

See also: goldmine help maintenance (doctor row), models (the model it serves)
""",
    "watch": """\
watch — folder auto-follow (dropped files become searchable within minutes)

  goldmine watch             watch [init] roots (foreground; Ctrl-C stops)
  goldmine watch --roots A B watch those folders for THIS session instead
  goldmine watch stop        stop the daemon (verified SIGTERM)
  goldmine watch --forever   no idle exit — the launchd deployment form

The scope invariant, mechanically: watch only ever follows folders you
explicitly added — [init] roots (chosen at `goldmine init`) by default, or
`--roots PATH…` on the command line (the same explicit act as `goldmine
ingest <path>`). Nothing is swept, ever. An ephemeral `--roots` session is
NEVER written to config; the paths are recorded once in the index's roots
(scope record, like any manual ingest) and may linger in `goldmine status`
afterwards — that is pre-existing roots_json behavior, removable with
`goldmine reset`.

How it works: a cheap scan detects changes (new/edited/deleted), a 2s
debounce collapses editor bursts, and the ordinary ingest pipeline does the
work (content-hash skip — a crash or reboot never re-does finished work).
Cadence adapts: 5s right after activity, doubling to 120s when quiet; a
backoff floor also keeps passes at least ~25s apart and the scan cost under
half the duty, so a churning folder cannot spawn a model storm.

Power honesty: under thermal throttling passes pause (scans continue); on
battery only batches of <= 50 changed files run and passes are paced >= 2min
apart (sweeps wait for AC); at kernel memory-pressure level >= 2 a pass
defers one tick (the serve daemon yields to the same signal). Every hold is
named in `goldmine status`, `goldmine doctor` and the heartbeat — never a
silent skip. Low Power Mode is reported but does not gate (v2.2).

Latency: a dropped file becomes searchable within `current interval + 60s`
(at the slow cadence <= ~3 min; right after you touch things, seconds).
Deletions are pruned by a throttled sweep (<= ~15 min). Known limit: an edit
with identical size AND mtime is invisible to the quick check (rsync-class
limitation) — caught by the periodic sweep, at the next watch start, or by
plain `goldmine ingest <roots>`.

`goldmine watch` and `goldmine serve` run together by design (separate
locks; the watcher spawns its own cold model per pass and defers under the
memory pressure that would make them fight).

See also: goldmine help ingest (what a pass does) · serve (coexistence) ·
maintenance (doctor row)
""",
    "troubleshoot": """\
troubleshoot — the failures people actually hit

no index at … — ingest something first
  fix: goldmine init        (or: goldmine ingest <roots>)

search: no embedding backend available
  fix: goldmine model check   FIRST — a broken server/arch masquerades as this
  or: goldmine search <query> --scorer lexical
  or: goldmine ingest <folder> --lexical-only   (keyword-only index)

backend failed to start (llama-server)
  why: carries llama-server's own error line
  fix: goldmine model check
  Known trap: brew's llama.cpp cannot load the embedding arch — use the
  release asset (<home>/bin/llama-server) or the vendor build.

refused: the index was built with a different embedding fingerprint (exit 3)
  why: vectors from the old model are still in the queryable set
  fix: goldmine reset --reindex
  or: goldmine search <query> --lexical        (keyword-only, no refusal)

N file(s) failed to extract (exit 5 — everything else was indexed)
  why: the first failure path + reason; per-file, never the whole run
  fix: goldmine ingest <roots>   (retries exactly the failed files)

protected by macOS (TCC) — folder walked as unreadable
  fix: grant Full Disk Access to your terminal (System Settings → Privacy &
  Security → Full Disk Access), or index a copy of the folder elsewhere.

model install failed / download keeps failing
  fix: re-run the same command — the .partial file resumes
  fix: goldmine model verify <KEY>   (pin vs bytes, if it keeps failing)

nothing found for '…' (exit 0 — honest zero, not an error)
  try: fewer/shorter words · drop kind:/under: filters · check
  goldmine status — if vectors are 0, re-run `goldmine ingest <roots>` with a
  model installed.

more on any failure: goldmine doctor
""",
}


def render_topic_list() -> str:
    """The `goldmine help` topic list."""
    rows = []
    for name, text in TOPICS.items():
        first = text.splitlines()[0] if text else ""
        # strip the "<name> — " prefix the topics open with
        summary = first.split(" — ", 1)[1] if " — " in first else first
        rows.append(f"  {name:<16} {summary}")
    return "topics: goldmine help <topic>\n" + "\n".join(rows)
