# 实施记录

更新：2026-10-22；分支 `codex/p0-p2-improvements`。审查定稿提交 `b2d6ec2`，历史生产基线 `7694837`，已有文档提交 `6701765` 保留。版本仍为 **0.2.0 development**，没有推送、打 tag 或发布。

## 状态与范围

24 项全部进入实施；状态逐项记录在 [tasks.json](tasks.json)。`IMPLEMENTED` 表示代码已存在，不能代替完整任务验收；`VERIFIED` 需要该任务全部必需证据。P2-05 的本地 CI 范围与 P2-06 的安装交付曾在 `7192395` 检查点通过固定 task-check；后续源码已有变化，当前整体验收需要新证据。hosted 验证仍为 MISSING，其他任务不能由这些局部通过推导为完成。

三位独立 AI agent 按 IR/统计、数据库/MCP 架构、QA/交付职责审查计划，接受并修订 18 项意见；实现后又接受并修复 6 项反例。角色分工不代表受聘的人类专家背书。具体见 [REVIEW.md](REVIEW.md) 与 `review/`。

当前代码已合入 typed 测量执行器、C002 锚定准入、完整比较报告 framing 与旧库兼容修复。最终 465 个代码/配置文件的历史多 profile CI 已完成：base 2575、native 232、MCP 6、Foresight 8（该 profile 现已按用户授权退役），合计 **2821 项通过、0 skip**；真实 actionlint、本地失败注入与 P2-05 task-check 也通过。该 CI 认证其记录的 Foresight 移除前源码，当前源码需要新 CI。一次预登记 current005 固定 dev 已完成，严格回放 **FAIL 12、MISSING 0**；机制和 CI 通过不代表质量达标。

独立审查复现的两项兼容性问题已闭环并合入 integration017：只读 schema4/5 按真实存储格式读取和核验来源；可选 whole-metadata 预查在计数前剔除不可能完整匹配的 alias/stem。`P1-06/legacy-readiness-design-002/` 的61项验收映射与原49项均已独立核对。当前 CI 与一次预登记 C/default210dev 已完成；接下来执行公开集、容量、生命周期、交付和完整竞品对照。真实 answerer/judge 配置与 GitHub remote 仍缺，已向用户请求信息；独立工作继续。原三参数注册、质量目标和未打开的 holdout 保持冻结。

## 当前 Mac 的接续入口

以下是现有冻结计划的执行游标；入口文件存在不代表实际运行成功。所有路径以仓库根目录为工作目录，报告目录前缀为 `reports/execution/`。第 1–3 步已完成；接下来执行第 4 步，后续真实任务尚未启动。

| 顺序 | 前置条件 / 入口 | 必须读取的结果 |
|---|---|---|
| 1 | `P1-06/legacy-readiness-proposal-001/` 的精确实现独立闭环后，由 root 合入 integration017 并运行 focused 与保留的原始反例 | 真实旧格式、完整 typed/owner/phrase/预算、只读与现代损坏反例均通过；不得改分数或基准合同 |
| 2 | host Python 3.12 执行 `P0-06/post-c1-ci-001/run.py`，再执行 `P2-05/ci-local-current-001/run.py` | 各 profile 全 PASS、0 必需 skip、同一源码；真实 actionlint/local-CI/P2-05 task-check PASS，hosted 仍单列 |
| 3 | 产品既有 `product-expansion-001/runtime/bin/python` 顺序执行 `P0-05/product-current-005/seal_fixes.py`、`run.py` | 一次固定 C/default210dev，固定 004 对照；原始 receipt、真实 index/span 回放及 PASS/FAIL 全保留，不能看到 FAIL 就重跑选分 |
| 4 | 已完成同源码005（严格 PASS 或 FAIL，不能 MISSING）后执行 `P2-08/goldmine-private-assets-002/refresh_installation.py`、`P0-06/public-current-002/run.py` | 新不可变 installation 完整 SHA；三个公开集全部四 scorer 的候选及既有原版严格回放，不缩题目 |
| 5 | public 完成，检查实际可用磁盘并停止本任务其他重负载后执行 `P2-02/full-capacity-current-002/run.py` | 完整12规模/每文件分布、全部固定阶段及 CLI/pack 调用和严格相等性/资源结果；小型 fixture 不能代替 |
| 6 | 最终源码保持不变，分别执行 `P2-08/full-source-view-001/run.py`、`P2-04/real-current-001/run.py`、`P2-03/latency-current-001/run.py` 和生命周期包既有 `execute.py --execute lifecycle-real-001` 入口 | 全122来源转换；120真实卡片文档；三组30样本及MCP配对；controlled/real watch各600秒和serve/增量序列，全部保留实际失败 |
| 7 | 同源码 CI/005 及文档内容稳定后，host Python 执行 `P2-06/current-001/run.py`、`P2-07/current-001/run.py` | 新 wheel/sdist/重建、离线 base/MCP/native/旧库升级恢复、14文档与22真实命令及固定任务检查；旧 snapshot 不能顶替 |
| 8 | `P2-08/engine-assets-current-002/commands.py --stage all` 仅渲染既有固定入口；按 README 逐阶段核验真实完成后再执行下一条 | 新安装/16工具probe/真实canary/来源与dev导出/18cell plan；先编译固定1728 packet，再3780质量请求、严格回放、完整1728测量/更新及回放 |

CI 或机械检查失败时保留原目录，先做具体修复并重新冻结源码；质量 FAIL 是结果，不是删除、换参数或重复挑分的理由。性能阶段单独执行，不与本任务测试、建库、转换或模型任务混跑。答案实际引擎/设置与 GitHub remote 尚待用户信息；旧169媒体输入缺有效原始 provenance 的问题单列，不能注入答案、减分母或伪造通过。holdout 仍关闭，全部必要条件齐备前不创建 RC/tag/push。

## 2026-10-22：7192395 后的实施与证据补齐

- `7192395` 的完整 CLI/pack 容量实测已结束：10k/100k/250k/500k × 每文件 1/10/100 片段，12/12 一致性通过，共 4,752 次真实 CLI 调用（含 warmup）、4,320 次正式计时，记录的命令失败为 0。独立回放在 `P2-02/full-capacity-review-001/review.json`。缓存阶段门槛通过，但 144 个 p50/p95 对照中有 56 个候选更慢，不能声称全部 CLI 变快；cold 表示新解释器，未清空 OS 文件缓存。该结果只认证上述检查点，后续生产修复需要重新测量。
- 已合入独立审查的连续片段接缝去重与多段引用折叠保护、词法回填查询规划修复、grader 实际 PACK 上下文绑定、hosted CI 收集/回放适配器。保留所有来源、位置、完整材料预算和原质量门槛；没有追加第四个准入参数候选。第一组当前源码集成回归 314 项通过，原始证据在 `integration-after-capacity-001/focused-001/`。
- 已采用 [answer_v3](../../evals/answer_v3/README.md) 的 80 题（四类各 20 题）及 [distill_real](../../evals/distill_real/README.md) 的 120 份真实文档（110 base +10 incremental、8 主题）。两套来源及标签均经独立审查，冻结只针对输入。答案的 480 次真实作答、960 次真实评分仍缺，不能以机械 fixture 充数；卡片的实际 native 准备、聚类和增量质量运行尚未完成。
- 已合入真实卡片准备/来源回放及竞品完整来源转换组件。新一组集成回归 **239 项通过、0 skip**，证据在 `integration-after-capacity-002/focused-002/`。首次运行的 24 个 setup error 来自 macOS `/var` 临时目录别名；只修正测试路径 canonicalization，生产所有权校验保持严格，首次失败保留在 `focused-001/`。另对按 manifest 顺序选定的 8 个原始文档/代码/PDF/截图/照片/音频完成 **39 条真实转换命令**，兼容性检查通过，21.88s；`P2-08/converter-canary-001/` 不代表完整语料、检索质量或资源门槛通过。
- 竞品实际 engine 捕获、有效 native resolver 和 opt-in 资源观察器的独立审查现已闭环并按精确 patch 组合合入；产品 dev 已完成下述 pack/SQL 检查点捕获。随后发现分块数据丢失，旧候选 public 评分已记录取消，修复后的候选需全新准备与实测。卡片、生命周期和新规模验收随后执行。新的完整 CI、安装和文档交付尚未捕获；下文 `*-009` 等结果保留为历史检查点。
- 答案材料/job 准备组件随后完成独立反例闭环并合入，57 项机械测试通过。错误 output 路径会在任何写入、打开 index 或启动 backend 前拒绝与冻结来源/实际 corpus 重叠。它只准备请求并校验外部记录，真实 answerer/judge 与执行桥仍缺。
- 新源码已经完成两个全新 native index 和四组固定 dev 实跑（原版 control + 候选既有三 profile），共 **840 个 dev 请求**。原版点估计全部再现；候选检索 success@5 仍为 **103/180**、noise 正确空仍 **5/30**。pack 全事实从 95/180 到 **96/180**，compound 从 5/30 到 **6/30**，唯一变化为 `compound-real-uv-05` 的第二个事实恢复；没有其他题目的 metric 变化。三 profile 仍各 **FAIL 11**，`selected=null`、默认保持 control、第四参数候选为 0。详见 [新源码 dev 摘要](product-dev-post-pack-20261022.json) 与 `P0-05/product-current-003/`；这是一个事实恢复，不是广泛收益或显著性结论。
- 公开三数据集的两个角色原始准备/四臂评分已经启动，原始 ZIP 仍在独立角色 cache，不在验收期间下载。QMD 的 3 个默认角色模型也已显式从官方不可变 revision 下载并完整核 byte/SHA，依赖复制为自有不可写目录；尚未执行 QMD 模型 canary 或竞品质量查询。这些准备不替代实际同题对照。
- opt-in 资源观察器 003 已按原样合入。独立审查关闭完整 manifest 写出后的磁盘计数、实际 root exec 之前的内存误归属、FD 数量与 `lsof` receipt 绑定三项问题；原反例和 001/002 提案均保留。根代理独立重建后 **169 项通过**，实际源码与 distill、答案准备、来源转换、公开捕获消费者联合回归 **287 项通过、0 skip**，在 `integration-after-capacity-004/`。观察器记录真实 sampled tree RSS/FD/输出预算；完整生命周期峰值仍未认证，普通 fork 的这一附加限制与必需 sampled measurements 分开报告，逃逸/缺计数/无 target 样本仍为 MISSING。这里没有竞品质量或性能通过结论。
- 公开三集两臂原始准备均已成功，reference 935.31s、candidate 1060.65s，完整 17,490 份文档已建库；2,029 个 test query 的四 scorer 排名与独立 qrels 回放正在进行。这些阶段时间有已披露的准备/审查并行活动，不是隔离性能比较。完整 122 份来源转换的 9 个工具/模型角色及许可/provenance 输入检查也已通过，实际全量转换尚未执行。
- 当前产品固定 task-check 随后完成：**94 项测试通过、0 skip**，从两份实际 SQLite 分别回放 **4,445 / 8,175 个证据 span**；`retrieval.product_dev` 仍 **FAIL 11**、MISSING 0，源码稳定性通过。证据在 `P0-05/product-current-003/task-readback-001/`，没有重新排名或重开 holdout。生命周期执行包的 122 份来源及完整 license/NOTICE 也已逐字节准备，根代理复跑其 9 项离线检查与实际 pytest/NumPy import 预检通过；尚未运行真实五阶段序列。
- engine 捕获的 27 个最终文件在架构审查与独立资源调用审查闭环后合入。真实 resolver 逐项绑定 11 个 Goldmine 工具；资源 publication 校验失败保留一条明确错误 disposition 并阻止下一子进程，已知实测 FAIL 优先于后续 MISSING。根代理独立组合重放 **81 项及 2 条 JS 语法检查通过**，实际合入后的比较/来源转换/资源/registry 联合回归 **229 项通过、0 skip**，在 `integration-after-capacity-005/`。生产与正在运行的 public evaluator SHA 映射均未改变。这里仍没有真实竞品质量、性能或增量结果。
- 对已暴露 dev 的只读分层诊断发现：180 道可答题中 **161 道 top5 含任一正确来源文件**，而严格证据 success@5 为 103/180；77 道严格失败中 58 道已找对文件、19 道连文件 top5 都未命中。此为定位信息，不能替换严格指标；下一步优先审核通用证据选择/材料包装配修复，全部 30 道 noise 也继续保留。长时间的最终容量/延迟捕获安排在该算法修改明确后，不以第四参数候选补调。
- 新只读诊断随后在实际 chunker 上证明段落边界会跳过下一 token。独立审查闭环后，三文件修复按原样合入为 `chunk.v4-paragraph`，普通 ingest 会刷新 stale content，metadata-only 不因版本变化重做。实际合入源码的 **79 项 focused 测试通过、0 skip，另 5 项原有慢/协议测试本轮明确未选**，根代理两条英文/中文原始反例均恢复字符守恒；`integration-after-capacity-006/` 保留精确 patch、来源与结果。尚未据此运行新质量评分。
- UTF8 byte-fallback 修复随后经精确 patch 独立闭环并合入为 `chunk.v5-utf8`；实际 **120 项通过、0 skip，4 项原有协议测试未选**，包含原 5MB token-index 扫描回归，见 `integration-after-capacity-007/`。字符回退路径的硬窗口尾部丢失也已独立修复并合入 `chunk.v6-char-tail`，实际 **165 项通过、0 skip，5 项原有慢/协议测试未选**，见 `integration-after-capacity-008/`；新修复对非正 size 直接抛 ValueError，旧提案的死循环反例保留。完整原生/CLI CI 仍需在最终生产源码上运行。这些是字符守恒与预算机制证据，尚无新质量评分。
- 证据/pack 的 B003 已独立闭环并按精确九文件 patch 合入，实际 **227 项通过、0 skip**，见 `integration-after-capacity-011/`。它保护 hybrid 的语义正文、按轮分配支持片段，并用实际来源顺序防止跨缺口接缝去重；001/002 遗留的同页 PDF 等反例保留。随后完整 CI 暴露 pure-semantic ablation 也新增支持片段，改变固定容量工作量；窄补充经独立 **232 项通过、0 skip** 后合入 `integration-after-capacity-012/`，恢复该模式既有 primary+neighbors 行为，未修改容量适配器或相等/计时门槛。当时噪声充分性 C 仍只有设计，数值单位 casefold 反例待修；随后 C002 的实现记录见下文。没有新质量结论。
- 固定竞品测量协议编译器 002 已独立闭环并合入。原 001 会把整个 `files.info` 丢弃，导致 EXIF 等语义 provenance 变化仍被判逻辑相同；根代理反例及原包保留。修订严格保留语义 metadata/detail，仅排除有实际生产依据的顶层 `ocr_ms` 且仍存原值，独立 **88 项及 JS 语法检查通过**。实际比较/编译器/资源/验收联合回归最终 **317 项通过、0 skip**，见 `integration-after-capacity-009/`、`010/integration-002/`。原先一次自然退出竞态和一次 root 测试目录设置导致的失败均保留；前者只修测试 worker 的完成握手，资源验证/限额/缺测判定未放宽。实际 typed 执行器与严格 raw replay 仍在报告目录实现，尚无真实竞品性能或增量结果。
- 因上述真实源文本丢失需要全新候选索引，root 在旧候选 public 未输出任何质量结果时取消其自有进程组。实际 receipt 为 **exit -15、timed_out=false、MISSING**，不是 PASS；`P0-06/public-current-001/cancellation-decision.json` 在发信号前登记，SHA `77a9138ad6c5cf8b82061f5d2e69423a5a40d849700811b3bc21d02f85c322a8`。此前 reference/candidate 准备和 reference 全部 2,029 query ×4 scorer 原始结果均保留。后续仅在确切修复源码冻结后，以新候选目录/索引执行，不改变参数或挑分重试。
- 新一轮多 profile CI 已完整执行，`P0-06/postfix-ci-001/` 保留 base **2137 PASS、10 FAIL、11 ERROR**，native **228 PASS、1 FAIL**，MCP **6 PASS**，源码前后稳定（当时还含现已按用户授权退役的 Foresight 8 项）。失败分别来自合成 converter 文件名触发原生工具禁用、上述 semantic-only 支持片段变化，以及 host Python 的 `-I` 子进程读不到 user-site NumPy。前两项做窄修，native 使用已锁定且隔离模式可导入 NumPy 的现有 venv；不移除 hermetic guard、`-I` 或任何断言。修复后的 `P0-06/postfix-ci-002/` 各组均 **PASS**：base **2163**、native **229**、MCP **6**、Foresight **8**（历史组成，该 profile 现已退役），合计 **2406 项、0 skip**，422 个代码/配置文件前后稳定。该集成检查点可用于下一次固定 dev 实测；旧失败保留，后续源码更改仍需新验收。
- 竞品完整 aggregate 另有已独立证明的 producer/reader 大小不匹配：18×210 个有效合成请求、每个 Goldmine excerpt 仅 19 ASCII bytes 时，完整 JSON 已为 **8,388,927 bytes**，超过现有 8MiB reader。精确 9 文件 framing 修复随后独立闭环并合入 `integration-after-capacity-016/`，通用 8MiB reader 不变，以有界 root/cell/typed streams 保留全部正文、向量、raw、准备证明及最终磁盘计数，Python verifier 仍返回完整逻辑 dict。root/架构各 **124 项及两项 JS 检查通过**，独立完整 **18×210 /3,780 rows** synthetic 往返保留每 cell 的 raw100/native10 窗口与 122 个 source/body/vector 记录；root 的 bounded root 为 **10,882 bytes**，并非真实引擎基线。实际 checkout 与 executor002 联合 **318 项通过、0 skip**，459 个代码/配置文件稳定，生产 87 文件不变。初始设计的精确原字节及 8 行追加说明在独立 amendment 包中保留；上限、注册表和质量合同未改。真实转换、canary、引擎质量/性能及最终资源验收尚未执行。
- `d1e313f` 已保存上述生产修复及准备/观察/验收组件，本地工作树在该检查点干净。随后 `current004` 使用全新 122-source native index、同一默认 control 完成 **210 个 dev 请求**，严格独立回放 **FAIL 11、MISSING 0**：PACK 全事实 **96→102/180**、compound PACK **6→11/30**，严格 success@1 **88→86/180**、success@5 **103→101/180**，noise 正确空仍 **5/30**。全部原始结果保留，未增加第四参数臂或挑分重试，holdout 未打开；这些是分块与 B003 的合并效果，不能归因给某一单独修复。完整聚合与组级 paired 区间见 [current004 摘要](product-dev-post-conservation-20261022.json)，区间均跨 0，不能宣称广泛显著收益。C002 设计独立闭环，但实现仍在报告目录，不读取该结果来调参。
- typed 竞品执行器 001 的作者及 root 独立机械回放均 **170 项通过、0 skip**，但 root 继续证明完整 cache witness 仍留在根部 slots stream：固定 1,728 slots 的有效合成 cache 形状生成 **19,492,615 bytes**，超过 16MiB administrative allowance。原失败包与反例保留。002 随后把完整 observation 移入固定 cell-owned shards，根部仅存有序 path/SHA/size 引用；没有裁减 cache、clock、raw 或提高限额。root 与独立架构复跑均 **194 项通过、0 skip**，另有完整 1,728 records/126 warm cache slots 的持久化回放，旧 inline **19,655,443 bytes** 对照新 root index **664,483 bytes**，所有 shard 纳入最终 18-cell inventory/census。精确 26 文件及测试注册已合入 `integration-after-capacity-014/`，实际 checkout 再次 **194 项通过、0 skip**，447 个代码/配置文件稳定。此为执行机制验收；base aggregate framing、真实转换、canary、质量及完整性能实测仍待完成。
- C1 首版实现的 49 个预登记案例及相关回归经 root/架构分别 **274 项通过、0 skip**，但独立实证又发现 NFKC 兼容正负号在 metadata component recall 中丢失的反例；首版不合入且原失败保留。002 改为核验 NFKC 后的完整真实 component，root/架构分别 **310 项通过、0 skip**，原反例全部拒绝、真实 signed-name 正例保留。精确 12 文件及 P1-06 测试注册已合入 `integration-after-capacity-015/`，实际 checkout 再次 **310 项通过、0 skip**；代码/配置为 452 文件，生产身份为 87 文件。行为登记为 `anchor.v1-lexical` / `search.v4-anchor-contract`，semantic remainder 仍 OFF，原三参数失败注册、目标、数据和 holdout 均不改；这仍不是质量达标。下一次 C dev launcher 已补齐历史 004 的实际 index/source/model/span 绑定，root 只读重放 **8,733 个 span** 通过；后续 public/immutable-installation launcher 的同源码完成链也已独立闭环：严格回读真实 capture 命令、准备 argv、run_id 与 index 身份，34 项捕获 fixture 及 7 项既有 raw 反例/正例均符合预期；其中 quality FAIL 可作为已完成 dev 的前置证明，MISSING 或过期来源不可。尚未启动这些真实运行。

- 旧库兼容与完整字段预查的15文件修复已按精确 patch 合入 `integration-after-capacity-017/`。schema4 保留原 NFC 校验、schema5/6 保留 NFKC+casefold；已知 body-only4 只读取真实 body，不补写 original。metadata 完整资格先于候选上限，仍保留32 anchors/2000 distinct source rows 与现代缺表/坏 pointer 错误。作者封存、独立架构、root 精确重建及实际正常 conftest 均 **419 项通过、0 skip**；最终实际回归20.58s，465代码/配置与90生产身份文件稳定。原版生产器/实际4→5迁移的原文/CIT回读与36次固定小型CLI/16项严格比较通过，它们不是完整规模或质量结果。原代码9个机械反例失败、001的LR56二次metadata重入失败，以及root一次父Git上下文导致的隔离patch no-op均保留；后者在任何实际源码变化前由15文件SHA映射断言拒绝，随后用独立Git sandbox重建成功。原始固定alias/stem反例在实际采用代码上回放6种actual/control组合，真实结果1/20/1且IDs、正文、分数与隔离control完全一致，未修改数据库。全部评分/解析器/分块/pack分配/迁移代码及数据、门槛、原三数值注册保持原SHA。下一步完整四profile CI；未开启新模型/质量/holdout运行。

- 最终 C002/兼容源码（`bd46490`）的多 profile CI 与本地 CI 已完成，原始结果为 `P0-06/post-c1-ci-001/results.json` 与 `P2-05/ci-local-current-001/results.json`，全部 PASS、0 skip，465 个代码/配置文件稳定。随后 current005 以全新 122-source native index 完成固定 **210 dev 请求**：严格 success@1 **86→83/180**、success@5 **101→96/180**、全事实 PACK **102→97/180**，noise 正确空 **5→20/30**。PDF success@5 为 **14→10/30**，额外触发 PDF success@1 门槛，严格回放 **FAIL 12、MISSING 0**。与预登记 004 对照的 source-group paired 区间在可答指标上均跨 0，noise 区间为 **+0.3333 至 +0.6667**；这是重复暴露 dev 内的取舍，不能宣称普遍质量提升或发布达标。完整聚合、原始 SHA 与失败门槛见 [current005 摘要](product-dev-post-anchor-20261022.json)。未增加参数候选、挑分重试或打开 holdout，接续游标已推进到新安装与公开集。


## 2026-10-25：product 验收门逐题归因与 R3 修复（chunk.v7-original-text）

- 逐题归因（`P0-05/product-failure-diagnosis-001/`，只读，评估器自身读回函数重判 + 在线捕获钩子）：product-current-007 的 70 道 all@5 失败中 **61 道目标文件已在 top-5（绝大多数第 1 位）**，非召回/排序问题。根因×门交叉表：R1 `select_evidence` 语义优先选错主指针段 **31**（cjk 12/12 全部）；R5 OCR 文本偏差（扫描专利断字/双栏交叉/错字 10、截图视觉换行 4）**14**；R3 chunker 压缩行内空白+空行致 doc 段行号漂移（178/420 段漂 1..27 行）**7**；R6 anchor_unresolved 硬空（EXIF 照片 4、`ssl.TLSVersion.*` 2）**6**；R2 exact 绑定取最早段（定义段丢失）**6**；其余 R4/R7/R8/R9 共 6。noise 13 泄漏中 26/31 个泄漏命中靠 `semantic_standout`（z≥3、cov≈0）。主指针"词法优先"规则在 180 题上模拟（控制复现 0 处不一致）：赢 30 输 26，**不可直接采用**，R1 需新信号走设计流程。
- **R3 修复已采纳**（预注册 `P0-05/chunker-original-text-001/preregistration.json` 冻结 sha 后实现）：`_clean_mapped` 建立 clean→原文偏移映射，prose chunk 携带逐字 `original` 切片、loc/loc_end 为**原文**行号；存储用原文、嵌入仍用清洗文本；CHUNKER_VERSION → `chunk.v7-original-text`；OCR 媒体段（存储文本即嵌入输入）保持不变并在执行记录注明。新增 `tests/test_chunker_original_text.py` 141 项（含与改动前 chunker 的 text/tokens 字节一致性对照）；验收清单迁移 b1f4fb9 遗留的 16 个已退役 distill nodeid 并登记 18 个替代，CI base profile **PASS**。
- product-current-008（候选全新 prepare，007 reference 臂复用——评估器文件集未变；固定 006/007 对照）：**6 题翻赢、0 题翻输**，semantic@5 0.533→**0.700**、semantic@1 0.467→**0.633**（差 1 题过门）、all@5 0.611→**0.644**；其余门与 noise 17/30 逐位不变。机械等价：1049/1049 (path,kind,vec) 多重集一致、嵌入文本多重集一致；doc 段"原文逐字落在 loc 行窗内" 614/980→**956/980**（残余 24 = 同样四份扫描专利 OCR 页，属 R5）。配对 vs 007 候选：success@1 +0.028、success@5 +0.033、MRR +0.031、facts +0.039，CI 均不含 0；noise delta 恰为 0。text-semantic-039/056 转入 R1（覆盖段已存在但主指针仍在别处）。
- **R2 修复已采纳**（预注册 `P0-05/exact-definition-site-001/preregistration.json`）：`Resolver.bind` 去重排序键改为 (level, 非定义位置, segment_id, raw)，去重 KEY 不变，owner 集合逐字节不变；`definition_site()` 只对 code 行判 `def/class/function/fn/struct/...` 声明、赋值 `NAME =`（排除 `==`）与 `#define`。不 bump ANCHOR/SEARCH 版本（已选 owner 文件内部的指针平局规则，非准入变更）。实现前在 69 道带 anchor 的 dev 题上模拟：**恰好 6 个绑定换段且全部由不覆盖变覆盖，零附带变化**。新增 `tests/test_exact_definition_site.py` 8 项，CI base PASS。
- product-current-009（固定 006/007/008 对照；共享轮次驱动 `P0-05/product-round-driver/driver.py`）：索引与 008 候选**逐字节一致**（向量多重集、嵌入文本、存储文本均相同，search 侧改动），**6 题翻赢（text-lookup-007/008/039/040/043/044，@1 与 @5）、0 输、noise 不变**，另 2 题 pack 全事实恢复；lookup@1 0.633→**0.833**（门 0.90，余下 5 题 = R6×2、R4×2、R8b×1）、all@5 0.644→**0.678**。配对 vs 008：success@1/@5 +0.033，CI [0.000, 0.074]——增益集中在 3 个 cpython source_group，组级 bootstrap 下界触 0，按规则记"方向为正、未达显著"，无任何题或门下降。门槛仍 11 个 FAIL；下一轮 R6（anchor 绑定路线：三段点分 `ssl.TLSVersion.TLSv1_x`、EXIF 文件名词绑定）。

## 2026-10-25：蒸馏卡片聚类质量修复（ens-coassoc.v3）

- 诊断（`P2-04/distill-purity-diagnosis-001/`，11 轮离线探测，冻结向量上基线逐位复现）：purity 0.191 的根因是四叠加——embedding hub（跨主题中位余弦 0.597、主题质心两两 0.79-0.95）、τ=P90 校准落在 within-topic 中位数（6.2% 跨主题对高于 τ，切割点图全连通）、leader first-wins 链式吸收（1026-chunk 巨卡含全部 110 文档）、D5 的 s̄ 触发器对 hub 失明（且实证 s̄ 与纯度错位，无标签分裂触发器不存在）。chunk 粒度与单一公共分量假说被排除；purity 天花板 0.8227（18 噪声+3 跨主题文档稀释）。
- 修复：专家子代理设计（`design-proposal.md`）→ 独立对抗评审 REJECT（canary mock 语料击穿 + micro 折叠公式对角错误等，`review-adversarial-001.md`）→ 修订 002 增加双通道重合并（共关联 mean/min 通道 + centered 质心/跨对 min 几何通道，修复可分语料碎片）→ 二轮评审 ADOPT-WITH-CHANGES（`review-adversarial-002.md`，全部必改项落实）→ 预注册冻结（`P2-04/distill-purity-fix-001/preregistration.json`）→ 实现 `goldmine/distill.py` ens-coassoc.v3（确定性 96-run 集成共关联，零 RNG；τ=F=0.45 定值，旧 Otsu/P90 校准退役为报告；稠密/micro 分档；增量改中心化双门并转发 center_mu；D5/s̄ 退役；cards-v2 schema/store 合同零改动）。
- 双语料试算（`trial_v3.py`+`trial_v3_sweep.json`）：真实冻结语料+canary 全变体联合过关面 36 配置，操作点 F0.45/re0.22/mn0.10/θc0.50/mc0.20。
- 验收：A1 真实冻结语料指标与试算逐位一致（**purity 0.7030、recovery 6/8、max_share 0.0915**，30 卡）；A2 canary 六变体全 PASS（main/hard/shuffle 8/8、incremental 9/9 走真实增量路径）；A3 test_distill 48/48（退役 9/重写 8/小改 4/新增 11，清单在设计修订），全套 2932 过/27 skip/1 失败——该失败在干净 HEAD worktree 同样复现（native `-I` 子进程 venv 环境问题，预存）；A4 micro 折叠公式暴力对照 err≤1e-9；A5 冒烟：50k 合成（40 主题）→ 40 张纯主题卡/max_share 0.025/97.6s，中等几何（6 可分+2 hub 主题）→ 7 卡/purity 0.875（micro_split_rate 监控值 0.81 记录为与质量脱钩：cell 撕裂不阻碍质心级几何重聚）；A6 完整真实评测（`P2-04/real-current-003/`，全新 native preparation+严格 capture+独立读回）**PASS 13/13 gates**——冻结 preparation 因绑定旧评测代码身份被正确拒绝的第一次尝试留档 `real-current-003-premature-001`。
- 实现期自抓并修复两缺陷：remerge 全对重扫 O(G³)（评审 N5 成真，中等语料卡死）→ 精确缓存对统计 O(G²)（冻结语料分组逐位不变验证）；micro 档 cell 索引误入 chunk 矩阵（50k 冒烟抓出 68% 巨卡）→ cell 质心矩阵。
- 未恢复 2 主题（cli 裂 3 纯卡恰差半篇、crypto 支持度 0.67<0.70）与一张 25 文档"Python 工具带"混合卡为已知残余；红线全守（标签/语料不动、无文件名分组、生产零标签读取、无质量重试、冻结向量评测）。

## 2026-10-23：接续会话——退役后基线、等价修复与新实测

### 2026-10-23 会话后半程实测结果

- 公共集：`public-current-006` 以冻结原始调用形态完成全新 reference 臂（prepare 959s + 2,029×4 评分约 23 分钟，严格回放 PASS）；candidate 评分在含 SC 修复的源码上再次于 7,200s 超时（exit 124、零 stdout、无重试、终端回放记录）。两次 harness 修正尝试（004 的 schema 键、005 的 argv 长度）被冻结 raw/v1 契约拒绝并保留为证据；SC-04 每查询 deadline 能力保留为显式可选，启用需用户明确修订契约。候选侧公共规模成本是下一工程项。
- 容量：`full-capacity-current-004` 在退役后新安装（`goldmine-private-assets-003`）上 12/12 一致 PASS、缓存收益 PASS；慢/快 p50-p95 对照保留原始样本；不做全面提速声明。
- 蒸馏卡片：`real-current-002` 完成真实 120 文档/8 主题运行（修复一个从未可运行的 cancelled 字段检查器，失败尝试保留）：诚实质量 FAIL——purity 0.191、主题恢复 1/8、最大卡片份额 0.958；全部引用/来源/增量门槛 PASS。
- 延迟：`latency-current-003` PASS（controlled/real/reference 三臂 30 冷热交错 + MCP 配对额外 p95 6.76ms）；两次失败尝试保留（内存压力 warmup 超时；被清扫的 reference 树已按原始字节恢复）。
- 生命周期：密封 packet 拒绝退役后源码（2/16 协议 pin 变化），按规则新 amendment packet-002 仅重 pin 这两个文件后执行 `lifecycle-real-002`：重复导入、controlled/real watch（各 600s churn）、controlled/real serve 全部 PASS，未变导入零 embedding/OCR/ASR 调用。
- 分发与文档：`P2-06/current-003`（wheel/sdist/重建/隔离安装/升级恢复/task-check）与 `P2-07/current-003`（固定文档+22 离线命令，绑定同一 bundle）均 PASS；task-check 的 PYTHONNOUSERSITE 作用域已收窄到 pip 阶段。
- 对抗审查附注：`P2-08/text-common-seed-001/corpus/` 的 4 个诊断 seed 文本（本会话前的清扫所为，01:01）已按退役后源码机械重同步但未随附披露，且同目录 Oct-21 的 recoll/qmd seed 索引未重建（诊断专用、无 sha pin；语料与索引暂不一致，随 P2-08 完整执行时重建）。SC 编号澄清：本会话实现 SC-01/SC-03 与 SC-04 能力（未启用）；SC-02 仅由 file-first 形态部分覆盖。
- 未决：公共 candidate、P2-08 完整 18 格、真实 answerer/judge 配置、旧 169 媒体 EXIF 决定、baseline re-pin 决定。


- 用户授权的 Foresight 退役已完成收尾并经对抗审查修正（`29b6272`、`33e2e08`）：活动代码/CI/文档/依赖无残留引用；frozen 历史证据（`evals/history`、baseline pins、`product_v4` seed、历史索引 JSON 与历史总数 2821/2406/1689/1435）逐字节保留并补注退役说明；420 题 frozen 套件 manifest `b8351272` 未动；legacy 活动套件因源目录已删除而移除 41 道 foresight 题（109/128 分母、78/31 split 重生成、corpus_version 升 v4）。已知未决：baseline pins 与两个活动输入的分歧使 offline profile `history` 检查 MISSING，等待用户明确 re-pin 决定。
- SC-01/02/03 请求内复用修复（`d77c195`）以失败先行的复现测试驱动：子句余项单元每子句一次（12 文件 13→1 次）、whole-metadata 派生集按值一次、FTS projection 每连接快照一次；file-scoped body_ids 改 segments 外层 CROSS JOIN，等价性由测试钉住，EXPLAIN QUERY PLAN 与 7,000 段 fixture 上 0.0774s→0.0118s 的同形对比记录在 `P1-06/request-local-reuse-001/`；小规模端到端时间中性，不做端到端提速声明。SC-04 公共评测 deadline 能力为显式可选，冻结 schema/argv 禁止静默启用。
- current006 全新 122 源索引 + 210 dev 一次完成：与 current005 全部成对指标 delta 精确为 0（等价性的最强实证），严格回放 FAIL 12、MISSING 0；P1-07 rerank A/B（同源同索引、真 Qwen3 rank-head）nDCG delta −0.000024 → EXPERIMENT_REJECTED；P1-08 multi-query A/B compound 全事实两臂同为 11/30、增益 0 → EXPERIMENT_REJECTED；两者保持默认关闭。
- C1 诊断 replay（proposal-002）：修复 normalization 校验（BOOK/ZK-12 retained/NULL 反例拒绝）与每字段复用（fixture 上 identifier_tokens 51→12 次）后，38 项 synthetic 负例通过，fixed41 一次性 replay 全部 REPLAYED_C1_REJECT（29 anchor_unresolved/12 remainder_unsupported），历史分支保持 UNKNOWN，原始 DB 未动。修正对抗审查发现的披露错误（29/12 而非 26/15；manifest 重生成披露）。
- 公共集 fresh reference 臂完整捕获（`public-current-006`：reference 17,490 文档 prepare+2,029×4 评分约 23 分钟）；candidate 评分在含 SC 修复的源码上于冻结 7,200s 再次超时（exit 124、零 stdout、无重试），MISSING 原样保留；两次 harness 修正尝试（004 schema 键、005 argv 长度）均被冻结 raw/v1 契约拒绝并保留为证据——engaging 每查询 deadline 或放宽预算需用户明确修订。候选侧公共规模成本（E×H 语句数与 C1 专属工作）仍是下一工程项。
- Foresight 退役后的新 immutable installation（`goldmine-private-assets-003`，86 文件，模型/运行时零执行复用）已刷新；容量 current004 以其为候选根执行中。

## 2026-10-22：新语料与验收补齐

- 新建独立的 [product_v4_expansion](../../evals/product_v4_expansion/README.md)，保留旧 28 题 seed 原字节。新增套件已完成 **420 题、122 份真实来源、175 份 oracle view、89 个可答来源组**；七类各 60 题，dev/holdout 各 210 题、各类各 30 题，组无交叉。来源包含原始代码/多语言文档、真实 PDF 页、历史扫描专利、浏览器截图、20 段发布方音频及 10 张原始 EXIF 照片。没有用生成图卡、TTS 或伪造 EXIF 填补真实媒体。
- 与题目作者独立的 AI agent 已按固定 seed 核查 **56 题，每类 8 题**，逐题记录实际原文/像素/发布方 transcript/元数据依据；60 道无答案题另检查全部 175 份 view 及来源范围。输入 manifest 已标为 FROZEN，SHA `b8351272194fed76e438d30d452a3fff9393f99a7e5b1264f710490cc65677b0`。这仅表示来源、标签与分割冻结，不表示质量达标或候选代码冻结。
- 两个不同目录的可搬运构建均只含 122 个注册来源文件，逐字节 SHA、20 个真实截图类型标记与 build receipt 一致。`P0-05/product-expansion-001/portability-002.json` 保存证据。原 `7694837` 与当前候选分别全新建库，模型、OCR、Whisper/VAD、poppler/ffmpeg 与 Python 3.12.0/NumPy 2.5.4 均实际核对。两臂均通过完整能力预检；原版 12 个 `.rst` 不支持结果按原 classifier 证明计 miss，不减分母。真实 dev 对照已完成；holdout 尚未打开。
- 来源审计发现并修复通用导入问题：7/10 照片原先把修改日期当拍摄日期；新增有界 DateTimeOriginal 读取与失败恢复；`.rst` 按真实文本导入；两个并发图像 worker 的临时目录初始化增加锁。10 张原始 JPEG 的独立 EXIF 对照全部一致；158 项相关生产/原生测试通过，重现的失败原始记录保留。独立复核确认普通重导入能从临时读取失败恢复，合法无 EXIF 的文件仍可 unchanged skip。
- 原始版本不支持 `.rst` 时，固定分母计为未命中；受支持格式缺 OCR/ASR 则仍为 MISSING。新增严格 original classifier/原始准备 receipt 与只读 schema-v4 回读适配，不能由调用者自报 unsupported、迁移旧库或缩减题目集合。ASR 与局部原文证据协议已在首次评分前登记，并加入数字、单位、符号、否定词、跨段拼接等拒绝反例；不会修改 oracle 来迁就实际提取结果。
- 公开评测已增加逐题 ranked doc IDs 和独立 qrels 重算；三个固定 ZIP 已下载到仓库外专用 cache，并实际核验全部 bytes/SHA；原始获取证据在 `P0-06/public-assets-001/`。仍需真实三数据集、四臂的独立准备与评分运行。文档、联合 holdout 和完整 CLI/pack 容量验收器已实现并经反例审查。当前源码的回归、离线安装和 22 条文档命令均已重新捕获；旧 CI/安装/性能结果保留为历史证据，新增源码不能自动继承其 PASS。

## 新产品集 dev 实测

同一物化 corpus、同一固定模型与 server，原始 `7694837` 对照 pack/SQL 修复后的 current003 检查点，**210 题全量：180 可答、30 noise；可答 46 个独立来源组**。下表保留随后分块/B 修复前的历史实测；最新完整 current004 结果及回退已记录在上方与 [新摘要](product-dev-post-conservation-20261022.json)。这里的 success 要求冻结的来源、位置及事实 span 证据，严格于只命中文件名；与 legacy150 的比例不能直接混列。current003 模型/源码/命令 SHA 见 [product-dev-post-pack-20261022.json](product-dev-post-pack-20261022.json)，此前结果保留在 [product-dev-20261022.json](product-dev-20261022.json)。

| 开发指标 | 原版 | current003 control，段落修复前 |
|---|---:|---:|
| 可答 success@1 | 54/180（30.0%） | 88/180（48.9%） |
| 可答 success@5 | 65/180（36.1%） | 103/180（57.2%） |
| pack 全部必需事实 | 64/180（35.6%） | 96/180（53.3%） |
| semantic success@5 | 3/30 | 12/30 |
| CJK success@5 | 17/30 | 18/30 |
| lookup success@1 | 5/30 | 18/30 |
| PDF 精确证据 success@5 | 0/30 | 12/30 |
| media-text success@5 | 13/30 | 20/30 |
| compound success@5 | 25/30 | 22/30 |
| compound pack 全部事实 | 17/30 | **6/30** |
| noise 正确空结果 | 5/30 | **5/30** |

默认 control 的可答 success@5 变化为 +21.1 个百分点，按 46 个来源组的 paired bootstrap 95% 区间约为 [+11.1,+30.5]，seed=20261021、10,000 次；它是该开发套件上的结果，不是总体能力或市场排名保证。nDCG 的组级区间仍跨 0。初次统计把 30 个 noise 的 rank 零分混入可答差值，已修复并独立复核；保留首次原始记录，以同样四个 arm 重新捕获，逐题结果与首次完全一致。

三组预登记参数均完成，全部 **FAIL 11 个质量检查**，没有选中参数，没有增加第四组：

| 参数 | 可答 success@5 | noise 正确空结果 | 结论 |
|---|---:|---:|---|
| control，z=3.0 | 103/180 | 5/30 | 保留默认，未达标 |
| higher_z，z=3.5 | 103/180 | 10/30 | 不足所需 27/30，拒绝选择 |
| within_file_margin=0.25 | 98/180 | 8/30 | 质量下降，拒绝选择 |

当前新参考在 `P0-05/product-current-003/reference/control/dev-command/`，三组候选在 `candidate/{control,higher_z,within_file_margin}/dev-command/`；`results.json` 保存全部门槛与组级对照。此前同一三臂在 `product-expansion-001/` 的原始记录不变，其 12 道 compound 全事实退步案例保存在 `compound-regressions.json`。本次恢复其中 1 道，其余 11 道仍未解决。接下来优先处理这些退步与无答案准入；不降低目标、不启用尚未通过的质量实验、不提前解封 holdout。

独立架构 agent 对此前 12 道已暴露 dev 题完成只读逐案诊断：24 个必需事实全部仍在真实 SQLite 的正确原文位置；原主要失败阶段为 **4 道 exact-anchor 准入、2 道证据窗口选择、6 道材料包装配**，共丢失 13 个原来已输出的事实。8 道两份必需文件仍在检索结果中；另有 8 道受整条材料预算省略影响，两集合有重叠。原始证明在 `P1-06/compound-diagnostic-001/diagnostic.json`，SHA `130a6d9eb2db5d00c24fda800f000ec288adfe075c508b2adefae757693ffb54`。已合入连续支持片段精确接缝去重和多段引用折叠保护，本次实际只恢复 `compound-real-uv-05` 的第二个事实；独立逐题差分在 `P1-06/product-current-delta-review-001/`，其余归因为 **4 准入、2 窗口、5 包装配**。不得直接删除 remaining-word 拒收，现有 multi-query grammar 对这 12 题均不扩展，单纯开启它没有已证收益。

当前固定入口 `P0-05/product-current-003/task-readback-001/task-check/manifest.json` 完成 **94 项任务测试 PASS**，并从原版/候选真实 SQLite 回读 **4,445/8,175 个证据 span**，结果为 **retrieval.product_dev FAIL、11 项质量门槛失败、MISSING 0**，source-stability PASS，178.97s。此前 `product-expansion-001/task-acceptance-004/` 的 4,445/8,169 个 span 与失败目录均保留；没有重新执行或筛选排名来获得这次回放结果。

## 7192395 冻结检查点的回归与交付

实际环境仍为 macOS arm64 / Python 3.12。该检查点四组无必需 skip，合计 **1689 passed**（base/native/MCP/Foresight，Foresight profile 现已按用户授权退役，历史组成照录），不认证之后已改变的源码。先前 `frozen-base-007` 的单项 hermetic fixture 失败保留；只隔离该假图像的 EXIF 调用后重新冻结，没有修改生产代码或放宽断言。

| 固定入口 / 范围 | 当前结果 | 原始证据目录 |
|---|---:|---|
| CI base | **1486 passed** | `reports/execution/P0-06/frozen-base-009/` |
| CI native | **189 passed** | `reports/execution/P0-06/frozen-native-009/` |
| CI MCP，locked SDK 环境 | **6 passed** | `reports/execution/P0-06/frozen-mcp-009/` |
| CI Foresight（历史组成，profile 现已退役） | **8 passed** | `reports/execution/P0-06/frozen-foresight-009/` |
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
| `--profile foresight`（历史组成，现已退役） | **8 passed** | `reports/execution/P0-06/frozen-foresight-006/` |
| 四组 CI + 固定失败注入 + 实际 actionlint | **PASS**，hosted=MISSING | `reports/execution/P2-05/ci-local-006/manifest.json` |
| wheel/sdist/重建 wheel、源码外安装、原生资源、升级恢复 | **PASS** | `reports/execution/P2-06/distribution-005.json` |

四组合计 **1435 passed**（含现已退役的 Foresight 8 项）。11 个防伪造/不完整 IR 证据反例已从临时审查报告提升为 `tests/test_ir_acceptance.py`，并加入 P0-06 的固定 task-check。

四组均有 collection、原始 stdout/stderr、JUnit、退出码、source-stability 和 SHA receipts；执行器会重放并验证当前代码内容。源码/验收脚本/测试/依赖锁/CI 工作流变化会拒绝复用；仅提交 Git 或更新任务进度不会使验收陷入自我依赖。

另实际运行了真实 llama 的 3 个 pack 测试：预算 512/1000/4000/32768 × context 0/1/3、CJK/表格/代码/长路径/引用、8190-byte 空结果查询，13.54s，`reports/execution/P1-10/live-pack-001.xml`。真实 bounded reranker 测试 1 passed，`reports/execution/P1-07/live-backend-001.xml`；六个 endpoint 输入/顺序探针在 `P1-07/runtime-probe-001/`。这些证明协议、预算和资源机制，不证明答案质量提升。

早期综合测试的两个失败（distill 评测错误读取规范化 FTS、watch 测试把第二轮删除数误当第一轮）均修复并包含在最终冻结回归中，原失败报告保留在 `P0-06/hermetic-integration-003/`。

## 测量与对照

历史六文件逐字节归档到 `evals/history/20261021/`。原始 legacy 150 题（145 可答 +5 noise）真实模型复跑得到 success@5 **0.7862068965517242**、success@1 **0.593103448275862**、MRR **0.6834619594964422**、nDCG@10 **0.6884711067454795**，原门禁 exit 0。它是历史基线再现，不能写成当前改进收益。

完整 12 组容量对照覆盖 10k/100k/250k/500k × 每文件 1/10/100 片段、768 维；独立原始/候选快照、独立进程、3 次 warmup、10 次交错测量，top IDs 与 z 一致。250k 的 SQLite 路径已触发 1.5GiB 内存条件，500k 同时触发冷载与内存条件，因此开展缓存实验。`P2-02/capacity-001/` 与 `cache-capacity-002/` 保存全部 12 组，不只保留获胜组。

缓存实验中 250k/500k 的**冷载向量阶段**分别约缩短 96%/98%，查询进程峰值 RSS 约减少 47–50%/46–50%；它不是完整 CLI/pack 延迟。实验绑定较早缓存源码快照；最后 Python int/str 序列兼容修复另有 99 项回归和 zero-copy 证据，不能据此自动继承正式最终性能 PASS。缓存仍须显式启用。后续完整 CLI/pack 在 `7192395` 检查点已实跑并独立回放，当前新源码仍需重新测量。

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

1. **P0-05 / 产品质量**：420 题真实来源、独立标签审计、分割冻结、同源原版/候选索引及真实 dev 参考均完成。三组预登记参数全部真实 FAIL，默认仅 103/180 success@5、5/30 noise 正确空结果；本次 compound pack 从此前候选 5/30 恢复到 6/30，仍低于原版 17/30。质量未达标，不能进入最终 sealed 审计。旧 28 题保留为诊断 seed，holdout 曝光为 0。
2. **P0-02/03 / P1-10 答案质量**：新 60 可答 +20 trap 的真实独立作答/四评分、校准与候选比较缺失。历史 20 题、fixture/mock 或改名输出不能顶替。
3. **P0-04 / P1-05 full-media**：只读预检的 19 项能力缺口已经按"修源数据、不修产品阈值"两次真实归零——v5（用户决策 D4，2026-10-23）把两张随机像素 PNG 重做为真 EXIF JPEG 关闭 13 项；v6（ocr-restore-001，2026-10-25）把 atlas-hybrid/atlas-scanned 扫描页重做为 8 行收据/海图级真实文字量，Vision OCR 每页实读 233-254 字符（≥ THIN_PAGE_CHARS=200），pdf_evidence 真实记录 `ocr_pages`，最后 6 项（media-045..050）关闭，full-media 预检 missing=0（rc=0），且六题全部 success@1=1.0。分母、标签、holdout 与冻结产物未动；分歧记录（D1 册）两次扩展入册。不能注入答案 metadata、修改旧标签或减分母来伪造通过；真实 420 题是独立新套件。原始回执在本地 `P0-04/ocr-restore-001/`（预注册、双端 OCR 探针、逐字节 fixture diff、重建前后索引身份、v6 预检 JSON）。
4. **P1-06/07/08**：拒收三臂真实 dev A/B 已完成且全部 FAIL，`selected=null`；可选多查询/rerank 的真实收益与完整资源门槛尚未通过，保持默认控制与关闭实验。
5. **P2-02/03**：`7192395` 的完整 CLI/pack 规模测量已经通过独立回放；后续 pack/SQL 新源码需要新一轮测量。真实媒体增量 benchmark、controlled/real 各 10 分钟 watch churn 和完整生命周期证据仍缺。已有真实模型的局部检查不能顶替这些。
6. **P2-04 卡片**：120 份真实文档、8 主题的独立来源/标签审查及数据冻结已经完成，严格 native 准备/增量身份/全部 emitted member 来源校验已实现。真实聚类、purity/recovery 和增量运行仍缺；mock hard-variant 结果只验证机制。
7. **剩余验收代码与输入**：public、联合 holdout、文档、完整容量和 hosted 适配器均已实现并经独立反例审查。1689 项回归（含现已退役的 Foresight 8 项）和安装/文档证据绑定历史检查点，当前整体重新捕获尚未完成。仍缺真实外部集等运行结果和实际 hosted 证据。未运行真实 holdout，零收集仍必须 MISSING。
8. **P2-08 完整竞品对照**：native/common view、完整真实 PDF/媒体、语义/quality 模式、重复性能/增量/资源对照和完整最终数据仍缺。主比较按同一文件级 qrels 计算 success@1/@5、MRR、nDCG；位置指标在可验证时另报，位置能力缺失不能成为文件级比较的额外前置条件。当前表只表示词法诊断。
9. **发布**：仓库没有配置 remote，hosted CI 未运行。`local_rc` 的必需数据、质量和生命周期条件也不齐，因此没有发布候选、tag、push 或公开发行。

接续执行严格沿 [P0.md](P0.md)、[P1.md](P1.md)、[P2.md](P2.md) 和 [CONTRACTS.md](CONTRACTS.md)。420 题、独立审计和原始参考已完成；下一步依据已暴露的 dev 反例修复证据选择与准入，完成真实公开集、容量和生命周期等必需运行。三个拒收参数臂已全部测量，不能追加第四组或放宽门槛；新算法需明确登记和审查。全部算法/参数冻结且前置质量达标后，才进行一次联合 sealed 审计。

原始大报告仅在本地忽略目录 `reports/execution/`；此文是紧凑索引，不能据摘要替代原始 receipts，也不能把较早源码的性能或安装结果沿用为未来版本验收。
