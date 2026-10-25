# 提交摘要档案 — 每个提交一份"人读 diff 摘要"

> 制度（2026-10-25 起）：**每个提交**在合并/推送前，于本文件追加一节。
> 读者是仓库所有者，不是 AI。格式固定四段：**关键行 / 每行的风险 /
> 我不确定的地方 / 你 90 秒怎么验**。摘要讲代码本体和风险——"大白话
> 总结"讲故事，这份讲行。标记规则：读完在心里标"懂/不懂/存疑"，不懂的
> 进下一次轮讲（docs/WALKTHROUGH-search.md 是第一讲的样板）。

---

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
