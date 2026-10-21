# 执行计划对抗审查与定稿

版本 2026-10-21.2。用户于 2026-10-21 授权专业角色审查和完整实施。三位独立 agent 分别按 IR/统计、数据库/MCP 架构、QA/交付职责读源码并提出反例；这是角色分工，不冒充实际受聘的人类专家。

原 v1 文档字节身份见 [plan-v1-manifest.json](review/plan-v1-manifest.json)。所有 18 项意见接受并修订直接合同；没有通过删除质量门槛来消除问题。

| 角色 | 发现数 | 证据与处理 |
|---|---:|---|
| IR/统计 | 4 | [statistics.md](review/statistics.md) |
| 数据库/MCP 架构 | 6 | [architecture.md](review/architecture.md) |
| QA/交付 | 8 | [delivery.md](review/delivery.md) |

采用的工具包括源码/测试检查、临时 SQLite 并发与特殊路径反例、概率反例、pytest collection、otool 动态依赖检查。原实现离线基线另实跑：995 passed、25 deselected、207.22s；新功能不能沿用该结果宣称通过。

定稿重点：预登记实际退步幅度、稳定性不伪装误报保证；dev 开发与一次 sealed holdout；按来源组统计与严格位置/事实 schema；请求级读快照及 canonical citation；旧 writer fencing；完整包预算和贯穿 deadline；hermetic profiles、原始 receipt 与本地候选/hosted 分层。

实现按 tasks.json 实现依赖推进，最终验收按固定检查注册表和原始证据，不读取同次待生成的 VERIFIED 状态。缺模型、真实数据或外部 CI 的项保持 MISSING/IMPLEMENTED，不用 mock 填入正式指标。

## 实现后的第二轮对抗审查

同一架构审查角色再次检查实际生产实现，发现以下六项；全部接受并修复。它们是针对实现的反例审查，不代表整个产品已经通过最终质量或发布验收。

| 编号 / 严重度 | 可复现问题 | 修复与回归证据 |
|---|---|---|
| R2-01 / 高 | HTTP 的逐次读取超时允许缓慢滴流的响应头或正文一直延长请求 | 所有模型请求共用绝对 deadline 的本地 HTTP transport；覆盖慢响应头、正文、chunked body、取消和旧后端兼容；`tests/test_http_transport.py` |
| R2-02 / 中 | tokenizer 首次成功、稍后不可用时，材料包混合精确计数与估算，却标为精确 | 任意后续失效使整包重新按估算构建；初始不可用则本次请求始终估算；`test_tokenizer_availability_never_mixes_exact_and_estimate` |
| R2-03 / 中 | PDF 同页有多个块时，按块序号扩邻居会挤掉相邻页 | 按真实页号选择邻页并合并同页块，预算不足仍整条省略；`test_pdf_neighbor_pages_are_not_displaced_by_same_page_chunks` |
| R2-04 / 中 | MCP 搜索丢弃已取得的 context，邻居原文和引用未抵达调用方 | 返回真实存储 parts，同一读快照内生成邻居 citation；真实官方 SDK 回读，`test_mcp_context_contains_stored_neighbors_and_round_trip_citations` |
| R2-05 / 高 | CLI 收到 SIGTERM 后可能留下自有 reranker 子进程 | CLI 和 MCP 的终止路径正常展开资源所有者并回收子进程；真实父子进程回归，`tests/test_cli_termination.py` |
| R2-06 / 中 | 重排沿用旧排名的 duplicate 标记，可能折叠唯一或首条证据 | 最终选择清除旧排名标记；pack 仅合并实际输出完全相同的材料，并保留每个来源路径与 citation；近似向量但不同事实不得折叠 |

相邻的整包复核还补上了 24 条材料上限的计数：所有超限命中均进入 `skipped_by_record_limit`，不能静默消失。50 条输入的反例明确得到 24 条材料和 26 条上限省略。

本轮使用真实本地 HTTP 滴流端点、实际 SIGTERM 父子进程、SQLite 并发写入、官方 MCP SDK stdio、真实 llama tokenizer 和固定模型的重排端点。原始输出在 `reports/execution/`，最终同源测试范围见 [PROGRESS.md](PROGRESS.md)。

## 2026-10-22：真实来源与验收器复审

本轮在新增语料首次排名前进行。420 题来源、标签和分组已冻结；56 题由未参与出题的交付 agent 按固定样本独立核查。以下发现均保留原始失败或严格拒绝证据，未降低质量门槛，也未查看 holdout 得分。

| 发现 | 处理与验证 |
|---|---|
| 7/10 真实照片的原修改时间被当作拍摄时间 | 有界 JPEG/TIFF DateTimeOriginal 解析与 macOS ImageIO 读取；原始 JPEG 字节不变，10/10 与独立 EXIF 一致。`tests/test_exif.py` |
| ImageIO 临时失败被缓存为永久无日期 | 显式区分 error/absent/unsupported/unavailable；普通重导入可重试恢复，只保留同 SHA 且有当前来源证明的日期。独立 timeout→恢复→unchanged 反例通过。 |
| 两个图像 worker 可互相覆盖并销毁共享临时目录 | 每 pipeline 的初始化锁；确定性双线程反例与真实 native OCR batch 回归。原失败和修复输出在 `P1-05/exif-scratch-race-001/`。 |
| 同名文件的 metadata view SHA 将无关原文合为同组，贪心 split 又可把某媒体能力集中到单折 | 只用真实内容/来源关系并组；固定 seed、硬覆盖约束与整组交换，不看排名。两折各 210，每类 30，OCR/ASR/metadata 每折均满足最低数量。 |
| 原版缺 `.rst` 支持，当前验收器却试图用 schema6 Store 回读原版 schema4 | 隔离实际原版 classifier、准备 receipt 与只读 common-table 回读；unsupported 固定分母计 miss，伪造豁免被拒绝，受支持格式缺能力仍 MISSING。 |
| ASR 逐字比对过严；简单 casefold/去标点又可错误接受数字或标识符变化 | 首次排名前登记严格连续 witness；保护 `.5`、符号、附着/分隔单位及 `client._transport`。同一输出 record、实际存储原串和精确 canonical span 必须同时成立；跨片段补事实被拒绝。`tests/test_product_alignment.py` |
| PNG 纯字节搬运缺少原版检测截图所需的原生类型标记 | 仅由已冻结且已审计的 screenshot kind 统一物化真实 capture 标记，两臂同目录、全新建库；不改名称、日期、正文或源字节。build receipt 与索引绑定；明确不测无标记 PNG 的自动检测。 |
| 构建校验只枚举普通文件，可放过 FIFO、悬空/目录链接等额外节点 | `lstat` 全部节点，只接受注册文件与必要父目录；四类反例独立复现后均拒绝，FIFO 从不被读取。`tests/test_product_materialization.py` |
| public summary 缺逐题 doc IDs，单独自报 DB SHA 不能证明独立准备 | 导出原始/截断后排名，按固定 ZIP 中 qrels 重算；准备与评分分别捕获并绑定实际最终索引、模型、服务器、源码和时间。旧 summary 仍不能作为正式证据。 |

测试通过只说明所列机制与反例被覆盖；真实 dev 质量、容量、答题、生命周期和最终联合审计仍分别验收。完整同源测试应在本轮所有验收代码稳定后重新运行，上个检查点的 PASS 不能自动继承。

## dev 实测后的验收器复核

以下问题在首次 dev 结果之后发现。原始结果保留，修复没有修改来源、题目、分割、单题分数或既定质量目标。

| 发现 | 修复与独立复核 |
|---|---|
| paired bootstrap 把 noise 的 rank 零分混入可答指标，扩大分母和来源组数 | 仅 rank/事实指标使用 180 个可答题、46 个来源组；完整 210 个 ID、分类、分组一致性仍先验证。30 个 noise 单独统计。独立反例通过；同样四臂重新捕获，逐题结果不变。`tests/test_product_statistics.py` |
| 联合审计可漏掉既定 rerank nDCG +0.02 门槛；尾部非法 usage 行能影响分母；未实际扩展的 multi arm 可冒充处理已执行 | 三项均补严并由另一 agent 独立验证：0.019 拒绝、0.020 接受；usage 长度和每行必须严格对应；multi 必须实际扩展且计数自洽。未打开真实 holdout。`tests/test_joint_holdout.py`、`test_joint_holdout_freeze.py` |
| 容量 warm worker 启动失败泄漏子进程；select 后 readline 可被半行绕过 deadline | 启动、请求失败均回收自有 child 并写失败 receipt；有界完整行使用同一绝对 deadline。独立半行反例在 0.059s 结束 0.05s 限时。`tests/test_scale_protocol.py` |
| human pack 篡改全部来源路径仍能通过；未测缓存收益也可自报成功 | 验证真实来源、位置、顺序、正文和索引/行绑定引用；同宽路径替换被拒绝。触发资源条件但未测缓存时为 MISSING、benefit=null。独立闭环记录 `P2-02/delivery-review-002/results.json`；容量相关 60 项集成测试通过。 |
| hermetic OCR/ASR 计数测试用无效 PNG 字节调用了真实 ImageIO，错误状态触发正确的重试逻辑 | 该测试显式隔离元数据读取，保留原 unchanged/OCR/ASR/embed 的全部断言；真实 EXIF 失败恢复测试不变。首次完整回归的失败原文在 `P0-06/frozen-base-007/`，修复后四组 `*-008` 合计 1689 项通过。 |

真实产品 task-check 已完成 210 题、12,614 个实际 SQLite 证据 span 的回读，结果为 **FAIL 11 个质量门槛，MISSING 0**。严格证据完整不代表产品质量通过。
