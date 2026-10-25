# AGENTS.md — goldmine 会话工作规约

给在这个仓库干活的 AI 会话（也给未来的维护者）。**简洁是刻意的**：
规则在这里，深入内容全部走链接。冲突时：用户当面指令 > 本文件 > 链接文档。

## 铁律（六条）

1. **证据三态**：PASS 必须有原始回执（argv/退出码/输出 sha/前后源码身份）；
   真实失败记 FAIL；证据不全记 MISSING。禁止把 FAIL 说成"基本通过"。
2. **预注册先行**：结构性改动先冻结预注册（逐文件 sha256 + 通过判据 +
   范围决定，模板照 reports/execution/digup-plan-002/）、过一轮对抗评审
   （ADOPT/修订落档），再动手。
3. **冻结物只走审计门**：基线/评测库/指纹身份只允许审计门下最小重生成
   并逐条记录，严禁夹带；holdout 保持密封（reports 之外的 holdout 路径
   不碰）。评测修改仅限指纹驱动的重建与 gate 复跑，不加新题。
4. **不做质量重试、不加参数臂**；z_gate/权重的再校准必须 A/B 且
   train/holdout 一致。
5. **产品安全**：search/MCP 不执行外部动作、不下载、不写库（estimate
   类只读命令在结构上就不带写入口）；dataless 文件在 open 前拦截。
6. **一会话一分支**：每个会话在独立分支干活，合并经用户——杜绝
   2026-10-25 那种双会话同树撞车。提交只包含本会话白名单文件。

## 每个提交一份"人读 diff 摘要"

合并/推送前，在 [docs/COMMIT_DIGESTS.md](docs/COMMIT_DIGESTS.md) 追加：
**关键行 / 每行的风险 / 我不确定的地方 / 用户 90 秒怎么验**（四段固定，
讲行和风险，不讲故事）。这是所有者的第一阅读面。

## 用户可亲手验证的锚点

[docs/ANCHORS.md](docs/ANCHORS.md) —— 涉及嵌入路径/评测/指纹的改动，
提交前自己先跑对应锚点；用户随时可能自己跑。

## 理解密度制度（所有者学习用，会话负责供货）

- 轮讲：每周一个子系统，走一遍后**用户复述**；缺口进下一讲。
  已完成：[第一讲 · 搜索打分](docs/WALKTHROUGH-search.md)、
  [第二讲 · 指纹与身份模型](docs/WALKTHROUGH-fingerprint.md)（各含十问）。
  排队：摄取管线（walk→extract→commit）/ 诚实契约 / 双常驻进程。
- 判断权归用户：预注册与源码冲突、gate 阈值、基线重建、产品语义变化
  ——停下来给冲突原文 + 建议 + 反方，**不自己决定后报备**。

## 深入文档地图（需要什么点什么）

| 主题 | 文档 |
|---|---|
| 模块与行数地图 | [docs/MODULE_MAP.md](docs/MODULE_MAP.md) |
| 当前能力/证据/缺口 | [docs/FEATURE_STATUS.md](docs/FEATURE_STATUS.md) |
| 设计决策与理由 | [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md) |
| DigUp 对照与事故教训 | [docs/digup-derived-plan.md](docs/digup-derived-plan.md) |
| 测试分层计划 | [docs/TEST_PLAN.md](docs/TEST_PLAN.md) |
| 锚点命令卡 | [docs/ANCHORS.md](docs/ANCHORS.md) |
| 提交摘要档案 | [docs/COMMIT_DIGESTS.md](docs/COMMIT_DIGESTS.md) |
| 轮讲系列 | [搜索打分](docs/WALKTHROUGH-search.md) · [指纹与身份](docs/WALKTHROUGH-fingerprint.md) |

执行记录：reports/execution/<任务名>/（本地留档、按惯例不入库——回执
在那里面，摘要在这里）。
