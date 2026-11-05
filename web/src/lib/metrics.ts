/* Captured records, not promises about a new corpus or a later source revision.
 * Public summaries: docs/FEATURE_STATUS.md. Raw reports are local ignored files.
 */
export const SUITE = {
  tests: "3,378",
  testsCount: 3378,
  testsNote: {
    zh: "2026-11-04/05 · 三类测试记录合计",
    en: "2026-11-04/05 · passes across three test profiles",
  },
  // verify: high-confidence-optimizations-learn-20261104; offline3046/native323/MCP9
  frozenQuestions: 420,
  frozenSources: 122,
  // verify: 2026-10-26 product-current-017; 210 dev queries, 180 answerable
  strictSuccess: "155 / 180",
  gatesPassed: 17,
  gatesTotal: 19,
  sqliteFiles: 1,
  // Scope: Goldmine's local indexing/search file uploads; MCP returns evidence to its caller.
  bytesUploaded: 0,
} as const;

export const QMD_HEAD_TO_HEAD = {
  // Historical2026-10-25: comparison-report-001/REPORT.md + comparison-matrix.json.
  // Times are incidental CLI observations in the quality run, not a performance protocol.
  rows: [
    {
      metric: { zh: "原件视图 · 前5条来源命中率", en: "Native files · source success@5" },
      goldmine: "78.3%",
      qmd: "66.1%",
      winner: "goldmine",
    },
    {
      metric: { zh: "该配置纳入索引的来源 · 共122份", en: "Sources indexed by this configuration · of 122" },
      goldmine: "122",
      qmd: "60",
      qmdNote: { zh: "未纳入62份", en: "62 not indexed" },
      winner: "goldmine",
    },
    {
      metric: { zh: "原件视图 · CLI逐题耗时中位数（含模型加载）", en: "Native files · median CLI time, including model load" },
      goldmine: "1.4s",
      qmd: "21.8s",
      winner: "goldmine",
    },
    {
      metric: { zh: "原件视图 · CLI逐题耗时p95", en: "Native files · p95 CLI time" },
      goldmine: "1.7s",
      qmd: "36.5s",
      winner: "goldmine",
    },
    {
      metric: { zh: "统一预处理文本 · 前5条来源命中率", en: "Common pre-converted text · source success@5" },
      goldmine: "91.7%",
      qmd: "100%",
      winner: "qmd",
    },
    {
      metric: { zh: "30题无答案查询 · 空结果率（Goldmine跨配置范围）", en: "30 no-answer queries · empty-result rate (Goldmine range across configurations)" },
      goldmine: "43–60%",
      qmd: "0%",
      winner: "goldmine",
    },
  ],
} as const;

export const LEDGER = [
  {
    no: "1",
    value: SUITE.tests,
    note: {
      zh: "2026-11-04/05分层记录：离线3046、原生323、可选MCP9，共3378条通过记录。三类均无失败或必须项跳过，所测运行时源码一致；这类测试验证实现边界。",
      en: "Recorded on 2026-11-04/05: offline 3046, native 323 and optional MCP 9, totaling 3378 passing test records. No failures or required skips, with stable runtime source. These tests check implementation boundaries.",
    },
  },
  {
    no: "2",
    value: { zh: "155 / 180 · 17/19项", en: "155 / 180 · 17/19 gates" },
    note: {
      zh: "2026-10-26开发集记录：210题中180题可回答，严格成功@5为155/180；19项质量门过17项。噪声与OCR两项未过。420是冻结题目总量，另210题保持密封。",
      en: "Historical development run on 2026-10-26: 180 answerable questions among 210; strict success@5 was 155/180, with 17 of 19 quality gates passing. Noise and OCR gates failed. The 420 frozen questions include 210 sealed questions.",
    },
  },
  {
    no: "3",
    value: { zh: "Success@5未变", en: "Success@5 unchanged" },
    note: {
      zh: "既有索引回归记录：real-103在两套回归中由第1降至第2；Top1差值为-0.0096/-0.0076，MRR为-0.0033/-0.0027。两门满足原通过规则，不代表排名完全相同。",
      en: "Existing-index regressions: real-103 moved from rank 1 to 2 in both suites. Top1 deltas were -0.0096/-0.0076 and MRR deltas -0.0033/-0.0027. Both gates met their existing rules; rankings were not identical.",
    },
  },
] as const;

export const REPO_URL = "https://github.com/sixtycat2000-ctrl/goldmine";
export const SITE_ORIGIN = "https://goldmine-site.sixtycat2000.workers.dev";
export const FEATURE_STATUS_URL = `${REPO_URL}/blob/main/docs/FEATURE_STATUS.md`;
export const HISTORICAL_QMD_URL = `${FEATURE_STATUS_URL}#historical-qmd-comparison-2026-10-25`;
