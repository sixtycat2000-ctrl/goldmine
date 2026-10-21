# 实施记录

更新：2026-10-22；分支 `codex/p0-p2-improvements`。审查定稿提交 `b2d6ec2`，历史生产基线 `7694837`，已有文档提交 `6701765` 保留。版本仍为 **0.2.0 development**，没有推送、打 tag 或发布。

## 状态与范围

24 项全部进入实施；状态逐项记录在 [tasks.json](tasks.json)。`IMPLEMENTED` 表示代码已存在，不能代替完整任务验收；`VERIFIED` 需要该任务全部必需证据。P2-05 的本地 CI 范围与 P2-06 的当前安装交付已通过固定 task-check；hosted 验证仍为 MISSING，其他任务不能由这些局部通过推导为完成。

三位独立 AI agent 按 IR/统计、数据库/MCP 架构、QA/交付职责审查计划，接受并修订 18 项意见；实现后又接受并修复 6 项反例。角色分工不代表受聘的人类专家背书。具体见 [REVIEW.md](REVIEW.md) 与 `review/`。

## 2026-10-22：新语料与验收补齐

- 新建独立的 [product_v4_expansion](../../evals/product_v4_expansion/README.md)，保留旧 28 题 seed 原字节。新增套件已完成 **420 题、122 份真实来源、175 份 oracle view、89 个可答来源组**；七类各 60 题，dev/holdout 各 210 题、各类各 30 题，组无交叉。来源包含原始代码/多语言文档、真实 PDF 页、历史扫描专利、浏览器截图、20 段发布方音频及 10 张原始 EXIF 照片。没有用生成图卡、TTS 或伪造 EXIF 填补真实媒体。
- 与题目作者独立的 AI agent 已按固定 seed 核查 **56 题，每类 8 题**，逐题记录实际原文/像素/发布方 transcript/元数据依据；60 道无答案题另检查全部 175 份 view 及来源范围。输入 manifest 已标为 FROZEN，SHA `b8351272194fed76e438d30d452a3fff9393f99a7e5b1264f710490cc65677b0`。这仅表示来源、标签与分割冻结，不表示质量达标或候选代码冻结。
- 两个不同目录的可搬运构建均只含 122 个注册来源文件，逐字节 SHA、20 个真实截图类型标记与 build receipt 一致。`P0-05/product-expansion-001/portability-002.json` 保存证据。原 `7694837` 与当前候选分别全新建库，模型、OCR、Whisper/VAD、poppler/ffmpeg 与 Python 3.12.0/NumPy 2.5.4 均实际核对。两臂均通过完整能力预检；原版 12 个 `.rst` 不支持结果按原 classifier 证明计 miss，不减分母。真实 dev 对照已完成；holdout 尚未打开。
- 来源审计发现并修复通用导入问题：7/10 照片原先把修改日期当拍摄日期；新增有界 DateTimeOriginal 读取与失败恢复；`.rst` 按真实文本导入；两个并发图像 worker 的临时目录初始化增加锁。10 张原始 JPEG 的独立 EXIF 对照全部一致；158 项相关生产/原生测试通过，重现的失败原始记录保留。独立复核确认普通重导入能从临时读取失败恢复，合法无 EXIF 的文件仍可 unchanged skip。
- 原始版本不支持 `.rst` 时，固定分母计为未命中；受支持格式缺 OCR/ASR 则仍为 MISSING。新增严格 original classifier/原始准备 receipt 与只读 schema-v4 回读适配，不能由调用者自报 unsupported、迁移旧库或缩减题目集合。ASR 与局部原文证据协议已在首次评分前登记，并加入数字、单位、符号、否定词、跨段拼接等拒绝反例；不会修改 oracle 来迁就实际提取结果。
- 公开评测已增加逐题 ranked doc IDs 和独立 qrels 重算；三个固定 ZIP 已下载到仓库外专用 cache，并实际核验全部 bytes/SHA；原始获取证据在 `P0-06/public-assets-001/`。仍需真实三数据集、四臂的独立准备与评分运行。文档、联合 holdout 和完整 CLI/pack 容量验收器已实现并经反例审查。当前源码的回归、离线安装和 22 条文档命令均已重新捕获；旧 CI/安装/性能结果保留为历史证据，新增源码不能自动继承其 PASS。

## 新产品集 dev 实测

同一物化 corpus、同一固定模型与 server，原始 `7694837` 对照当前候选，**210 题全量：180 可答、30 noise；可答 46 个独立来源组**。这里的 success 要求冻结的来源、位置及事实 span 证据，严格于只命中文件名；与 legacy150 的比例不能直接混列。完整摘要、模型/源码/命令 SHA 见 [product-dev-20261022.json](product-dev-20261022.json)。

| 开发指标 | 原版 | 当前默认 control |
|---|---:|---:|
| 可答 success@1 | 54/180（30.0%） | 88/180（48.9%） |
| 可答 success@5 | 65/180（36.1%） | 103/180（57.2%） |
| pack 全部必需事实 | 64/180（35.6%） | 95/180（52.8%） |
| semantic success@5 | 3/30 | 12/30 |
| CJK success@5 | 17/30 | 18/30 |
| lookup success@1 | 5/30 | 18/30 |
| PDF 精确证据 success@5 | 0/30 | 12/30 |
| media-text success@5 | 13/30 | 20/30 |
| compound success@5 | 25/30 | 22/30 |
| compound pack 全部事实 | 17/30 | **5/30** |
| noise 正确空结果 | 5/30 | **5/30** |

默认 control 的可答 success@5 变化为 +21.1 个百分点，按 46 个来源组的 paired bootstrap 95% 区间约为 [+11.1,+30.5]，seed=20261021、10,000 次；它是该开发套件上的结果，不是总体能力或市场排名保证。nDCG 的组级区间仍跨 0。初次统计把 30 个 noise 的 rank 零分混入可答差值，已修复并独立复核；保留首次原始记录，以同样四个 arm 重新捕获，逐题结果与首次完全一致。

三组预登记参数均完成，全部 **FAIL 11 个质量检查**，没有选中参数，没有增加第四组：

| 参数 | 可答 success@5 | noise 正确空结果 | 结论 |
|---|---:|---:|---|
| control，z=3.0 | 103/180 | 5/30 | 保留默认，未达标 |
| higher_z，z=3.5 | 103/180 | 10/30 | 不足所需 27/30，拒绝选择 |
| within_file_margin=0.25 | 98/180 | 8/30 | 质量下降，拒绝选择 |

原始新参考在 `P0-05/product-expansion-001/reference-002/dev-command-final/`，三组候选在 `candidate-002/{control,higher_z,within_file_margin}/dev-command-final/`；`dev-selection-final.json` 保存全部门槛与组级对照。12 道 compound 全事实退步案例另存 `compound-regressions.json`，包含真实命中/输出材料与缺失事实，不凭空归因。接下来优先处理这些退步与无答案准入；不降低目标、不启用尚未通过的质量实验、不提前解封 holdout。

独立架构 agent 随后对这 12 道已暴露 dev 题完成只读逐案诊断：24 个必需事实全部仍在真实 SQLite 的正确原文位置；主要失败阶段为 **4 道 exact-anchor 准入、2 道证据窗口选择、6 道材料包装配**，共丢失 13 个原来已输出的事实。8 道两份必需文件仍在检索结果中；另有 8 道受整条材料预算省略影响，两集合有重叠。不是证据对齐器误判，也不是半句截断。原始证明在 `P1-06/compound-diagnostic-001/diagnostic.json`，SHA `130a6d9eb2db5d00c24fda800f000ec288adfe075c508b2adefae757693ffb54`。下一处最小实现方向是对已证明连续的支持片段恢复精确接缝去重，保留全部来源、引用、断续标记和整包预算；尚未实施或声明通过。不得直接删除 remaining-word 拒收，现有 multi-query grammar 对这 12 题均不扩展，单纯开启它没有已证收益。

固定验收入口 `P0-05/product-expansion-001/task-acceptance-004/manifest.json` 已完成 **94 项任务测试 PASS**，并从原版/候选真实 SQLite 回读 **4,445/8,169 个证据 span**。结果为 **retrieval.product_dev FAIL、11 项质量门槛失败、MISSING 0**，source-stability PASS。首次 task-check 误用了只装 NumPy 的评测专用环境，缺 pytest/项目导入；失败目录保留，以准备好的项目解释器和显式当前源码路径重跑，未重做或筛选模型排名。

## 当前冻结源码的回归与交付

实际环境仍为 macOS arm64 / Python 3.12。当前固定源码四组无必需 skip，合计 **1689 passed**。先前 `frozen-base-007` 的单项 hermetic fixture 失败保留；只隔离该假图像的 EXIF 调用后重新冻结，没有修改生产代码或放宽断言。

| 固定入口 / 范围 | 当前结果 | 原始证据目录 |
|---|---:|---|
| CI base | **1486 passed** | `reports/execution/P0-06/frozen-base-009/` |
| CI native | **189 passed** | `reports/execution/P0-06/frozen-native-009/` |
| CI MCP，locked SDK 环境 | **6 passed** | `reports/execution/P0-06/frozen-mcp-009/` |
| CI Foresight | **8 passed** | `reports/execution/P0-06/frozen-foresight-009/` |
| 四组原始回放、失败注入、实际 actionlint | **PASS**，hosted=MISSING | `reports/execution/P2-05/ci-local-008/manifest.json` |
| P2-05 本地固定 task-check | **PASS** | `reports/execution/P2-05/task-acceptance-008/manifest.json` |
| 当前 wheel/sdist/重建 wheel、离线源码外 base/MCP 安装、实际 native OCR/768 embedding、v4→v6 备份升级恢复 | **PASS** | `reports/execution/P2-06/distribution-006.json` |
| P2-06 固定 task-check，显式绑定全部资产 | **PASS** | `reports/execution/P2-06/task-acceptance-008/manifest.json` |
| 14 份文档、55 个本地链接、22 条实际离线命令 | **PASS** | `reports/execution/P2-07/docs-002/manifest.json` |
| P2-07 文档组件固定 task-check | **PASS**，全任务仍未完成 | `reports/execution/P2-07/docs-task-003/manifest.json` |

完整 CLI/pack 容量工具的四项复核漏洞已闭环，60 项集成测试通过；源码、12 组固定命令、资源估算和失败保留规则在 `P2-02/full-cli-source-freeze-001.json`。最后一处测试文件 EOF 空行清理也触发严格源码 SHA 变化，故重捕获四组 `*-009`、本地 CI 与任务证据；生产与评分结果未变化。完整容量实测单独记录，不能用小型 fixture 或早期向量阶段顶替。各项 SHA 与当前结果索引见 [validation-20261022.json](validation-20261022.json)。

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

## 上一个冻结源码检查点的验收

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

1. **P0-05 / 产品质量**：420 题真实来源、独立标签审计、分割冻结、同源原版/候选索引及真实 dev 参考均完成。三组预登记参数全部真实 FAIL，默认仅 103/180 success@5、5/30 noise 正确空结果，compound pack 事实由 17/30 降为 5/30。质量未达标，不能进入最终 sealed 审计。旧 28 题保留为诊断 seed，holdout 曝光为 0。
2. **P0-02/03 / P1-10 答案质量**：新 60 可答 +20 trap 的真实独立作答/四评分、校准与候选比较缺失。历史 20 题、fixture/mock 或改名输出不能顶替。
3. **P0-04 / P1-05 full-media**：固定 169 行的只读预检发现现有库缺实际 EXIF/部分 OCR provenance，严格 exit 2，分母保持 164+5。需按原许可范围补齐真实证据再测。
4. **P1-06/07/08**：拒收三臂真实 dev A/B 已完成且全部 FAIL，`selected=null`；可选多查询/rerank 的真实收益与完整资源门槛尚未通过，保持默认控制与关闭实验。
5. **P2-02/03**：最终缓存源码的完整 CLI/pack 规模测量、真实媒体增量 benchmark，以及 controlled/real 各 10 分钟 watch churn 和完整生命周期证据仍缺。已有真实模型的局部检查不能顶替这些。
6. **P2-04 卡片**：严格真实标注/来源回放评测器已实现；真实至少 100 文档、8 主题的独立标签缺失。mock hard-variant 结果只验证机制。
7. **剩余验收代码与输入**：public、联合 holdout、文档和完整容量工具均已实现并经独立反例审查；当前源码 1689 项回归和安装/文档证据通过。仍缺真实外部集/完整容量等运行输入与结果；hosted 适配器仍为未实现且缺实际远程证据。未运行真实 holdout，零收集仍必须 MISSING。
8. **P2-08 完整竞品对照**：native view、真实 PDF/媒体、语义/quality 模式、同标准位置验证、重复性能/增量/资源对照和完整最终数据缺失。当前表只表示词法诊断。
9. **发布**：仓库没有配置 remote，hosted CI 未运行。`local_rc` 的必需数据、质量和生命周期条件也不齐，因此没有发布候选、tag、push 或公开发行。

接续执行严格沿 [P0.md](P0.md)、[P1.md](P1.md)、[P2.md](P2.md) 和 [CONTRACTS.md](CONTRACTS.md)。420 题、独立审计和原始参考已完成；下一步依据已暴露的 dev 反例修复证据选择与准入，完成真实公开集、容量和生命周期等必需运行。三个拒收参数臂已全部测量，不能追加第四组或放宽门槛；新算法需明确登记和审查。全部算法/参数冻结且前置质量达标后，才进行一次联合 sealed 审计。

原始大报告仅在本地忽略目录 `reports/execution/`；此文是紧凑索引，不能据摘要替代原始 receipts，也不能把较早源码的性能或安装结果沿用为未来版本验收。
