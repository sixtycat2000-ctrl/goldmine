# goldmine — Homepage Copy (site/content/home.md)

> Source of truth for every claim below: `README.md`, `docs/FEATURE_STATUS.md`,
> `docs/JOURNEY.md`, `docs/USER_GUIDE.md` (all read at 2026-10-25, package 0.2.0
> development). Section markers are HTML comments; each block is zh first, then en.
> If a claim on the rendered site ever drifts from `FEATURE_STATUS.md`, the site is wrong.

<!-- ═══════════════════════ SECTION: HERO ═══════════════════════ -->

## HERO

### 定位语（中文）

**开源、本地优先的 macOS 语义搜索引擎——按含义搜索你的文件，一切只在你的 Mac 上发生。**

副标题（两句）：

你的文件说着不同的语言：文本、PDF、截图、录音、视频。关键词搜索碰不到含义，云端搜索要先读走你的文件——goldmine 在你的 Mac 上按含义搜索，一个字节都不离开本机。

### Positioning (EN)

**An open-source, local-first semantic search engine for your Mac — files found by meaning, never uploaded.**

Sub-line (two sentences):

Your files speak different modalities: text, PDFs, screenshots, voice notes, video. Keyword search misses the meaning, and cloud search reads your files first — goldmine searches by meaning, entirely on your Mac.

<!-- ═══════════════════════ SECTION: THE PROBLEM ═══════════════════════ -->

## THE PROBLEM

### 痛点一：关键词搜不到「同一个意思，不同的词」

你记得笔记里写过「长文件如何续读」，但搜「给长书分页做索引」——零结果。全文检索靠词匹配：词不重合，含义再近也找不到；于是你学着用文件里「可能用过的词」去猜，而不是用脑子里的意思去问。

#### Pain 1: Keyword search can't find the same meaning in different words

You remember a note about "resuming a long document," so you search "paginate a long book index" — zero results. Full-text retrieval matches terms, not meaning: if the words don't overlap, the closest paragraph stays invisible. You end up guessing which words the file *might* have used, instead of asking for the idea you actually have in mind.

### 痛点二：把文件交给云端索引，先交出去的是隐私

云端搜索要用含义，就得把你的文件送上去；系统级索引默认扫盘，范围由它决定而不是由你决定。便利的代价是信任：你的合同、笔记、录音去了谁的机器、留了多久，你只能选择不去想。

#### Pain 2: Cloud indexing means handing over your files first

To get meaning-based search in the cloud, your files have to go there; system-wide indexers scan by default, on their terms rather than yours. The price of convenience is trust: whose machines your contracts, notes and recordings land on, and for how long, becomes something you simply choose not to think about.

### 痛点三：截图、录音、扫描件——知识的孤岛

那张记着关键结论的截图、那段一小时的经验录音、那份只有扫描图像的 PDF：信息都在，检索都不在。它们没有文字层，关键词进不去，于是只能靠文件名和记忆，或者干脆重来一遍。

#### Pain 3: Screenshots, voice notes, scans — knowledge islands

The screenshot holding the key conclusion, the hour-long voice memo, the scan-only PDF: the information is all there, the searchability is not. With no text layer, keywords can't get in — so you rely on filenames and memory, or redo the work entirely.

<!-- ═══════════════════════ SECTION: HOW IT WORKS ═══════════════════════ -->

## HOW IT WORKS

### 工作原理（四步）

**第 1 步 · 你指定文件夹，goldmine 只索引这些。**
没有默认扫描，绝不碰你整个主目录。`goldmine init` 会逐步引导并让你显式选择范围；所选 roots 记录在 config 里，`goldmine status` 随时可见「它到底在看哪里」。

**第 2 步 · 文本变成向量（embedding），多模态各有各的路。**
钉版（pinned）的 embeddinggemma-2（Q8_0）由 llama.cpp 在本地推理，把每段文字变成一个 768 维向量——含义相近，向量就相近。安装可选的常驻助手 goldmine-embed 与 mmproj 后，照片、截图、轻量 PDF 页获得图像向量，无转录音频获得原始音频向量，视频按约 3 秒采关键帧（aHash 去重，至多 1200 帧）；OCR（macOS Vision）与 whisper 转录把截图和录音变成带时间戳的可搜文本。

**第 3 步 · 混合检索：含义 + 精确词，每份文件附诚实的能力行。**
语义分数抓「意思」，词法覆盖抓编号、路径、精确短语（`ZT-84` 这种只有词法能命中），融合后排序。每个文件以四种状态之一入库：`content-indexed`（全文可搜）、`metadata-only`（仅名字可搜，附原因）、`unsupported`（刻意不读，附原因）、`failed`（附原因）——降级从不静默。

**第 4 步 · 所有东西都在磁盘上的一个 SQLite 文件里。**
原文、词法索引、fp16 向量，全在一个 `.sqlite` 文件中：可备份、可搬走、可删除。没有上传，没有服务端。

### 零模型起步（README 原命令）

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
mkdir -p sample
printf '中文知识库：退款期限为四十五天。\n' > sample/notes.md
.venv/bin/goldmine ingest sample --db sample.sqlite --lexical-only
.venv/bin/goldmine search '退款期限' --db sample.sqlite --lexical --pack
```

### 启用语义检索（README 原命令）

```sh
goldmine model install embeddinggemma-2-Q8_0
goldmine ingest /chosen/folder --db library.sqlite
goldmine search 'memory pressure' --db library.sqlite --pack --budget 4000
```

真实搜索长这样（来自用户指南的实跑记录，路径已截断；`«»` 标出词法命中处）：

```
$ goldmine search "pod eviction memory pressure"
1. score 3.68  z 1.68  cov 1.00   doc    /…/docs/projects/k8s.md  L1-5
   "# Cluster notes — «eviction» The scheduler evicts a «pod» when a node is under
    «memory» «pressure» and the «pod»…"
```

`--why` 逐条展示分数算术（`why: 1·z 1.71 + 2·cov^1.5 1.00 = 3.71 · bm25 #1`）——排序不是黑盒。

### How it works (four steps)

**Step 1 · You choose folders; goldmine indexes exactly those.**
No default sweep, never your whole home directory. `goldmine init` walks you through setup and asks for scope explicitly; chosen roots are recorded in config, and `goldmine status` always answers "what does it actually look at?"

**Step 2 · Text becomes vectors (embedding) — each modality gets its own path.**
The pinned embeddinggemma-2 (Q8_0) runs locally through llama.cpp, turning each passage into a 768-dimensional vector: closer meaning, closer vector. With the optional resident helper goldmine-embed and the pinned mmproj installed, photos, screenshots and thin PDF pages get image vectors; transcript-less audio gets raw-audio vectors; videos are sampled into keyframes (one every ~3 s, aHash near-duplicate merge, at most 1200 frames). OCR (macOS Vision) and whisper transcription turn screenshots and recordings into timestamped, searchable text.

**Step 3 · Hybrid search: meaning + exact words, with an honest capability row per file.**
The semantic score catches "the idea"; lexical coverage catches identifiers, paths and exact phrases (`ZT-84` is lexical-only territory); the fused score ranks the results. Every file lands in one of four states: `content-indexed`, `metadata-only` (name-searchable only, with the reason), `unsupported` (deliberately not read, with the reason), `failed` (with the reason) — nothing degrades silently.

**Step 4 · Everything lives in ONE SQLite file on your disk.**
Source text, lexical indexes and fp16 vectors, all in a single `.sqlite` file: back it up, move it, delete it. Nothing is uploaded; there is no server.

<!-- ═══════════════════════ SECTION: FEATURES ═══════════════════════ -->

## FEATURES (six cards)

### 卡片 1 · 🔀 混合检索，受统计评测门约束

语义与词法融合排序，`--why` 公开每条命中的分数算术。检索质量对着一套冻结的 420 题 / 122 来源评测套件度量（210/210 分组 dev/holdout，holdout 封存未启）；搜索准入参数预注册，改动必须过统计门（McNemar / 成对 bootstrap），过不了就回滚。当前分数与未过的门全部公开在 FEATURE_STATUS。

#### Card 1 · 🔀 Hybrid search, gated by statistical evaluation

Semantic and lexical signals are fused into one ranking, and `--why` shows the score arithmetic for every hit. Retrieval quality is measured against a frozen 420-question / 122-source suite (a grouped 210/210 dev/holdout split; the holdout stays sealed); search admission parameters are preregistered, and changes must pass statistical gates (McNemar / paired bootstrap) or be rolled back. Current scores — and the gates they currently fail — are published in FEATURE_STATUS.

### 卡片 2 · 🖼️ 多模态：图像向量、原始音频向量、OCR、ASR

装上常驻 goldmine-embed 助手与钉版 mmproj 后，照片、截图、轻量 PDF 页获得图像向量；无转录音频获得原始音频向量；视频关键帧支持画面检索。OCR 走 macOS Vision，PDF 走 Poppler，语音走 whisper（转出 `0:06s` 式时间戳，命中可跳到那一秒）。不装助手照样工作——只是退回纯文本模式，且索引指纹逐字节不变。

#### Card 2 · 🖼️ Multimodal: image vectors, raw-audio vectors, OCR, ASR

With the resident goldmine-embed helper and the pinned mmproj installed, photos, screenshots and thin PDF pages get image vectors; transcript-less audio gets raw-audio vectors; video keyframes enable visual search. OCR runs on macOS Vision, PDF extraction on Poppler, speech on whisper (timestamps like `0:06s`, and a hit opens at that second). Without the helper everything still works — the install simply stays text-only, and the index fingerprint stays byte-identical.

### 卡片 3 · 🤝 诚实契约：能力行、零命中诚实、指纹身份门

每个文件一行能力状态（可搜 / 仅名字 / 刻意跳过 / 失败——都带原因），`goldmine status` 给出整张网格。零命中就是零命中：如实报告「在已索引快照中无可采信证据」，不假装证明信息不存在。换过嵌入模型的旧向量绝不与新向量混库——搜索拒绝并打印精确的重建命令（exit 3）。`goldmine open` 跳不了时间点就明说，从不装作成功。

#### Card 3 · 🤝 The honesty contract: capability rows, zero-hit honesty, fingerprint identity gates

Every file gets a capability row (searchable / name-only / skipped on purpose / failed — each with its reason), and `goldmine status` shows the whole grid. Zero hits means zero hits: it reports "no admitted evidence in the indexed snapshot" and never pretends that proves absence. Vectors from a different embedding model are never mixed in — search refuses and prints the exact rebuild command (exit 3). If `goldmine open` can't jump to a timestamp, it says so and opens plainly; it never fakes success.

### 卡片 4 · 🔒 本地优先：一个 SQLite 文件，零上传

索引是磁盘上的一个 SQLite 文件；唯一的网络请求是你显式执行的模型下载、和你显式指定摄取的 URL。iCloud 未下载占位文件直接跳过——索引永远不会触发一次下载。`goldmine backup` 生成校验过的单文件备份。整盘默认扫描与遥测明确在范围之外。

#### Card 4 · 🔒 Local-first: one SQLite file, zero uploads

The index is one SQLite file on your disk; the only network traffic is the model download you explicitly run and the URLs you explicitly ingest. iCloud placeholders that were never downloaded are skipped — indexing never triggers a download. `goldmine backup` produces a verified single-file copy. Whole-Mac default scans and telemetry are explicitly out of scope.

### 卡片 5 · 🧾 证据纪律：回执、预注册、对抗评审

这是这个仓库的工程文化。每条 PASS 结论必须附原始回执（命令行、退出码、输出哈希、前后源码身份）；结论只有三态：PASS / FAIL / MISSING，「基本通过」是禁语。结构性改动先预注册（冻结设计与通过判据再动手），再由独立评审视角专职攻击设计，裁决 ADOPT / 修订 / REJECT。DigUp 上测得的收益在 goldmine 复测为零，就按规则回滚——先验不当作证明。

#### Card 5 · 🧾 Evidence discipline: receipts, preregistration, adversarial review

This is the repo's engineering culture. Every PASS conclusion must carry a raw receipt (command line, exit code, output hash, before/after source identity); conclusions come in exactly three states — PASS / FAIL / MISSING — and "basically passes" is banned vocabulary. Structural changes are preregistered (design and pass criteria frozen before the work), then attacked by an independent review pass that rules ADOPT / revise / REJECT. A gain measured on DigUp that re-measured as zero on goldmine was rolled back by the rules — priors are not treated as proof.

### 卡片 6 · ♻️ watch / serve 守护进程 + MCP 集成

`goldmine watch --roots <文件夹>` 只看你选定的 roots 并刷新索引；`goldmine serve --db <路径>` 让检索模型常驻（受既有功耗 / 温度 / 内存守卫约束）。两者都需显式启动，安装不注册任何后台服务。`goldmine mcp --db <路径>` 提供四个只读工具（search / get / context / status），带预算、不透明引用与取消语义——把你的索引安全地接给 AI 客户端。

#### Card 6 · ♻️ watch / serve daemons + MCP integration

`goldmine watch --roots <folder>` watches only your chosen roots and refreshes their index; `goldmine serve --db <path>` keeps the search model resident under the existing power / thermal / memory guards. Both must be started explicitly — installation registers no background service. `goldmine mcp --db <path>` exposes four read-only tools (search / get / context / status) with budgets, opaque citations and cancellation semantics — a safe way to hand your index to AI clients.

<!-- ═══════════════════════ SECTION: HONEST LIMITS ═══════════════════════ -->

## HONEST LIMITS (current state, stated as a strength)

### 今天还没做到的（照实说）

goldmine 是 **0.2.0 development**：未配置公开发布，未宣称任何发布候选。最近一次完成的 420 题开发套件跑分（current005，2026-10-22）strict success@5 为 96/180，**12 项质量门为红**——没有缺失评测证据，就是没达标。当前源码的最终实测质量门、真实答案 / 评分 / 卡片聚类的剩余任务、hosted CI 首跑都还没有发生；可选的 quality 模式（multi-query、本地重排）收益未验证，默认关闭。历史分数只属于它记录的那份源码与索引快照，新实现不继承。把这些印在首页，是因为「知道自己在哪」本身就是这个项目的功能之一。

### What isn't done yet (stated plainly)

goldmine is **0.2.0 development**: no public release is configured and no release candidate is claimed. The latest completed 420-question development run (current005, 2026-10-22) scored strict success@5 96/180 and **fails 12 quality gates** — no missing evaluation evidence, just not there yet. The final real-model quality gates for the current source, the remaining answer / grading / card-clustering jobs, and a first hosted CI run have not happened; the optional quality mode (multi-query, local reranking) has unproven benefit and stays off by default. Historical scores belong only to the recorded source and index snapshots they were measured on; a later implementation inherits none of them. We print this on the homepage because "knowing exactly where you stand" is one of this project's features.

<!-- ═══════════════════════ SECTION: OPEN SOURCE ═══════════════════════ -->

## OPEN SOURCE

### 开源（MIT）

MIT 许可证，见仓库根目录 `LICENSE`。目前未配置公开发布渠道，从本地 checkout 安装（README 原命令）：

```sh
git clone <repository-url>   # ← 工程师：插入仓库规范 URL
cd goldmine
python3 -m venv .venv
.venv/bin/python -m pip install .
```

Python 3.12+，基础包只依赖 numpy；语义检索需要按 README 安装钉版模型与 llama-server。仓库的核心资产不只是代码，还有完整的证据链：`docs/FEATURE_STATUS.md` 的能力与质量台账、`docs/execution-plan/PROGRESS.md` 的逐提交记录、`docs/JOURNEY.md` 的全部决策考古。想贡献，先读 `AGENTS.md` 的协作铁律（一会话一分支，合并经用户）与三态结论工作法——在这个仓库，证据先于主张。

### Open source (MIT)

MIT License — see `LICENSE` at the repository root. No public package release is configured yet; install from a local checkout (commands from the README):

```sh
git clone <repository-url>   # ← engineer: insert canonical repo URL
cd goldmine
python3 -m venv .venv
.venv/bin/python -m pip install .
```

Python 3.12+; the base package depends only on numpy — semantic search additionally needs the pinned model and llama-server per the README. The repository's core asset is not just code but the evidence chain around it: the capability ledger in `docs/FEATURE_STATUS.md`, per-commit records in `docs/execution-plan/PROGRESS.md`, and the full decision archaeology in `docs/JOURNEY.md`. To contribute, start with the collaboration rules in `AGENTS.md` (one session one branch; merges go through the owner) and the three-state-conclusion workflow — in this repo, evidence precedes claims.

<!-- ═══════════════════════ SECTION: DOCS PAGE IA ═══════════════════════ -->

## DOCS PAGE IA (docs.html information architecture)

> IA only — nav sections with one-line descriptions mapping EXISTING repo docs. Content on docs.html stays links/summaries pointing at these files.

### 文档站信息架构（五个导航分区）

**1. Getting started · 入门**
- `README.md` — 零模型关键词快速开始、语义安装、修复与源范围命令总入口。
- `docs/USER_GUIDE.md` — 安装、八步 init、日常使用、故障排查前五名：新用户的主路径。
- `docs/BUILDING.md` — 本地原生构建与分发工件校验（钉版 llama.cpp b11461 + 审阅过的补丁）。
- `docs/CI.md` — 本地与 hosted 校验的区别；如何跑离线验收 profile。

**2. Concepts · 概念（含学习地图）**
- `docs/JOURNEY.md` — 八幕旅程 + 术语考古词典（A1–G5）：完整学习地图，每个术语挖到没有魔法为止。
- `docs/WALKTHROUGH-search.md` / `-ingest.md` / `-honesty.md` / `-fingerprint.md` / `-processes.md` — 五讲轮讲：系统现在长什么样（搜索 / 摄取 / 诚实 / 指纹 / 进程五个子系统）。
- `SPEC.md` — 命令行规范契约：实现必须与它一致，或与它同一次修改。

**3. Architecture · 架构**
- `ARCHITECTURE.md` — 总体架构：「摄取任何东西，搜索任何东西」的设计叙述。
- `docs/MODULE_MAP.md` — 模块边界与职责归属表。
- `docs/DESIGN_DECISIONS.md` — v1 决策日志：每条记录背景、选项与裁决。
- `docs/digup-derived-plan.md` — DigUp 考古：继承什么、搬什么（带 file:line）、明确不搬什么（带测量依据）。

**4. Eval & evidence · 评测与证据**
- `docs/FEATURE_STATUS.md` — 当前能力与质量台账：站点一切能力主张的锚点。
- `docs/PUBLIC_EVALUATION.md` — 源绑定的公开检索回归说明。
- `docs/HOLDOUT_AUDIT.md` — holdout 封存与联合审计规则。
- `docs/PERFORMANCE_VALIDATION.md` — 可复现性能证据的采集规程。
- `docs/COMPARISON.md` — 与 QMD / Recoll 的同题对照（诊断性，非市场排名）。
- `docs/RERANK_EXPERIMENT.md` · `docs/VECTOR_CACHE.md` — 两个显式可选实验的记录。
- `docs/execution-plan/PROGRESS.md` + `CONTRACTS.md` — 逐提交执行记录与执行契约。
- `evals/`（product_v4_expansion / answer_v3 / distill_real）— 冻结的评测输入套件。

**5. Operations · 运维**
- `docs/MCP.md` — 只读 MCP 集成：SDK 锁、预算、不透明引用、取消行为。
- `docs/CAPACITY.md` — 合成容量测量与回放规程。
- `docs/TEST_PLAN.md` — 离线优先的测试计划。
- `docs/ANCHORS.md` — 锚点命令卡：你亲手可跑、输出自己可解读的验证（<1 分钟）。
- `docs/USER_GUIDE.md` § Upgrade and recovery + `docs/FEATURE_STATUS.md` § Upgrade and recovery — 备份、升级、回滚与恢复路径。
- `docs/ROADMAP.md` — 产品需求路线图（大白话版）。

### Docs IA (five nav sections)

**1. Getting started**
- `README.md` — zero-model keyword quickstart, semantic setup, repair and source-scope commands; the main entry point.
- `docs/USER_GUIDE.md` — install, the eight-step init, daily use, top-5 troubleshooting: the primary user path.
- `docs/BUILDING.md` — local native builds and distribution artifact checks (pinned llama.cpp b11461 + reviewed patch).
- `docs/CI.md` — local versus hosted validation; how to run the offline acceptance profile.

**2. Concepts (with the learn map)**
- `docs/JOURNEY.md` — the eight-act journey plus the term-archaeology glossary (A1–G5): the complete learn map, every term dug down until no magic remains.
- `docs/WALKTHROUGH-search.md` / `-ingest.md` / `-honesty.md` / `-fingerprint.md` / `-processes.md` — the five-part walkthrough of the system as it stands (search / ingest / honesty / fingerprint / processes).
- `SPEC.md` — the normative CLI contract: the implementation must match it or change it in the same commit.

**3. Architecture**
- `ARCHITECTURE.md` — the overall design narrative: "ingest anything, search anything."
- `docs/MODULE_MAP.md` — module boundaries and ownership table.
- `docs/DESIGN_DECISIONS.md` — the v1 decision log; each entry records context, options and verdict.
- `docs/digup-derived-plan.md` — DigUp archaeology: what was inherited, what was ported (with file:line), what was deliberately not (with measurements).

**4. Eval & evidence**
- `docs/FEATURE_STATUS.md` — the current capability and quality ledger; the anchor for every capability claim on this site.
- `docs/PUBLIC_EVALUATION.md` — the source-bound public retrieval regression.
- `docs/HOLDOUT_AUDIT.md` — holdout sealing and the joint audit rules.
- `docs/PERFORMANCE_VALIDATION.md` — the protocol for reproducible performance evidence.
- `docs/COMPARISON.md` — the same-questions comparison with QMD / Recoll (diagnostic, not a market ranking).
- `docs/RERANK_EXPERIMENT.md` · `docs/VECTOR_CACHE.md` — records of the two explicit opt-in experiments.
- `docs/execution-plan/PROGRESS.md` + `CONTRACTS.md` — per-commit execution records and execution contracts.
- `evals/` (product_v4_expansion / answer_v3 / distill_real) — the frozen evaluation input suites.

**5. Operations**
- `docs/MCP.md` — the read-only MCP integration: SDK lock, budgets, opaque citations, cancellation.
- `docs/CAPACITY.md` — synthetic capacity measurement and replay protocol.
- `docs/TEST_PLAN.md` — the offline-first test plan.
- `docs/ANCHORS.md` — anchor command cards: verifications you run yourself and can interpret yourself (under a minute).
- `docs/USER_GUIDE.md` § Upgrade and recovery + `docs/FEATURE_STATUS.md` § Upgrade and recovery — backup, upgrade, rollback and recovery paths.
- `docs/ROADMAP.md` — the product roadmap in plain language.

<!-- ═══════════════════════ SECTION: CTA ═══════════════════════ -->

## CTA LINES

### 行动号召（三选）

**CTA-1（动手型）**
zh：从三行命令开始——索引你的第一个文件夹。
en: Start with three commands — index your first folder.

**CTA-2（诚实型 / 推荐主 CTA）**
zh：先读 FEATURE_STATUS：我们把还没做到的，也写在首页。
en: Read FEATURE_STATUS first — we publish what doesn't work yet, too.

**CTA-3（开源型）**
zh：MIT 开源。从 checkout 安装，亲自审计这条证据链。
en: MIT licensed. Install from a checkout and audit the evidence chain yourself.

<!-- ═══════════════════════ SECTION: FOOTER ═══════════════════════ -->

## FOOTER NOTE

### 页脚（许可与免责）

zh：
goldmine 按 MIT 许可证发布（© 2026 goldmine contributors，见仓库 `LICENSE`）。当前为 0.2.0 development，未配置公开发布渠道。本页所述能力以 `docs/FEATURE_STATUS.md` 为准；任何历史评测分数仅属于其记录的源码与索引快照。搜索结果是来自已索引快照的证据，零命中不证明信息不存在于你的全部文件。软件按「现状」提供，不附任何担保。

en:
goldmine is released under the MIT License (© 2026 goldmine contributors — see `LICENSE` in the repository). The package is 0.2.0 development; no public release channel is configured. Capabilities described on this page are bounded by `docs/FEATURE_STATUS.md`; any historical evaluation score applies only to the recorded source and index snapshot it was measured on. Search results are evidence from an indexed snapshot, and a zero-hit result does not prove the information is absent from your files. The software is provided "as is", without warranty of any kind.

<!-- ═════════════════ END OF FILE ═════════════════ -->
