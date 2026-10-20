# 实施记录

更新：2026-10-21；分支 `codex/p0-p2-improvements`。审查定稿提交 `b2d6ec2`，历史生产基线 `7694837`，已有文档提交 `6701765` 保留。版本仍为 **0.2.0 development**，没有推送、打 tag 或发布。

## 状态与范围

24 项全部进入实施；状态逐项记录在 [tasks.json](tasks.json)。`IMPLEMENTED` 表示代码已存在，不能代替完整任务验收；`VERIFIED` 需要该任务全部质量、数据、性能和交付证据。当前没有把缺证据的任务标为 VERIFIED。

三位独立 AI agent 按 IR/统计、数据库/MCP 架构、QA/交付职责审查计划，接受并修订 18 项意见；实现后又接受并修复 6 项反例。角色分工不代表受聘的人类专家背书。具体见 [REVIEW.md](REVIEW.md) 与 `review/`。

## 已落地的生产改造

- **只读应用服务与引用**：CLI/MCP 共用一次请求级 SQLite 读快照。canonical citation 绑定 index UUID、segment、来源/原文/位置和证据类型；get/context 使用已存原文。特殊路径按 URI 编码，不在搜索时隐式迁移或重新读取来源文件。
- **词法与精确检索**：派生索引做 NFKC/casefold 与 CJK 单字/双字，短语再验证连续性；原文保留。schema v6 支持原串/alias、文件名/路径/编号分层命中。文件名命中标为 metadata，不伪造正文位置。
- **迁移与并发**：显式 `reindex lexical` 仅重建派生字段；旧打开 writer 的整个事务由 RAISE(ROLLBACK) fencing 拒绝，不能只撤销半次正文/FTS 更新。待处理文件不会用新 SHA 配旧正文参与搜索。
- **PDF 与媒体证据**：PDF 使用真实页号并正确处理同页多块；每文件至多三条支持证据，跨页保留位置和引用。OCR/ASR/EXIF 状态、失败/无文字/未运行和证据模态可区分；默认 llama 明确是文本 embedding，未宣称图片视觉语义。
- **完整材料包预算**：header、查询、原文、why、来源路径和所有引用均计数；tokenizer 中途不可用则整包重新估算。空包仍装不下时返回类型化错误。只折叠实际输出完全相同的材料并保留所有来源；24 条上限有显式省略计数。
- **超时与进程所有权**：排队、SQLite、分词、embedding、rerank、慢速 HTTP 头/正文共用绝对 deadline；取消可传播。CLI/MCP 的 SIGTERM 会展开自有资源，回收自有重排子进程。
- **MCP**：显式安装官方 SDK `mcp==2.0.0`，stdio 四个只读工具；严格 schema、一项执行/两项排队、真实 context 与邻居 citation、错误/取消/队列反例。CLI 与 MCP 的实际材料包一致。
- **可选质量实验**：确定性多查询只使用原查询中保守可分的最多两个子句；有限 RRF 与按主题选材料。固定 Qwen3 rank-head GGUF 的本地 rerank 只处理已接受的至多 20 条，每条至多 384 tokens。两者默认关闭，收益未验证；拒收参数注册最多三组，`selected=null`，保留原控制值。
- **向量规模优化**：显式构建、单独启用的不可变 fp32 mmap 缓存；SQLite 为事实来源，以 UUID/fingerprint/schema/epoch/trigger/shape 校验。崩溃、磁盘满、损坏或失效回退 SQLite，不自动启用，不自动删除旧代。
- **增量可观测性**：scan/hash/extract/embed/commit 五阶段与真实 embed/OCR/ASR/native startup 计数；未变化导入先检查文件与 fingerprint，不启动模型或做维数探测。关闭 instrumentation 保持原输出行为。
- **可维护性与交付**：CLI 拆到命令适配器和共享服务，入口不超过 350 行；wheel/sdist 打入新子包。原生静态包使用固定 source/patch、依赖闭包和签名验证；离线源码外安装覆盖实际 OCR/embedding 与备份升级回滚。

## 同一冻结源码的验收

实际环境：macOS arm64、Python 3.12。不是尚未运行的 Linux/Python 3.13 hosted 结果。以下必需测试均无 skip；其他 profile 的测试被明确划分，不冒充本 profile 已执行。

| 固定入口 / profile | 实际结果 | 原始证据目录 |
|---|---:|---|
| `ci_validation.py --profile base` | **1233 passed**，130.92s | `reports/execution/P0-06/frozen-base-006/` |
| `--profile native` | **188 passed** | `reports/execution/P0-06/frozen-native-006/` |
| `--profile mcp`，独立 locked SDK 环境 | **6 passed** | `reports/execution/P0-06/frozen-mcp-006/` |
| `--profile foresight` | **8 passed** | `reports/execution/P0-06/frozen-foresight-006/` |
| 四组 CI + 固定失败注入 + 实际 actionlint | **PASS**，hosted=MISSING | `reports/execution/P2-05/ci-local-006/manifest.json` |
| wheel/sdist/重建 wheel、源码外安装、原生资源、升级恢复 | **PASS** | `reports/execution/P2-06/distribution-005.json` |

四组合计 **1435 passed**。11 个防伪造/不完整 IR 证据反例已从临时审查报告提升为 `tests/test_ir_acceptance.py`，并加入 P0-06 的固定 task-check。

四组均有 collection、原始 stdout/stderr、JUnit、退出码、source-stability 和 SHA receipts；执行器会重放并验证当前代码内容。源码/验收脚本/测试/依赖锁/CI 工作流变化会拒绝复用；仅提交 Git 或更新任务进度不会使验收陷入自我依赖。

另实际运行了真实 llama 的 3 个 pack 测试：预算 512/1000/4000/32768 × context 0/1/3、CJK/表格/代码/长路径/引用、8190-byte 空结果查询，13.54s，`reports/execution/P1-10/live-pack-001.xml`。真实 bounded reranker 测试 1 passed，`reports/execution/P1-07/live-backend-001.xml`；六个 endpoint 输入/顺序探针在 `P1-07/runtime-probe-001/`。这些证明协议、预算和资源机制，不证明答案质量提升。

早期综合测试的两个失败（distill 评测错误读取规范化 FTS、watch 测试把第二轮删除数误当第一轮）均修复并包含在最终冻结回归中，原失败报告保留在 `P0-06/hermetic-integration-003/`。

## 测量与对照

历史六文件逐字节归档到 `evals/history/20261021/`。原始 legacy 150 题（145 可答 +5 noise）真实模型复跑得到 success@5 **0.7862068965517242**、success@1 **0.593103448275862**、MRR **0.6834619594964422**、nDCG@10 **0.6884711067454795**，原门禁 exit 0。它是历史基线再现，不能写成当前改进收益。

完整 12 组容量对照覆盖 10k/100k/250k/500k × 每文件 1/10/100 片段、768 维；独立原始/候选快照、独立进程、3 次 warmup、10 次交错测量，top IDs 与 z 一致。250k 的 SQLite 路径已触发 1.5GiB 内存条件，500k 同时触发冷载与内存条件，因此开展缓存实验。`P2-02/capacity-001/` 与 `cache-capacity-002/` 保存全部 12 组，不只保留获胜组。

缓存实验中 250k/500k 的**冷载向量阶段**分别约缩短 96%/98%，查询进程峰值 RSS 约减少 47–50%/46–50%；它不是完整 CLI/pack 延迟。实验绑定较早缓存源码快照；最后 Python int/str 序列兼容修复另有 99 项回归和 zero-copy 证据，不能据此自动继承正式最终性能 PASS。缓存仍须显式启用。完整 CLI/pack 规模阶段仍 MISSING。

最终候选的真实 legacy 回归门禁 exit 0（`P0-01/candidate-final-004/`，9.77s）。success@5 **0.8000**、success@1 **0.6069**、MRR **0.6930**、nDCG@10 **0.7013**；lookup@1 10/16，noise 正确空 2/5。相对原 baseline 的主指标变化均很小且未建立统计显著提升；这里只证明历史回归门禁通过，不替代新产品集。原始 stdout 保持 legacy 的两个 JSON 文档（检索结果、gate）；`summary.json` 仅为据原文派生的索引。

最终命令延迟使用同一固定 v4 输入快照、同一已验证 native-001 server、同一模型与开发查询，对候选 controlled/real 及原 `7694837` 做三组各 30 warm/cold 交错测量；候选另外做每组 30 次 direct/MCP 同请求配对。原始 receipt 重算后的 fast 与 MCP 门禁均 **PASS**（`P2-03/latency-verified-current.json`）。

| 30 样本 / arm | 常驻 p50 / p95 | 冷启 p50 / p95 | MCP 成对额外 p95 |
|---|---:|---:|---:|
| 最终候选 controlled | 149.07 / 204.65 ms | 1051.95 / 1098.23 ms | 3.40 ms |
| 最终候选 real，未覆盖内存压力 | 148.07 / 205.31 ms | 1045.17 / 1088.92 ms | 2.33 ms |
| 原 `7694837` controlled | 144.04 / 149.35 ms | 1031.87 / 1064.87 ms | 原版本无 MCP |

候选/历史常驻 p50 比为 **1.03491**，在预定 1.20 退步上限内；不是速度提升声明。结果绑定 `P2-03/search-{controlled,real,reference}-006/`，输入在 `latency-fixed-inputs-005/` 与 `latency-inputs-current.json`。较早报告的索引/server 不同，配对验收正确拒绝复用，保留原报告并重跑整组。

新解包 server 的一次首个 warmup 超时也保留在 `search-controlled-005/`（零条正式计时样本）。独立实际 backend 启动随后为 0.80s、embedding 成功；未据此推断超时根因。最终性能结果按合同是在三次 warmup 后测量，不作为首次新安装初始化的保证；首次冷启仍需生命周期复核。

QMD 2.8.3 与 Recoll 1.44.2 只装在忽略目录，不改用户索引/全局配置。三方各实际运行同一 28 题、12 份原始 MIT 文档/源码的 common-text 词法诊断；84/84 请求完整，170 个原始流逐字节回放后重算。可答题 success@5 为 goldmine 17/22、QMD 3/22、Recoll 2/22；noise 正确空结果为 2/6、6/6、6/6。保留 goldmine 噪声弱点；默认查询语义不同、样本偏本项目且很小，**不能作为市场整体排名**。详见 [COMPARISON.md](../COMPARISON.md)。

## 未完成且不能放宽的验收

1. **P0-05 / 产品质量**：当前 draft 仅 28 题、12 个原始来源；420 题尚缺 392 题、真实 PDF/扫描件/媒体及合格 grouped split、冻结原始参考。没有拿模板凑数，没有建立或打开正式 product sealed holdout。
2. **P0-02/03 / P1-10 答案质量**：新 60 可答 +20 trap 的真实独立作答/四评分、校准与候选比较缺失。历史 20 题、fixture/mock 或改名输出不能顶替。
3. **P0-04 / P1-05 full-media**：固定 169 行的只读预检发现现有库缺实际 EXIF/部分 OCR provenance，严格 exit 2，分母保持 164+5。需按原许可范围补齐真实证据再测。
4. **P1-06/07/08**：拒收/多查询/rerank 的真实 dev A/B、质量增益与完整资源门槛未过；保持默认控制与关闭实验。
5. **P2-02/03**：最终缓存源码的完整 CLI/pack 规模测量、真实媒体增量 benchmark，以及 controlled/real 各 10 分钟 watch churn 和完整生命周期证据仍缺。已有真实模型的局部检查不能顶替这些。
6. **P2-04 卡片**：严格真实标注/来源回放评测器已实现；真实至少 100 文档、8 主题的独立标签缺失。mock hard-variant 结果只验证机制。
7. **剩余验收代码**：public 评测需导出逐题 ranked doc IDs 才能独立按 qrels 重算；当前旧 advisory summary 被明确拒绝。一次 sealed holdout、最终文档/hosted 证据适配器也尚未完成，计划中的 `tests/test_documentation.py` 尚未建立；零收集必须 MISSING，不能以文档已更新替代。
8. **P2-08 完整竞品对照**：native view、真实 PDF/媒体、语义/quality 模式、同标准位置验证、重复性能/增量/资源对照和完整最终数据缺失。当前表只表示词法诊断。
9. **发布**：仓库没有配置 remote，hosted CI 未运行。`local_rc` 的必需数据、质量和生命周期条件也不齐，因此没有发布候选、tag、push 或公开发行。

接续执行从上述依赖最前端补齐，严格沿 [P0.md](P0.md)、[P1.md](P1.md)、[P2.md](P2.md) 和 [CONTRACTS.md](CONTRACTS.md) 的标准。先扩大并独立审计 dev/来源组，完成原始参考和公开逐题导出，再做预登记实验；所有算法/参数冻结后只做一次联合 sealed 审计。不要用反复查看 holdout 或放宽门槛消除 MISSING。

原始大报告仅在本地忽略目录 `reports/execution/`；此文是紧凑索引，不能据摘要替代原始 receipts，也不能把较早源码的性能或安装结果沿用为未来版本验收。
