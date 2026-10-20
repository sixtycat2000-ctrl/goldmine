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
