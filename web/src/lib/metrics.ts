/*
 * Every number on the site, in one auditable place.
 * Copy is transcribed from the verified v3 index.html; each block carries its
 * `verify:` anchor, same discipline as the old `<!-- verify: source doc -->`
 * comments. If docs/FEATURE_STATUS.md changes, this file changes with it.
 */

export const SUITE = {
  tests: "3,200+",
  testsNote: { zh: "离线测试（全量 8 分钟）", en: "OFFLINE TESTS (8-MIN SUITE)" },
  // verify: 2026-10-25 suite run 3200/37/2; evals/product_v4_expansion manifest
  frozenQuestions: 420,
  frozenSources: 122,
  // verify: 2026-10-26 official run product-current-017, 210-question dev fold
  strictSuccess: "155 / 180",
  strictSuccessPct: "86.1%",
  gatesPassed: 17,
  gatesTotal: 19,
  sqliteFiles: 1,
  bytesUploaded: 0,
} as const;

export const QMD_HEAD_TO_HEAD = {
  // verify: docs/COMPARISON.md + reports/execution/P2-08/comparison-report-001 (QMD pinned 2.8.3)
  rows: [
    {
      metric: { zh: "直接搜原件 · 成功@5", en: "Raw files · success@5" },
      goldmine: "78.3%",
      qmd: "66.1%",
      winner: "goldmine",
    },
    {
      metric: { zh: "122 份里读得了的", en: "Of the 122 files, can read" },
      goldmine: "122",
      qmd: "60",
      qmdNote: { zh: "62 份原生读不了", en: "62 natively unreadable" },
      winner: "goldmine",
    },
    {
      metric: { zh: "每题中位耗时（含模型加载）", en: "Median per-query time (incl. model load)" },
      goldmine: "1.4s",
      qmd: "21.8s",
      winner: "goldmine",
    },
    {
      metric: { zh: "每题 p95 耗时", en: "Per-query p95" },
      goldmine: "1.7s",
      qmd: "36.5s",
      winner: "goldmine",
    },
    {
      metric: { zh: "纯文本精搜 · 成功@5（同一份预处理文本）", en: "Pure-text precision · success@5 (same pre-converted text)" },
      goldmine: "91.7%",
      qmd: "100%",
      winner: "qmd",
    },
    {
      metric: { zh: "捣乱查询敢交白卷（30 题无答案）", en: "Dares to return zero hits on noise queries (30 with no answer)" },
      goldmine: "43–60%",
      qmd: "0%",
      winner: "goldmine",
    },
  ],
} as const;

export const LEDGER = [
  {
    no: "01",
    value: "3,200+ / 8 min",
    note: {
      zh: "2026-10-25 全量：3200 过 / 37 跳 / 2 个已档案存量失败。回执在仓库 reports/execution/。",
      en: "2026-10-25 full suite: 3200 passed / 37 skipped / 2 archived known failures. Receipts in repo reports/execution/.",
    },
  },
  {
    no: "02",
    value: { zh: "155 / 180 · 17/19 门", en: "155 / 180 · 17/19 GATES" },
    note: {
      zh: "2026-10-26 终局官方跑（product-current-017，210 题开发折）：严格成功@5 155/180，19 项质量门 17 过。两门未过均为已归档硬上限：noise 20/30（陷阱虚构在问题前提层，无词可查——闸门方案被仿真证伪后放弃）、media-OCR 6/9（冻结评测器 32 字符对齐最小窗×2 + OCR 内容缺口×1）。明细在 docs/FEATURE_STATUS.md。",
      en: "Terminal official run (product-current-017, 2026-10-26, 210-question dev fold): strict success@5 155/180, 17 of 19 quality gates passing. Both fails are archived hard limits: noise 20/30 (the fabricated premise has no verbatim token to check — the gate idea was disproven by simulation and dropped) and media-OCR 6/9 (frozen-evaluator 32-char alignment floor ×2 + an OCR content gap). Details in docs/FEATURE_STATUS.md.",
    },
  },
  {
    no: "03",
    value: "Δ 0.0000",
    note: {
      zh: "两个回归门（legacy 109 题 / 全媒体 128 题）最近一次全绿的逐指标差值。参数改动必须过 McNemar / 成对 bootstrap 显著性检验，过不了就回滚。",
      en: "Per-metric delta of the last green run of both regression gates. Any admission-parameter change must pass McNemar / paired-bootstrap significance or roll back.",
    },
  },
  {
    no: "04",
    value: { zh: "1,412 行 C", en: "1,412 LINES OF C" },
    note: {
      zh: "媒体嵌入边车的全部代码：静态链 llama.cpp + mtmd，图片缩放与参考实现逐位一致（黄金表测试钉死）。文本与媒体两条嵌入管线互不干扰。",
      en: "The media sidecar in full. Image resize is bit-identical to the reference (golden-table test).",
    },
  },
  {
    no: "05",
    value: "3.88 s",
    note: {
      zh: "本机实测：ffmpeg 对 5 分钟 1080p H.264 抽关键帧 3.88 秒（对照组 macOS 原生 4.07 秒）；选型记录在案，含反例数据。",
      en: "Measured locally: ffmpeg extracts keyframes from a 5-min 1080p H.264 in 3.88 s (vs 4.07 s native); the decision, counterexamples included, is on record.",
    },
  },
  {
    no: "06",
    value: { zh: "0 条", en: "0" },
    note: {
      zh: "静默降级。OCR 缺了写原因、模型没装写原因、页数截断写标记——每个文件四种状态都带原因，status 一屏可见。",
      en: "Silent degradations. Every file is in one of four states, each with its reason, visible in one status screen.",
    },
  },
  {
    no: "07",
    value: { zh: "11 → 17 门", en: "11 → 17 GATES" },
    note: {
      zh: "质量六仗（2026-10-25/26，两天）：窗口两级裁决、元数据绑定、交叉编码器窗口仲裁、点号定义优先、名字浓度准入、OCR 再水合——每仗先对抗评审后预注册冻结再实现再官方重跑，六轮零门回退；公共基准三连在界内（|Δ|≤0.02）；独立审计 840 项重评分零错配。每仗证据链在仓库 reports/execution/P0-05/。",
      en: "Six quality battles in two days (window tiers, metadata binding, cross-encoder window arbitration, dotted-definition precedence, name-concentration admission, OCR rehydration) — each adversarially reviewed, preregistration-frozen, implemented, and officially re-run with zero gate regressions; public benchmarks held within |Δ|≤0.02; an independent audit re-scored 840 row-metrics with zero mismatches. Full chains under reports/execution/P0-05/.",
    },
  },
] as const;

export const COMPARE_TABLE = {
  columns: [
    { key: "cap", label: { zh: "能力", en: "CAPABILITY" } },
    { key: "gm", label: { zh: "goldmine", en: "goldmine" } },
    { key: "spot", label: { zh: "Spotlight", en: "Spotlight" } },
    { key: "cloud", label: { zh: "云端 AI 搜索", en: "Cloud AI search" } },
    { key: "grep", label: { zh: "grep / ripgrep", en: "grep / ripgrep" } },
  ],
  // cell: yes | partial | no | text
  rows: [
    {
      cap: { zh: "换个说法还能搜到（语义）", en: "Finds paraphrases (semantic)" },
      gm: "yes", spot: "no", cloud: "yes", grep: "no", spotNote: true,
    },
    {
      cap: { zh: "精确编号 / 路径 / 短语", en: "Exact identifiers / paths / phrases" },
      gm: "yes", spot: "yes", cloud: { zh: "部分", en: "partial" }, grep: "yes",
    },
    {
      cap: { zh: "图片、录音、视频按内容搜", en: "Image / audio / video by content" },
      gm: "yes", gmNote: { zh: "本地", en: "local" }, spot: "no",
      cloud: { zh: "需上传", en: "upload first" }, grep: "no",
    },
    {
      cap: { zh: "文件离开本机", en: "Files leaving the machine" },
      gm: { zh: "0 字节", en: "0 bytes" }, spot: { zh: "系统内", en: "on-device" },
      cloud: { zh: "全量上传", en: "full upload" }, grep: { zh: "0", en: "0" },
    },
    {
      cap: { zh: "离线可用", en: "Works offline" },
      gm: "yes", spot: "yes", cloud: "no", grep: "yes",
    },
    {
      cap: { zh: "每个文件读到没读到、为什么", en: "Per-file what-was-read and why" },
      gm: "yes", gmNote: { zh: "能力行", en: "capability row" }, spot: "no", cloud: "no", grep: "—",
    },
    {
      cap: { zh: "检索质量有公开度量", en: "Published retrieval-quality gates" },
      gm: "yes", gmNote: { zh: "420 题 + 统计检验", en: "420 Qs + significance" },
      spot: "no", cloud: "no", grep: "—",
    },
  ],
  footnote: {
    zh: "* 对比限于各产品公开行为（2026-10）。Spotlight 的语义能力限于个别 App 内的智能搜索；云搜索指主流「上传后 AI 检索」类产品。表里没有一项是营销话术——每一行都能在仓库 docs/FEATURE_STATUS.md 里找到依据。",
    en: "* Based on publicly documented behavior (2026-10). Every row is backed by docs/FEATURE_STATUS.md in the repository — none of it is marketing.",
  },
} as const;

export const REPO_URL = "https://github.com/sixtycat2000-ctrl/goldmine";
export const SITE_ORIGIN = "https://goldmine-site.sixtycat2000.workers.dev";
