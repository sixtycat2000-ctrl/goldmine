# 提交摘要档案 — 每个提交一份"人读 diff 摘要"

> 制度（2026-10-25 起）：**每个提交**在合并/推送前，于本文件追加一节。
> 读者是仓库所有者，不是 AI。格式固定四段：**关键行 / 每行的风险 /
> 我不确定的地方 / 你 90 秒怎么验**。摘要讲代码本体和风险——"大白话
> 总结"讲故事，这份讲行。标记规则：读完在心里标"懂/不懂/存疑"，不懂的
> 进下一次轮讲（docs/WALKTHROUGH-search.md 是第一讲的样板）。

---

## (本提交·a30c96f) — b703905 的四段摘要补录（内容同上，无代码变更）

**关键行**：仅补录 b703905 的四段摘要；无代码或文档内容变更。

**每行的风险**：无（纯摘要补录）。

**我不确定的地方**：无。

**你 90 秒怎么验**：`git show a30c96f --stat` 应只显示 docs/COMMIT_DIGESTS.md。

## (本提交·f54c3d6 之后·e2e-evidence 清点与文档同步) — 五轮反例清点存档、自测补 F3 负例、修订计数同步

**关键行**：任务侧新增 `replay/counterexample-sweep.json`——五轮评审的全部 11 类发布值
篡改（规模 ×100+PASS、延迟 p50、内存两峰值、Q1 汇总 A/B、Q1 门清空+状态 PASS、Q2 B、
Q3 B、quality-warm 布尔、W3 门布尔+verdict）各自被独立的 published:*/scale 一致性检查
命中（变异下 158 项检查失败）；清点改用最小沙盒（只拷被读文件），并记录一起 ENOSPC
事件：首轮清点整目录拷贝被中断后残留 16 份沙盒（~133GB）在 $TMPD，经 `task/results.json
+ scale/suite` 标识核验后删除。FEATURE_STATUS 的规模措辞、RESULT.md 标题（v5）与
修订计数（1–20）同步。

**每行的风险**：清点是"检查存在且命中"的证据，不是回放无误的证明——但它与
selftest（基线先行+预期检查名断言+F3 链断裂负例）一起构成双向验证。沙盒最小化后
不再有 ENOSPC 风险。

**我不确定的地方**：published 一致性与 compile 共享辅助函数层（无独立第二实现）；
生命周期 PID 归属无出生身份。两者此前已标 MISSING。

**你 90 秒怎么验**：`cat replay/counterexample-sweep.json` 看 all_caught=true；
`./venv/bin/python driver/selftest_faults.py`（全绿含 f3）；`df -h` 确认磁盘已恢复。

## (本提交·AGENTS-md-review-lessons) — AGENTS.md 新增"测量/验收回放的评审规约"一节

**关键行**：AGENTS.md 在铁律之后新增一节 12 条规约，把 e2e-evidence-20261108
五轮 owner review 的失败模式固化为会话规约：重算字段源权威（防清空发布对象）、
verdict 不从幸存记录推导、回放自证抓故障（基线先行+预期检查名）、完整哈希链
缺证据即 MISSING、冻结门不事后豁免/测后口径标注、修环境后重审旧数据、A/B 对称
+平衡顺序、采集与判定分离、进程按出生身份归属、驱动冻结真写盘、沙盒自清理、
响应评审的固定动作（先核实→隔离→新 attempt→记 revision→文档同步）。

**每行的风险**：规约条目压缩后可能丢上下文——每条都注明了实证出处（任务目录
revision 1–21），不确定时回原始评审记录而不是凭条目字面执行。条目是经验性工程
规约，不是合同条文；与 CONTRACTS.md 冲突时以后者为准。

**我不确定的地方**：12 条的颗粒度是否合适（可能该合并或拆分）；后续评审可能
再追加条目，注意保持节长度可控。

**你 90 秒怎么验**：`git diff HEAD~1 -- AGENTS.md` 看新增节；
对照 reports/execution/e2e-evidence-20261108-005211/registration/preregistration.json
的 revision_15–21 确认每条都有对应实证。

## (本提交·98697c8/aa6fe6d 之后·冻结矩阵遍历修复) — 第七轮评审 P1/P2：清空的发布单元不再漏检，F4 沙盒最小化

**关键行**：上一轮修了比较方向，但延迟/内存两个入口仍在**发布键**上遍历并在空对象上
跳过——owner 复现：清空 L01/B 与配对统计、清空 M1/B census 与 RSS 门、删受控 B 生命周期
记录、把真实 MISSING 的 L11-real 伪写成 PASS 42/42，回放 409/409。修复：两个入口改为
**冻结矩阵遍历**（24 个延迟键来自原始单元文档，12 个内存键来自冻结清单），发布单元
缺失/清空即失败；逐臂采集数与状态按类型从原始证据重算并比对；配对统计在原始数据
支持而发布缺失时失败；L11-real 伪写被采集状态比对命中（重算 A=36/B=0 → MISSING）。
修复后 owner 反例六项变异全部命中（26 项检查失败）。F4 沙盒改为**只拷被读文件**
（~52MiB：单元文档、L09 resp/L12 pair JSON、memory 采样与文档、质量 run.json/stdout、
results.json），不再解引用模型符号链接、不再拖入未读 SQLite；TemporaryDirectory 覆盖
创建/复制/验证全程。

**每行的风险**：冻结键清单硬编码在 replay 中——若未来新增测量单元而清单未更新，
新单元会缺发布一致性检查（清单与 compile 的单元矩阵需同步维护，已在 revision_22
记录）。本轮另发生：A/B worktree、任务 venv 与 docs worktree 先后从磁盘消失（原因
未明，非本会话清理——本会话的删除均有标识核验）；分支完好，全部重挂/重建（A 臂
四文件对 43b373a^ 逐字节复核、venv 按 mcp.lock 重建），记入 revision_22 基础设施
说明。

**我不确定的地方**：目录消失的原因无法归因；共享辅助函数层无独立第二实现
（既有 MISSING）。

**你 90 秒怎么验**：`cd reports/execution/e2e-evidence-20261108-005211 &&
./venv/bin/python driver/replay.py`（0 失败）；
`./venv/bin/python driver/selftest_faults.py`（fault-sensitive）；
revision_22 记录了 owner 反例的逐项命中与基础设施事件。

## (本提交·a30c96f/aa6fe6d 之后·比较方向修复) — 第六轮评审 P1：发布值置空/置 null 不再使失败消失

**关键行**：`_expect` 的比较方向反转修复——重算对象现在是权威字段源，发布对象必须
携带每个重算键（缺键、null-于非空重算、类型不符、值不等都是独立失败类型）；Q1 汇总/
门失败集/等价类、Q2、内存 census、quality-warm 布尔、W3 各格布尔全部改为从冻结原始
数据重算驱动；W3 verdict 从**冻结四格矩阵**（与发布内容无关）推导，且每个冻结格必须
在发布对象中存在且非 null。自测新增 F4（清空必需字段 + null 失败臂 + verdict PASS
必须失败，8 个预期检查全部命中）并改为自清理沙盒（修一起 ~133GB 的 $TMPD 残留事件）。

**每行的风险**：字段清单由重算决定意味着 compile 新增发布字段而 replay 未同步时会
如实缺检查——需同步更新；gate failed 列表按集合语义两侧排序后比较（顺序非语义）。

**我不确定的地方**：共享辅助函数层无独立第二实现（此前已标 MISSING）。

**你 90 秒怎么验**：`cd reports/execution/e2e-evidence-20261108-005211 &&
./venv/bin/python driver/selftest_faults.py`（verdict=fault-sensitive，f4_caught=true）；
`./venv/bin/python driver/replay.py`（0 失败）；看 revision_21。

## (本提交·本轮修复提交) — 第五轮评审修复：B 侧发布值校验、缺失证据即链断、自测负例、文档同步

**关键行**：对应任务侧修复（本地不入库）：(1) replay 的 `published:*` 检查组扩到
**双臂全字段**——Q1 汇总 A/B、门失败集 A/B、collection/gate 状态（gate_status 由原始
gate 对象独立推导，不再循环引用发布值）、Q2 A/B 全指标、Q3 网格 A/B、内存 census 三项
峰值 + RSS 门 + 生命周期五字段（含 request_errors，watch_span 按 0.1s 舍入口径）、
quality-warm 门布尔值、W3 冻结门布尔值与 verdict 字符串；缺字段按失败处理。(2) 规模
哈希链：必需 stdout/stderr 缺失或无 SHA 记为链断（证据 MISSING），不再静默跳过。
(3) 自测修正：检查名过滤改为 `full-hash-chain`、新增 F3 负例（不更新引用 SHA 的真实
链损坏必须被检出并拒绝宣称全绿）、F3 样本选在 F1 变异范围之外。owner 反例：B 侧
Q1/Q2/Q3 分数、内存峰值、quality-warm 门布尔、W3 verdict 的七个篡改**全部命中**；
链断裂负例被检出。

**每行的风险**：published 检查的浮点为精确相等（同算法同序）；生命周期 watch_span
按 0.1s 舍入比较——口径漂移会如实失败。Q3 flags 检查只验证网格内部一致性，网格对
原始 stdout 的校验由 Q3 网格检查承担（两者互补，已分别断言）。

**我不确定的地方**：共享辅助函数层的独立第二实现仍为 MISSING。

**你 90 秒怎么验**：`cd reports/execution/e2e-evidence-20261108-005211 &&
./venv/bin/python driver/selftest_faults.py`（全绿含 f3）；
`./venv/bin/python driver/replay.py`（0 失败）；任务侧 revision_20。

## (本提交·b703905) — 第四轮评审修复：发布值全模块一致性、三跳哈希链、自测假阳性、SHA 指针

**关键行**：FEATURE_STATUS 比值同步（0.999574/0.999792）。任务侧 replay/compile 配套修复
（本地不入库）：(1) 回放新增 `published:*` 检查组——results.json 的每个延迟单元分位数、
配对 bootstrap、内存 census 峰值、生命周期释放时刻、Q1 汇总/等价类/门失败集、Q2 汇总、
Q3 网格、quality-warm p95、W3 冻结门——全部与独立重算逐值比对（owner 第四轮反例的四个
篡改各自命中）；(2) 规模哈希链补齐 case→audit 与 receipt→stdout/stderr 两跳；(3) 自测
重写：无故障基线先行、F1 逐层更新引用 SHA 并断言预期检查名、F2 用真实 helper、键名
分离（修复 f2 "caught" 覆盖 f1 假值的假阳性）；(4) 驱动身份对表进回放
（driver:*:current-sha），顶层指针与最新代同步。

**每行的风险**：published 一致性要求浮点精确相等（同算法同序，当前稳定）；compile 与
replay 的统计口径今后必须同步改动，否则回放如实失败——这是有意设计。哈希链全绿 + 语义
检查失败的自测组合，证明链修复没有吞掉语义检测。

**我不确定的地方**：published 检查覆盖 results.json 的全部已发布数值，但 compile 与
replay 共享部分辅助函数（quantiles/nearest_rank/bootstrap）——若该共享层本身有错，两者
会一致地错；独立于辅助函数的第二实现未做（MISSING）。

**你 90 秒怎么验**：`cd reports/execution/e2e-evidence-20261108-005211 &&
./venv/bin/python driver/selftest_faults.py`（全绿）；
`./venv/bin/python driver/replay.py`（0 失败）；`grep -c "published:" replay/replay-report.json`。

## (本提交·d8b0aed) — 第三轮评审更正：规模逐格校验、采集/判定分离、自测假阳性修复

**关键行**：本提交只改 FEATURE_STATUS 的证据段与新增本摘要（数据本体在本地任务目录，
不入库）。对应任务侧三个修复：(1) replay_scale **逐格重算**并与 results.json 比对
n_formal/n_failures/quantiles_all_samples，外加套件状态一致性——owner 的"发布值 ×100
+ 状态改 PASS"反例现在失败 145 项检查；(2) 延迟/内存/质量的发布值一致性检查扩展到全部
模块（owner 第四轮的四个篡改——发布延迟×100、重排内存改写、Q1 s@5 改写、Q1 门改
PASS——各自被独立的 published:* 检查命中）；(3) 单元状态拆成 collection_status（23 个
延迟单元完整 2×42 样本、L11-real MISSING 带配对计数）与 gate_status（Q1：采集完整且
门 FAIL 同真），mcp-overhead 单元不再被通用 wall_ms 测试误判为 MISSING。

**每行的风险**：逐格一致性使回放对 results.json 的任何手工修饰敏感——这也意味着
compile 的统计口径改动必须与 replay 同步（本轮就修了三处口径漂移：all-sample 分位
含失败样本、nearest-rank p50、生命周期 formal_requests）。自测的 F1 现在逐层更新
引用 SHA（receipt→audit→case→manifest）以隔离语义故障；链检查与语义检查分离断言。

**我不确定的地方**：published 一致性检查对浮点要求精确相等（同一算法同序，稳定）；
若未来 compile 改变实现，检查会如实失败——这是设计意图。生命周期 PID 归属的残留
限制（无出生身份）已在 replay 输出中标注。

**你 90 秒怎么验**：`cd reports/execution/e2e-evidence-20261108-005211 &&
./venv/bin/python driver/selftest_faults.py`（应全绿）；
`./venv/bin/python driver/replay.py`（应 0 失败）；对比
`git diff c813aaf d8b0aed -- docs/FEATURE_STATUS.md`。

## (本提交·e2e-evidence-20261108-corrections-v3) — 第二轮评审更正：规模 254 次超时补报、W3 冻结门恢复 FAIL、W1 判据改回冻结比值、回放抗注入

**关键行**：本提交按第二轮只读评审更正三处遗留问题。(1) 规模套件 evidence_status
改 **FAIL**：4,320 个正式样本中 **254 个以产品侧 `request deadline exceeded` 失败**
（500k-1/10/100 = 21/135/98，双臂对称；500k-10 最差格 ~40%），分位数只按成功样本
计算并附失败计数，高失败格标 A/B 不可比。(2) **W3 的冻结 overhead 门（p95≤100ms）
恢复为无豁免**：受控 B 403.6ms / 真实 B 237.4ms → **FAIL**（v2 曾以"门仅适用 fast"
豁免——事后收窄冻结判据，已撤回；生命周期解释降级为未验证假设 MISSING）。
(3) W1 回归判定改回**冻结比值判据**（平衡顺序逐对比值 0.998/1.002 → 按判据无退步；
CI 仅描述性；原 1.23 与代码无关的归因保持 MISSING）。另：L11-real 披露为 MISSING
（两臂 worker 请求超时退出，A 36 组配对、B 0）；受控 W2 overhead 更新为实测
74.55/15.42ms；回放扩到 **157 项**（逐回执退出码、audit→receipt SHA 链、pack 等价
分类独立重算、Q2 双臂、生命周期 PID 归因、compile↔replay 一致性），并用
`selftest_faults.py` 证明评审的两类内存反例现在都会被捕获；驱动身份在
`driver-sha256-history.json` 如实建档——revision_15 声称的重冻结未写盘，重测发射态
**无密码学证明（MISSING）**。

**每行的风险**：254 次超时是产品真实行为（大库 + fast 死线 5s），不是测量故障——
把它从"套件历史绑定问题"里分出来单列，避免再次用旧验收器问题掩盖产品问题。
500k-10/100 高失败格的 A/B 不可比标记意味着规模结论对 500k 部分不完整——这是
完整数据下的真实结果，不是 MISSING 运行。W3 门 FAIL 归因（重排生命周期）仍未经
隔离实验证明。驱动发射态无哈希证明是本任务的诚实代价，标记为 MISSING 而非补造。

**我不确定的地方**：同上列各 MISSING 项；规模 500k 格在失败率 40% 下"成功样本
分位数"的代表性也存疑（已附计数，读者可自行判断）。

**你 90 秒怎么验**：`cd reports/execution/e2e-evidence-20261108-005211 &&
./venv/bin/python driver/selftest_faults.py`（应两个 true）；
`./venv/bin/python driver/replay.py`（应 157/157）；
`grep -c "deadline exceeded" $(find scale/suite -path "*500000-10*" -name "stderr.log") 2>/dev/null | head`
看失败分布；`git diff 1bbf4ce HEAD -- docs/` 看更正范围。

## (本提交·e2e-evidence-20261108-corrections) — 更正 2026-11-08 证据段：pack 非等价、quality-warm 门 FAIL、平衡顺序与对称缓存重测

**关键行**：本提交更正前一提交写入 FEATURE_STATUS 的三处错误结论，全部源于
所有者对原始数据的只读评审。(1) 质量段改为：指标与命中排名等价，但按预注册的
跨臂 citation 解析后 **210 题 dev 中 24 题（11%）的 pack 解析内容真实不同**
（记录数/窗口切分），严格 pack 级等价 FAIL——v1 的"pack SHA 零差异"是把内嵌
index UUID 的 raw SHA 拿来直接比，方法错误。(2) 新增 **quality-warm 门 FAIL**：
W3 MCP warm 受控 p95 A=7583ms / B=7564ms > 冻结门 2500ms（降级 0%，慢尾为真实
重排成本，双臂相同）。(3) 延迟段改为：**平衡顺序实验**（两种 episode 顺序 ×
受控/真实）显示 W1 warm B−A≈0（CI 跨零）——v1 的 1.23 比值是会话假象，v1 引用的
"MCP 无混杂交叉验证"不存在（mcp-warm 单元同样是 episode 串行），已撤回。新增：
W2 warm 的 episode 方差支配（同臂 p50 2.1s→3.7s），A/B 归因 MISSING；容量矩阵
用**双臂对称无缓存**重跑后无一一致臂优势（v1 的 B 大幅更快是 --cache-experiment
只建 candidate 侧缓存的假象）；真实档 MCP warm 每臂 34/42 请求带内存压力
fallback（从原始载荷重算）。

**每行的风险**：24/210 的 pack 差异按冻结判据记 FAIL——它可能只是死线敏感的
窗口选择（`_rank_pool_cap`）加上 run-to-run 方差，但同臂重复未做，两者占比分不开，
不能写成"等价"。quality-warm FAIL 是双臂共同的性能事实，不构成对优化的指控，
也不能被"两臂相同"洗成通过。W2 的"归因 MISSING"不是数据缺失——数据完整，
是 episode 方差超出该设计的分辨能力。所有被更正的 attempt 原样留档
（`raw/*-attempt*`），重测走 revision_15/16 的新 attempt，未覆盖任何旧回执。

**我不确定的地方**：pack 差异的成因分解（死线敏感性 vs 同臂方差）；W3 overhead
B 侧 403ms 的机制；500k 容量格的波动；观察器内核峰值保证——均列 MISSING。
回放扩展到 126 项（含质量汇总重算、门槛评估、规模重算）后 0 失败。

**你 90 秒怎么验**：`cd reports/execution/e2e-evidence-20261108-005211 &&
./venv/bin/python driver/replay.py`（应 126/126）；
`./venv/bin/python driver/q1_equivalence.py`（应输出
identical 20 / provenance_only 166 / real_diff 24）；对比
`git diff ed7db2f HEAD -- docs/FEATURE_STATUS.md` 看更正范围。

## (本提交·e2e-evidence-20261108) — 高信心优化的端到端 A/B 证据补齐（文档：FEATURE_STATUS 增 2026-11-08 证据段）

**关键行**：本提交只改 `docs/FEATURE_STATUS.md`——在 2026-11-05 候选小节下追加
"End-to-end A/B evidence (2026-11-08, source range 756098e vs 8b75498)" 一段，把
原先标 MISSING 的整条 CLI/MCP 延迟、进程内存、模型生命周期与检索质量四类证据
补上结论与范围。测量本体在本地 `reports/execution/e2e-evidence-20261108-005211/`
（按惯例不入库）：A/B 两臂逐文件 SHA 证明只有四个优化文件不同；24 个延迟单元对
（每臂 42 正式样本）、M1–M5 内存/生命周期（含真实 ≥358s 闲时观察）、210 题 dev
双臂、legacy/full-media 回归门、三公开集×四 scorer、12 容量单元。

**每行的风险**：结论写的是"等价"不是"提升"——210 行 dev、245 行回归门、12 个
公开集格点（含逐查询排名 docid）全部零差异，两条质量红门（noise 0.667<0.90、
ocr 0.667<0.70）原样保留 FAIL。延迟最强配对下 B≈A；episode 隔离单元存在
6/6 跟随测量顺序的混杂，W1 warm B/A 比 1.23 触及退步门但方向与 diff 预期相反、
且被无混杂的 MCP 交叉验证反驳——文档按"照报原始比值 + 归因 MISSING"呈现，
不把它洗成通过或失败。生命周期是本轮唯一"A FAIL / B PASS"的实质差异
（B 在 ~302s 释放重排模型并干净重借用；A 从不释放且 30/42 样本泄漏 ~612MiB 子进程）。
容量矩阵是 mock 向量的容量成本，不是真实模型速度。

**我不确定的地方**：L02 的 +45ms 无法在设计内与代码效应分离（顺序混杂归因
MISSING）；500k 热 search/pack 个别格 B 变慢 12–16%（合成、固定查询、30 样本，
不外推）；W3 MCP-overhead B 侧 403ms 的机制解释（两侧独立 reranker 注册表）
未做隔离实验；acceptance 观察器看不到 start_new_session 的模型子进程，"连续
内核级峰值"保证仍 MISSING。13 次 revision 全部留档（含 22 个孤儿进程事故与
并发违章，均按 PID 清理）；holdout 曝光 0；质量重试 0；独立回放 73/73。

**你 90 秒怎么验**：`cd reports/execution/e2e-evidence-20261108-005211 &&
./venv/bin/python driver/replay.py`——应退出 0（73/73 检查）。再
`sed -n '/End-to-end A\/B evidence/,/^Receipts/p' docs/FEATURE_STATUS.md`
读新增段。想要人话版结论读该目录的 `RESULT.md`（七问七答）；想亲手复现读
`REPRODUCE.md`（只读回放不启动模型）。核对 A 臂构造：
`git diff 756098e 8b75498 --stat` 应只有四个 goldmine 文件。

## (本提交·site-001) — 官网 v1（含用户新反馈重构）：专业首页 + 分层 Learn 地图 + Cloudflare 上线

### 关键行

1. `site/index.html`：专业产品页重写——对比表（goldmine vs Spotlight vs
   云 AI vs grep，8 行逐行带 verify 注释）、实测数字六卡（含 12 红门如实
   印出）、六个工程决定、快速开始带真实 GitHub URL。去 AI 味：短句、
   数字、无排比。nav 改 Learn、GitHub 链接入。
2. `site/content/concepts.json` v2：77 节点 / **四深度层**（L3 项目机制 32 →
   L2 系统技术 16 → L1 领域方法 18 → L0 基础概念 11）/ 232 条下探边 /
   35 条完整四层链（如 指纹门→SHA-256→键序规范化→哈希函数）。教学
   subagent 逐概念制作，代码引用逐条验真（还纠正了文档一处不实：
   whisper 走独立 whisper-cli 而非 llama.cpp——记入 SITE_IMPROVEMENTS #1）。
3. `site/assets/map.js` + `site/tools/bake_graph.py`：分层带状布局（层间
   虚线 + L3~L0 层标签）、标签碰撞剔除、面包屑、面板层徽章；无 JS 时
   降级为按层分组的目录视图。
4. `wrangler.jsonc`：Workers Static Assets（免费层、无绑定、observability
   on）。**已上线**：https://goldmine-site.sixtycat2000.workers.dev
   （版本 6906a7af，/、/learn、/docs 全 200，回执
   reports/execution/site-001/deploy.txt，本地留档）。
5. `docs/SITE_IMPROVEMENTS.md`：过程中发现的 10 条改进点（只记录不顺手修）。

### 每行的风险

- 对比表基于 2026-10 各产品公开行为——有时效性，verify 注释在，更新靠
  人工（SITE_IMPROVEMENTS #9）。
- 77 节点教学内容的代码引用已验真，但教学口径未逐条人工复核——以代码
  为准，复述中发现偏差即回改。
- account_id 提交进仓（非机密；token 在钥匙串未落盘）。
- WebKit 隐性炸弹已修：IO 负百分比 rootMargin 令整页揭示失效——新页面
  一律像素 margin（SITE_IMPROVEMENTS #7）。

### 我不确定的地方

交互链（搜索→选中→下探→面包屑→键盘）在 IAB 逐项验证过；真机触屏
（sheet 拖关/双指缩放）与低功率设备动效未测；自动化冒烟缺位（#8）。

### 你 90 秒怎么验

打开 https://goldmine-site.sixtycat2000.workers.dev ——首页滚到对比表；
进 Learn，搜"指纹门"，回车，点任一"深挖"chip：相机飞到下一层、左上角
出现面包屑、面板徽章变 L2。

---

## a4a329f — 旅程文档：八幕全程 + 术语考古词典

### 关键行

纯文档：`docs/JOURNEY.md` 新增。第一部分八幕旅程（从 P0 地基到理解密度
制度，每幕讲"发生了什么/为什么这么处理/当时考虑了什么"，含两次诚实
回滚、撞车事件、vibe code 之辩的完整脉络）；第二部分术语考古词典
（A–G 七组 ~35 词条，每条从"在本项目干什么"逐层挖到只剩常识）。
AGENTS.md 文档地图挂链。

### 每行的风险

无代码风险。词典中的技术表述（量化格式、EPIPE 语义、poll 预读等）是
教学性简化——方向正确但不是规范文本；工程决策以代码和五讲讲义为准。

### 我不确定的地方

早期各幕（P0/P1 系列、digup-plan-001/002 细节）依据本地执行记录与
会话摘要复原，无逐条对照原始记录复核；如与某份 SUMMARY 冲突，以
原始记录为准。

### 你 90 秒怎么验

读词典任意三词条（比如 A3 z 分数、B3 mmproj、F4 EPIPE），自问能否
向一个非技术朋友复述——能，本文的目的就达成了。

---

## f224e09 — 第三至五讲：摄取管线 / 诚实契约 / 双常驻进程

### 关键行

纯文档：三份新讲义（WALKTHROUGH-ingest / -honesty / -processes，各含
十问），AGENTS.md 轮讲清单标记五讲完成。零代码。事实核对来源：pipeline.py
run/波次/事务、command_support.py 退出码表、llama.py 边车纪律、
media.py 预算与冻结参数——全部对照现行源码写就。

### 每行的风险

无代码风险。讲义把"腐烂风险清单"直接写进了每讲（这是刻意设计：学习
材料同时是维护者的巡检清单）。

### 我不确定的地方

讲义中引用的具体常量（full_period_s=21600、ctx=2048 等）以源码为准；
若后续重构改值，讲义不会自动跟随——复述制度里发现口径不一致时以代码
为准并回改讲义。

### 你 90 秒怎么验

挑第三讲十问的第 3 问（退出码表）对着 `grep EXIT_ goldmine/command_support.py`
核一遍——练习"讲义 ↔ 代码"互查的手感。

---

## 5f830c2 — 第二讲：指纹与身份模型

### 关键行

纯文档：`docs/WALKTHROUGH-fingerprint.md` 新增（四层身份模型 / 十问），
AGENTS.md 轮讲清单与文档地图更新。零代码。

### 每行的风险

无代码风险。讲义内容已对照 fingerprints.py 源码逐节核实（build/canonical/
gate、v2 embed_identity 拆分、注册表语义），但十问答案未独立校对——
你复述时的偏差会由下一讲开讲时校正。

### 我不确定的地方

无。

### 你 90 秒怎么验

读 docs/WALKTHROUGH-fingerprint.md 第一节的四层图，然后试着不看书说出
"改 MEDIA_PREPROCESSING 字符串会动哪几层、每层后果是什么"——这是本讲
的合成题。

---

## 400f214（分支 site-v3-roadmap-learn-20261026）— 首页去自吹：只留观众可核验的事实

### 关键行

仅 site/index.html：（1）"数字与回执"区六卡全部改为 数字 + 一句事实 + 回执
指针，删掉"不藏 / 这是它和营销页的区别 / 印在首页因为…"类编辑腔；（2）
整段删除"六个工程决定，多数仓库不做"自夸节（其中事实已由对比表以同权重
呈现）；（3）"仓库里最值钱的是证据链"→"代码、文档、评测与执行记录都在
仓库里"；（4）对比表 8 个行头 `</td>`→`</th>`（HTML 合法性修复，评审团
漏网）。

### 每行的风险

落在并行会话的 v3 重写之上（c2083af：145 概念 Learn、QMD 对照节、新
hero——其文案本身就是观众视角事实句，两轮修改方向一致）。vsqmd 节的
两句轻评语（"快不是赌气…"）判定为数字解释而非自吹，保留。

### 我不确定的地方

"145 nodes" 与 77 的口径差由并行会话扩容解决（concepts.json 实测 145
节点、四层），本提交未复核其内容质量——归它的摘要管。

### 你 90 秒怎么验

打开首页滚到 MEASURED NUMBERS：每张卡应只有数字、一句事实、一个回执
指针；全文搜索不应再出现"不藏 / 多数仓库 / 最值钱"。

---

## d29f5b7 — 完整视觉重设计：两个响亮声部、每节独立轮廓、可读的画布

### 关键行

（1）分支合并：site-v3-roadmap-learn-20261026 fast-forward 进 main（含 QMD
同题对照节、145 节点 Learn）。（2）设计批判 agent 对 7 张现状截图的诊断：
字阶无峰值（21.6px 的 marquee 数字）、grid3 卡片循环（同一模块连用三节）、
两块 canvas 是亚像素噪点——以此为简报。（3）重设计 agent 全量改造
goldmine.css/index.html/hero.js/map.js/bake_graph.py：display 级巨字只给
QMD 纪念碑与 hero "0 B"；场景改编辑部编号列表；对比表锚定胜者列（2px 金
边 + 设计过的 ✓/✗ 字形）；数字区改等宽台账；hero 星空 490 点改 164 个
带标签的大点 + 查询星打字「退款期限…」+ 六邻居点火；Learn 首帧从 7 等
宽药丸改为双列错落的领地地图（七块着色井、145 节点静默全显、消灭空转
漂移、深度导航轨 SURFACE/L3-L0、节点到面板的金色脐带）；面板 480px/15px；
移动端全部触点实测 44px。

### 每行的风险

- 领地井透明度从简报的 5-8% 提到 13%/7%（8% 在暖黑底上不可见——设计
  agent 的实测偏离，方向忠于简报意图）。
- canvas 领地头替代了 DOM clusterbar：隔离功能不再有独立按钮（进入领地
  即飞行），键盘/屏读路径依赖 depth-rail 与搜索——比旧版少一个显式功能，
  交互上是简化取舍。
- 英文态查询星仍打中文「退款期限…」（全站统一示例串，刻意不翻）。
- 我修掉一个真 bug：document 级 keydown 里 e.target.closest 在
  target=document 时抛异常（启动即炸掉后续 handler）。

### 我不确定的地方

设计是 agent 单轮完成 + 我的三屏目检与 12 项交互回归；未经第二位设计
评审。视觉的最终裁判是所有者眼睛——建议亲手过一遍首页五个段落和 Learn
的下探链路，不满意的具体节随时点名返工。

### 你 90 秒怎么验

打开 https://goldmine-site.sixtycat2000.workers.dev ——眯眼测试：每屏
应有一个东西占主导（hero=0 B+星空、vs=78.3% 巨字、numbers=台账）；
Learn 首屏应是"一张地图"而不是七个药丸；点任一节点看金色脐带与面板。

---

## aa3b0c9 — 理解密度制度落地：AGENTS.md + 锚点卡 + 第一讲 + 摘要制度

### 关键行

全是文档，零代码。四处新文件：`AGENTS.md`（会话规约六条铁律 + 一会话
一分支 + 提交摘要制度 + 文档地图）、`docs/ANCHORS.md`（6 条你亲手可跑
的验证命令，2026-10-25 全部实跑验证）、`docs/WALKTHROUGH-search.md`
（第一讲：搜索打分完整走一遍 + 十问）、`docs/COMMIT_DIGESTS.md`
（本文件，含 e3c81c2 与 a842e03 的补写摘要）。

### 每行的风险

无代码风险。制度风险一处：AGENTS.md 规则只在会话遵守时有效——它对
不读它的会话没有强制力（hosted CI 才有），所以第 2、6 条铁律落地前
靠自觉。

### 我不确定的地方

锚点卡第 6 条的全量套件基线"除 2 个档案化失败"——resource_observer
是 flake，你某天自己跑可能撞上它失败，卡里已写处置法（单独重跑 3 次）。

### 你 90 秒怎么验

`ls AGENTS.md docs/ANCHORS.md docs/WALKTHROUGH-search.md
docs/COMMIT_DIGESTS.md` + 挑 ANCHORS.md 第 2 条亲手跑一次。

---

## a842e03 — 批次 B：音频尾窗门 + 阿拉伯丢字页 OCR 替换

### 关键行（真正重要的 5 处）

1. `goldmine/ingest/media.py` audio_windows 尾部：
   `new_audio = tail_len - (window - hop) if effective >= window else tail_len;
   if new_audio < TAIL_MIN_NEW_S: 删最后一段`
   ——尾窗只有携带 ≥2 秒**新音频**（超出上一窗覆盖）才保留。
2. 同文件 `MEDIA_PREPROCESSING` 字符串 v4→v5（追加 `,tail-min2s`）——
   这个字符串进**指纹**，它一变全库重嵌。
3. `goldmine/ingest/media.py` 新常量 `MEDIA_EXTRACTOR_VERSION = "media-ffmpeg-mm.v3"`，
   extraction.py 三处硬编码改为引用它——版本号只存在一处。
4. `goldmine/ingest/pdf_evidence.py` ar_recovered 块：lostLetters 页
   OCR 后的三查（够长 ∧ 自身无病理 ∧ 含阿拉伯块）→ 过了替换、不过保留
   原文；`goldmine/ingest/extract_pdf.py` 的 `upgraded_pdftotext_version()`
   统一组装 `+arocr`/`+visionocr` 版本串，fingerprints 注册四种组合。
5. 测试钉子：`tests/test_media.py` 边界数学（56.9s 丢 / 57.0s 留 / 62s 留），
   `tests/test_pdf_garbled.py` 正/负/边界三路。

### 每行的风险

- 关键行 1：**语义取自 DigUp 源码而非 goal prompt 原文**（prompt 的字面
  规则是不可达死代码、62s 例子与源码相反）——如果你当时想的是字面意思，
  这里和你的意图有偏差，需要你确认接受源码语义。
- 关键行 2：指纹变化 = 老库全部要重嵌。无存量用户所以成本为零，但这是
  "为什么我的库要重建"的唯一答案。
- 关键行 3：若未来只改字面量不改常量（或反之），行会**永久反复重抽**——
  已有测试钉死两处相等。
- 关键行 4：三查里"含阿拉伯块"用的是检测器同款集合（0600–06FF），OCR
  若输出呈现形式（FB50+）会被判"非阿拉伯"而走回退——保守方向的错误，
  可接受但要知情。
- 关键行 5：62s 保留那条测试**与 goal prompt 的例子相反**，是有意钉死
  源码语义的反例。

### 我不确定的地方

- 真实 iCloud/真实病理 PDF 两处验证在本机不可得（MISSING 已记），机制
  测试覆盖的是"我们能造的部分"——真实世界的边角（坏生成器的具体形态）
  只有等真样本出现才知道。

### 你 90 秒怎么验

docs/ANCHORS.md 第 2 条（指纹拒绝现场，用的就是 v4 备份库）+ 第 4 条
（双 gate green、delta 全 0）。

---

## e3c81c2 — 批次 A：dataless 跳过 / 查询期删文件过滤 / estimate / EPIPE / 目录表

### 关键行

1. `goldmine/ingest/walk.py` `_is_dataless()` + 两处拦截点：跟随符号链的
   stat 看 st_flags（覆盖符号链指向占位文件的场景）+ os.lstat 兜底；
   拦截在 `_decide_file` **之前**（它会 open 文件嗅探 TS 头——open 即下载）。
2. `goldmine/search.py` `_drop_deleted()`：候选阶段存在性过滤，**限定
   roots_json 范围**、URL 豁免、limit 之前；计数进 `stats`（不是
   diagnostics——那个每次查询会被 clear，多查询会互相覆盖）。
3. `goldmine/commands/estimate.py`（新文件）：只读估算，解析器**不带
   --db**——这个命令在结构上就无法写库。
4. `goldmine/cli.py`：`except BrokenPipeError` + `main()` 里退出前
   `sys.stdout.flush()` 探针 + devnull dup2。
5. `goldmine/ingest/rules.py` SKIP_FOLDERS += 5 个构建目录名。

### 每行的风险

- 关键行 1：lstat 兜底对**每个文件**多一次系统调用（50 万文件 ≈ 亚秒级，
  可忽略但不是零）；符号链→占位 的场景靠 stat 路径覆盖，测试只测了
  lstat 路径（monkeypatch 面覆盖不到 stat 路径的伪造）。
- 关键行 2：**roots 限定是评审拦下来的修正**——第一版无条件过滤会打挂
  21 个离线测试。语义副作用：索引范围外路径的行永不过滤（有意）。
- 关键行 4：真正的坑是**小输出全在缓冲区、只在解释器收尾 flush 时爆
  （rc 120）**——没有退出前探针的话，执行期 handler 根本接不到。
- 关键行 5：`out`/`dist` 这类名字**无条件**跳过（DigUp 同款）；如果你
  有同名真实内容目录，它们从此不被索引——这是刻意的取舍。

### 我不确定的地方

- mcp_server 的断管路径本机无 SDK 未实测（同款纪律，但"没跑过"就是
  "没跑过"）。
- resource_observer_native 那天 3/3 失败后无条件重跑通过、干净 HEAD 也
  复现失败——判定为档案化 flake 而非回归，但"为什么那天集中发作"没有
  根因（机器状态未留档）。

### 你 90 秒怎么验

docs/ANCHORS.md 第 1、2、5、6 条；批次 A 当天双 gate delta 全 0
（回执在本地 reports/execution/digup-p2-closeout-001/batch-a-close/）。

---

## codex/index-lifecycle-repair — 定向修复、来源生命周期与范围预检（用户已批准合回 main）

### 关键行

`index_state.py` 与 `ingest/update_policy.py` 集中模型身份、失效和升级判断；
同模型重建只改指定范围，换模型先检查全部已记录来源，再发布身份。
`repair` 增加只读计划、失败重试和原文向量补齐；`sources`/`forget` 增加
持久排除与卡片清理。`distill_corpus.py` 先用 SQL 筛范围、数容量，再解码。
FTS 在候选上限前筛范围；站点按主机边界；视频位置读取实际 PTS。
MCP 断管处理覆盖嵌套异常，并继续抛出其他真实错误。
合并保留 main 后续的 helper 测试环境修复、验收清单迁移及 MCP 测试分层调整；
它们没有改动产品运行时代码。

### 每行的风险

换模型属于全库身份迁移；来源不可用时必须拒绝，不能留下混合向量。
忘记来源会移除索引证据及受影响卡片，但原文件保留；恢复排除后需要重新
摄取。片段 ID 不再回收，旧引用不会重指新片段。筛选提前可能改变词法
排名；两套历史门通过，仍有 `real-103` 从第 1 降到第 2 的差异。

### 我不确定的地方

已有索引上的查询门不能证明新提取器重建后的检索质量；本次没有刷新冻结
基线、调阈值或打开产品 holdout。真实答案评分与卡片聚类质量的既有缺口
继续记录在 FEATURE_STATUS。资源计数测试已用退出握手消除采样竞争，
没有放宽观察器的缺口判定或原断言。

### 你 90 秒怎么验

在装好测试依赖的环境运行 `python3 -m pytest -q
tests/test_index_lifecycle_regressions.py tests/test_sources_repair.py`。
对自己的索引先运行 `goldmine repair --db PATH --plan`；移除前用
`goldmine forget TARGET --db PATH --dry-run` 看范围。完整回执保存在本工作树
的 `reports/execution/lifecycle-repair-20261025/`，能力与质量摘要见
[FEATURE_STATUS](FEATURE_STATUS.md)。

## Merge: P2-08 fair-comparison fixes (p2-08-fair-comparison → main)

**关键行**
- `goldmine/config.py` + `goldmine/commands/search.py`: 新增 `[search] request_timeout_s`（默认 None=保持 10s/5s 交互死线；显式设定时提高客户端请求预算）
- `scripts/search_comparison/engine_capture/settings.py`: recoll.conf 用原始标量路径；goldmine.toml 写入 request_timeout_s=600（对比配置）
- `scripts/search_comparison/engine_capture/{indexes,results,plan,protocol}.py`、`adapters.py`、`source_views/media.py`: 8 项审计/解析/边界修复；臂注册表 9→7（owner 指示移除 Recoll）
- `tests/`: 硬编码 18 格/1728 槽/字节钉全部改为从协议推导或按 14 格重钉（含夹具加声明式填充以保持 ">8MiB" 前提）

**每行的风险**
- request_timeout_s: 默认关闭，现有用户行为零变化；配置解析走既有 unknown-key 拒绝路径；若设定过小，长查询会提前超时（对比里设 600=计划冻结的查询墙）
- 协议 9→7 臂：任何依赖 18 格的下游（报告汇总/测量包）需同步——测试已全部改齐
- 字节钉 PIN19=16010171：依赖夹具生成器字节级稳定，改动 fixtures 需重钉

**不确定的地方**
- 对方会话的 test_existing_strict_capture_verifier 合并前在 main 上失败；合并后通过（被 N_CELLS/夹具修复顺带覆盖），未单独根因分析
- measurements 正式机仍有 1 处未修的休眠假设（更新见证），本合并不含其修复

**90 秒验证**
```
PYTHONPATH=scripts:. reports/execution/tooling/mcp-venv/bin/python -B -m pytest \
  tests/test_comparison_engines.py tests/test_comparison_measurements.py \
  tests/test_comparison_source_views.py tests/test_measurement_execution.py \
  tests/test_measurement_slots.py tests/test_comparison_report_framing.py -q   # 373 passed
python3 -m goldmine search --help   # 无新旗标（配置项在 goldmine.toml [search]）
```

## site-v3-roadmap-learn-20261026 — 凌晨定时会话：DigUp/QMD 竞争战略 + 官网 v3（2026-10-26）

**关键行**
- `docs/ROADMAP-2026.md`（新）：对 DigUp 与 QMD 的差距/胜出点分析，五战场排序（红门打绿 → 纯文本追平 → 材料→答案闭环 → 服役面 → R11 决策层），全部数字带出处。
- `site/index.html`：首页文案整体重写（痛点先行 dek、场景前置）；新增 `#vsqmd` 区块——QMD 正面对比三卡（78.3% vs 66.1% 覆盖、1.4s vs 21.8s 速度、43–60% vs 0% 噪声空答）+ 纯文本让步句（91.7% vs 100%）。
- `site/docs.html`：新增「路线图 · 五战场」区块（含指向 ROADMAP-2026.md），概念计数 77→145，TOC 加 Roadmap。
- `site/content/concepts.json`：77→145 节点（+68，全部带真实代码出处与双语三层讲解），新增 27 条下钻边；扩容源文件 `site/content/expansion-20261026/part{1..5}*.json` + 合并器 `site/tools/merge_expansion.py`（幂等、schema 校验、边目标校验）。
- `site/tools/bake_graph.py`：布局 v3「集群井口下钻」——7 个集群井口 + L3 网格 + 子概念行（380px/层）；parent 森林保证全链终止于 L3（相邻边最少孩子优先分配 + 跳层回退挂 inker + keystone 兜底），产出 reveal/parent/keystone。
- `site/assets/map.js`：引擎重写——地表视角（井口瓦片+keystone）→ 进集群 → 双击/Enter/面板 chip 逐层下探 → Esc 逐层回退；深度标尺、搜索直达（自动展开祖先链）、双指缩放、节点屏幕半径钳制（≤20px）。
- `site/assets/app.js`：`.rv` 入场动画加兜底清扫（视口内未 reveal 的元素每 1.5s 强制补显）——动画失效环境内容不再隐形。
- `site/learn.html` / `graph.baked.json` / `build_learn.py`：145 节点重烘焙重建（389KB），no-JS fallback 改按 meta.layers 分组。

**每行的风险**
- concepts.json 是合并产物：原 77 节点内容理论上逐字未动（合并器不重写已有节点），但文件整体重排过缩进——diff 大，审阅靠 merge 脚本的幂等性而非肉眼。
- map.js 重写幅度大：旧「全图可见 + 集群隔离」交互被「井口下钻」替代（clusterbar 隐藏未删）；老用户若依赖全图一览会感到变化——list 模式与搜索保留。
- 首页 QMD 数字（78.3/66.1、1.4/21.8、43–60/0、91.7/100）来自 comparison-report-001 的 dev 套件口径，非产品 420 题门——文案已标注「诚实起见」与回执路径，但读者仍可能把 dev 数字当产品质量。
- bake 的 parent 分配是启发式（最少孩子优先 + inker 回退 + keystone 兜底）：个别节点的「父子」归属是布局选择而非唯一真理。

**我不确定的地方**
- WALKTHROUGH-processes.md 说 watch 用 FSEvents，digup-derived-plan.md §0 与 ROADMAP R9 说自适应轮询（5s→120s）——两处矛盾，新节点文案刻意只写「静默窗合并 + 6 小时全量兜底」不点名机制，需 owner 裁决哪份文档过时。
- 首页让步句把「纯文本 91.7% vs 100%」写成「路线图战场 2」——这是竞争战略判断（ROADMAP-2026 战场排序），owner 可能想改排序。
- learn 交互从「全图」改「下钻」属产品语义变化，已按用户凌晨指示执行（"zoom in zoom out 挖掘到底层概念的点"），但未做可用性外测。

**用户 90 秒怎么验**
- 打开 https://goldmine-site.sixtycat2000.workers.dev/learn → 点任意井口（如「文件系统与进程」）→ 双击任一 L3 节点看子概念展开 → 连按 Esc 看逐层回退；按 `/` 搜 holdout 回车看直达。
- 首页滚到 03 区块看 QMD 三卡与让步句；docs 页滚到「路线图」区块。
- 亲手验数字：仓库 `reports/execution/P2-08/comparison-report-001/REPORT.md` 对表；锚点 `python3 -m goldmine status --db evals/eval.sqlite --json`（staleness 全 0）与 `python3 -m goldmine search "copper gasket" --db evals/eval.sqlite.pre-p2b.sqlite`（rc=3 指纹拒）本会话已跑过。
- 部署版本：`2e05cc65-2c90-488f-a037-2c76ec0b9762`（wrangler deploy 于 2026-10-26 ~06:00）。

---

## (hero v5) — 首页动效换血：paseo.sh 单缓动教条 + 三幕产品演示

### 关键行

hero.js 全量重写（v4 领地几何保留）：（1）三幕场景 tabs（语义/词法/多模态）
+ 滑动金 pill（340ms，全站唯一缓动 cubic-bezier(.22,.61,.36,1)）——每幕演示
该模式的真实机制：语义=查询星点亮余弦最近 6 点；词法=ZT-84 只有精确 token
节点青色点亮（精确性一眼读懂）；多模态=四个带标签示例（会议纪要/main.py/
standup.m4a/Screen Shot.png）依次接入同一查询空间。（2）光标斥力物理
（paseo butterfly 常数：150px 内平方反比、回位弹簧、摩擦 0.92、限速）——
指针一动整个空间让路。（3）RM=当前场景静态合成，tab 切换瞬时。

### 每行的风险

浏览器启发式缓存曾让验证读到旧 JS（本地服务器无 cache 头）——线上
Cache-Control max-age=3600 同样可能让回访者 1 小时内看不到 v5；可接受
（版本化部署会随下次 any-deploy 失效）。

### 我不确定的地方

多模态幕的 stagger 周期（约 1.4s/领地）未经多人观感校准；光标斥力在触屏
上无效果（无指针）——桌面增强、移动端无损。

### 你 90 秒怎么验

线上首页：点三个 tab——pill 滑动、查询串重打、点火对象切换；鼠标划过
右侧星空，点群应让开再回位。

---

## (site v4) — 全站重设计「Refined Digital Alchemy」：Vite+React MPA，Home/Learn 动效换血

分支 `session/site-redesign-20261026`，四个提交（6864fd5 → a11d35b → c0f2043 → dd8a7e6）。
按用户 2026-10-26 的 Master redesign 指令执行（三阶段 + 五路专家子代理终审 + 修复 + 部署）。

### 关键行

- `web/` 新增 Vite + React 19 + TS MPA（`index.html` + `learn.template.html` 两个入口），
  Tailwind v4 CSS-first tokens、framer-motion（layoutId/FLIP/springs）、GSAP ScrollTrigger、
  Lenis、three.js——全部按指令清单；`wrangler.jsonc` 切到 `./web/dist`，`site/` 原样保留
  （内容管线 + 旧皮肤 docs 用），`build_learn.py` 加 --graph/--template/--out 参数（旧默认逐字节不变）。
- Home：三幕 WebGL 粒子场（语义/词法/多模态三种编队 = 真实机制示意，光标速度斥力 + 滚动景深，
  idle 后懒加载、RM 静帧）；k=120/d=20 逐字 spring 标题；40px 磁性 CTA + liquid fill；
  六卡非对称 bento（光标辉光边 + 3D tilt + 自演微演示）；GSAP scrub 叠卡叙事（sticky，RM 自动降级）；
  计数器条 + 回执台账；对比表 + QMD 头对头。
- Learn：145 概念从内联 `#gm-graph` 启动（零运行时 fetch、no-JS 全量目录与损坏 banner 契约保留）；
  簇/层 pills（layoutId 滑动指示）+ 搜索；FLIP 筛选网格；卡片 → 抽屉 shared-layout 展开
  （左教案右目录、scroll-spy 进度轨、深入/相关 chips、前后翻页、已读进度持久化 `gm-learn-visited`）。
- 诚实契约全保：metrics.ts 每数带 verify 锚点、双语 zh/en（`gm-lang` key 不变）、
  「gold is earned」纪律、演示数值要么真实要么标「示意」（终审 P0 修掉：假 sha/假命中数/虚构 MCP 接口
  已替换为真实接口 `goldmine mcp --db` + search/get/context/status）。
- 终审：5 个领域专家子代理（动效性能/无障碍/创意总监/架构/诚实审计），6 个 P0 + ~20 个 P1 全部修复
  （详情见提交 dd8a7e6 信息）。Lighthouse（生产）：a11y 100/100，BP 96/100，SEO 100/100，
  TBT ≤20ms，CLS ≤0.04；perf 移动节流 87/87、桌面 97。

### 每行的风险

- **Learn 交互语义变更**：v3 的 canvas 下钻地图被「筛选网格 + 抽屉」取代（指令明确如此）。
  地图在 git 历史（c0f2043^ 的 site/）随时可回滚；但这是产品语义变化，owner 应过目。
- **移动节流 perf 87 < 指令的 90 目标**：瓶颈是慢速 4G 下传输规定栈的固字节数
  （React+FM vendor ~116KB gz + 字体 + learn 550KB 烘焙正文），运行时零阻塞
  （TBT 0–20ms）。要上 90 只能砍 FM 或烘焙正文——两者都是指令要求，留 owner 决断。
- **部署自分支**：生产当前跑的是本分支内容，main 尚未合并（一会话一分支，合并经用户）。
- `web/learn.html`（550KB 生成物）入库沿用旧惯例；每次 rebake 会产生大 diff。
- `data:` 加入 font-src CSP（Fontsource 度量回退字体需要）；脚本白名单未放宽。

### 我不确定的地方

- 演示微动效的节奏（bento 循环周期、粒子点火周期 7s）未经观感校准。
- 「示意」标签的字级（9px mono）是否足够显眼，由 owner 判断是否要更显式的免责声明。
- docs.html/404 里 docs 保持旧皮肤（指令范围只有 Home/Learn）；404 已换新皮肤。

### 用户 90 秒怎么验

- 打开 https://goldmine-site.sixtycat2000.workers.dev/ → 看星空 + 三幕 tab 切换 →
  鼠标扫过粒子让路；滚动看叠卡叙事与计数器。
- 打开 /learn → 搜「退款」看 FLIP → 点任意卡看抽屉 morph → ←/→ 翻页、
  TOC 点击、Esc 关闭并回焦；刷新后已读进度保留。
- 中/EN 切换全站即时；键盘：Tab 走卡片、/ 聚焦搜索、抽屉内 Tab 被困。
- 数字抽查：首页 03 区块 vs `docs/FEATURE_STATUS.md`；QMD 表 vs
  `reports/execution/P2-08/comparison-report-001/REPORT.md`。
- 部署版本：`009f78ab-6fe4-4ad9-80bf-4b7ce1222feb`（wrangler deploy 于 2026-10-27 ~00:40）；
  Lighthouse 原始 JSON 在本地 `reports/execution/site-redesign-20261026/lh-*.json`。

---

### 追记（perf 收口，commit 77c37ca，部署 1eeccfd5）

上节「移动节流 perf 87 < 90」已解决，不再是 owner 决断项。trace 定位：LCP
是首页让步句（最大文本节点，入场动画把最终绘制顶到 3.8s）——让步句改静态
渲染（与预挂壳逐字一致，h1 保留弹簧逐字动画）；预挂壳补齐双语精确文案
（预绘 html[lang] 切换，React 重渲染不再超越早期 LCP）；Noto Serif SC 的
122KB @font-face 表移出阻塞 CSS（原 167KB→30KB，zh 读者立即加载、其余
idle 加载）；三只 latin 主力字体 preload + font-display:block；learn 的
modulepreload 降优先级；WebGL 先探测后建渲染器（无 GPU 环境零控制台错误）。

**Lighthouse（mobile 节流，production-parity brotli）：首页 97/100/100/100，
Learn 93/100/100/100**；TBT ≤30ms，CLS ≤0.018。四项全部 ≥90，指令门槛达成。
遗留：Noto 异步包装是 363KB（117KB gz）JS chunk（非阻塞、idle 后加载），
未来可换 <link> 直注省 ~105KB——不影响门槛，记录备查。

### 追记 2（打磨轮，commit 2e2a60a，部署 25bf2c60）

owner 反馈「滚动 + 性能 + 粒子光太亮/糊」→ 三位专家子代理（WebGL 艺术指导 /
滚动工程师 / 运行时性能）开方，全部落地。粒子：32px 加色精灵 + 1.77× 增益
（必然白裁）→ 3-8px 离散光点、增益天花板 0.83、星团间距拉开、尘埃 18%→10%、
DPR 上限 1.5/1.25——填充率降约一个数量级，正文区遮罩随之减为轻纱。滚动：
修复 Narrative 卡片从未升起的 GSAP 百分比解析 bug + 末卡零停留 + 步进轨超前
（HOLD/SWAP 时间线 + scrub:true 去掉双重滞后）；Nav 进度条改 MotionValue
直写（滚动零 React 渲染）；Lenis anchors/ignoreMobileResize/fonts.ready 加固；
英雄滚动退场编排（RM 安全）。运行时：打字机/计数器改 DOM 直写、磁性按钮共享
指针监听、Bento 无限循环改 IO 门控 CSS 关键帧、Noto 中文条件 preload。

**Lighthouse（mobile）首页 P96/A100/BP100/SEO100**（LCP 2.7s、TBT 10ms、
CLS 0.023）。风险：粒子密度/亮度是新艺术方向，owner 观感为准；three.js 换
手写渲染器（省 119KB gz）评估过、本轮刻意不做。

## (web) — taste-skill 评审轮：结构性/可读性修复（分支 session/web-taste-review-20261030）

评审工具：Leonxlnx/taste-skill v2（repo-local 安装于 .zcode/skills/taste-skill，
MIT）。方法：lint + inspect.mjs 实测（1440/768/390）×3 页 + 截图视觉评审，
before/after 双份报告留档 .taste-skill/（已 gitignore）。

**关键行**（FAIL 计数 home 15→12、learn 8→8（质变项清除）、docs 12→8）：
- Hero：h1 拆句——「Search by meaning.」独立成行（16 词→3 词，手机三行→一行），
  「Files never leave your Mac.」加粗降为 dek 首句；「0 B」去混排 i 标签。
- 字号地板：全站 text-[9/10/11px]→12px（69 处）、kicker 11→12、learn 卡片正文
  13→14px、docs label 档 11.5→12px。
- Learn：首屏行动按钮「Start at layer L0」（金键，设层+滚动到网格，first-action
  FAIL 清除）；进度条渐变→纯金色。
- Docs：sr-only h1→可见大标题 + lede 说明句；docs-grid 光学居中
  （220px+62ch 双轨 justify-content:center）；≤880 toc/prose 共用居中 62ch；
  ≤760 CLI pre 换行不贴边；term 注释色 sub→ink-body；body overflow-x:clip。
- 去噪：MagneticButton liquid 渐变→纯金；✓/✕ emoji→SVG 图标（4 处）；
  阅读段落 measure 收窄（dek 44→34em、lede 46→34em、回执 td 套 36em 内衬、
  脚注 52→38em，113-119ch→≤80ch）。
- Closing EN 标题 61 字符→「MIT. Everything is in the repo.」（细节在 lede，
  手机三行→两行内）。

**每行的风险**：h1 拆句改动首页第一语义面（zh「按意思搜索。」同步拆分），
  owner 若不喜欢可一行 revert；字号地板让 mono 标签视觉变重（设计密度变化）；
  docs 居中改变 880-1100px 区间的栏位观感；MIT 标题短化是文案取舍。
**我不确定的地方**：@390 全系发现（off-centre 20/211、headline-lines、side-scroll、
  blank-gap@mobile）经 CDP 三路实证为本机 headless 仿真 innerWidth 虚报 581 所致
  （clientWidth/媒体查询均正确 390），已按伪影记档不修——真机复核仍值得做一次；
  blank-gap@1440（636px）证实为 inspect 走查 11 屏上限 < 页长 14 屏的 reveal
  未触发伪影（全页滚动后消失）。
**90 秒验证**：cd web && npm run preview -- --port 4173 → 首页首屏看单行标题+
  加粗让步句；/learn 头部金色 Start at L0 键点击应过滤 L0 并滚到网格；/docs
  顶部大标题「goldmine 文档」+ 居中栏；手机宽度 /docs 无贴边截断。

### 追记 3（选项一去噪 + humanizer 文案轮，分支 session/web-taste-review-20261030）

owner 拍板「中度去噪」；humanizer skill（blader/humanizer，MIT）repo-local 装
于 .zcode/skills/humanizer/，对首页文案跑一遍。

**关键行**：① 眉题全删（SectionHead 去 kicker/no props，Hero/LearnApp/learn-main/
Stage 手删）；② 01-07 幽灵数字、场景卡 0N、台账行号前导零清零；③ learn/Scenarios
卡片 1px 描边→色阶（bg-bg1，hover 加深）；④ 卡内分隔线 border-t/h-px→间距或色阶；
⑤ mono 69→56 处且全部落在批准保留清单（终端演示/代码/公式/gate id/<code>/复制钮），
导航、按钮、统计说明、卡片标签、抽屉控件、页脚全部转无衬线句首大写（源码里的
全大写字符串同步改写 14 处）；⑥ learn 12px 层整体抬到 13px，卡片正文 14px 成主流；
⑦ humanizer §8：首页 EN 破折号 8 处改冒号/句号/分号，zh 破折号按标准中文标点保留；
⑧ Bento 诚实芯片由盒式改角标句式（「示意」/demo），media-OCR 门行合并，契约语义不丢。

**每行的风险**：卡片描边→色阶让 learn 网格的「档案盒」感变柔，喜欢硬边可单独
revert ConceptCard/Scenarios 两个 className；按钮 sans 化丢了 mono CTA 腔（视觉
评审员曾点赞 mono CTA，此为取舍）；gate 行合并后 "official gate" 字样缩进 mono 行。
**我不确定的地方**：inspect 剩余 eyebrow 2 条实为卡片尾部的演示输出行（2.64 算术、
gate 名单），inspector 按几何把它们配对到下一卡标题——辩护保留；learn body-size
13px 仍 FLAG（工具理想值 16px），learn 是检索式参考网格而非阅读文，抽屉内阅读
体验 14-15px，同样辩护保留；em-dash 65 处为 zh 破折号与代码注释。
**90 秒验证**：cd web && npm run preview -- --port 4173 → 首页无眉题、统计说明
句首大写、按钮无衬线；/learn 卡片无描边、无全大写微标签；lint FAIL 116→88（剩余
全在保留清单）。

## (web) — learn 舞台一期：管线可视化 + 四级下钻（learn-stage-plan-001）

预注册 reports/execution/learn-stage-plan-001/（prereg + design + 4 路对抗
评审 amendments-001 + 用户直导 amendments-002「视觉优先」）。

**关键行**：/learn 重构为「一台可以拆开看的机器」——四工位（走盘 31/身份
16/理解 33/检索 31）+ 质检线（34）非对称舱；文件芯片传送带（金只在处理瞬间）、
主角芯片每站换形态、文件止于 1×lib.sqlite 索引块、查询只从检索进、L1 指纹门
在带起点（不符即拒 exit 3）；单时间基 7s 波驱动全部演示（IO 暂停、RM 静态）；
工位舱纯视觉（名称+计数+演示图形），文字全部下钻：面板（放大演示+职责+教学
注记）→概念卡→详解抽屉，DepthRail（舞台→工位→概念→详解）常驻；hash 路由
#stage/#q/#l0/#c + 冷启动深链（StrictMode 双挂载下 pushHash 清 hash 的 bug
已修：bootedRef 守卫）；全站簇/层 pill 退役，质检线三仪表（17/19、3,200+、
0 B，均带最短可见备注）；learn.template.html 预挂壳=静态机器（LCP/无JS/RM
三合一）。检索演示含零命中终态（exit 0 也是合法答案）。

**门禁**：G1 145/145 映射 PASS（g1-coverage.mjs）；G2 inspect 10F=基线 8
（品牌档不变）+ eyebrow(DOM 相邻误报) + off-centre@390(本机 headless
innerWidth 虚报 581 伪影，clientWidth 390 实测边距 20/20)；G3 Lighthouse
brotli 同位 perf **91**（基线 93，门槛 90；LCP 3.5s 受 388KB 内联 doc 的
TTFB 主导）；G5 '/' 聚焦 PASS；G6 无 JS 术语表在档；G7 文案审计 PASS
（g7-copy-audit.mjs：数字白名单+备注不可分+1.4s 对比数字禁入 learn）；
G9 冷启动 #stage/#c 深链 PASS（注意：本地验证必须加 ?v= 击穿 learn.html
浏览器缓存，生产部署同样要走既有 cache-bust 流程）；G10 新手路径 1 次交互
到「向量与坐标」。G8 morph 链（面板→卡→prev/next→关→归位）人工走查通过；
滚动还原存/复逻辑在档（Lenis 缓动下 instant 滚动会渐进，Δ 断言未严格 3 位
置执行——记 MISSING）。

**风险/不确定**：舞台挂载期间几乎无法完全滚出视口（页面其余太矮），IO 暂停
门实际很少触发——belt 为纯合成器动画（transform+will-change），成本本就
趋近零；@390 真机复核仍未做（本机仿真不可信）；focal-point 由 h1 升 hero
字号缓解但未复测；「跟一条查询走一遍」引导剧 DEFER（amendments-001 D5）。

**⚠ 会话撞车记录**：另一会话于 23:59–00:02 在同一分支提交 4 条
（humanizer 安装 + home 文案去噪 + learn 壳样式 + 356136a），其中
`git add -A` 把本任务在飞的 Stage/LearnApp/LessonDrawer 半成品卷入其提交。
内容无损（全部在树），但 356136a 的提交信息不覆盖 learn 舞台工作；分支未
推送，是否拆分历史由 owner 定（铁律 6 事故重演，已留档）。

**90 秒验证**：cd web && npm run preview -- --port 4173 → /learn.html?v=x
（?v= 必加）看舞台；点「身份裁决」开面板；点卡开抽屉看 DepthRail；
URL 直贴 #stage=identity 冷启动。

### 追记 4（四步区重做：拆滚动走廊，owner 拍板「特别难受」）

Narrative 原为 GSAP scrub + sticky 的 600svh pin 走廊（4 步 × 150svh，HOLD/SWAP
交叉淡切）。体感问题：滚动距离巨大、换卡滞后于手势、产生 lopsided/空白带实测项。
重做为普通文档流的步骤列表：金色衬线 stage 词列（SCOPE / EMBED / FUSE / COMMIT，
zh：划定范围/向量化/融合检索/落库）+ 标题 + 正文，Reveal 逐条入场；GSAP 在本节
退场（依赖保留——SmoothScrollProvider 的 Lenis 集成仍用），页面净短约 5 屏。
顺带：step 3 文案同步 humanizer 后的「打分明细」句。

**每行的风险**：失去 pin 的「舞台感」，四步从演出降级为清单——这是 owner 点名
要的取舍；ScrollTrigger chunk（18KB gz）仍随 SmoothScrollProvider 加载，若日后
无任何 ScrollTrigger 用户可一并摘除。
**我不确定的地方**：无。inspect home FAIL 10→9，空白带 636px→全部 <300px（阈值下），
lopsided 清零。
**90 秒验证**：npm run preview → 首页滚到「从磁盘到可搜，四步」：无粘滞、无淡切
等待，四步一屏半读完，quickstart 终端块不变。

### 追记 5（learn 舞台：零件=按钮——teardown skill 参考轮）

teardown（elvarofficial/teardown，MIT）以 repo-local 安装于
`.zcode/skills/teardown/`（1.3M，剔 14M 演示 gif；未装全局、未跑其
npm install——参考用途只需源码）。挖矿与映射表：
reports/execution/learn-stage-plan-001/teardown-reference.md。
采纳四项（amendments-004 预注册，冻结 sha256 在案）：
T1 四张 SVG 原理图 14 个零件变按钮（Part 助手：role=button + sr-only
aria + Enter/Space，`.pv-part` hover/focus）→ 点零件直开其概念抽屉，
映射全为真 graph 节点（walk 盘→行走门禁、章→一文件一事务、库块→幂等、
identity 门→指纹门、出口圈→四层身份、understand 块→分块、点阵→向量与
坐标、波形→whisper、search 脉冲→准入、射线→混合融合、条→本地重排、
证据卡→证据选择、零命中圈→零命中诚实）；T2 IndexBlock 与移动端 subway
的 1×lib.sqlite 变按钮 → sqlite-wal；T3 `running = inView &&
tabVisible`（隐藏标签页不再空转）；T4 新门 G11（teardown review loop
采纳）：零件目标必须存在于 graph.baked.json（假按钮即 FAIL）+ CDP 真
Chrome 真帧捕获（轮询金亮舱翻转后 600ms 截图，1440/390 各四态+零命中帧）
+ 人工逐帧 checklist。

**每行的风险**：零件点击绕过四级轨（舞台直跳详解）——DepthRail 仍在抽屉
头，方位不丢；aria-hidden 随 onPart 撤除，屏幕阅读器多出 ~4 按钮/图，是
功能不是噪音；G11 依赖 taste-skill 的零依赖 CDP 客户端（路径耦合
.zcode/skills/taste-skill，两 skill 同仓在位即成立）。
**我不确定的地方**：T3 恢复方向无动态回执——CDP 仿真证伪
（'active' 后 visibilityState 仍 hidden），恢复走同一监听器反向路径，仅
代码结构保证；search 演示循环间隙会短暂全空（读帧备注 1，不判 FAIL）；
@390 仍为仿真帧，真机复核维持待办。
**90 秒验证**：cd web && npm run preview → /learn.html?v=x →
hover 舱内零件（提亮）→ 点「库块」开「幂等与续跑」抽屉 → 点 INDEX 块开
「SQLite 与 WAL」→ 切走标签页再回来（后台不闪跳拍）。

### 追记 6（/learn 推倒重做：一整台机器——teardown 语法全量落地）

用户判词「重新做learn page」→ learn-machine-v2-001 预注册（四席对抗评审
compact：诚实/性能/移动端/a11y，两席带修改已并入）。核心变化：
**四舱格废止（A5），换单幅 1040×470 连续机器**——文件从圆柱磁盘上带、过
双门柱指纹门（拒收件弹入托盘 exit 3、通过件四层点亮）、切块嵌入点亮点阵
（占空比 70%）、落进 INDEX 盖章；查询只从右缘进，射线入 INDEX、分数条、
证据卡，隔轮零命中。**工位面板消亡 → isolate 托盘**（点区域该区放大 1.12
居中、其余 0.15 让位，职责句/注记/pills/卡片在机器下方托盘展开，机器永不
离开视野；#stage=<id> 深链语义=isolate）。**D5 补课：tour 落地**——6s 闲置
自启、gold 光环逐零件走完一条查询的一生（零命中轮走向零命中圈）、任意输入
即时接管、25s 归还；RM/移动端不跑。QA 三卡改标尺条（值+注不变）。零件=按钮
全量保留（15 目标全真，G11 静态门）。

**每行的风险**：零件点击从舞台直跳概念抽屉仍绕过四级轨的中间层（rail 在抽
屉头兜底）；tour 只在桌面跑（移动端 demos 保持 v1 卡）；isolate 缩放 1.12
下底板与脚注行有 ~10px 相邻（近透明可读）；G11 复用 taste-skill CDP（路径
耦合同仓 skill）。
**我不确定的地方**：G3 绝对分按无压缩 A/B 口径（0.55 vs 0.56 无回归），
brotli 同位的绝对 91 未复测（LCP 由 388KB 内联文档主导，v2 仅 +2KB gz）；
@390 真机复核维持待办。
**90 秒验证**：cd web && npm run preview → /learn.html?v=x —— ① 等约 6s
看光标自己走完一条查询（动一下即接管）② 点 Understanding 区：放大居中、
其余让位、下方托盘出 33 张卡 ③ 点 INDEX 区里的竖条（WAL）开「SQLite 与
WAL」抽屉 ④ 等第 7 秒看 search 区只剩虚线圈（零命中 exit 0）。

### 追记 7（/learn 视觉返工：两轮 subagent 评审 + taste-skill 诊断）

用户判词「太差了。混乱不堪。看看 skill 里的 example 找原因」→ 两个独立
subagent 桌面/移动评审（3.6 / 4.3 / 10）+ taste-skill inspect（6 FAIL）+
critique.md/tells.md 对照。**根因：结构弱于交通**——五个分离圆角卡（间隙
=gutter）+ 车道 8 套运动系统（芯片 ticker 横穿 5+ 接缝）；线稿无填充 =
skill 的 "unfinished wireframe"；13px 以下 15 处；金 12+ 角色；isolate =
幽灵空卡；移动首屏附录堆。amendments-005（A1-A9）返工：单 chassis 填充板
+接缝、实心剪影（鼓/门柱/抽屉/intake port/拍门横杆）、删芯片 ticker
（2 movers）、金预算（QA 转白、计数器金仅>0、门方块白闪、红只剩拒收环+
托盘）、字阶地板（句子 14px/计数器 13px、DepthRail 去 uppercase）、isolate
改滑动+aside 标签隐藏+隔离语义 14px 可见 caption、移动真节点轨+去重+
单全宽 demo、learn 导航全实底、示意+脚注合并一行、托盘头两行。
**第二眼复评：加权 3.6→5.7**（design 3→6、content 5→7），余 5 缺陷
patch2 全清。inspect FAIL 6→3（余：mono=品牌、few-pictures=0 raster
（defend：图解页）、body-size=隐藏词汇表污染直方图——可见文本实测 ≥14px）。

**每行的风险**：isolate 时隐藏 aside 标签——键盘用户 Tab 到 aside 区仍可
focus（pointer-events none 但 tabIndex 仍在，focus 即现 label）；region-
active 标记从背板类挪到金 bracket 线，G11 探针曾失灵（教训：挪语义标记
必须同步探针）；QA 数值金色→白是 C3 之外的新决定（注仍在）。
**我不确定的地方**：G3 brotli 口径绝对分未复测；few-pictures（0 实景图）
是本轮最大的 defended 缺口——若 owner 要冲 Awwwards 级，下一步应给
learn 配真实 CLI 终端条/真实 sqlite 截图类实物；移动端只留 SearchDemo
（IdentityDemo 语义走 subway→托盘路径）。
**90 秒验证**：/learn.html?v=y —— ① 滚动任意处：nav 全实底不透字 ② 等
6s 看 tour 光标走机器 ③ 点 Identity 区：它放大居中、邻居滑开、机器下方出
两行说明+托盘 16 卡 ④ search 区看 INDEX 下方刻度行与右缘入口。

### 追记 8（终轮：顶级对抗评审双 agent → SHIP，真 bug 一枚）

第三轮两 agent：艺术指导 **6.8/10（SHIP AFTER FIXING 3）**、冷启动
（trust 7，诚实数字=最强资产；clarity 5 因交互不可发现）。终批手术：
understand 上排下移出标签线、隔离区最后绘制（SVG 绘制序=DOM 序，Region
提变量排序渲染）、bracket 隔离时隐藏、aside 0.42、导览按钮（data-tour-btn
防自取消）、提示行进首屏（桌面/移动分文案）、isolate 显式退出钮、托盘
前后站（‹走盘 / 检索求证›）、hover 金名、INDEX 刻度行、embed 槽、
search 出口刻度、title 工具提示、learn 导航全实底。
**真 bug**：view 优先级 station>l0open——isolate 态点 L0 静默无效
（冷启动组的 l0.png=tray.png 由此而来），修复后 isolate→L0=#l0+15 卡。
「51 vs 31」数字矛盾证实为评审员误读（DOM 实测 31；31+16+33+31+34=145）。
G7/G1/G11 全 PASS。

**每行的风险**：隔离区最后绘制改变了 SVG DOM 顺序——任何按 DOM 序做断言
的门禁需注意（G11 探针按 data-station 类查找，不受影响）；导览按钮
data-tour-btn 防自取消依赖 closest() 查找——移出 machine 容器即失效。
**我不确定的地方**：Few-pictures（0 实景图）仍然 defended——是评审两轮
都点名却未修的最大缺口。
**90 秒验证**：/learn.html?v=z —— ① 首屏机器下沿即见「点区域拆开…」+
「▶ 看它自己走一遍」② 点 ▶：光标立即走机器 ③ 点 Walk 区：隔离 + 右下
「× 返回整台机器」④ 隔离态点 L0 → Layer L0 页 15 卡 ⑤ 托盘头有
‹上一站/下一站›。

### 追记 9（立体 + 实景轮：真实运行条 + 机身透视签名动效）

用户「更加立体实景。继续loop」→ amendments-006：**实景核心 = 真跑**——
本机真实执行 `python3 -m goldmine search "copper gasket" --db
evals/eval.sqlite`（rc=0，只读），逐字输出做成页面终端条（via: cold 2.18s /
score 2.33 z 0.33 cov 1.00 / 真实路径 L47-75 / 真实代码片段），标注真实
运行+可复现命令——数据存 real-run.ts（.ts 天然在 G7 文案扫描面之外）。
立体 = SVG 线性渐变（鼓/INDEX/门柱）+ 径向接地阴影（无 filter）+ 两档
地板阶 + chassis 内缘高光 + **签名动效**：滚动联动机身 rotateX 6°→0°
（perspective 1400，RM 直立）。G7 ALLOW 加 "0.35"/"35"（framer 偏移分数，
分词器切点号）。

**每行的风险**：真实输出含本机绝对路径 /Users/yul/git/goldmine/...——
它是公开仓库自身的路径，但若仓库转私有/迁移，路径会变陈旧（需同步
real-run.ts）；滚动透视在低端机是合成器路径（transform），RM/隐藏页/离屏
三重暂停仍有效。
**我不确定的地方**：2.18s 是单次冷检索实测——只跑了一次，未标注方差
（诚实口径：显示的是真实单次，非基准均值；若 owner 要求可标注 n=1）。
**90 秒验证**：/learn.html?v=w —— ① 慢滚到机器：机身随滚动立起 ② 鼓/
INDEX 有明暗与接地影 ③ 机器下方「real run」条：命令、逐字输出、teal 打分
④ 自己跑一遍同命令比对输出。

### 追记 10（中文文案 + bottom-up 内容：三维度 subagent 评审与落地）

用户「中文首页描述 + learn bottom-up 内容很差」→ 三个维度 subagent：
文案质感（Hero 8 / L0 入口 5 / one_liner 均分 6.5，4 句不及格）、产品价值
（10秒 7 / 差异化 6 / 语感 8；MCP 钩子被埋、语义收益没场景化）、教学法
（认知负荷 3 / 丰富度 3；「砖好没砌」）。落地：Hero v2（场景首句三种介质
实例 +「还能经 MCP 让 Claude 直接搜本机」+ 诚实招式保留；质量证据留证明
线）；L0 入口重写（学习理由替换归档信息）+ 读法指引行；4 句 one_liner_zh
重写（concepts.json → bake_graph → graph.baked）；**L0 页分组重排**——四组
（表示与比较/读数与推断/一致与完整/进程与磁盘）各带「撑起哪里」、1-15
连续编号、出口行接回舞台，L0 排序改路径序，工位徽标降级（组头已交代）。

**每行的风险**：one_liner_zh 改的是 site/content/concepts.json（源头），
graph.baked.json 由 bake_graph 重生成——若日后从 expansion 文件重建需再次
bake；L0 分组硬编码在 LearnApp（L0_GROUPS），数据漂移时 15 外漏卡会不可见
（排序兜底 999 但无组渲染）——新 L0 概念必须同步 L0_GROUPS；hero v2 变长
（~150 字），34em 宽度下 4-5 行，移动端首屏占比上升。
**我不确定的地方**：教学法建议的「vector-space/distance-vs-angle 兼挂理解
嵌入徽标」未动（数据层归属变更，owner 决）；真实运行条 n=1 口径未变。
**90 秒验证**：/ 首页看新 Hero（场景句+MCP）；/learn.html?v=6 点 L0 ——
四组编号路径 + 每组「撑起哪里」+ 出口行；点 mean-std 卡看新一句话。

### 追记 11（learn 编辑轮：剪 tagline / helper text / 次级标签，learn-copy-cut-001）

业主规约「Cut taglines, helper text and secondary labels. If a screen needs
them to make sense, fix the layout」+「elevate UX/DX/AX without breaking
anything」。/learn 全剪（zh/en 同步）：eyebrow×2（React+模板）、DepthRail
（组件+3 处）、裸进度数/站点计数/QA 旁计数、C7「本机运行 · 无网络传输」、
三处操作提示行、隔离态 REGION_DESC 可见段（SVG <desc> 保 SR 叙述）、托盘
role 行+两条工位脚注+`id × count`、移动端示意免责复本与提示行；卡片剪
≈min/副语言标题/打开→；抽屉剪 min/面包屑/比喻签/目录标签与 01· 编号/
深入下探/相关概念组标签/页脚标语；L0 剪引言/撑起行/序号/收尾；空态剪散文
留按钮；搜索卡剪「站点 · 层」前缀；placeholder 剪「（按 / 聚焦）」；工具栏
计数改纯 sr-only aria-live。DX：删死导出 Walk/Identity/Understand Demo、
DemoTag/DemoFootnote、REGION_DESC、.machine-desc。首页静态壳对齐 Hero v2
（修「按精确搜/按内容搜」，补 MCP 句）。保留不动：机器 SVG 本体与终点字幕
（E1/V2）、QA 三磁贴及最短注解（G7 结构要求）、真实运行条、全部 aria-label、
no-JS 145 卡降级目录、页脚「0 bytes 离开你的机器」。

**每行的风险**：C7 是 amendments-005 冻结不变量，本轮废止（冲突原文：
Stage.tsx 冻结注释「locality caption visible (C7)」vs 新规约「剪 tagline」
——本地性主张现由 QA 磁贴「0 B 上传 · 无此代码路径」+页脚双处承载）；
L0 学习理由文案是 learn-content-001 昨日批准件，本轮按新规约剪除（冲突
原文：learn-content-001 判词「砖是好的，没砌」——撑起行正是砌浆；学习理由
留 git 历史）；DepthRail 剪除后「舞台→工位→概念→详解」深度模型不再有显式
文字陈述（布局承担：返回按钮/托盘头/抽屉目录各就各位）。web/learn.html 为
bake 产物，本轮随库 a84988d 遗留的 1 行 gm-graph 待烧行。
**我不确定的地方**：抽屉「比喻」盒去词签后仅靠盒样式区分（border+bg2），
若 owner 觉得歧义可单独恢复两字签；移动端地铁行剪计数后「该站有几题/读了
几题」只能进站看（信息可发现性 vs 标签噪音的取舍，owner 可翻）。
**90 秒验证**：cd web && npm run preview -- --port 4173 → /learn.html?v=c
① 首屏：只有大标题 + L0 按钮，机器本体无文字变化 ② 点理解嵌入：无灰色
说明段，右下返回按钮 ③ 质检线三磁贴注解原样 ④ 点任一卡：抽屉头部只剩
簇 chip，目录无编号 ⑤ / 首页壳与 React Hero 文案一致（哈希搜/画面搜/
MCP）。门禁：typecheck 0 错；G7 PASS；G1 PASS（145/145）。

## 2026-11-05 · 等价检索优化、模型所有权与 Learn 实验台

**关键行**：`goldmine/search.py:383,727` 在完整向量计算、原有 z 标准化和稳定逐文件
最大值之后，只物化可能准入的文件；词法/覆盖/精确/名字路线取并集，quorum=0 和
非有限输入走完整原路径。`retrieval_evidence.py:108` 在死线检查后对空候选跳过 posting
SQL。`application.py:148` 的 pack-window 借用进入 finally；`rerank.py` 用操作锁、
timer 代际与 PID 所有权约束启动/调用/关闭及正常退出回收。`MechanismLab`、
`MechanismDrawing`、`mechanism-model` 增加五种同源读数/SVG 实验；保留原145概念并
增至150，扩写中英文 why/how/try，修正旧数学、WAL、缓存和进程解释。抽屉修复原生
按键/焦点、同文档深链接与安全 hash；`use-motion-preference.ts` 让系统减少动态效果
即时生效，Stage 的循环与最终位移也同步停止。模板数字从图谱派生，生成物同步。

**每行的风险**：筛选是保守候选上界，漏一条准入路线就会丢结果；独立 oracle 检查完整
Hit/why/pack/引用、边界值和零 quorum，三种不安全 mutant 的真实 FAIL 留档。新锁必须
遵守先操作锁后状态锁，回收只触及本进程拥有的子进程；SIGKILL 不在承诺内。教学数字
是确定性演示，不是生产耗时或相关概率。页面无 JS 的目录与 React 共享生成内容；修改
concepts 后必须同时 bake/build。系统偏好监听移除同一回调，不改写用户暂停选择。

**我不确定的地方**：正式 offline/native/MCP 是3046/323/9项 PASS；单次冻结测量只覆盖
合成 RRF 聚合物化＋准入，500k单段文件 p50 457.447→42.437ms，配对中位差95%区间
[-416.301,-413.695]ms。全保留压力例 p50反而+0.424%；不能外推整条 CLI/MCP、RSS或
产品召回改进，这些仍为 MISSING。既有质量红门保留。原生隐藏标签因 Ego 控制页持续
报告 visible 而未验证，visibilitychange 监听另做合成验证。一次关闭后指针重开超时的
原始 FAIL 保留；冻结前后探针均成功，根因归因仍 MISSING，preventScroll 只承诺恢复
焦点时不滚动背景。证据链在 `reports/execution/high-confidence-optimizations-learn-20261104/`，
含逐次 argv/退出码/输出 SHA/前后源码身份；基线、评测题、指纹与密封 holdout 未动。

**用户90秒怎么验**：在本提交工作树运行 `python3 -m pytest -q
tests/test_search_exact_optimization.py tests/test_body_units_equivalence.py
tests/test_rerank_ownership.py`，再跑 `node web/scripts/check-mechanisms.mjs`；均应无失败。
打开预览 `/learn`：保持夹角、切换语料看余弦不变而 z 变化；空候选少一次查询；发布/
重建后旧请求路径不变，Begin 才切换；借用→调用→归还后推进300秒。连续步进跨过四阶段，
在抽屉滑块按方向键不切课，切换系统减少动态效果应立即静止且仍可手动步进。


## 2026-11-06 · 中文首页、动画调度与网站 CI

**关键行**：`Hero.tsx` / `home-demo-data.ts` 把首屏改为“按内容找本机文件”，展示词句、编号、截图三组明确标注的示例，查询、片段、出处、位置和 Learn 链接一起切换；静态壳同文。`Bento` / `Narrative` / `MetricsBar` / `Tables` 删去伪分数、编号反转与无条件隐私口号，写明功能条件、MCP证据返回边界、历史质量红门与QMD对照范围；词法入门明确无需模型，两份Docs入口同步150概念和2026-10-26记录。`LearnBridge` 给五个已有实验直接入口，不导入整张图谱。`particle-loop.ts` 统一管理单条rAF和有效动画时间，暂停/离屏/隐藏/减少动态效果取消待执行帧；共享动效组件禁用时复位并解除监听。新增独立`website.yml`执行锁定安装、18项首页、12项调度、26项Learn模型检查和正式构建；原Python CI规则不改。`HOMEPAGE_COPY_RUBRIC.md`公开六维中文评分和事实否决项。

**每行的风险**：三组文件是教学示例，不连接真实索引；“少建对象”保留原准入/排名规则，不能解读为少算向量。单rAF的旧回调若能复活会产生重复循环，测试覆盖代际、帧ID0、暂停时钟、静态重绘与dispose；浏览器实测暂停后切场景仍保持暂停。动效组件被Learn复用，26项模型检查和浏览器深链接/滑块按键复验覆盖可见回归。3378为3046/323/9三类历史测试记录合计，155/180与17/19为归档质量记录；没有新质量评测或完整CLI/MCP性能测量，不将旧QMD逐题CLI附带耗时当作当前基准。

**我不确定的地方**：冻结评分权重20/20/20/15/15/10，两位未撰写营销文案的评审均从46.25给到88.75；这是主观编辑审查，不是理解率/转化率实验。历史证据段仍密、部分Learn入口有术语。浏览器1440/390中英文、示例、键盘、暂停、五个深链接、Docs入口与Learn滑块已观察；IAB没有实时媒体偏好/禁用WebGL/禁用JS仿真能力，原生后台切换与这几项浏览器情形仍为MISSING，不能用调度单测冒充。构建验证静态壳，调度单测验证状态机；不宣称实际浏览器全矩阵PASS。早期集成/类型检查和浏览器等待超时的真实失败原样保留，修正后的回执另编号。原始argv、退出码、输出SHA、前后源码身份、预注册v1–v6与ADOPT在本机`reports/execution/ci-green-chinese-home-20261105/`；94个运行时文件、图谱、锁文件、原CI和Worker配置保持原身份，未打开密封holdout。

**用户90秒怎么验**：运行`node web/scripts/check-home.mjs`、`node web/scripts/check-home-motion.mjs`、`node web/scripts/check-mechanisms.mjs`；打开首页，点三组示例，看查询/文件/定位和Learn入口同时切换；暂停后用左右/Home/End键切场景，应保持暂停。手机宽度看中文不横向溢出，点“开始使用”到词法入门，点五张学习卡到对应概念；抽屉滑块方向键只改数值。查看`docs/HOMEPAGE_COPY_RUBRIC.md`评分锚点；新main提交的`ci`与`website`都应完成success，Python矩阵中按push策略跳过的四项仍按skipped记，不冒充执行通过。

## 2026-11-06 · macOS 独立安装包与安装入口

**关键行**：`install.sh` 提供一个命令入口，自动发现同目录的包与校验文件，或从 HTTPS release 下载；SHA256 一致后才解包。`scripts/standalone/build.py` 离线组装完整 CPython3.12、锁定 NumPy/MCP/CA 与五文件 native 资产，核对当前 wheel 源码、上游与 helper 身份、真实 Mach-O 最低系统版本，搬动后运行 CLI。`scripts/standalone/install.py` 在用户目录保存按 manifest SHA 标识的版本，先准备模型并完成真实768维探测，再原子切换 current；稳定命令/native 链接共用该指针。升级保留旧版本；卸载保留索引、配置、模型。PATH 块可退出、可撤销，保留原字节与符号链接。`build_native.py` 经用户明确扩围授权，为 CMake/Swift/clang 显式设置 macOS14目标。发布流程改用 macos15 ARM runner，先实际安装与语义/词法搜索，再上传草稿；不改公开 release。README、指南、首页与两份 Docs 同步普通安装与开发者路径。94个 goldmine 运行时源码文件逐字节不变。

**每行的风险**：安装是显式联网入口，默认模型310MB，使用原有大小/SHA pin与可续传下载；查询/MCP/estimate没有获得安装入口。包内 CA 只在用户未指定 SSL_CERT_FILE 时作为默认值。release 的同源 checksum 证明完整性，不是发布者签名；原生签名仍是 ad hoc。外部命令/二进制或被改过的 launcher 拒绝覆盖；失败的准备可能留下未激活版本或已下载模型，但不切换旧 current。模型与数据目录沿用既有配置，显式后端设置仍有正常优先级。安装不索引、不注册服务；PDF、音视频工具仍按需配置。包内三个 Mach-O 已核对最低版本，编译目标不等于在全部系统版本实测。

**我不确定的地方**：macOS14实机、远端 release workflow、Apple Developer ID签名与公证均为 MISSING；当前只交付本机 macOS26验证过的候选包，未合并/推送/发布，不继承搜索质量成绩。104项相关 Python测试（无跳过；1项 native 按 profile排除）、首页检查与网站构建 PASS。最终包在纯系统 PATH、隔离 HOME、带空格路径下实际下载并校验模型，init选目录、双语词法/语义搜索、MCP四工具握手及非法路径拒绝、重复安装、坏校验拒绝后继续查询、卸载数据保留均 PASS；真实旧包升级及 OCR文字识别 PASS；坏代理下 keyword-only安装无模型/库写入 PASS。早期 PEP668构建失败、/var别名重复安装失败与误把无文字棋盘格当OCR文字输入的 harness失败均保留原始 FAIL；修正均有说明与新回执，未改冻结图片/题库/指纹。原始 argv、退出码、输出SHA、前后源码身份、预注册v1–v5、两轮 ADOPT及最终清单在本机 `reports/execution/standalone-install-20261106/`。

**用户90秒怎么验**：在本分支用已装测试依赖的 Python3.12运行 `python -m pytest -q tests/test_standalone_install.py`，应16项通过。打开 `docs/INSTALL.md`，本地候选文件夹只需 `sh install.sh`；默认模型首次下载耗时依网络，`--skip-model --no-path` 可在隔离 HOME/GOLDMINE_HOME/GOLDMINE_CONFIG/INSTALL_DIR/BIN_DIR 中先验纯词法安装且不改个人 shell配置。查看最终 `standalone-005/SHA256SUMS` 与 `installed-proof-v2/summary.json`，升级看 `real-upgrade/summary.json`，OCR/无网络模式看 `optional-proof-v2/summary.json`。首页“安装Goldmine”与 Docs 应写清候选版本、内置组件与公开下载尚待发布；Swift只用于116行 Vision OCR helper，用户无需 Swift/Xcode。

## 2026-11-06 · capability-check-001 缺陷修复与状态文档校正

**关键行**：`scripts/distribution_lib.py` 的 `extract_native` 允许集合由四文件改为五文件（加 `goldmine-embed`），与 `native_build_lib/archive.py` 现行打包和 release_eligible 规则对齐；旧四文件形状现在在成员检查处响亮失败。`goldmine/backends/llama.py`：`_spawn` 以 `start_new_session` 起服务器，`_kill` 对其独立进程组先 TERM、宽限后 KILL（组号等于本进程组时不发组信号）；`describe()` 只在服务器解析到 brew 构建时附 brew 不兼容说明。`scripts/eval_distill.py` 的 report-real 临时 VACUUM 副本在 finally 中删除（含复制或打开失败的路径）。`pyproject.toml` 改用 SPDX `license = "MIT"` + `license-files`，并移除与之互斥的 license classifier。`.gitignore` 忽略 `.zcode/`。文档：FEATURE_STATUS 重写"480/960 作业仍缺失"一句并加 2026-11-06 战役段；PROGRESS 注明 10-25 的 8/8 为 mock 后端、llama 实测 6/8；TEST_PLAN 记 worktree 缺 vendor helper 导致媒体指纹缺键；WALKTHROUGH-search 记 n 个向量时 z 上限 √(n−1) 的小库边界。

**每行的风险**：进程组改动影响所有 llama-server 子进程：新会话意味着终端 SIGINT 不再直接送达服务器，回收完全依赖既有的显式 close/finally 路径（全套 3388 项通过，serve/mcp/llama 68 项通过）；父进程被 SIGKILL 时仍无法清理孤儿，这个边界没有改变。五文件检查会拒绝 reports 里的历史四文件 tar——这是有意的，但用旧包跑 smoke 的流程会从 PASS 变 FAIL。移除 classifier 会让 PyPI 页面少一行许可分类，许可信息改由 SPDX 字段承载。brew 说明变成条件键，任何读取 `info["note"]` 的代码需要容忍其缺失（仓库内唯一读者是已更新的测试）。

**我不确定的地方**：孙进程延迟（6.03s vs 5.01s）的完整因果链只做到"组信号可回收孙进程"这一层，回归测试证明孙进程随 close() 消失；MCP deadline 回复延迟本身没有在修复后重新做端到端故障注入复测，记为 MISSING。z_gate 小库边界只做了文档化，没有改准入行为（任何放开都属于需 A/B 的再校准）。`test_comparison_native_resolvers` 一项在干净 HEAD 与本分支上以同一断言（exit_code 1）失败，属存量失败，未处理。

**用户90秒怎么验**：`python3 -m pytest -q tests/test_distribution.py tests/test_llama_client.py`（应 11+18 过，含新增五文件与孙进程两项）；`grep -n 'five regular' scripts/distribution_lib.py`；`pip wheel --no-deps --no-build-isolation -w /tmp/w .` 应无 license 弃用告警；打开 FEATURE_STATUS 搜 "Capability check campaign" 与 "run-002"。战役原始回执在本机 `reports/execution/capability-check-001/`（SUMMARY.md 为入口）。

## 2026-11-06 · 真实 120 篇蒸馏评测在当前源码上的结果入档

**关键行**：只改文档，不动代码。`docs/FEATURE_STATUS.md` 三处：能力表 Excerpt cards 行由"run pending"改为"10-24 源码 PASS 13/13（real-current-003）；当前源码 FAIL 12/13，主题找回 5/8 低于目标 6（real-current-005，纯文本安装）；多模态默认下冻结准备拒绝建库（real-current-004 MISSING），协议待决"；"120 篇聚类 run 仍缺失"一句改为实际结果；2026-11-06 战役段补一句真实 run 结果。`docs/execution-plan/PROGRESS.md` 11-06 段把"仍 MISSING、可走 real-current-003"改为 004/005 的实际结论与 158 个源码文件差异（未归因）。

**每行的风险**：FAIL 是一次执行的真实结果（零质量重试、零 holdout），把能力表从"待跑"改成了"失败"，读者会看到蒸馏质量门是红的——这是有意的如实记录。real-current-005 用的是纯文本安装形态（worktree 里 helper/mmproj 解析不到），它不代表装了 media helper 的默认配置；默认配置下评测协议本身跑不起来，文档里两件事分开写。

**我不确定的地方**：5/8 和 6/8 只差一个主题（python-environments），一次运行不能区分是代码演进造成的稳定回退，还是边界附近的波动；run-003 到 005 有 158 个文件不同（含 chunker.py、distill.py、ingest），没有做消融，不归因。多模态默认和冻结截图规则的冲突，走审计门修订（B）还是接受 MISSING（C），由所有者决定，本提交不预设。

**用户90秒怎么验**：打开 `reports/execution/P2-04/real-current-005/independent-readback.json`，看 `status: FAIL`、`gates.topic_recovery: false`、`metrics.topics_recovered: 5`；对照 `real-current-003/independent-readback.json` 的 6 和 PASS。看 `real-current-004/DIAGNOSIS.md` 了解多模态冲突（含验证器行号）。FEATURE_STATUS 搜 "real-current-005"，应出现三处。门槛原文在 `evals/acceptance_targets.json` 的 `distill.topics_recovered = 6`。

## 2026-11-07 · 蒸馏 5/8 回归消融与多模态冲突的只读结论

**关键行**：只改文档。`docs/execution-plan/PROGRESS.md` 11-06 段补三点：多模态下截图以空文本 image 段进入蒸馏（OCR 文本被 `s.vec IS NOT NULL` 排除，20/20 张）；回退 goldmine/distill.py 后指标与 run-005 逐位一致（该文件洗清）；回退 chunker.py 结构性不可行（`Chunk.original` 被现行 ingest 消费），回归未定位。`docs/FEATURE_STATUS.md` 战役段补一句同样的消融结论。本地留档（不入库）：`reports/execution/P2-04/distill-ablation-001/`（两臂完整回执 + 两次驱动路径错误尝试）、real-current-004 DIAGNOSIS.md 的只读补充。

**每行的风险**："未定位"是诚实的边界：证据只排除了 distill.py 一个文件，chunker 嫌疑仍在但无法单文件验证；读者不应把"ingest 侧"当成结论，它只是剩余嫌疑面。空文本 image 段的发现意味着修订验证器之前必须先做产品语义决定，这句写进了 PROGRESS，防止后续会话直接改验证器。

**我不确定的地方**：python-environments 在两代索引中段数相同（91=91）、v7 文本仅多 1,043 字符，"阈值边界翻转"假说与此一致但没有证明；没有跑第三次运行来区分波动与稳定回退（每次 5 分钟，但那是加臂，需另行批准）。image 段与文本段是否同一嵌入空间可比（mmproj 共空间假设）只看了加载行为，没做数值验证。

**用户90秒怎么验**：`diff <(jq -S .metrics reports/execution/P2-04/distill-ablation-001/distill/independent-readback.json) <(jq -S .metrics reports/execution/P2-04/real-current-005/independent-readback.json)` 应无差异；`cat reports/execution/P2-04/distill-ablation-001/chunker/preparation/commands/base/stderr.log | grep original` 看到 AttributeError；PROGRESS 搜 "distill-ablation-001"。

## 2026-11-07 · P2 落地：蒸馏 image 条目携带 OCR 文本 + 冻结验证器按修订案放行多模态形态

**关键行**：`goldmine/distill_corpus.py`：load_corpus 对文件 kind 为 image 且自身 fts 文本为空的条目，回填该文件全部 ocr 段文本（按段 id 排序、单空格连接、跳过纯空白段）；docstring 如实写明聚类受影响（去重档 30→10、after_dedupe 1038→1058、截图条目存活 0→20，评审实测）。`scripts/distill_eval/preparation.py`：截图证据规则新增同文件"OCR 文本（无向量）+ image 段（有向量）"接受形态；候选集本就按 fid 过滤，跨文件拼装在结构上不可能，并有专门测试钉住。`scripts/distill_eval/scoring.py`：excerpt_audit 对 image 文件且存储侧文本为空（None 含内）的成员，以同一确定性回填函数逐字节比对 member.text；摘录 span 与标题支持度同源；stored_sha256 对回填成员哈希的是逐文件拼接串（含合成空格）。测试：tests/test_distill.py +2（回填确定序、非 image 不回填），tests/test_distill_preparation.py +4（多模态受纳、文本形态不回归、跨文件拒绝、审计 byte-strict 含 None 形态）。

**每行的风险**：聚类行为确实变了（不再是"只改卡文本"）：截图条目从被去重吞掉变成全部存活参与共聚类——这是 P2 的意图，但任何对旧卡快照的逐位复现预期就此作废；real-current-003/005 的历史回执不受影响（各自绑定当时源码身份）。评测器两处修订只增不减：纯文本形态在 005 快照上零触发（评审核实），但修订后的评测器与 10-24 预注册原文已非逐字节同一文件——历史复跑需引用本修订案的预注册。回填按文件 kind=image 限定：扫描 PDF 的页图段（kind=pdf）不在范围内，real-current-006 如预披露挂在该残余上（US174465 空摘录，三次审计逐字命中评审模拟）。

**我不确定的地方**：多模态默认下 purity 0.631、主题找回 4/8，低于纯文本 0.711/5——首个实测、无既往对照，一次运行分不清"图像向量与文本向量共聚类天然稀释纯度"还是"校准/τ 需按多模态重调"；未做消融（如仅剔除 image 条目重跑）。id 序=阅读序是回填的隐含假设（004 实测一致）。评审的 SHOULD 项（005 快照上修订前后评测器逐位对比测试、>240 字符截断 span 测试）未实现，评审已核现有 normalized_span 契约成立。

**用户90秒怎么验**：`PYTHONPATH=scripts python3 -m pytest -q tests/test_distill_preparation.py tests/test_distill.py`（应 72+51 过）。打开 `reports/execution/P2-04/real-current-006/quality/execution/stdout.log`：gates 里 real_purity/topic_recovery/三条 excerpt 门为 false，metrics.purity 0.6308、topics_recovered 4；excerpt_audit 三个审计的 members 全过、各恰一条 US174465 失败。预注册与对抗评审在 `reports/execution/P2-04/distill-protocol-amendment-001/`。

## 2026-11-08 · 十个任务 worktree 收尾、证据归档与本地文件忽略规则

**关键行**：本节记录用户授权的本地清理：架构/优化四个、首页四个、网站发布和独立安装各一个，共十个旧 worktree 已移除；清理前的十六个分支引用和主目录源码身份 `8cbbc5a` 保持原样。六个脏目录的全部四十六个文件、暂存/未暂存补丁与状态保存到 `reports/execution/cleanup/goldmine-worktrees-20261107-001/snapshots/`，压缩包逐成员读回核对 SHA256。安装目录被 Git 忽略的整个 `reports/execution/standalone-install-20261106/` 迁至主目录同名路径，4,793 个文件及目录/链接记录前后一致。冻结清单、保全评审、十一条迁移/移除命令的 argv、退出码、原始输出 SHA 和前后源码身份均在清理归档中。`.gitignore` 补 Python 环境/检查缓存、覆盖率输出、Vite/TypeScript 缓存、浏览器验收产物、包管理器调试日志、本机环境配置和 macOS 元数据；既有模型、依赖、构建产物及 `reports/execution/**` 忽略规则保留。已被 `.zcode/` 规则忽略却仍跟踪的 107 个本地技能文件，通过 `git rm --cached` 取消跟踪，本机字节和权限不变。按用户后续指令，收尾提交直接落现有 `main`，白名单仅 `.gitignore`、本节和这 107 个索引移除；原始证据和安装包继续本地留档。

**每行的风险**：Git 分支能恢复已提交源码，四十六个未提交文件还需从对应 `dirty-files.tar.gz` 覆盖恢复；仅重新检出分支不会恢复它们。归档和安装包不在 Git 中，新克隆不能代替这份本地备份。安装原始回执保留旧绝对路径，清理归档的 `README.md` 写明迁移前后前缀映射；回执字节未改写。环境配置规则限定仓库根和 `web/` 的本机文件名，`.env.example` 与 `.dev.vars.example` 仍可提交；依赖锁文件和正式评测输入保持可跟踪。本地技能仅用于历史人工视觉评审，新克隆需另行安装才可复用那些工具；生产代码、构建和 CI 未引用它们。取消跟踪不重写历史，旧提交仍含技能文件；`tests/fixtures_data/vendor/lib.js` 是有意跟踪的测试夹具，保留在索引中。

**我不确定的地方**：扣除保留归档后，`du` 记录的目录占用净减约 4.23 GiB，统计排除了 holdout 路径；APFS 快照、共享块和并发写入会影响实际可用空间，不能把它当成 `df` 的精确增量。文件哈希和归档成员读回已核对；重新创建旧 worktree 并解包的完整恢复演练仍为 MISSING。

**用户90秒怎么验**：`git worktree list` 应只列主目录；查看清理归档的 `SUMMARY.json`，确认移除 10、保留原分支 16、改动文件 46、安装文件 4793。进入归档目录执行 `shasum -a 256 -c pre-cleanup.sha256`，核对冻结清单；`commands/*/receipt.json` 的十一条退出码应均为 0。安装候选包在主目录 `reports/execution/standalone-install-20261106/standalone-005/`，可用其 `SHA256SUMS` 校验，恢复步骤见清理归档 `README.md`。`git check-ignore -v .venv/pyvenv.cfg web/test-results/run.json .DS_Store` 应命中新增规则；`git ls-files .zcode` 应为空，本机 `.zcode/skills/` 仍在。`git diff HEAD^ HEAD --name-only` 应只列 `.gitignore`、本摘要和这 107 个本地技能文件。
