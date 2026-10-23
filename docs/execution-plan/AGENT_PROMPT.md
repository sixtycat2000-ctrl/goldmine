# Goldmine 执行 Agent 入口提示词

下面的文本可直接交给执行agent。用户已要求执行全部计划时，该授权涵盖全部本地实现与验证；无需逐ID再次确认。只在用户仅要求规划时不实施。

```text
你在 goldmine 仓库执行已经明确指定的工程任务。

先从当前会话读取执行授权。用户已要求全部计划时直接执行，不重复索取授权；只规划时不实施。
执行时按顺序阅读：
1. 当前适用的 AGENTS.md。
2. docs/execution-plan/README.md。
3. docs/execution-plan/CONTRACTS.md。
4. docs/execution-plan/tasks.json。
5. 当前 ID 对应的 P0.md / P1.md / P2.md 完整任务段。
6. docs/execution-plan/ACCEPTANCE.md。

若只指派一个 ID，只做该 ID 和已明确要求的前置实现；缺前置先报告，
不要擅自把一个小任务扩成全项目重写。若用户要求执行全部计划，按
tasks.json 的依赖顺序逐项推进。依赖的实现已可用、仅真实测量缺条件时，
可以继续不依赖该测量的后继实现并保持IMPLEMENTED；不把缺外部条件
误当作停止全部工作，也不把尚未通过的里程碑标绿。

每项任务开始：
- 检查 git status 和当前 commit，保留用户未提交修改。
- 核对起点变化；计划基于7694837，后续有相关改动时先阅读并复用，不能回退覆盖。
- 记录任务的 allowed scope、必须保持的行为、required checks 和预计副作用。
- 核对引用的新文件/命令是否已由前置任务实现，不假设它们现在存在。
- 并行执行已获授权时，先登记每名agent独占文件和共享接口；同一文件的修改串行合并。

实现时：
- 先理解现有代码，再做最小完整变更；不要复制 Searcher/pack 逻辑。
- 查询只读，原文和出处不能造假，模型指纹不能混用，目录范围不能扩大。
- 默认本地，不增加聊天、云模型、账号、图谱、GUI、队列或外部向量数据库。
- 不自动修改baseline/标签/门槛，不删测试，不用mock或skip替代真实验收。
- 改schema遵守v5 CJK、v6 lookup及单事务备份约定；改切块正确标stale。
- 模型、SDK、竞品版本需要下载时先按任务的固定选择规则锁定来源与SHA；
  不能凭兼容性猜测引入额外框架或升级底层。
- 产品search/MCP不得执行下载、导入、迁移、任意路径读取或外部动作。
- 不创建额外agent、任务、自动化或外部消息，除非用户或适用指令明确授权。

验证时：
- 先运行任务局部有意义的检查，再跑阶段所需全量检查。
- 使用task-check --task ID验证本项dev与既有回归；该模式不读holdout，
  不要求后续任务指标；M1/M2检验dev。最终冻结全部算法/参数后才运行一次联合sealed holdout审计，其他profile消费
  其原始结果与SHA，不能重复跑后挑最好结果。失败后该集合已暴露，再次声明发布质量需独立未见来源。
- offline必须在无网络/模型/cache/native/optional SDK的干净环境通过；真实资产、
  原生工具与SDK各有固定profile（optional-mcp）/collection，缺条件只影响对应profile。
  nodeid分类迁移必须有映射，模块importorskip不能冒充已执行。
- 原始stdout/stderr、真实exit code、输入与输出SHA必须保存，不能只写“通过”。
- 从固定registry展开检查；receipt绑定run/check/argv/cwd/退出码与原始产物。
  validate重算结果，拒绝错suite、缺项、重复项、非有限指标、身份不符和路径逃逸。
  hashes证明一致性，不能声称对整套本地文件的任意改写具有防伪认证。
- 严格验收exit0只代表所有必需检查真的完成；任何missing/skip不可冒充PASS。
- 调参只用dev，最多3组预登记候选；holdout只作确认，不用来挑参数。
- 重排/多查询无收益时按实验拒绝流程保存证据，不默认开启，不转用生成式方案。
- 缺真实模型、语料、评分或CI运行时标明缺失；实现完成不等于验收完成。
- latency warm/cold各至少30次同query交错运行，实际验收p50/p95；MCP开销用
  同请求配对差值。规模fixture用分块float32生成，每个臂全新进程分别测生成、
  查询与子模型RSS；不能混用高水位或平台单位。
- 安装与native tar均在源码外验证；固定revision/patch、新staging、动态库依赖闭包、
  清除DYLD覆盖并实跑。版本探测失败不能吞掉。

交付时：
- 按ACCEPTANCE模板生成result.md和manifest，更新tasks.json的当前任务状态。
- local_rc验证本地原始检查，成功后才更新P2-07/M3；不能要求它们先VERIFIED。
  hosted_release另需真实run/artifact，缺remote或runner时保持MISSING，不能
  阻塞本地安装/候选验证，也不能为了补证据擅建remote或push。
- 逐条列出通过/失败/缺失，指标必须含分母、参考、候选、目标和判定。
- 清理本次拥有的进程与临时资产，不能按进程名杀其他服务。
- 只做授权范围内的本地改动；不擅自推送、打tag、上传包或发布。
- 最终说明完成了哪个ID，实际验证了什么，还有什么阻塞。

开始任务：若指定ID就执行该ID；若用户明确要求全部计划，从第一个未完成且
具备实现前置条件的ID开始，持续推进，不等待再次指定ID。
```
