# goldmine v2 — Product & UX (03)

> The redesign pass that makes goldmine **effortless** without surrendering the CLI identity:
> no GUI app, no menu bar, no daemon you didn't ask for — a Python CLI that a stranger can
> install, get a relevant result from inside ten minutes, and keep living in. Everything here
> is opinionated and concrete; where a sibling doc owns a mechanism, this doc owns the
> *surface* the user touches and defers (04 §6.3 owns the warm path, 05 §4.3 the model
> manager, 05 §4.4 the daemon substrate, 02 §5.6 watch mode).
>
> Product reference throughout: [DigUp](../../../DigUp) — same model, same machine class,
> a GUI whose onboarding/honesty patterns translate to dialogue states and output design.

---

## 1. Mission & UX target metrics

**v2 mission (product reading of 04 §1's "search anything LOCAL and ACCURATE and easy"):**
every capability visible, every error self-remediating, the first result reachable without
reading any documentation. The tool stays a CLI; "easy" is delivered by an onboarding
command, an interactive finder, universal error structure, and honest output — not by a
window server.

| # | Metric | v1 today | v2 target |
|---|---|---|---|
| U1 | Cold install → first relevant result | ~7 commands incl. pip, doctor, manual GGUF placement (README.md:44-55); undocumented model/brew-arch traps (README.md:26-29) | **≤ 4 commands, ≤ 10 min**: `pip install goldmine` (real PyPI publish — v2.0, review C1) → `goldmine init` → `goldmine search "…"` |
| U2 | `find` keystroke → rendered results, warm | no TUI exists | **deferred with the find TUI** (2026-10-16 review: killed as a v2.0 metric) — v2.0's warm-path target is in-process `search` p50 ≤ 300 ms |
| U3 | Failure paths that name an exact copy-pasteable fix command | partial — doctor has fix_hints (doctor.py:27-29); ad-hoc elsewhere | **≥ 95%** of audited failure paths emit `fix:` + `more: goldmine doctor` (§7) |
| U4 | Ingest feedback latency / ETA error | summary printed only at end (cli.py:290); no ETA | ticker visible **≤ 2s** after start; ETA ±20% (04 T7/T10) |
| U5 | Zero-hit behavior | honest empty + one line (cli.py:434-435) | honest + a *next step* (widen scorer / check kinds / ingest more), never an error |
| U6 | Machine output stability | stable but unversioned bare payloads (SPEC §2-7) | additive-only evolution promise; all **new** commands emit `{"schema": "goldmine/1", …}` (§5.5) |
| U7 | Open-in-context honesty | nothing opens anything | per-kind jump with a stated ceiling (PDF: terms into Preview search, not a page jump — §5.3); failure never silent |

Non-goals (§5.9 kill list): GUI, menu bar item, telemetry, accounts, auto-update.

<!-- amended 2026-10-16: Reviewer 2 C2 — find TUI killed from the roadmap (revisit with a
     named user), serve deferred to v2.1; the interactive layer for v2.0 is `search` with
     excellent output + `--format fzf` + the shell widget (§5.6, pulled forward). U1's
     install step is now a real PyPI publish (C1). -->

---

## 2. v1 state (what exists, with file:line)

**Command surface.** Seven commands: `ingest | search | distill | status | doctor |
backends | reset` (SPEC.md:60-62; parser at cli.py:71-146, argparse with global options
cli.py:53-68). Exit codes 0/1/2/3/4/5/130 as constants (cli.py:33-39, SPEC.md:41-52).
stdout is data-only, progress to stderr (SPEC.md:56-58, honored by cli.py:269-271 logging).

**First-run reality.** The README quickstart (README.md:44-55) assumes: a repo checkout
(`cd ~/git/goldmine`), `pip install --user -e .` for numpy, and GGUFs already in `models/`
(README.md:28-29) — plus the trap that brew's llama.cpp b11146 cannot load the
`gemma-embedding2` arch, so a vendor build is required (README.md:26-28). Config points at
the repo-relative model path by default (SPEC.md:365-367). There is no `init`; the user
must know to run doctor, then ingest with the right roots, then search. U1 is roughly
7 commands and easily 30+ minutes with the arch failure in the middle.

**Search output.** Plain text hit blocks — rank, score/z/cov, kind, path, loc, excerpt —
no color, no term marking (cli.py:441-487; `util.excerpt` is a plain whitespace-collapse +
truncate, util.py:131-136). Location rendering already per-kind: `p.N`, `0:06s`, `L120-128`
(cli.py:490-503). Zero hits exit 0 with `nothing found for …` (cli.py:434-435). The raw
score components a `--why` needs are all in the hit and the JSON (SPEC §3: score, z,
cosine, coverage, segment, file_status, scorer; rendered cli.py:444-464), and query terms
are computable (`Searcher.terms`, search.py:259).

**Ingest output.** One summary block after the run (cli.py:298-354): per-capability counts
with kind breakdowns, reasons, vectors/segments/fingerprint line, notices. Good honesty,
zero in-run feedback beyond INFO lines gated behind `-v` (cli.py:269-271).

**Error UX inventory (what exists today, audited in §7):**
- ingest without backend: message + hint incl. the `--lexical-only` alternative
  (cli.py:244-251) — the best error in v1.
- search without index: one line "run `goldmine ingest` first" (cli.py:363-365);
  same in status with data-home named (cli.py:512-518); distill likewise (cli.py:653-655).
- fingerprint mismatch: message + remediation printed (cli.py:400-408, exit 3).
- reset interactive confirm with explicit non-interactive instruction
  "re-run with --yes" (cli.py:904-931) — another good pattern, trapped inside one command.
- distill corpus-too-small: honest count + "ingest content first" (cli.py:686-693).
- top-level handlers: BackendUnavailable prints fix_hint when present (cli.py:995-998);
  BackendStartupError maps to exit **2** with a documented deviation note (cli.py:999-1007);
  EmbeddingError exit 1 (cli.py:1008-1011); catch-all prints type+message (cli.py:1015-1018).
- doctor: OK/WARN/FAIL rows with detail + `fix:` hint lines (cli.py:779-789;
  CheckResult doctor.py:24-29), and doctor never fixes anything (doctor.py:2-4).

**What v1 lacks product-wise:** no onboarding, no interactive anything, no opener (nothing
ever launches Preview/QuickTime/editor), no `find`, no `serve` surface, no NO_COLOR env,
no ticker/ETA, no integrations. `serve`, `model`, `find`, `watch`, `reindex` do not exist
in the parser (cli.py:81-146).

---

## 3. DigUp product reference (what to copy, with file:line)

**Onboarding as a state machine.** Four steps (welcome → folders → hotkey → ready) plus a
fifth code step only when the Mac has code (Onboarding.swift:6-9, 62-64, 112-115). The
model download starts at "Get Started" and its status rides along at the footer of every
later step (Onboarding.swift:138-140, 249-272, 316-345); chosen folders start indexing the
moment you continue past them (Onboarding.swift:142-145). Folder defaults are the six home
folders that exist (Onboarding.swift:126-132); code search is **off unless turned on**
(Onboarding.swift:526). TCC is pre-warned in the folders step: "macOS will ask before
DigUp can read folders like Desktop…" (Onboarding.swift:443-451). The ready step states
what will happen and when search becomes useful, honestly, including "results fill in as
it goes" and name-only search until the model lands (Onboarding.swift:653-664). Privacy is
one sentence on the welcome screen (Onboarding.swift:375-380).

**Download discipline as UX.** Resumable `.partial` + HTTP range (ModelDownload.swift:5-8,
336-344), sha256 checked before the file earns its real name (ModelDownload.swift:9-10,
193-219), auto-retry 2/5/15/30s (ModelDownload.swift:229-239), disk precheck naming the
missing bytes (ModelDownload.swift:111-116), HF-cache clone shortcut (ModelDownload.swift:16-17,
117-135), and a *four-state* progress vocabulary: "42% · about 1 min left" / "Checking the
download…" / "Waiting for the network…" / named failure (ModelDownload.swift:65-78).

**Estimates as product copy.** Measured per-kind seconds, "the app will calibrate these
with a few seconds of real work on first launch" (Estimator.swift:5-7); first-pass vs
"later" split for long files (Estimator.swift:37-40, 74-93); per-subfolder breakdown so a
big first pass has an address (Estimator.swift:116-131); footer copy "First pass: about
X · Y GB" or "Choose at least one folder" (Onboarding.swift:283-296). The dev CLI prints
the same estimate per folder with biggest subfolders (main.swift:253-270).

**Result presentation.** Excerpts wrap matched words in `« »` (Results.swift:17, 56-66);
a per-hit *place badge*: `p. 3`, `0:06`, `♪ 0:25` (video sound), `L12–40`
(Results.swift:68-79); a per-hit *reason sentence* ("Matches the picture", "Has your
words", "Matches the text on page 3" — Results.swift:97-113); a details line (kind · date ·
dimensions/pages · size — Results.swift:115-141); strong results grouped by kind with the
weak tail (below half the top score) folded underneath (Results.swift:212-244). The CLI
mirrors this compactly: rank/kind/name + `[place]` + `(why)` + tilde folder + excerpt +
a timing footer (main.swift:366-377).

**Open-in-context machinery.** PDFs: an Open-Documents Apple event carrying
`keyAESearchText`, so Preview fills its search box with landmark words and lists matches
by page — **it does not jump by itself** (Moments.swift:99-107, 153-163). Recordings:
QuickTime via `/usr/bin/osascript` with whole milliseconds (deliberately a subprocess so
macOS's control prompt can't deadlock a main thread — Moments.swift:172-193), falling back
to plain open on -1743 (not allowed) / -1712 (no answer) (Moments.swift:196-217); IINA via
`iina://open?url=…&mpv_start=` (Moments.swift:140-142, 165-170); the *open button title
changes* to "Open at 0:06" only when the default app is known to jump (Moments.swift:42-49,
112-115). Quick Look jumps via displayState set twice (0.6s, 1.2s) because the panel drops
early sets (Moments.swift:58-77). Code: a 23-editor table, each with its exact method —
URL schemes (`vscode://file/<path>:<line>:1`, cursor, windsurf, vscodium, `txmt://`,
`x-bbedit`), bundled commands (Zed `Contents/MacOS/cli`, Sublime `subl`, Xcode `xed --line`,
JetBrains launcher `--line`), chosen preferring a *running* editor, with a fallback chain
scheme → bundled binary → plain open (CodeEditors.swift:8-21, 27-55, 64-128).

**Always-visible state.** Menu bar icon with a progress ring, pause, and a status line +
detail + progress header (StatusMenu.swift:76-188, 191-227) — the *pattern* (one glanceable
state surface) transfers even though the menu bar itself is killed (§5.9).

**Keys as documentation.** Seven-row key table in the README (DigUp README.md:53-61) and
the same table rendered inside onboarding's shortcut step (Onboarding.swift:580-592) —
learn-it-where-you-use-it.

**Unreadable ≠ empty.** A privacy-denied folder looks empty to a crawl and must never be
mistaken for "everything was deleted" (main.swift:122-126); the estimate command reports
"can't read it (not there, or not allowed in macOS privacy settings?)" per folder
(main.swift:203-212).

---

## 4. Gap analysis

| Capability | DigUp has | goldmine v1 | v2 answer |
|---|---|---|---|
| Guided first run | 4-5 step onboarding, model rides along (Onboarding.swift:6-9) | none; README assumes repo + GGUFs + vendor build (README.md:44-55, 26-29) | `goldmine init` state machine, resumable, `--yes` (§5.1) |
| Model obtainment | resumable, sha-pinned, cache-shortcut download with 4-state copy (ModelDownload.swift:5-17) | manual GGUF placement; sha checked only at use (README.md:28-29; SPEC.md:408-412) | `goldmine model install` UI per 05 §4.3; init drives it (§5.1) |
| Arch-failure trap caught early | n/a (bundles its runtime) | discovered at first search, as a confusing load error (README.md:26-28) | init runs `model check` live probe *before* first ingest (05 §4.3; §5.1 step 4) |
| Time-to-result estimate | Estimator + footer copy, self-calibrating (Estimator.swift:5-7, Onboarding.swift:283-296) | nothing | init/ingest print ETA; `--estimate` lands with 04 §6.6 (§5.1, §5.5) |
| In-run progress | per-file event lines `[ 42/120] …` (main.swift:292-297), menu ring (StatusMenu.swift:113-123) | INFO lines only behind `-v` (cli.py:269-271) | stderr ticker + ETA, 2s cadence (§5.5) |
| Interactive search | global hotkey panel, grouped results, weak tail folded (Results.swift:212-244) | one-shot `search` only | `goldmine find` curses TUI (§5.2 — deferred 2026-10-16; v2.0 answer: `--format fzf`) |
| Open at the match | full Opener + CodeEditor machinery (Moments.swift, CodeEditors.swift) | nothing | `opener.py` + `goldmine open` (§5.3) |
| State you can see | menu bar (StatusMenu.swift:76-188) | `status`/`doctor` on demand | `serve status` + doctor daemon row + `via: serve\|cold` in search footer (§5.4) |
| Error self-remediation | per-folder TCC honesty (main.swift:203-212) | good fragments, no pattern (§2) | universal 4-line error shape + retrofit (§7) |
| Keys doc | README table + in-product table (README.md:53-61) | n/a | find key table in `--help`, `goldmine help find`, and the TUI's `?` overlay (§5.2, §5.8) |
| Warm repeat search | resident in-app model | 0.6-0.9s spawn + load every command (04 §3.7; README.md:57-58) | `goldmine serve` + auto-start-on-search per 05 §4.4 (§5.4) |

---

## 5. v2 design

### 5.1 `goldmine init` — onboarding (v2.0)

A resumable state machine; the CLI equivalent of DigUp's onboarding with the model
download riding along. States stored in `<home>/init_state.json` (one JSON object:
`{step, choices, started_at}`); every step is idempotent so re-running `goldmine init`
after Ctrl-C (exit 130) or a crash resumes at the interrupted step, skipping completed
ones (verified model → skip; already-ingested unchanged roots → skipped by the existing
sha256 path, cli.py:104-106's v1 contract).

```
goldmine init [--yes] [--root PATH]... [--index NAME] [--no-serve] [--skip-demo]
```

| # | State | What happens | Failure behavior |
|---|---|---|---|
| 1 | `welcome` | 4 printed lines: what goldmine is, the one-sentence privacy line ("everything stays on this Mac; the only network use is downloading the model once" — DigUp's welcome copy, Onboarding.swift:375-380), what the next screens will do. `--yes` prints and moves on | — |
| 2 | `env` | the doctor check set relevant to init (python, numpy, pdftotext/textutil presence, disk free) rendered as a compact table; every FAIL prints its fix command | interactive: offer "fix and re-check" loop; `--yes`: abort exit 4 with the fix lines |
| 3 | `model` | resolve embedding GGUF per 05 §4.3 order: manifest-installed (sha ok) → HF-cache clone+verify → download with the 4-state stderr line every 2s (`42% · 12.3 MB/s · eta 0:38`, resumable `.partial`) | disk-short → named-bytes error (ModelDownload.swift:111-116 pattern); hash-twice-failed → stop and point at `goldmine model verify` |
| 4 | `check` | run the `model check` live probe (spawn + embed "ping", dim 768 — 05 §4.3) so the b11146-class arch failure dies **here**, with its exact remedy, not at first search | interactive: show fix (server source table), offer re-check; `--yes`: exit 4 |
| 5 | `folders` | propose defaults: `~/Documents ~/Downloads ~/Desktop` that exist (DigUp's candidate list narrowed to the three a docs-and-notes CLI cares about; Onboarding.swift:126-132 precedent), show per-root file counts **plus media counts** (images / audio hours / video hours via Spotlight metadata — the honest estimate must include ASR/OCR cost, not just file counts; review C5); `[Y/n]` / edit; `--code` stays **off** here with a one-line "code search is opt-in: re-run with `--code` or ingest with `--code`" (DigUp keeps code off unless asked, Onboarding.swift:526). TCC pre-warning line if a root is a TCC-guarded folder (Onboarding.swift:443-451 pattern); an unreadable root prints "can't read it (macOS privacy settings?)" and is dropped from the proposal, never silently swallowed (main.swift:203-212 pattern) | empty selection → re-prompt; `--yes` accepts defaults |
| 6 | `ingest` | print the ETA first — **including a media line** (`~N screenshots → OCR · ~M h audio → ASR`; C5) — then run ingest with the ticker (§5.5); first fingerprint written here, so `model check` (step 4) has already de-risked it. **Media-model offer (C5):** if media counts are non-zero, offer "transcribe audio + read screenshots? installs whisper-turbo (574 MB) + goldmine-ocr [Y/n]" — default from a stated `--media` flag (`init --media` / `init --no-media`); skipping = media stays metadata-only, stated honestly, installable later via `goldmine model install` | partial failures → exit 5 copy, init continues (index is usable); cancelled → state saved, resume message |
| 7 | `demo` | pick two query terms sampled from the corpus's top TF-IDF terms (the same machinery distill uses), run two real searches, print the top hit of each with the result-card format (§5.5) | zero hits still prints honestly + the widen suggestion (U5) |
| 8 | ~~`serve`~~ | **Removed in v2.0** (2026-10-16 review, C2: the daemon is deferred to v2.1) — no consent step, no auto-start; `serve.auto_start` ships unset until C4 lands and the §4.4 decision is re-ratified | — |

Exit 0 with a closing block:

```
init complete — 3 commands from zero to this result.
  goldmine search <words>   one-shot, pipeable (--format fzf for interactive pick)
  goldmine status           what's indexed and what isn't
  goldmine help             key topics (search-syntax, models, index-lifecycle)
```

**U1 accounting:** `pip install goldmine` (real PyPI publish, C1) → `goldmine init` →
`goldmine search "…"` = **3 commands** (≤ 4 with one remediation detour). Time on the
target machine:
~20s model (310 MB), ~2s check, minutes of ingest for a few hundred files — inside 10 min.
v2.0 init's estimate copy says "typically" until 04 §6.6 calibration lands in v2.1 and the
number becomes personal ("your last run: 94 files/min").

`init --yes` is also the CI/test path: deterministic, no network when the model is
present, full offline under `--backend mock` (the test-suite convention, README.md:59-61).

### 5.2 `goldmine find` — the interactive finder (deferred; design of record)

<!-- amended 2026-10-16: Reviewer 2 C2 — the curses TUI is KILLED from the roadmap
     (revisit only when a named user commits to it); v2.0's interactive layer is
     `search` + `--format fzf` + the shell widget (§5.6, pulled forward), with a
     line-based REPL as the noted alternative. Section kept as the design of record
     for that revisit; U2 died with it as a v2.0 metric. -->

An fzf-like TUI in **stdlib curses only** (`import curses`; house stdlib-first rule), the
front door for humans, while `search` stays the scriptable primitive. Warm-path budget per
keystroke: serve IPC ~2ms + query embed ~9-12ms (04 T5) + fusion/scan (04 §6.3's ~40ms
p50) + render — **p50 < 300ms end-to-end** (U2). Cold (no daemon): first keystroke eats
the ~0.7s daemon auto-start (05 §4.4) or the cold path; the footer says which
(`via: serve · warm` / `via: cold · starting daemon…`), matching 05 §4.4's visible-not-
silent rule.

**Behavior**

- Debounced live search: query re-run 150ms after the last keystroke, minimum 2 chars;
  a new keystroke cancels the in-flight render (per-keystroke request id — see Risks).
- Arrows/`^N/^P` move; `PgUp/PgDn` page; selection follows the fold (below).
- **Preview pane** (right ~50%, `p` toggles full-width excerpt): the excerpt with query
  terms marked `«term»` (v1's excerpt is unmarked — util.py:131-136; marking is a render-
  time job over `Searcher.terms`, search.py:259, so the store stays untouched), a `--why`
  line, and a details line.
- **Weak-tail fold**: hits below half the top score render under a `· weaker matches ·`
  divider, skipped by arrows unless unfolded with `f` (DigUp's ResultSet fold,
  Results.swift:212-244).
- Kind filter: `Tab` cycles all → doc → pdf → code → image → audio → video → webpage
  (drives the existing `--kinds`, SPEC.md:149).
- Enter opens at the match (§5.3); `q` quits; `Esc` clears the query first, quits when
  already empty (fzf convention).

**Key table** (mirrors DigUp README.md:53-61 in spirit: open/copy/quit, learn it in
place):

| Key | What it does |
|---|---|
| (type) | search live (150ms debounce) |
| ↓ ↑ / ^N ^P | move through results |
| ↩ | open at the match: PDF with terms in Preview's search, recording at the moment, code at the line, web in browser |
| o | open plainly (default app, no jump) |
| e | open at the line in `$EDITOR` (code hits) |
| w | cycle the why breakdown (score → components → scorer path) |
| Tab | cycle kind filter |
| f | fold/unfold weaker matches |
| c | copy the path (`pbcopy`) |
| y | copy the excerpt with « » stripped |
| p | toggle preview pane |
| ? | key help overlay |
| Esc / q | clear query / quit |

The same table ships in `find --help`, `goldmine help find`, and the `?` overlay —
DigUp's render-it-where-you-use-it rule (Onboarding.swift:580-592).

**Preview pane layout**

```
 notes/k8s.md  · doc · Oct 12 · 41 KB                score 3.41
 /home/yul/notes/k8s.md  L120-128
 why: meaning z 3.12 (gate 2.0) · words 2/2 (cov 0.67) · kind hint 0 · hybrid
 ──
 …the scheduler «evicts» a «pod» when a node is under
 pressure and the pod has no «toleration»…
```

`w` expands the why line into the fusion arithmetic (SPEC §3: `w_m·max(z,0) +
w_c·coverage^1.5 + kind_hint`), which is the honesty feature: a user can see *why* rank 1
is rank 1, including the `no-vector` / `lexical (stale-vectors)` degenerate cases v1
already labels (cli.py:470-472, 41).

**TTY fallback (decided):** stdin or stdout not a TTY → find *is* `search`: same ranked
output, same exit codes, zero surprise in pipes. `--no-tui` forces it explicitly (scripts
that call find under a pty). There is no alternate TUI library and no mouse support in
v2.0 (curses mouse events are terminal-dependent; document keyboard-only).

### 5.3 Open-in-context — `opener.py` + `goldmine open` (v2.0)

One module used by find's Enter and by a thin user-facing `goldmine open PATH [LOC]`
(LOC = page/seconds/line; the subcommand exists so fzf/Raycast/VS Code integrations share
one entry point, §5.6). Per kind:

| kind | Open with | Jump mechanism | Honest ceiling |
|---|---|---|---|
| pdf | default app → Preview | `open` the file; print/search terms: Preview **cannot be told a page** from a Python CLI — DigUp's page-landing trick is an NSWorkspace Apple event with `keyAESearchText` (Moments.swift:153-163), not reachable via stdlib `open`/osascript (Preview has no AppleScript search command) | find prints `p.4 — Preview search: «toleration evict»` and copies the terms, so the user pastes two words; open's exit line states `opened at file start (page jump not supported)` |
| audio/video | default app if QuickTime/IINA, else `open` | **QuickTime**: the exact DigUp osascript port (Moments.swift:174-193) — `/usr/bin/osascript -e <script> <path> <ms>`, subprocess so the "Terminal may control QuickTime Player?" prompt can't wedge anything; -1743/-1712/any failure → plain `open` and one stderr line (Moments.swift:196-217). **IINA**: `iina://open?url=…&mpv_start=` (Moments.swift:165-170) | the find footer/button says "open at 0:06" only when the default player is QuickTime or IINA (Moments.swift:112-115 bundle-id check, ported via `mdls kMDItemCFBundleIdentifier` on the default app); otherwise plain "open" |
| code | editor per table | Port CodeEditors.swift:27-52 verbatim: URL schemes `vscode://file/<pct-path>:<line>:1`, `cursor`, `windsurf`, `vscodium`, `vscode-insiders`; `txmt://open?url=…&line=`, `x-bbedit://open?…`; bundled commands Zed `cli <path>:<line>`, Sublime `subl <path>:<line>`, Xcode `xed --line N`, JetBrains launcher `--line N` (dig up the binary inside the .app bundle) | detection stdlib-first: probe `/Applications` + `~/Applications` for the known bundle names, prefer a *running* editor (CodeEditors.swift:64-73; running-check via `pgrep -f <bundle path>`, best-effort); scheme → bundled binary → **$EDITOR fallback** → plain `open` (CodeEditors.swift:111-127 chain) |
| doc (md/txt/…) | $EDITOR if set and kind is editable text, else `open` | `+N` line hint appended only for editors known to accept it (vi, vim, nvim, nano, emacs, less, mg — git's `+LINE` convention); otherwise plain | the open line says which happened |
| image | `open` (Preview) | none (v1 indexes images metadata-only under llama; moments land with 02's OCR work) | — |
| webpage | `open <url>` (default browser) | none | — |

**Fallback chain (uniform):** jump mechanism → plain open → printed instruction. Every
degrade prints one stderr line naming what happened (`quicktime didn't go to the moment
(not allowed) — opened at start`); nothing fails the command; exit stays 0. Failure
honesty is the product: a jump we can't make is *stated*, never pretended (U7).

<!-- amended 2026-10-16: Reviewer 2 C9 — v2.0 scope trimmed to the rows that pay for
     themselves; the 23-editor table + URL schemes defer to v2.1 -->
**v2.0 scope (2026-10-16 review):** ship plain `open`, the **QuickTime/IINA osascript
jump** (DigUp-proven, Moments.swift:172-217 — this is what makes ASR timestamps clickable
and is the highest wow-per-line in the table), and the **`$EDITOR`** path for text. The
23-editor table + URL schemes defer to v2.1 (highest-maintenance row, smallest audience);
until then the `code` row is `$EDITOR` → plain `open`, stated honestly in the open line.

### 5.4 serve UX — the visible daemon (design of record; ships v2.1)

<!-- amended 2026-10-16: Reviewer 2 C2 — `goldmine serve` is deferred to v2.1, so nothing
     in this section ships in v2.0: no init consent step, auto-start default OFF until
     C4 lands. Kept as the design of record; the guard copy below is now pressure-based
     (B3) and the RSS claim carries a scale-honesty caveat (B15c). -->

05 §4.4 owns the substrate; the user-facing surface:

- **Consent once, at init** (§5.1 step 8): the 484 MB number and the 5-min idle exit are
  in the prompt itself. Nobody discovers a daemon they weren't told about.
- **Autostart default ON for search** (05 §4.4 decision), opt-outs `--no-serve` (command)
  and `serve.auto_start=false` (config). `goldmine serve install|uninstall` remain the
  explicit launchd path (05 §4.4, RunAtLoad + KeepAlive=false). init does *not* install
  the LaunchAgent; it only sets auto_start — "warm while you're using it" is the default,
  "resident across logins" is opt-in.
- **`goldmine serve status`** (05 ships the data; this is the rendering): pid, uptime,
  RSS vs the 484 MB reference, resident model + sha prefix, leased indexes, linger
  countdown, last memory-guard reading; `--json` schema envelope.
- **Memory honesty**: `status` and the doctor daemon row (05 §4.4) print the guard state
  in pressure terms ("memory pressure 34 % — guard armed: warn 70 % (drop cache) /
  critical 85 % (exit)"; the old free-pages copy was wrong — vm_stat idle-free ≈ 1.5 GB
  makes free-page thresholds meaningless, 04 §6.3, B3). The daemon's exit by guard is
  announced on stderr by the next command that notices ("serve exited under memory
  pressure 4m ago; fell back to cold").
- **RSS scale honesty (2026-10-16, B15c)**: the quoted "~484 MB" is the llama-server's
  model-dominated residency and is index-size-independent; the vector pack is file-backed
  (purgeable page cache, not RSS) and per-query scan blocks are bounded (~12 MB). Copy
  must say "the model server ~484 MB; index pages live in reclaimable file cache" —
  never present 484 MB as a total-footprint promise, and never scale it with corpus size.
- **Security note, one place, stated plainly** (`goldmine help serve`, and init's prompt
  detail): the control socket is a mode-0600 UDS under `$TMPDIR/goldmine-serve-<uid>/`
  (05 §4.4) — filesystem auth, single-user machine; llama-server itself binds **127.0.0.1
  on an ephemeral port** exactly as v1's backend does, never 0.0.0.0; loopback is not
  exposed via Tailscale or any VPN tunnel, and `doctor` warns if a config ever points the
  server at a non-loopback host. If a future remote-search feature exists, it goes through
  an explicit, authenticated surface — never by loosening these binds.
- **Cold fallback**: any one-shot silently uses the v1 path when the socket doesn't answer
  (05 §4.4's order: env → serve.json → cold), and the search footer carries
  `via: serve (40ms)` / `via: cold (1.9s)` — latency as data, per DigUp's timing footer
  (main.swift:376-377).

### 5.5 Output polish (v2.0, mostly)

- **Ingest ticker** (stderr, 2s cadence — ModelDownload.swift:180-183's cadence):
  `[ 42/120 files · 1,203/4,231 texts · 68 texts/s · eta 0:44]`, carriage-returned in a
  TTY, appended as periodic lines when piped (so logs keep history). Final summary
  unchanged in shape (cli.py:298-354 is good) plus a measured `files/min` line (04 §6.2
  publishes it — this is where users see it). ETA sources: 04 §6.6 rates in v2.1; v2.0
  uses in-run exponential average over completed files only, shown from file 5 on.
- **Result cards** (search + find + demo share one renderer): rank, kind tag, path (~-
  abbreviated, middle-truncated like Results.swift:31-35's repo-relative folder logic),
  loc badge (`p.4` / `0:06` / `♪ 0:25` / `L120-128` — Results.swift:68-79, extended by
  v1's cli.py:490-503), the why line on demand (`--why` flag and find's `w`), marked
  excerpt. Default stays two lines per hit; `--why` adds the third.
- **NO_COLOR**: presence of `NO_COLOR` (any value, the no-color.org convention) disables
  ANSI even on a TTY; `--no-color` unchanged; color palette: 2 colors + bold + reverse
  (term marking uses reverse/standout in curses, `«»` in plain text).
- **Versioned --json**: promise — existing v1 payloads never change shape, only gain
  optional keys (search's bare array stays a bare array). Every **new** command (find,
  serve, model, open, init) emits `{"schema": "goldmine/1", "command": …, …}` and additive
  evolution bumps the minor. One line in SPEC §1; enforced by a test that diffs recorded
  v1 fixtures.
- **Honest zero hits** (U5): `nothing found for 'x'` + one tailored line: kind filter
  active → "try Tab/all kinds"; zero vectors → "index is keyword-only (stale vectors) —
  goldmine reindex plan"; tiny index → "ingest more roots".

### 5.6 Integrations — the smallest set worth shipping (fzf: v2.0; rest: v2.1)

<!-- amended 2026-10-16: Reviewer 2 C4 — `--format fzf` + the shell widget are PULLED
     FORWARD into v2.0 (they are the interactive layer now that the curses TUI is
     deferred); `--alfred` + the VS Code snippet stay v2.1. -->

Three, all thin clients over existing commands; no editor plugins, no browser extension:

1. **fzf shell widget** (S — **v2.0**, pulled forward): documented snippet + `goldmine
   help fzf` emits it:
   bindkey calls `goldmine search "$buffer" --limit 50 --format fzf` (new `--format fzf`:
   `rank\tpath\tloc\texcerpt-with-«»`), pipes the pick to `goldmine open`. ~10 lines of
   zsh; fzf users get their muscle memory, we ship no TUI competition.
2. **Raycast/Alfred script-filter mode** (S): `goldmine search Q --alfred` emits Alfred
   Script-Filter JSON (items: title=name, subtitle=`kind · loc · why-short`, arg=path,
   mods: cmd=copy path, alt=open plain, variables loc/loc-args for the `goldmine open`
  follow-up). Raycast's "Script Filter (Alfred-compatible)" consumes the same payload —
   one format serves both launchers.
3. **VS Code task** (S): a copy-paste `tasks.json` snippet in the docs that runs
   `goldmine search` in the integrated terminal and an `open` task wired to the
   selection. No extension — that's on the kill list.

### 5.7 Error UX audit — the universal shape (v2.0 retrofit, small)

Every failure path prints, in this order, on stderr:

```
<what happened, one line, lower-case, no stack>
why: <root cause as data — path, exit code, missing thing>
fix: <exact copy-pasteable command>            ← ≥1 line, mechanical
more: goldmine doctor                          ← when the cause may be broader
```

Rules: `fix:` is a real command (never "install the tool"); when two remedies exist both
print (`fix: goldmine model check` + `or: goldmine ingest --lexical-only`); `--json` mode
gets the same fields as structured keys (`error.code`, `error.fix[]`) so scripts can react;
exit codes unchanged (SPEC.md:41-52).

**Retrofit table (v1 path → today → v2 output):**

| Path (v1) | Today | v2 adds |
|---|---|---|
| search/status/distill, no index (cli.py:363-365, 512-518, 653-655) | "no index at … — run `goldmine ingest` first" | `fix: goldmine init` (or ingest) + `more: doctor`; status keeps its data-home line |
| ingest, no backend (cli.py:244-251) | best-in-v1 already | add `fix: goldmine model check` as the *first* fix (the arch failure masquerades as this) |
| fingerprint mismatch (cli.py:400-408) | message + remediation | reformat into the shape; `fix: goldmine reindex plan` (05 §4.2) once it exists, `or: goldmine search --lexical` today |
| backend startup failure (cli.py:999-1007, exit 2) | bare message + deviation note | `why:` carries llama-server's stderr tail (the ring buffer v1 keeps); `fix: goldmine model check`; the exit-2-vs-1 deviation gets revisited in SPEC (a startup failure is not a usage error — candidate: new exit 4) |
| EmbeddingError mid-run (cli.py:1008-1011) | type + message | include the server's own error line (the 2670-token class, 04 §6.2 item 3) — v2.0's token guard removes the common cause |
| reset non-interactive (cli.py:919-925) | already exemplary ("re-run with --yes") | pattern promoted to the universal shape |
| distill < 2 chunks (cli.py:686-693) | count + advice | shape + `fix: goldmine ingest --root …` |
| catch-all (cli.py:1015-1018) | type + message | keep, append `more: goldmine doctor` + `report: goldmine status --json > bug.txt` pointer |
| unreadable root (TCC) | silently walked as empty (v1 has no isReadable gate — DigUp's main.swift:122-126 lesson) | walk records `unreadable_roots` and the summary prints "1 root unreadable (macOS privacy settings?) — grant Full Disk Access or pick another" (new small pipeline change, §6) |
| doctor itself (cli.py:779-789) | already the pattern | `fix:` lines must pass the copy-pasteable bar; audit existing hints (doctor.py:29+ pass except a few "install X" phrasings → give the brew/uv command) |

### 5.8 Docs plan — minimal and decided

- **README quickstart rewritten around the 3-command path** (§5.1): install → `goldmine
  init` → `goldmine search`. Everything else (manual model, mock backend, named indexes)
  moves under a "going further" cut. The capability matrix table stays — it is the product
  (README.md:66-83).
- **`goldmine help [topic]`** (stdlib arg-parse, no new deps): topics `find` (key table),
  `search-syntax` (scorers, lookup-inversion, why components), `models`, `serve` (incl.
  the security note, §5.4), `index-lifecycle` (fingerprint, reindex, reset), `integrations`
  (fzf/Alfred/VS Code snippets — also machine-emittable: `goldmine help fzf --snippet`).
  Prints to a pager when stdout is a TTY, plain otherwise.
- **No man page (decided).** pip --user/uv installs have no reliable mandir wiring; a man
  page would instantly rot against `--help`. argparse `--help` per command + `goldmine
  help <topic>` + the README cover it; revisit only if a packager asks (Homebrew tap
  would generate one from `--help` output at package time, not our problem in-repo).
- `SPEC.md` gains the new commands' normative sections in the same change (house rule,
  SPEC.md:3-4).

### 5.9 Kill list (product side; 05 §4.9 owns the architecture list)

| Killed | Why |
|---|---|
| Menu bar app / status item | requires a GUI framework + bundle; the daemon's state fits one `serve status` call and the find footer; DigUp needed the ring because indexing ran unseen — goldmine's runs are commands the user invoked |
| GUI app (any window, any toolkit) | the identity is the CLI; a GUI would fork onboarding, settings, and trust surfaces for one platform; find IS the interactive layer |
| Telemetry / crash reporting | local-first is the promise; the honesty features (status/doctor/exit codes) already surface everything a maintainer needs, offline |
| Accounts / sync | nothing to account for; index portability is a file copy (05 §4.5 registry keeps honesty about it) |
| Auto-update (self or model) | models change = reindex cost; that decision stays a human command (`model check`/`install` per 05 §4.3) with the fingerprint gate as the seatbelt; brew/pip own the binary's updates |
| Editor extensions / browser extension | three thin integrations (§5.6) cover the real entry points; an extension per editor is maintenance forever |
| Mouse support in find | curses mouse events are terminal-fragile; keyboard-only is testable and documented |

---

## 6. Phasing & effort

(S ≤ 1 day, M ≤ 3 days, L > 3 days — one engineer; overlaps 04 §8 / 05 §6, cross-referenced)

<!-- amended 2026-10-16: Reviewer 2 C2/C4/C9 — find TUI (7) killed from the roadmap and
     serve status (6) deferred to v2.1; opener (2) trimmed to the v2.0 sliver (plain open
     + QuickTime/IINA + $EDITOR; editor table → v2.1); `--format fzf` + widget pulled
     forward into item 3's renderer work (v2.0). Phasing owned by REDESIGN §5. -->

| # | Item | Size | Release | Notes / dependencies |
|---|---|---|---|---|
| 1 | Universal error shape + retrofit of the §5.7 table + `--json` error keys + tests | M | v2.0 | independent, do first — every later command inherits it |
| 2 | `opener.py` + `goldmine open` — v2.0 sliver: plain open, QuickTime osascript, IINA, `$EDITOR` fallback (editor table + URL schemes → v2.1) | S | v2.0 | pure stdlib; testable with a dry-run mode like DigUp's `Opener.dryRun` (Moments.swift:108-109) |
| 3 | Output polish: ingest ticker + ETA (with per-root media counts + audio hours, C5), result-card renderer, NO_COLOR, `--why`, **`--format fzf` + shell widget (pulled forward)** | M | v2.0 | renderer shared by search/demo |
| 4 | `goldmine model` UI copy riding on 05 item 6 (progress lines, states) | S | v2.0 | 05 §4.3 owns mechanics; this is the 4-state stderr vocabulary |
| 5 | `goldmine init` state machine + resume + `--yes` + demo step + media-model offer (`--media`, C5); no serve-consent step | M | v2.0 | depends on 05 items 6-7 (model manager/check) and 4 |
| 6 | `goldmine serve status` rendering + doctor daemon row copy + security note | S | v2.1 (was v2.0) | 05 item 8 owns the daemon; this is its face |
| 7 | `goldmine find` curses TUI (live search, preview, fold, keys, TTY fallback) | L | **killed** (revisit with a named user) | design of record in §5.2; REPL + fzf cover the need |
| 8 | README rewrite + `goldmine help` topics | S | v2.0 | after 2-5 settle |
| 9 | TCC unreadable-root detection in the walk + summary line | S | v2.0 | pipeline one-liner; the DigUp lesson (main.swift:122-126) |
| 10 | Integrations: `--alfred`, VS Code snippet + `help integrations` (fzf moved to v2.0, item 3) | S | v2.1 | needs stable `goldmine open` (2) |
| 11 | init/ingest estimate upgrade to 04 §6.6 self-calibrating rates (personal ETA copy) | S | v2.1 | lands when 04 item 8 lands; copy only |
| 12 | ~~find polish~~ (died with the TUI; `--why` overlay lives in `search --why`) | — | — | — |
| 13 | Watch UX: status surfaces for `goldmine watch` state (02 §5.6) | S | v2.2 | needs watch to exist |
| 14 | Multi-index UX pass: init `--index`, `status --all` rendering, registry errors as §5.7 shape | M | v2.2 | 05 §4.5 owns registry (v2.1) |

Critical path: 1 → 2 → (3, 5); 8 trails them by days, not weeks. (6 trails its owner into
v2.1; 7 is killed from the roadmap.)

---

## 7. Risks & open questions

**Risks**

| Risk | Likelihood | Mitigation |
|---|---|---|
| curses availability/behavior varies (pyenv builds, Terminal vs iTerm2, esc-delay, 256-color) | medium | stdlib `_curses` is present in pyenv 3.12 on macOS (verify in init step 2 with a 1-line `curses.wrapper` smoke test); key handling via `keypad(True)` + documented esc-delay quirk; color optional (grayscale layout must stand alone) |
| Per-keystroke query embeds pile up (slow cold path, daemon queue) | medium | 150ms debounce + 2-char minimum + client cancels stale renders by request id; **needs an optional `id` field in the serve `search` request** so the daemon can drop superseded queries — filed to 05 §4.4 as an addendum (backward-compatible optional key) |
| find's 300ms p50 misses when the daemon is cold mid-session | medium | footer states `via: cold` (U2 measured as warm-path metric); auto-start (05 §4.4) makes cold a first-keystroke-only event |
| osascript QuickTime control prompt lands at a confusing moment (first find-Enter on a video) | low | init/folders copy pre-warns once (DigUp README.md:63-64 does exactly this in prose); the prompt is per-terminal-app, one time; refusal degrades to plain open with a printed line |
| Editor detection table rots (new forks, bundle moves) | medium | table is data (one Python list mirroring CodeEditors.swift:27-52); `goldmine open --dry-run` prints the chosen method for debugging; plain-open final fallback means rot degrades gracefully, never errors |
| init's 10-minute promise breaks on slow networks | medium | download is resumable and skippable (`init --skip-model` → lexical-only index, stated consequence: name/keyword search now, reindex after `model install`); the promise is restated as "≤10 min on a normal connection" in docs, not code |
| Output renderer divergence (search cards vs find pane vs --json) | low | one renderer module; find consumes the same hit objects; fixtures test all three outputs from one ingest |
| `--alfred`/`--format fzf` accrete into a second output contract | low | both are thin serializations of the same hit objects with their own schema-versioned envelope (§5.5) |

**Open questions**

1. Does `find` become the documented default entry (README headline) once stable, with
   `search` demoted to "scripting" docs? Leaning yes at v2.1. [this doc, post-release]
2. BackendStartupError's exit code: promote the cli.py:999-1007 deviation into a SPEC
   change (startup failure → 4, prerequisite-missing) or keep 2 for compat? Leaning 4,
   documented in the v2.0 SPEC pass. [SPEC owner]
3. Should `goldmine open` print the jump it attempted (`opened k8s.md at L120 in zed`) on
   stdout (pipeable) or stderr (human-only)? Leaning stderr — stdout stays data-only
   (SPEC §1.2). [decide in review]
4. Demo-step term sampling: top TF-IDF terms can be boring ("the", proper nouns of one
   repo). Alternative: sample a mid-ranked distill card's terms. Cheap to iterate; not
   load-bearing. [06 alignment]
5. fzf widget vs `find`: do we ever advertise both, or position fzf as "for fish/nu users
   whose terminals curses handles poorly"? [docs]
