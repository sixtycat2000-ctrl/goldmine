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
