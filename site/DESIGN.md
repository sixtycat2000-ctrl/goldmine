# goldmine — Official Site Design System & Interaction Spec

Spec for the static marketing + learning site: `site/index.html`, `site/docs.html`, `site/learn.html`.
Authored 2026-10-25. Source of visual truth: `docs/explainer.html` (this spec continues that identity, it does not replace it).

**Hard constraints (apply to every decision below)**

- Vanilla HTML/CSS/JS. No build step, no bundler, no CDN, no webfonts, no framework. Works at `file://` and on Cloudflare Pages (site root = `site/`).
- Nothing is fetched at runtime: all data (learn.html graph, all bilingual copy) is inlined in the HTML. `fetch()` is forbidden (breaks at `file://`).
- Dark-only theme, by design (see §1.1).
- Chinese-first content with a full EN toggle (rules in §6).
- Numbers on the site must match `docs/FEATURE_STATUS.md` at authoring time. Every metric carries an HTML comment `<!-- verify: source doc -->`. Shipping a stale number is a design bug.

---

## 0. Design principles

1. **Instrument, not brochure.** The site should feel like a precision instrument panel for a search engine: hairlines, mono readouts, tabular numerals, restrained color. Decorative gradients and glassmorphism are banned.
2. **Gold is earned.** `--gold` means one thing across all three pages: *meaning / attention / the query*. It marks the query star, the selected node, the pipeline packet, the active state. Gold never decorates; if nothing semantic is happening, gold is absent.
3. **Dark, warm, one light source.** The single light in the room is the gold. Ink hierarchy (three levels) does all everyday work. Dark-only: the palette's luminance math is built for `#0F0E0C`; gold `#E3A94C` on paper is ~1.9:1 and would force a second identity. Token structure allows a future `[data-theme]` override; do not ship one.
4. **Honesty is a visual feature.** "nothing found", four-state accounting, `--why` — the site renders honesty states (empty, dimmed, disabled) with the same craft as success states. Never fake a full state.
5. **Explore, don't read.** learn.html is a map you fly over, not an article. Camera, hover, and flight are first-class; text lives in the side panel.
6. **Still water.** Ambient motion is slow (drift, breathing). User-initiated motion is fast and confident (170–700ms, expo out, no bounce). Nothing oscillates at the user.

---

## 1. Design tokens

All tokens are CSS custom properties on `:root`. The full `:root` block is the single source of truth in `site/assets/goldmine.css`; values below are normative.

### 1.1 Color

**Base**

| Token | Value | Role | Contrast on `--bg` |
|---|---|---|---|
| `--bg` | `#0F0E0C` | page background | — |
| `--bg-2` | `#14120E` | inset panels (`.term` blocks, secondary wells) | — |
| `--bg-3` | `#171510` | cards | — |
| `--bg-deep` | `#0B0A08` | terminal windows, code blocks | — |
| `--ink` | `#EDE7DC` | headings, primary text | ≈15:1 |
| `--ink-body` | `#CFC8B9` | body prose | ≈11:1 |
| `--sub` | `#96907F` | secondary text, captions | ≈5.9:1 |
| `--sub-2` | `#6E6858` | decorative mono only (timestamps, de-emphasized terminal dim). **Never** meaningful text below 18px | ≈3.4:1 |
| `--line` | `rgb(237 231 220 / .13)` | hairlines, card borders | — |
| `--line-strong` | `rgb(237 231 220 / .28)` | terminal chrome, inputs, dividers that carry weight | — |

Expose raw channels for alpha composition: `--ink-rgb: 237 231 220; --gold-rgb: 227 169 76;` etc., so derived alphas are `rgb(var(--ink-rgb) / .13)`.

**Brand**

| Token | Value | Role | Notes |
|---|---|---|---|
| `--gold` | `#E3A94C` | the semantic accent: query, selection, active, progress | ≈8.9:1 on bg — safe as text |
| `--gold-bright` | `#F0C578` | hover/active gold, lit nodes, links | — |
| `--gold-dim` | `rgb(227 169 76 / .55)` | pre-highlight edges, mid states | UI-only, not text |
| `--gold-glow` | `rgb(227 169 76 / .16)` | halo rings (`box-shadow: 0 0 0 4px …`) | — |
| `--ink-on-gold` | `#141109` | text on gold buttons | ≈8.9:1 |
| `--teal` | `#6FB3AA` | the "other leg": lexical/keyword, before-state in comparisons | ≈7.8:1 |
| `--red` | `#C97A62` | failures, rejected files, noise questions | ≈5.7:1; ≥14px or UI dots only |

**Cluster palette (learn.html only, but defined globally)**

Seven clusters, seven hues. All are muted, all pass ≥3:1 on `--bg` as UI marks, and the four used for text chips pass ≥4.5:1.

| Cluster | Token | Value |
|---|---|---|
| A 搜索与表示 | `--cl-a` | `#E3A94C` (gold — the product's home cluster) |
| B 模型与推理 | `--cl-b` | `#F0C578` |
| C 数据与身份 | `--cl-c` | `#6FB3AA` |
| D 评测与统计 | `--cl-d` | `#C97A62` |
| E 多媒体管道 | `--cl-e` | `#A8B08C` |
| F 文件系统与进程 | `--cl-f` | `#8E9AA6` |
| G 过程与协作 | `--cl-g` | `#C9B8A0` |

Selection always overrides cluster color with `--gold` — when you select a node it joins "the query's family".

**Usage rules**

- Borders are `--line`; a card never uses a shadow instead of a border. Shadows only add depth *on top of* a hairline.
- Links: `--gold-bright` with `1px` bottom border `rgb(227 169 76 / .35)`, border brightens to `--gold` on hover, 160ms.
- `::selection` stays `background: var(--gold); color: #141109;` (from explainer).
- `color-scheme: dark` on `:root`.

### 1.2 Typography

No webfonts. System stacks, CJK-aware, in this order:

```css
--sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
        "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
--serif: "Songti SC", "STSong", "Noto Serif SC", "Source Han Serif SC", serif;
--mono: ui-monospace, "SF Mono", Menlo, Consolas, "Cascadia Mono", monospace;
```

Division of labor: **serif** = human voice (display, section titles, node panel titles). **sans** = body prose, UI. **mono** = the machine's voice (kickers, labels, code, numbers, map labels, stats). A page section always announces itself in mono and speaks in serif.

**Scale** (all `clamp()`; lh = line-height):

| Token | Size | lh | Family / weight | Tracking | Use |
|---|---|---|---|---|---|
| `--fs-hero` | `clamp(2.5rem, 6.8vw, 4.75rem)` | 1.08 | serif 500 | .015em | homepage H1 |
| `--fs-h1` | `clamp(2.125rem, 5vw, 3.5rem)` | 1.16 | serif 500 | .01em | page titles, panel node title |
| `--fs-h2` | `clamp(1.75rem, 4vw, 2.75rem)` | 1.28 | serif 500 | .01em | section titles |
| `--fs-h3` | `clamp(1.19rem, 2.4vw, 1.5rem)` | 1.45 | serif 500 | 0 | card titles, panel subtitles |
| `--fs-body-lg` | `1.09375rem` (17.5px) | `--lh-lang` | sans 400 | 0 | lede paragraphs |
| `--fs-body` | `1rem` | `--lh-lang` | sans 400 | 0 | body prose |
| `--fs-small` | `.875rem` | 1.7 | sans 400 | 0 | card body, captions |
| `--fs-xs` | `.78125rem` (12.5px) | 1.65 | sans 400 | 0 | meta, hints |
| `--fs-code` | `.84375rem` (13.5px) | 1.95 | mono 400 | 0 | terminal, code blocks |
| `--fs-kicker` | `.75rem` | 1 | mono 400 | `.34em` | kickers (gold) |
| `--fs-label` | `.6875rem` (11px) | 1 | mono 400 | `.22em` | FIG. labels, panel section labels |
| `--fs-chno` | `clamp(7.5rem, 22vw, 14.375rem)` | 1 | mono 700, stroke only | 0 | giant outlined chapter numerals |

`--lh-lang` is `1.85` under `[lang="zh-CN"]` and `1.62` under `[lang="en"]` (CJK needs the air; English gets compacted). Set on `html` when the language toggles (§6).

**Typographic details**

- Kicker pattern (keep from explainer): mono, letter-spaced, gold, preceded by a 34px `1px` gold rule. Section openers always: kicker → serif title → lede.
- Chapter numerals (`.chno`): transparent fill, `-webkit-text-stroke: 1px rgb(237 231 220 / .09)`, absolute top-right of section, `aria-hidden`. Subtle scroll parallax: `translateY(6%)` across the section (RM: none).
- All numbers in mono + `font-variant-numeric: tabular-nums`. Count-ups never change column width.
- Headings: `text-wrap: balance` (progressive enhancement). Body: no hyphenation for zh ever; EN body may hyphenate only inside the docs prose column ≥680px.
- Inline `code`: mono, `rgb(237 231 220 / .07)` bg, radius `--r-xs`, `.1em .4em` padding, 0.92em — exactly as explainer.

### 1.3 Space, shape, elevation

**Spacing** — 4px base: `--sp-1:4px` `-2:8` `-3:12` `-4:16` `-5:24` `-6:32` `-7:48` `-8:64` `-9:96` `-10:128`. Section rhythm: `--sp-section: clamp(72px, 12vh, 128px)`. Homepage container `--w-container: 1120px`; docs prose `--w-readable: 680px`; learn panel `--w-panel: 420px`; `--nav-h: 60px`; learn toolbar `--map-bar-h: 56px`.

**Radii** — `--r-xs:4px` (inline code) · `--r-sm:8px` (chips, inputs) · `--r-md:10px` (buttons, small cards) · `--r-lg:14px` (cards, terminal) · `--r-xl:18px` (sheets/modals) · `--r-full:999px` (pills, dots).

**Elevation** (dark theme: depth = deeper black + hairline, never light gray):

| Token | Value | Use |
|---|---|---|
| `--elev-1` | `inset 0 1px 0 rgb(237 231 220 / .03), 0 6px 16px -8px rgb(0 0 0 / .45)` | cards at rest |
| `--elev-2` | `inset 0 1px 0 rgb(237 231 220 / .05), 0 16px 40px -16px rgb(0 0 0 / .55)` | cards on hover, popovers |
| `--elev-3` | `0 24px 64px -20px rgb(0 0 0 / .65)` | sheets, mobile panel |
| `--glow-gold` | `0 0 0 4px var(--gold-glow)` | active dots, focused nodes (DOM twin) |

**Z-index scale**: canvas 1 · map UI 10 · panel 30 · nav 40 · sheet/backdrop 50 · toast 60.

### 1.4 Motion tokens

Durations: `--t-0:80ms` (hover micro) · `--t-1:160ms` (hover, small fades) · `--t-2:240ms` (tabs, chips) · `--t-3:400ms` (panel slide, popover) · `--t-4:640ms` (section reveals) · `--t-reveal:800ms` (hero/section entrance) · `--t-flight:700ms` (camera flights).

Named easings:

| Token | Value | Character | Use for |
|---|---|---|---|
| `--ease-out-quad` | `cubic-bezier(.25,.46,.45,.94)` | soft exit | tiny opacity fades |
| `--ease-out` | `cubic-bezier(.22,.61,.36,1)` | default | most UI transitions |
| `--ease-out-expo` | `cubic-bezier(.16,1,.3,1)` | confident, long tail | camera flights, panel, tab underline, count-ups |
| `--ease-in-out` | `cubic-bezier(.65,.05,.36,1)` | symmetric | crossfades, language swap |
| `--ease-reveal` | `cubic-bezier(.2,.7,.2,1)` | the explainer reveal | scroll entrances (keep verbatim) |

JS-side (not CSS): camera flight = fixed 700ms `easeOutExpo`, **retargetable** (on new input, capture current camera as start, restart the ease). Query-star pointer follow = spring `k=120, c=18` (slightly under-damped, one whisper of overshoot). Map drift return factor `0.012/frame` (explainer precedent). Pan inertia friction `0.92/frame`, cut below `0.05px/frame`.

Stagger: `--stg:70ms` between grid/card children, `--stg-lg:110ms` between the kicker/title/lede trio of a section. Cap: 8 staggered children, remaining children animate together on the 8th step.

### 1.5 Atmosphere

- **Noise overlay**: the explainer's fractal-noise SVG data-URI, `position:fixed; inset:0; opacity:.05; pointer-events:none; z-index:9`. On all pages, exactly once. Never above z-40 chrome.
- **Hero scrim**: `linear-gradient(90deg, rgb(15 14 12 / .92) 0%, rgb(15 14 12 / .55) 38%, transparent 68%)` over the starfield, keeping hero text at ≥10:1.
- Scrollbar (WebKit + `scrollbar-color`): track transparent, thumb `rgb(237 231 220 / .18)`, 10px, radius full.
- Focus: `:focus-visible { outline: 2px solid var(--gold-bright); outline-offset: 3px; }` globally. On gold-filled elements, outline flips to `--ink`.

### 1.6 Iconography

One inline `<svg><symbol>` sprite per page (first element in `<body>`, `display:none`), stroke `1.5`, `currentColor`, 16px and 20px variants via `width/height` attrs. Inventory (nothing else may be invented ad hoc): `logo-glyph` (rotated square with off-center dot — the "gold nugget"), `search`, `close`, `plus`, `minus`, `reset` (target), `arrow-left`, `arrow-right`, `external`, `copy`, `check`, `globe` (language), `keyboard`, `menu`, `branch` (GitHub stand-in — do not inline the octocat path), `chev-down`. No emoji, ever.

---

## 2. Shared shell (all three pages)

**Nav** — fixed, `--nav-h` 60px, `display:flex`, container-aligned. Left: logo glyph + wordmark `goldmine` (mono, lowercase, 15px, letterspaced `.04em`) → links `index.html`. Center/right links: `首页` `文档` `概念图` (active page: gold-bright + 1px gold underline offset 6px) · GitHub ↗ (external icon) · language toggle. Behavior: transparent over hero; at `scrollY > 24px` add `.scrolled` → `background: rgb(15 14 12 / .72)`, `backdrop-filter: blur(14px) saturate(1.1)`, `border-bottom: 1px solid var(--line)`; transition `--t-2 --ease-out`.

**Language toggle** — pill `64×32px`, radius full, border `--line-strong`, two mono 12px segments `中 | EN`. Active segment: `rgb(227 169 76 / .14)` bg + `--gold-bright` text. The control always shows both labels; the non-active one is the *target*. Keystroke `L` on learn.html. Persistence + mechanics in §6.

**Progress bar** — 2px, `linear-gradient(90deg, var(--gold), var(--gold-bright))`, fixed top, `z-60`, width = scroll fraction. All pages (explainer precedent).

**Footer** — hairline top, container: left `goldmine · 本地优先语义搜索 / local-first semantic search` (mono xs); middle: page links; right: GitHub, LICENSE, and the mark `0 bytes 离开你的机器 / leave your machine`. Mono, `--sub`.

**File layout**

```
site/
  index.html  docs.html  learn.html
  assets/goldmine.css      (all tokens + shared shell + page styles)
  assets/app.js            (nav, lang toggle, progress, reveals — all pages, defer)
  assets/hero.js           (index starfield + counters, index only)
  assets/map.js            (learn concept map, learn only)
  assets/docs.js           (toc highlight + copy buttons, docs only)
```

Cloudflare Pages: project root `site/`; links between pages are explicit `*.html`; add `Cache-Control: public, max-age=3600` for `assets/*` via `_headers` (optional).

---

## 3. index.html — Homepage IA & motion script

### 3.1 Section map

| # | id | Purpose | Key components |
|---|---|---|---|
| S0 | `#hero` | the promise | starfield canvas, kicker, H1, dek, 2 CTAs, stats strip, scroll cue, modality legend chips |
| S1 | `#problem` | what problem this solves | 3 failure cards (synonyms unheard / media invisible / cloud reads your files), one before→after query strip |
| S2 | `#how` | the six-station pipeline | spine + gold packet (scroll-linked), 6 stations, capacity note |
| S3 | `#two-legs` | semantic × lexical fusion | legs diagram (static SVG), score formula card, `--why` example chip |
| S4 | `#features` | capability grid | 6 cards: local-first privacy · text · images (OCR) · audio (timestamps) · video keyframes · precise `open` (jump to second/line) |
| S5 | `#architecture` | architecture glimpse | 5 condensed layers + dashed flow connectors + "one SQLite file" token |
| S6 | `#numbers` | evidence band | 4 count-up stats + note that all numbers are locally measured |
| S7 | `#opensource` | CTA | H2, `pip install`/clone command block with copy button, GitHub button, license + status line |
| — | `footer` | — | shared shell |

Copy direction (zh primary): keep the explainer's H1 verbatim — `Spotlight 认字，不认<em>意思</em>。` (`em` = gold). EN toggle renders `Spotlight reads characters, not meaning.` Dek expands one sentence with the "vectors bring meaning-near content mathematically close" idea. CTAs: primary gold `开始使用 →docs.html`, ghost `GitHub ↗`.

Metrics slots (S0 stats, S6): values tagged `<!-- verify: FEATURE_STATUS.md -->`. If a number can't be verified at publish time, the slot shows an em-dash, never a stale figure — the honesty principle applies to our own site.

### 3.2 Hero centerpiece — the Semantic Starfield (precise spec)

**Concept.** A living slice of the product's actual mechanism: an embedding space of drifting points grouped in modality clusters (text / code / audio / visual). A gold query star sweeps through on a slow orbit; the nearest neighbors ignite and hold cosine-connection lines to it. This *is* semantic search, running continuously in the background of the H1.

**Tech: single `<canvas>` 2D** behind the hero copy (`position:absolute; inset:0; aria-hidden="true"`). Canvas, not DOM/SVG, because ~600 alpha-blended moving sprites + ~8 dynamic lines at 60fps is trivial for one draw loop and hopeless for style recalculation; 2D, not WebGL, because zero dependencies is a hard constraint and this load is far below the 2D ceiling.

**Composition** (relative coords of hero rect):

- 4 clusters: text `(.24,.34)` ink dots, code `(.72,.28)` teal dots, audio `(.30,.74)` sand `#C9B8A0` dots, visual `(.74,.70)` dim-gold `rgb(227 169 76 / .38)` dots. Distribution: Gaussian, `r = sqrt(rand) * (.09 + rand*.05)`, x-stretched ×1.5 (ellipses, explainer precedent).
- 1 dust field: uniform, ink at `.16` alpha — reads as "everything else on your disk, not yet chosen".

**Counts** (set at resize, never reallocated):

| Viewport | Per cluster | Dust | Total |
|---|---|---|---|
| ≥900px | 130 / 130 / 110 / 120 | 100 | 590 |
| 600–899px | 85 | 60 | 400 |
| <600px | 48 | 40 | 232 |

DPR cap 2. Point size: rest `1.1–1.6px` radius circles (seeded), dust `0.9px`.

**Drift** — every point orbits its anchor on a lissajous: `x = ax + sin(t·ωx + φ)·A`, `A ∈ [6,10]px`, `ω ∈ [.05,.12] rad/s`, per-point seeded. Slow enough to feel like water, not noise.

**Query star** — 4-point gold star (`--gold-bright`, r=9px) with a 64px radial-gradient glow sprite and a 46px velocity ray (gold, gradient fade). Orbit: `qx = .5 + .30·sin(2πt/T)`, `qy = .44 + .26·sin(4πt/T + π/3)`, **T = 16s** desktop / 20s mobile.

**Ignition** — each frame compute distance star→each clustered point. The 6 nearest within radius `R = clamp(180 · min(W,H)/800, 120, 240)px` are lit. Hysteresis: a lit point stays lit while `dist < R·1.15` (kills flicker). Per-point lit-ness eases: rise to 1 over 240ms, decay over 600ms — the star leaves a comet tail of dimming connections. Lit point: gold-bright, r=2.2, glow sprite; line to star: 1px gold, `alpha = .55·lit`. Everything beyond R stays at rest brightness — the crowd is calm, the neighborhood is awake.

**Rendering recipe (performance law)** — no `shadowBlur`, no per-frame string colors, no allocations in the loop. Pre-render 3 glow sprites (gold-bright, gold, teal) to offscreen 32×32 canvases once; draw points as `fillRect`/`arc` with cached rgba strings; store points in a typed-array-backed pool. Adaptive quality: if median frame >24ms across 30 frames, reduce dust by 30% once.

**Interaction** — pointer parallax only (never hijack scroll): normalized pointer offset from hero center shifts cluster layer by `±14px` and dust by `±26px` (dust = foreground parallax, moves most), lerped at `0.04/frame` (~600ms settle). On `pointerleave`, targets return to 0. No other pointer behavior; the starfield is not interactive on index (interaction is learn.html's job).

**Lifecycle** — canvas fades in `opacity 0→1` over 900ms (delay 150ms after load). First star orbit is fast (`T=3s` for the first revolution — show the trick) then eases to 16s over one cycle. rAF pauses when hero `<10%` visible (IntersectionObserver) or `document.hidden`.

**Reduced motion** — one static composed frame: star at `(.62,.42)`, its 6 nearest points lit with full lines, all other points at anchors (no drift). No rAF, no parallax, no first-orbit trick. The hero still *explains* — it just doesn't move.

**Mobile** — counts per table; parallax off (no hover); star keeps sweeping; canvas height = hero `100svh` minus nothing (text overlays with the scrim; reduce scrim stop to 55% width <600px so text keeps ≥7:1).

**No-JS** — hero text, stats, CTAs are plain HTML, visible without script (see §9 reveal rules). Under the canvas, always paint `.hero-nebula`: two soft radial-gradients (gold `.10` at 70%/40%, teal `.06` at 20%/70%) on `--bg-2` — so the hero composes even with zero JS.

### 3.3 Motion script (scroll choreography)

Entrance pattern is the explainer's `.rv` system: `opacity:0; translateY(22px)` → `.in` via IntersectionObserver (`threshold:.12, rootMargin: 0 0 -6% 0`), `--t-reveal --ease-reveal`, unobserve after firing. Stagger within a grid: `--stg` per child via `transition-delay` set inline by JS.

| Section | Trigger | What moves | Detail |
|---|---|---|---|
| S0 hero | load | kicker → H1 (two lines rise) → dek → CTAs → stats | `--stg-lg` (110ms) steps, each `--t-reveal`. Canvas fade-in 900ms @150ms. Star: first fast orbit, then 16s. Scroll cue pulse 1.8s infinite (RM: static, low-opacity) |
| S1 problem | `.15` view | kicker/title/lede trio; then 3 cards staggered 70ms; query strip slides `translateX(-12px)` last | cards hover: border→`--gold-dim`, `translateY(-2px)`, `--t-1` |
| S2 pipeline | scroll-linked | gold packet rides the spine (`top = f(scroll)` linear, no easing — it is a scrubber); station activates (dot fills gold + halo, title → `--gold-bright`) when packet passes `offsetTop+20px` | station activation transition 400ms; RM: all stations lit, packet pinned at 50%, no scrub |
| S3 two-legs | `.15` | formula chips count in (each coefficient counts up 700ms `--ease-out-expo`, tabular); legs diagram draws once (stroke-dashoffset 900ms) | `--why` chip hover reveals the breakdown line, `--t-2` |
| S4 features | `.15` | 6 cards staggered 70ms | icons are inline SVG, `opacity .0→1` + `translateY(6px)` inside card, +120ms after card |
| S5 architecture | `.15` | 5 layer cards staggered 90ms; between them, dashed connector flow `stroke-dashoffset` loop 2.4s linear infinite (RM: static dashes) | SQLite token pulses its border once on entry (600ms) — gold is earned: it marks where everything lives |
| S6 numbers | `.4` view | 4 stats count up 900ms `easeOutCubic` from 0 (or from the "before" value where the story is a delta, e.g. `0% → 67%`); suffix fades in after | RM: final values |
| S7 opensource | `.15` | trio stagger; on first view, command block "boots": border flashes `--line → --gold-dim → --line` once (400ms); copy button icon↔check morph 180ms | no confetti, no bounce |
| footer | — | none | — |

Copy feedback: clicking any code block's copy button swaps `copy → check` icons 180ms, shows `已复制 / copied` mono xs for 1.2s, `aria-live="polite"`.

### 3.4 docs.html — brief IA (companion page, same shell)

- Layout: `grid-template-columns: 240px minmax(0,1fr)`; sticky sidebar TOC (hairline right), prose column `--w-readable`, `--fs-body`, generous `--sp-7` section rhythm. Mobile (<880px): sidebar becomes a `<details>` TOC pinned under the nav.
- Sections: 快速开始 (quickstart with the README's venv commands) · 安装与模型 (install, `model install`, optional tools: Poppler/ffmpeg/whisper/Vision helper) · CLI 参考 (one sub-section per command: ingest / search / open / status / repair / forget / model / backup / serve / watch / mcp / estimate — each: synopsis, 2–4 flags, one real example) · 可搜格式与能力 (formats & four-state accounting) · 架构总览 (link-card to ARCHITECTURE.md) · 评测与证据 (suite structure, gates, link FEATURE_STATUS) · MCP · 故障排查 (top 5 from USER_GUIDE) · FAQ.
- Code blocks: `--bg-deep`, `--r-lg`, header bar with filename (mono xs, `--sub`) + copy button; syntax coloring limited to 3 token colors (`--gold-bright` commands, `--ink` args, `--sub-2` output). No highlighter library.
- Right-rail dot TOC (explainer style) on ≥900px; active dot = gold + halo.
- Motion: reveals only + progress bar. Nothing else — docs are a reference; still water.

---

## 4. learn.html — the Concept Map (centerpiece)

An explorable map of ~40 concepts in 7 clusters (the JOURNEY.md glossary). Not an article: a territory you fly over, with a side panel for depth.

### 4.1 Rendering technology — decision

**Canvas for all visuals + a mirror DOM layer for accessibility/keyboard (40 focusable buttons).** Continuous ambient drift, ~120 edge segments, LOD label fading and a free-form retargetable camera need exactly one draw loop; 40 SVG groups updated every frame instead pay per-element style-recalculation and transform bookkeeping for visuals that never get more complex than dots, rings and lines. Accessibility cannot live in canvas *or* SVG anyway — real focus, names and roles must come from real DOM elements — so a DOM twin of 40 buttons is required under either choice, which erases SVG's one structural advantage. Therefore canvas renders, the DOM twin is the semantic layer, and both read one shared state object.

### 4.2 Information model

Node schema (authored once, inlined as `<script type="application/json" id="gm-graph">`):

```
node: { id:"A3", cluster:"A", keystone:false,
  title_zh:"z 分数", title_en:"Z-score", aliases:["标准分","standard score"],
  def_zh / def_en, plain_zh / plain_en, example_zh / example_en,
  refs:[{path:"goldmine/search/fusion.py", lines:"120-141", note_zh/en}],
  deep:[{to:"A5", rel_zh:"进总分前先标准化", rel_en:"…"}],
  x, y }   // world coords, baked (see 4.3)
```

**Clusters and inventory (exactly 40 nodes)**

| Cluster | Keystone | Members (from JOURNEY.md) |
|---|---|---|
| A 搜索与表示 `--cl-a` | `A0` 向量空间 Vector space | A1 嵌入 · A2 余弦相似度 · A3 z 分数 · A4 FTS5/BM25 · A5 混合融合 · A6 查询模式 Query modes |
| B 模型与推理 `--cl-b` | `B0` llama.cpp | B1 量化 Q8_0 · B3 mmproj 投影 · B4 池化 Pooling · B5 边车 Sidecar |
| C 数据与身份 `--cl-c` | `C0` SQLite 单文件 | C1 WAL/事务 · C2 sha256 · C3 指纹 Fingerprint · C4 键序规范化 |
| D 评测与统计 `--cl-d` | `D0` 金标集 Gold set | D1 s@5/MRR/nDCG · D2 McNemar · D3 holdout · D4 基线与审计门 |
| E 多媒体管道 `--cl-e` | `E0` 多模态管道 | E1 双三次缩放 · E2 aHash · E3 RMS 静音门 · E4 ASR/VAD · E5 OCR |
| F 文件系统与进程 `--cl-f` | `F0` 行走门禁 Walk gates | F1 iCloud/SF_DATALESS · F2 符号链 · F3 inode 移动检测 · F4 EPIPE · F5 poll/os.read |
| G 过程与协作 `--cl-g` | `G0` 过程纪律 Process | G1 三态结论 · G2 预注册 · G3 对抗评审 · G4 一会话一分支 · G5 flaky 归因 |

**Edge classes**

1. `member` (keystone ↔ member, intra-cluster): always logically present, drawn at `alpha .10` only when zoom ≥0.9 *or* the cluster is hovered/selected/isolated.
2. `deep` (curated cross-cluster, the "dig-deeper" spine). Authored list, ≤16 total: A1↔B3 (同一空间) · A2↔A1 · A3↔A5 · A6↔A4 · A5↔A4 · B1↔C3 · E1↔C3 (逐位一致⇒家族) · E1↔B3 · C2↔C3 · C3↔C4 · D2↔D4 · D3↔D4 · F1↔F2 · F5↔B5 · G2↔G3 · G1↔D4. Drawn **only** when a connected node is hovered/selected: gold, 1px, dashed `4 4`, dash-offset animating (slow drift 1.2s/loop; RM: static dashes).

### 4.3 Layout & world

- **World**: fixed 1600×1000 coordinate space. Camera = `{x, y, scale}`.
- **No runtime force simulation.** Positions are *authored/baked*: clusters sit on a heptagon (radius 380, world center 800,500) ordered clockwise from top for adjacency: **A −90°, B −38.6°, E 12.9°, D 64.3°, G 115.7°, F 167.1°, C −141.4°** (so B neighbors A like model neighbors representation; G neighbors D like process neighbors evaluation). Keystone at cluster centroid; members on two seeded orbit bands (70–105 and 110–150 world units) at golden-angle spacing; a one-time overlap-relaxation pass (30 iterations, min gap 34 units) is run at authoring time and its output is *committed into the JSON*. Runtime is pure render + drift. Why baked: a map must be stable and memorable — a layout that re-flows every visit is a slide deck, not a map; and it makes load deterministic and CPU-free.
- **Ambient drift**: each node lissajoses around its anchor, amplitude 6 world units (keystones 3), `ω ∈ [.04,.09] rad/s` seeded. When a node is selected, *its* drift damps to 0.3× — the map goes quiet where you are reading.
- Node radius: keystone 24, standard 9 (world units). Keystone additionally carries the cluster label above it (mono 13 world-units, `--ink`; zh + EN on two lines, EN at `--sub`).

### 4.4 Camera (pan / zoom)

| Input | Behavior |
|---|---|
| wheel / trackpad pinch (ctrl+wheel) | zoom to cursor, `scale *= exp(-deltaY · 0.0016)`, range `0.45–2.6` (relative to initial fit ×1.0) |
| wheel without ctrl | also zoom (map is an app surface, not a scroll surface); page scroll is available everywhere outside the canvas |
| pointer drag (mouse/touch) | pan 1:1; on release, inertia `0.92/frame` until `<0.05px` |
| two-finger pinch | zoom on midpoint distance ratio, pan on midpoint travel |
| double-click / double-tap | zoom ×1.5 toward point, 400ms flight |
| buttons `+ − ⌖` | ×1.25 steps / reset-to-fit, `--t-flight` |

Zoom-to-cursor anchor (the whole formula): `cam.x = cursorWorld.x - cursorScreen.x/scale;` same for y. Flights are 700ms `easeOutExpo`, retargetable mid-flight (restart from current interpolated camera). Clamp: the visible rect must always overlap the world rect by ≥40% per axis; beyond the world edge, draw a vignette fade to `--bg-2` so "off the map" looks intentional.

**LOD** (scale = current zoom): keystone labels always; member labels fade in 0.75→0.95; member edges ≥0.9 (or cluster active); member labels hide when a search filter or another selection dims them.

### 4.5 Node visual states

| State | Visual | Transition in/out |
|---|---|---|
| rest | cluster-color dot `alpha .8`, label `--sub` | — |
| hover | dot r +2px, halo sprite, label → `--ink`, member edges to keystone fade in, deep edges ghost-preview `alpha .25` | 160ms `--ease-out` |
| focus (keyboard) | same as hover **plus** canvas focus ring: 2px `--gold-bright` circle at r+6 with 4px `--gold-glow` | 160ms |
| selected | dot → `--gold-bright`, glow sprite, ring `--gold`; label → `--gold-bright`; drift damped | 240ms |
| related (deep-neighbor of selection) | rest color, `alpha .85`, thin 1px gold ring | 240ms |
| dimmed (anything not rest-related when a selection/search/isolate is active) | `alpha .12`, label hidden | 300ms fade |
| visited (seen this session) | 3px `--sub-2` outline crescent at 45° — a quiet "you've been here" | 300ms |

Cursor: `grab` over empty map, `grabbing` while panning, `pointer` over a node.

### 4.6 Selection & drill-down panel

Click (pointer-up with <4px movement) or Enter selects: state changes ripple (related/dimmed), camera flies to frame the node (`scale 1.35`, node offset left of viewport center by 12% so the panel doesn't cover it — on ≥1100px the panel *pushes* the map via CSS grid and the camera recenters within the remaining area; below 1100px the panel overlays and the offset still applies), panel opens.

**Panel = tabs, not accordion.** The three knowledge layers are parallel translations of the same node (professional / plain / worked example) — the reader switches between *views*, they don't disclose a sequence, and tabs keep one cognitive object on screen at a time in a 420px column where stacked accordions would bury the worked example behind scroll; they also give us the sliding-underline transition that carries the whole panel's identity.

```
┌ #panel (420px, right) ─────────────────────────┐
│ [A·搜索与表示 chip]              [close ×]     │  ← chip: cluster color dot + mono label
│ z 分数                          ← serif h1     │
│ Z-score · standard score        ← mono xs sub  │
│ ─────────────────────────────────              │
│  定义 Definition │ 大白话 Plain │ 实例 Example │  ← tabs, sliding gold underline 240ms expo
│ ┌ active layer prose (fade+rise 180ms) ─────┐  │
│ └───────────────────────────────────────────┘  │
│ 真实代码 REAL CODE                              │  ← mono list `path:lines` (gold links to
│   ≤3 shown · [展开 expand]                     │     GitHub), collapsed if >3
│ 深挖 DIG DEEPER                                 │  ← chips: `rel label → target title`
│                                                 │     cluster-colored dot of target
│ [← prev]      A · 搜索与表示       [next →]     │  ← prev/next within cluster
└─────────────────────────────────────────────────┘
```

- Panel entrance: `translateX(24px)→0` + fade, `--t-3 --ease-out-expo`. Layer switch: content crossfade + 4px rise, 180ms `--ease-in-out`. Tab underline: absolute 2px gold bar, `left/width` animate 240ms expo.
- Esc or × closes; selection state clears; camera stays (you keep your bearings).
- Focus management: focus moves into the panel on open (first tab); Esc returns focus to the node's DOM twin.
- Empty layer fields never render blank tabs — see §4.10.

### 4.7 Dig-deeper behavior

Clicking a deep chip (or a canvas deep-edge's target node) = **flight**: 700ms expo camera flight to target, panel content swaps with the 180ms crossfade, and the *source* node keeps a 2s fading gold ring (you remember where you came from). Each flight appends to the **breadcrumb trail** — a top-left strip of mono chips, max 6: `A1 嵌入 → C3 指纹 → B1 量化`. Clicking a breadcrumb flies back. Esc with panel closed walks the trail backwards one hop.

### 4.8 Search & filter

Input in the toolbar (`找概念 · find a concept`, `/` focuses it). Matches `title_zh + title_en + aliases + cluster name`, 120ms debounce. While filtering: matched nodes stay lit, everything else → dimmed state; matched-substring underlined gold in results; cluster chips show match-count badges. Results dropdown (max 8, keyboard-navigable): `[cluster dot] title_zh · title_en`. Enter = fly + select first. Esc: first press clears filter, second clears selection, third resets camera. Empty result: `没有匹配 — 试试：cosine · 指纹 · holdout` with those as one-tap suggestion chips. Combining search + cluster-isolate: if a cluster yields 0 matches while isolated, show the inline hint chip `当前簇内无匹配 · 清除筛选` (a real button).

### 4.9 Bilingual toggle mechanics (learn-specific; global rules §5)

One toggle, instant swap, no reload. All strings come from the inlined graph JSON + a UI string table. On switch: `document.documentElement.lang` flips (`zh-CN`/`en`), canvas labels re-render on next frame, DOM twin labels swap, panel content re-renders (keep the active tab + scroll position), transition = 150ms opacity dip on affected regions only (`--ease-in-out`) — never a blank flash. Canvas label font stays mono 12.5 world-units regardless of language; zh labels get no letter-spacing, EN labels `.06em`.

### 4.10 Keyboard map & screen readers

- The **DOM twin**: `<div id="node-list" class="sr-only-focusable">` with 40 `<button>`s (one per node, ordered A→G). This is what tab/arrow/Enter operate on; the canvas reads state from it. Buttons never display visually (clip to 0) except when focused (a small visible label chip appears at the node's screen position, so sighted keyboard users see where they are). Roving tabindex: only the "current" node button is tabbable.
- Keys: **←↑↓→** move to the *geometrically* nearest node in that direction (spatial navigation, not DOM order) · **Enter/Space** select · **Esc** close panel / clear trail · **+ −** zoom · **0** reset camera · **/** search · **?** shortcuts popover · **L** language.
- `aria-live="polite"` region announces `已选中 A3 · z 分数` on selection. Canvas is `aria-hidden`; the twin buttons carry `aria-label="A3 z 分数 Z-score, 搜索与表示"` and `aria-describedby` → panel.

### 4.11 Cluster legend & isolate

Bottom-left: 7 chips (cluster dot + mono name + count). Hover = soft highlight of that cluster's nodes. Click = **isolate**: fly to keystone, other clusters dim to `alpha .06`, chip gets gold ring; click again (or Esc) releases. Chips double as the color legend — the map teaches its own palette.

### 4.12 States (empty / error / no-JS)

| Case | Treatment |
|---|---|
| no JS | learn.html ships a **static fallback**: the same 40 nodes as a CSS grid of 7 `<details open>` cluster groups, each term a card with all three layers + refs — a real (if plain) glossary. JS removes it on boot and mounts the map. This is also the print/referencer view. |
| graph JSON parse error (corrupt hand-edit) | catch at boot → keep static fallback, show inline mono banner `地图数据损坏 — 已降级为目录视图 / map data invalid — fell back to list view` |
| node layer field missing | hide that tab (`aria-disabled`, `.4` opacity) — never an empty pane; if `refs` empty, hide the block; if `deep` empty, hide the block |
| camera limit reached | `+/−` buttons disable at bounds (`.4` opacity, `aria-disabled`) |
| search empty | §4.8 empty state |

### 4.13 Mobile strategy (<760px)

- Map is full-screen app: toolbar 56px, canvas fills the rest. One-finger drag pans, two-finger pinch zooms; the page itself does not scroll (except inside panel/sheet). Hit radius for touch: `max(node_screen_r + 6, 22)px`.
- Panel = **bottom sheet**: `position:fixed; bottom:0; height: 72vh; border-radius: --r-xl --r-xl 0 0; --elev-3`, 36×4 drag handle, backdrop `rgb(0 0 0 / .4)` tap-to-close, swipe-down closes past 120px drag or with velocity. Entrance: `translateY(16%)→0`, `--t-3 --ease-out-expo`.
- Legend collapses to a horizontally scrollable chip row above the map; zoom controls stay bottom-right (44×44px, spaced 8px).
- **目录视图 (list view) toggle** in the toolbar swaps the canvas for the static fallback grid inline — some tasks (scanning all of cluster D) are simply better as a list on a phone. Same component as no-JS.

---

## 5. Bilingual typographic & language rules

**Decision: titles are always bilingual; prose fully toggles; zh is default.** Field terminology *is* English ("embedding", "holdout") — hiding it behind a toggle would sabotage the panel's job of teaching the real words; but full inline parentheticals in running prose wreck both languages' rhythm. So:

1. **Node/card titles**: zh title primary (serif), EN term as mono xs subtitle beneath — *in both languages*. In EN mode the two swap visual roles (EN serif primary, zh mono subtitle). A reader always gets both, hierarchy chooses the native one.
2. **Body prose** (defs, plain, examples, homepage dek, feature cards): authored in both languages; the toggle swaps wholesale. No mixed sentences — a zh sentence never carries a translated-in-place EN clause, and vice versa.
3. **First-mention rule (zh prose)**: the first occurrence of a technical term in a page gets the EN in mono parentheses — `余弦相似度（cosine）` — using full-width parens in zh, half-width in EN. After first mention: zh term only.
4. **Never translated**: code, identifiers, file paths, CLI flags, metric names (`s@5`, `MRR`), model names (`EmbeddingGemma 2`, `Q8_0`), brand names. Always mono.
5. **Punctuation**: zh text uses 「」 for quotes and full-width `，。：；` — never half-width commas inside zh prose. EN uses straight quotes. CSS `quotes` swap via `[lang]`.
6. **Numbers & units**: always half-width, always mono + `font-variant-numeric: tabular-nums` in stats/tables. `7.2×` stays `7.2×` in both languages.
7. **Language attribute**: the toggle sets `<html lang>`; `[lang="en"]` switches `--lh-lang` to 1.62 and permits `-0.01em` tracking on EN display; zh keeps ≥0 tracking always (negative tracking on CJK is forbidden).
8. **Persistence & default**: `localStorage("gm-lang")`; default = stored → else `navigator.languages` first match of `zh*/en*` → else `zh-CN`. The `<html lang>` of the shipped files is `zh-CN` (content is authored zh-first).
9. **Toggle control**: §2 shell spec. In EN mode the label chip in nav reads `中` as the target and vice versa; both segments always visible so the affordance is self-evident.
10. **Untranslated-yet content**: forbidden to ship — if a string lacks one language, ship only that language's text in *both* modes (mono fallback), and file it as a bug. Never machine-fallback silently.

---

## 6. Accessibility floor

- **Focus**: `:focus-visible` gold outline everywhere (§1.5); never `outline:none` without the canvas/DOM-twin equivalent (§4.10 provides it for the map). Focus is never removed by pointer interaction.
- **Contrast**: text ≥4.5:1 (all tokens in §1.1 are compliant; `--sub-2` is decorative-only by rule); UI marks (dots, rings, cluster colors) ≥3:1. Verify final pages with axe or Lighthouse audits; treat a fail as a release blocker.
- **Reduced motion** (`prefers-reduced-motion: reduce`): all CSS transitions/animations collapse (`transition-duration: 1ms`), hero renders its static composed frame (§3.2), camera flights snap (0ms), drift stops, dash-flow stops, typewriter/counters render final values, first-orbit trick disabled. The site remains fully functional.
- **Touch targets** ≥44×44px with ≥8px spacing: nav links (60px bar handles it), zoom buttons, chips, tabs (min-height 44), close buttons, copy buttons, breadcrumb chips.
- **Screen readers**: nav/landmarks (`header/nav/main/aside/footer` + labels); learn canvas `aria-hidden` with the DOM twin as the semantic layer (§4.10); `aria-live` announcements for selection and copy actions; docs headings hierarchy strict; all icon-only buttons carry `aria-label`.
- **Zoom & reflow**: 200% browser zoom and 360px width reflow without horizontal scroll (panels become sheets, grids become single column, starfield counts drop per §3.2).
- **Motion sickness**: no parallax on touch devices; no infinite loops faster than 1.2s period; nothing flashes.

---

## 7. What makes this feel top-tier — the six details

1. **Parallax with real depth separation**: hero clusters shift `±14px`, dust `±26px` against pointer (dust *is* the nearer layer), lerped at `0.04/frame` — two-layer depth, not one flat smear. Same law on the map: dust absent, but keystone labels breathe at 0.3× the node drift rate so the foreground leads.
2. **The query star teaches in one glance, then gets out of the way**: first orbit runs at `T=3s` (fast — you *see* neighbors ignite and lines form), then eases to the ambient 16s period. Attention captured, calm restored — the product's whole pitch as a motion arc.
3. **Camera flights are zoom-to-cursor and retargetable**: `cam.x = cursorWorld.x - cursorScreen.x/scale`, 700ms `easeOutExpo`, and a new input mid-flight restarts from the current interpolated position — never a jump, never a queue. This is the single biggest "feels like Linear/Google Maps" factor.
4. **The map goes quiet where you read**: selecting a node damps *its* drift to 0.3×, dims non-related nodes to `.12`, and holds related nodes at `.85` with thin gold rings — a reading state, not a demo state.
5. **Hairline discipline**: every border is `rgb(237 231 220 / .13)` 1px; elevation adds black shadow *on top of* the hairline, never instead. Giant chapter numerals are stroked at `.09` alpha — typography as architecture, visible only when you look for it. No border on this site is brighter than its text.
6. **Numbers behave like instruments**: tabular numerals everywhere (count-ups never wiggle columns), coefficient count-ins use the same `easeOutCubic` 900ms as the hero stats, and every metric on the page is tagged `<!-- verify -->` — the craft extends to not lying.

---

## 8. Degradation, performance budgets, QA checklist

**No-JS rules (file:// without script must still deliver the pitch)**

```html
<html lang="zh-CN" class="no-js"> …
<script>document.documentElement.className="js"</script>
```

- `.rv` hidden-initial styles apply **only** under `.js` — without JS every section is plainly visible.
- index: hero text/stats/CTAs fully static HTML; `.hero-nebula` CSS gradients stand in for the starfield.
- learn: static fallback grid (§4.12) is the default markup; JS mounts the map over it.
- docs: zero JS dependency by construction (its JS only adds TOC highlight + copy buttons).

**Performance budgets** — one CSS file + ≤3 small JS files per page, all `defer`; total JS ≤40KB unminified across the heaviest page (learn). Canvas frame budget ≤6ms on Apple Silicon, ≤10ms on 2018 Intel iGPU (sprite atlas law §3.2; no per-frame allocations). No layout thrash: canvases read `getBoundingClientRect` once per gesture start, not per frame.

**QA checklist (run before publish)**

1. Open all three pages via `file://` with JS **disabled**: hero text visible; learn shows full glossary grid; docs fully readable.
2. Toggle 中/EN on every page: no mixed-sentence leaks, no missing-string mono fallbacks, no layout jump in stats.
3. Keyboard-only walk of learn: `/` search → arrows spatially sane → Enter selects → tabs reachable → Esc chain (close → trail-back → reset) works; focus ring always visible.
4. `prefers-reduced-motion` pass: zero movement anywhere, all states still reachable.
5. Contrast audit (axe) on all pages: 0 serious violations.
6. Touch pass on a real phone: sheet drag-close, pinch zoom, no accidental page scroll inside map, 44px targets honored.
7. Verify every `<!-- verify -->` metric against `docs/FEATURE_STATUS.md`.
8. Weigh assets: gzip totals (HTML+CSS+JS per page) ≤60KB learn, ≤35KB index, ≤25KB docs.
9. Cloudflare Pages deploy: links work with `.html` suffixes; no console errors; no external requests in the Network tab (fonts, analytics — none).
10. Resize torture: 360px, 768px, 1440px, 200% zoom — no horizontal scrollbars, starfield/map counts step down correctly.
