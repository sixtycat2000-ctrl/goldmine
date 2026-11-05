# 锚点命令卡 — 你亲手能跑、输出你能自己解读的验证

目的：**不信任何会话的转述**。下面每条命令都在 2026-10-25 实跑验证过，
"怎么读输出"写给人类。总耗时 < 1 分钟（除标注外）。

## 1. 库健康一屏（~2 秒）

```sh
python3 -m goldmine status --db evals/eval.sqlite --json
```

**怎么读**：`staleness` 一节里四个计数（`stale_files` / `embed_stale` /
`chunker_stale` / `extractor_stale`）应该**全是 0**。任何一个非 0 = 库里有
内容是用旧版本规则提取/嵌入的（行会自动重抽，不是坏事，但你应该知道）。
`current.embed_id` 是当前嵌入身份指纹——它变了意味着全量重嵌过。

## 2. 指纹拒绝现场（~2 秒）★最重要的一条

```sh
python3 -m goldmine search "copper gasket" --db evals/eval.sqlite.pre-p2b.sqlite; echo "rc=$?"
```

**怎么读**：必须看到 `refused: ... different embedding fingerprint` +
**rc=3**。这个库是 2026-10-25 批次 B 之前的备份（媒体预处理串 v4），当前
构建是 v5——门当场拒绝、退出码 3、并告诉你修法。这条是整个"身份纪律"的
活证据：**向量家族永远不混**。加上 `--lexical` 再跑一次：rc=0、有结果
（词法检索不依赖向量身份，永远可用）。

## 3. 向量数学没被改坏（~6 秒，需要已装模型）

```sh
GOLDMINE_LIVE=1 python3 -m pytest -q tests/test_reference_vectors.py tests/test_reference_media_vectors.py
```

**怎么读**：`2 passed, 2 skipped`（跳过的 2 个是"再生成守卫"，设计如此）。
含义：钉死的模型对冻结提示词产出**逐位相同**的向量、媒体向量 cosine 1.0。
任何动了嵌入路径的改动，先跑这条。

## 4. 双回归门（各 ~2-3 分钟，需要模型）

```sh
python3 scripts/eval_real.py --suite legacy --backend llama --gate
python3 scripts/eval_real.py --suite full-media --backend llama --gate --baseline evals/media_full_baseline.json
```

**怎么读**：结尾 `gate: green` 表示没有触发现有统计回归门，仍须读每项
delta。2026-10-25 的 lifecycle 检查中，两套 Success@5 均不变；同一道
`real-103` 从 Top1 降至第 2 位，故 Top1/MRR 有小幅下降，不能写成
“零差异”。这些命令对已有索引做查询回归，没有重新验证新的媒体提取。
产品成绩单看 FEATURE_STATUS 里的 product 套件（目前仍有红门）。

## 5. 只读估算不写库（~2 秒）

```sh
python3 -m goldmine estimate <任意目录>
```

**怎么读**：结尾两行——预估分钟数（DigUp M4 Pro 先验，标签会明说"goldmine
校准待做"）和"nothing was read, embedded, or written"。跑完目录里不该多出
任何 .sqlite 文件。

## 6. 分层回归套件（离线约 6 分钟，原生约 3 分钟）

```sh
python3 scripts/acceptance.py run --profile offline --out reports/execution/anchor-offline-001
python3 scripts/acceptance.py run --profile native --out reports/execution/anchor-native-001
python3 scripts/acceptance.py run --profile optional-mcp --out reports/execution/anchor-mcp-001
```

**怎么读**：先在 Python 3.12+、装有锁定测试依赖的虚拟环境执行；原生需要本机工具，MCP
需要 SDK extra。三个命令按顺序运行，每次用新的输出目录。原生的源码漂移
对抗用例会临时改写源码，与离线的源码身份校验不能在同一工作树并行运行。

要求 `pytest.*` 与 `source.stable` 均为 PASS；必需测试的 skip、丢失入口和
缺少回执都不是通过。旧的 resolver 失败与资源观察器时序问题已有定位及
验证；任何新的 FAIL 都需要查原始日志，不能靠反复重跑消掉。最新计数和
回执范围见 [FEATURE_STATUS](FEATURE_STATUS.md)。

---

独立于会话的机器验证（补上后第 1/6 条可全自动）：
hosted CI workflows 已写好、从未在远端跑通过——见 docs/CI.md 与
FEATURE_STATUS 的 Hosted delivery 行（诚实标 MISSING）。

## 7. 等价筛选与资源归还（约 7 秒，离线）

在包含本次优化提交的工作树中运行：

```sh
python3 -m pytest -q tests/test_search_exact_optimization.py tests/test_body_units_equivalence.py tests/test_rerank_ownership.py
```

**怎么读**：应无失败和跳过。这里验证完整命中/证据/pack/引用对照、零 quorum
回退、稳定首个最大值、取消/死线、旧 timer、正常退出与 fork 所有权；没有模型
或质量题重跑。`_aggregate_by_file` 默认接口仍保留全量输出，显式候选筛选发生
在全库标准化之后。它验证等价与生命周期，不代表整条 CLI 提速或召回率上升。

## 8. 学习实验台与解释模型（约 5 秒＋浏览器）

```sh
node web/scripts/check-mechanisms.mjs
npm --prefix web run build
npm --prefix web run preview -- --port 4175
```

**怎么读**：检查器与构建退出码均为 0，打开预览的 `/learn`。在实验台保持目标
夹角不变、切换语料分布，余弦不变而 z 可变；精确筛选保留低 z 的词法文件；
候选为 0 时新路径 posting 查询为 0。快照模式先发布/重建，再开启新请求，
已持有请求的版本和读取路径保持不变。借用后先“调用”再“归还”，虚拟 300 秒
到期才显示已加载模型的回收。抽屉内按滑块方向键不应切走课程；暂停与 reduced
motion 不应自行恢复动画。演示读数来自纯模型，页面不运行真实索引或推理模型。
系统“减少动态效果”在页面打开后切换也应立即生效；关掉它不能覆盖用户刚才的手动暂停。

## 9. 首页文案与示例（Node24，约 5 秒＋浏览器）

```sh
node web/scripts/check-home.mjs
node web/scripts/check-home-motion.mjs
npm --prefix web run build
```

**怎么读**：三个命令都应退出0。第一个检查短文案、示例数据、真实课程链接、
静态壳及历史数字的口径；它不会给中文自然度自动打分。编辑标准见
[首页中文文案评分标准](HOMEPAGE_COPY_RUBRIC.md)。第二个用实际动画调度器验证
暂停、减少动态效果、离屏、可见性与销毁时的帧所有权和活动时钟。

打开首页，切换“描述内容／精确编号／截图报错”：查询、摘录、文件与位置应一起变，
且可见“示例”标记。它展示固定例子，不会搜索访客电脑。三个例子都能进入相应
Learn课程。暂停后切换系统动态偏好再恢复，不应重载页面或覆盖暂停选择；没有WebGL
时标题、示例与链接仍可使用。原生后台标签若因自动化浏览器限制未验证，必须单列
MISSING，不能用合成可见性事件冒充。
