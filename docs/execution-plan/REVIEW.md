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
