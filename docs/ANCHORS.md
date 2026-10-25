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

**怎么读**：结尾 `gate: green`，且各指标 delta 全 `+0.0000`。这是**回归
底线**（没变差），不是产品质量证明——产品成绩单看 FEATURE_STATUS 里的
product 套件（那是另一回事，目前还有红门）。

## 5. 只读估算不写库（~2 秒）

```sh
python3 -m goldmine estimate <任意目录>
```

**怎么读**：结尾两行——预估分钟数（DigUp M4 Pro 先验，标签会明说"goldmine
校准待做"）和"nothing was read, embedded, or written"。跑完目录里不该多出
任何 .sqlite 文件。

## 6. 全量离线套件（~8 分钟）

```sh
python3 -m pytest -q tests
```

**怎么读**：当前已知基线 = **全过，除 2 个档案化失败**
（`comparison_native_resolvers` 确定性旧败；`resource_observer_native`
时序 flake——若它失败，单独重跑 3 次再下结论）。**出现任何其他 FAILED
就是新问题**，拿着名字去问会话要归因（就像 2026-10-25 撞车那次：stash
 A/B 一验就知道是谁的）。

---

独立于会话的机器验证（补上后第 1/6 条可全自动）：
hosted CI workflows 已写好、从未在远端跑通过——见 docs/CI.md 与
FEATURE_STATUS 的 Hosted delivery 行（诚实标 MISSING）。
