# Goldmine 执行合同

本文件固定范围、数据身份、接口、性能及完成标准。任务描述中的“按合同”均指本文件。实现过程中发现冲突时，先保留失败证据；不得自行把合同改成更宽松的版本。

## 1 范围与状态

- 只处理显式添加的目录、URL 或显式选择的索引；测试不访问用户资料目录。
- 产品只检索和组织证据，不生成回答、图片描述或推测性知识卡片。
- 默认依赖继续为 numpy；MCP 和重排依赖放入可选 extras，并记录锁定版本。
- `serve` 继续只管理模型 RPC，不迁入 SQLite 查询。MCP 是独立只读入口，不复用私有 UDS 协议冒充 MCP。
- 不增加 GUI、账号、同步服务、云推理、图谱、分布式向量数据库、后台工作队列或新的系统常驻安装。
- 修改结果排序、切块、提示词、分词或数据库时必须记录版本；禁止偷偷重建用户索引。
- 用户已于 2026-10-21 明确授权对全部计划进行专业角色对抗审查、定稿并开始实施。可开展本地实现、测试与候选组装；对外发布、推送或新建远程仍须相应授权。

任务状态只使用：`TODO`、`RUNNING`、`IMPLEMENTED`、`VERIFIED`、`BLOCKED`、`EXPERIMENT_REJECTED`。

- IMPLEMENTED：代码与必需离线检查通过，但还有真实依赖/测量未完成。
- VERIFIED：任务所有验收项均有对应成功证据；文档任务仅需其文档检查。
- BLOCKED：列出缺少的模型、数据、环境或未达指标，已有实现保留。
- EXPERIMENT_REJECTED：仅允许 P1-07 的重排候选、P1-08 的多查询候选或 P2-02 的扩容方案使用；必须完成实验并证明不满足预定收益，功能不得默认开启。

依赖任务的必需实现已完成时允许推进独立实现；各任务可用 task-check 完成其独立实现验收；尚未具备的全阶段质量不冒充已通过。日常质量验收使用 dev；最终质量由一次联合发布审计派生。M0–M4 状态从原始证据计算，不作为同次检查的输入，禁止自循环。

## 2 历史数据与报告

历史输入为 baseline-20261021.json 中列出的文件和 SHA256。P0-01 原样复制到 `evals/history/20261021/`，保留原文件名和原字节；附 manifest。历史语料可因本地源文件缺失无法重建，此时记录 NOT_COMPARABLE，不得补造历史输出。

新运行写入 `reports/execution/<任务ID>/<commit>-<run序号>/`，可重建的大日志、SQLite 和向量文件不进入 Git。提交小型结构化摘要、来源清单和哈希；不得提交私有原文、凭据、下载模型或数百 MB 数据。P0-01 为运行目录增加明确的 gitignore 规则。

每次运行的 `manifest.json` 至少包含：

```json
{
  "schema": 1,
  "task_id": "P1-02",
  "git_commit": "完整 SHA",
  "dirty_diff_sha256": "干净时为 null",
  "run_id": "P1-02-001",
  "started_at": "带时区 ISO8601",
  "platform": {"os": "...", "arch": "...", "python": "..."},
  "suite_id": "product.v4",
  "corpus_sha256": "...",
  "queries_sha256": "...",
  "split_sha256": "...",
  "index_identity": {"schema": 4, "embedding": "...", "chunker": "...", "lexical": "..."},
  "search_settings": {"scorer": "hybrid", "mode": "fast", "z_gate": 3.0},
  "model_files": [{"role": "embedding", "sha256": "..."}],
  "commands": [{"argv": ["python3", "..."], "exit_code": 0, "log_sha256": "..."}],
  "required_checks": [],
  "passed": [],
  "failed": [],
  "missing": [],
  "status": "PASS"
}
```

字段必须是实际值，不得留下省略号或把示例默认值当实测。运行中的时间戳可变，pack、查询排序和评测指标必须可重现。

所有外部 run 输入采用 `{manifest_path,sha256}`，同时验证 corpus/code/index 身份。每个 subprocess 保存独立机器 receipt，绑定 run/check/argv/cwd/env 摘要、退出码、stdout/stderr、JUnit/result JSON 哈希；不能根据日志里的 PASS 字样推断成功。validate 对照代码内固定检查注册表重算，拒绝缺项、重复项、同一 run 冒充双臂、非有限数、错 suite、逃出报告目录的资产路径。receipt 与哈希提供本地一致性和溯源校验，不声称能认证一个可任意同时改写全部文件的攻击者。

新验收执行器退出码：`0=所有必需检查通过`，`1=检查确实失败`，`2=缺依赖、身份不匹配、结果不完整或不可比较`。原产品 CLI 的 0/1/2/3/4/5/130 合同保持不变。底层 pytest 因 skip 返回 0，不足以让严格验收返回 0。

## 3 评测集与调参

保留三类套件，不能相互覆盖：

| suite_id | 输入 | 用途 |
|---|---|---|
| legacy.v3 | 原 145 可答题 + 5 无答案题，原过滤规则 | 历史行为对照；单列已知未达标能力 |
| media-full.v3 | 原 164 可答题 + 5 无答案题，包含 asr/ocr/exif | 当前完整能力对照，能力缺失显式失败 |
| product.v4 | 新的可搬运 420 题集 | 新功能调参、holdout 验收、阶段门槛 |

product.v4 的主类互斥，最少数量：

| 主类 | 题数 | 题目必须检验 |
|---|---:|---|
| semantic | 60 | 无原词复述的文本语义检索，代码/文档各至少 15 |
| cjk | 60 | 纯中文至少 30，中英混合至少 15，日/韩至少各 5 |
| lookup | 60 | 文件名/路径、混合编号、带标点编号各至少 15 |
| pdf-page | 60 | 文本 PDF 至少 30、扫描/混合 PDF 至少 20，精确页约束 |
| media-text | 60 | 截图 OCR、ASR 内容、照片名称/EXIF 各 20 |
| compound | 60 | 至少两个证据来源；其中跨文件至少 30 |
| noise | 60 | 无可支持问题的证据，允许部分词面/主题重合；近主题误导、随机词、冲突编号各 20 |

共 360 道可答题 + 60 道无答案题。次级 tags 可多选，主类和套件分母不得重叠计数。照片画面语义另设 capability probe，不进入 text-only llama 的可答分母，但“明确不支持”的能力呈现必须通过；不能用此规则排除照片名称/EXIF 题。

题目、相关文件、位置约束与参考事实必须在看候选输出前冻结。每道可答题至少列一个明确的高相关来源；noise 的 expect 与 required_facts 必须为空。PDF 必须给页，编号给原始字符串，compound 给至少两个必需事实。单个文件最多承担 8 道题；每个可答主类至少 12 个独立来源组，noise 按上述三种类型各自分组隔离。原文里不得出现完整评测问句、答案清单、生成器代码或评分提示词。

product.v4 的初始 reference 固定为完成 P0-05 时、尚未修改生产排序/切块/分词的代码与依赖版本，并保存可重建的 commit 和环境锁；不是未来任意时刻的最新 main。后续需要重建时用这个版本的独立临时 checkout/venv 构建，不让当前 PYTHONPATH 或工作目录把候选模块误导入 reference。参考与候选共享源资料身份，各自保留真实索引身份。

按 `source_group` 分组分层，种子 `20261021`，目标 dev/holdout 各 50%；同文档的不同页、格式转换版、近重复版和同一音频的转录均属于同组。每主类 holdout 至少 25 道；不足时补数据，不移动已看到的 holdout 题。记录题目/来源组/分割 SHA。

日常开发、失败归因、参数选择及任务级质量验收只看 dev。每个实现任务最多预登记 3 组候选（含控制臂），先登记规则再看结果。holdout 的 reference 可提前计算并密封，不展示逐题或汇总指标。全部算法、参数与实验接受规则冻结后，仅运行一次联合发布审计；记录候选代码/依赖/参数 SHA、冻结时刻和 exposure ledger。失败后该集合已暴露，不能回 dev 调参后再称同一 holdout 未见；再次声明发布质量需独立未见来源。旧 train 名称仅作 dev 的兼容别名。

新 product/answer 题目与事实 schema 必须机器验证，未知键一律拒绝（exit 2）：

- `expect` 元素含唯一 `evidence_id`、corpus-relative `path`、`grade`、`location`；位置为 `{unit:"page"|"line"|"second",start,end}`，闭区间。页/行整数且从 1 开始，秒为有限非负数；页仅 PDF，行仅文本，秒仅音频/视频，起点不得大于终点。元数据证据单列明确类型与存储字段，不伪造页/时间。
- `required_facts` 元素含 `fact_id,description,sufficient_evidence`。后者为 alternatives 列表；每个 alternative 是若干已标注原文 span 的合取，alternatives 之间为析取。span 包含 evidence_id 和原文字符区间/精确原文 SHA。
- pack 事实覆盖仅根据实际发出的原文及可回读 citation 完整包含必需 span 判断。文件命中、页区间相交或关键词出现不等于事实覆盖。supporting citations 不增加排名命中数。
- paired CI 按冻结 source_group 整组重采样，compound 共享来源先取连通分量，保留组内所有题；报告题数与独立组数。独立组少于 12 时不输出“显著改善”。旧按题 bootstrap 仅保留历史诊断；未检出显著退步不构成证明没有退步。

## 4 质量门槛

以下为 **product.v4 冻结验收样本的新目标**。dev 用于任务检查；最终仅一次 sealed holdout 联合审计。比例是工程门槛，不声称证明总体命中率。每个主类样本至少 25，比例按 `命中数/n` 原值计算，显示舍入不能决定通过。

| 检查 | 快速模式门槛 |
|---|---:|
| 全部可答题 success@5 | ≥ 0.85 |
| semantic success@5 / success@1 | ≥ 0.85 / 0.65 |
| cjk success@5 / success@1 | ≥ 0.85 / 0.65 |
| lookup success@1 | ≥ 0.90 |
| pdf-page success@5 / success@1 | ≥ 0.60 / 0.40 |
| media-text success@5 | ≥ 0.80 |
| media-text 三种子能力各自 success@5 | ≥ 0.70 且各自 holdout n ≥ 8 |
| noise 正确空结果比例 | ≥ 0.90 |
| 合法查询异常率、引用文本与声明位置不符率、越界读取 | 0 |

compound 在 P1-08 前只报告；新增多查询能力的接受门槛为 `all_required_facts@pack ≥0.75`，且相对同一候选的单查询控制臂提升至少 0.10。若完成实验后未达到门槛，必须 EXPERIMENT_REJECTED、保持关闭并列明限制；M2 检查拒绝证据，不再声称交付了该能力。质量模式在适用题上不能显著降低简单查询的 Top-1，不能降低 noise 门槛。

legacy/media-full 在相同输入和评测定义下保留成对回归检查：四个主指标下降 >0.02 且成对检验显著则拒收，沿用检索门禁的 McNemar/paired bootstrap 逻辑。样本不足的分组只报告，但已定义的确定性能力契约不得回归。切块/索引身份变化时，在同份源语料上为参考版与候选版分别重建；不能直接比较不同语料输出。

公开集固定 SciFact、NFCorpus、ArguAna 数据哈希；至少运行 lexical/semantic/rrf/hybrid 四臂。候选 hybrid 各集 nDCG@10 相对参考下降不得超过 0.02；外部集不用于挑参数。不同套件或不同相关性定义的分数不得放在同一列冒充可比。

## 5 答题评测合同

- 运行记录区分四组：`answer_a1/judge_1`、`answer_a1/judge_2`、`answer_a2/judge_1`、`answer_a2/judge_2`。每个 grade 带被评答案 SHA、pack SHA、prompt SHA、模型和参数；不能把重答与重判混用。
- SHA 只验证来源和字节身份；答题波动按 0/1/2 得分或事实原子变化计算。不得再用文本是否不同代表质量波动。
- 校准至少有 60 道可答题 + 20 道 trap；single/multi/cross 可答题各 20；相同答题和评分引擎、相同输入各运行两轮，并给两轮答案各判两次。扩题不能只从当前 pack 已答对的题里选。
- 每题“波动”定义为四次有效得分不全相同。`p_score` 为波动题占可答题比例；文本变更率另列 diagnostic，不参与阈值。
- `threshold` 是候选运行前登记的最小实际退步幅度，单位为原始 0..2 均分，默认 0.10；允许值 0.10/0.15/0.20/0.25/0.30，必须在查看候选得分前冻结并保存 SHA。四组评分并非四份独立答案，且运行内可能相关；`p_score` 及其区间仅作稳定性诊断，不导出 binomial 误报率。无独立 no-change run 证据时记录 `calibration_status=uncalibrated`，不得输出 `calibration_valid=true` 或误报率<5%。机械检查、检索和实际幅度门禁可以继续。若额外声称误报保证，需独立 run 级 null 审计及一侧区间；例如 59 次独立 null 对比均无误拒时，一侧 95% 上界才约 0.0495。该额外保证不列为本轮实现的前置依赖。
- 正式候选比较中，每题 score 为该 run 的两轮答案、每轮两评的四个 0..2 原始评分的平均值，再在可答题上求均分；必须同时保留四个原分。平均退步 >预登记实际幅度阈值拒收。trap 成功要求两份答案都明确拒绝、每份两评均值≥1.5，且四份评分的结构化 fabricated_fact 均为 false；成功题比例≥0.90 是独立门槛。trap 不能被可答题均分冲掉。评分 1.875/2 不能称为“93.75% 答对率”。
- 新 pack 导致 SHA 改变时必须重新答题/评分。比较可以允许 pack/排序/切块作为实验变量变化，但必须验证每个答案来自它所属 run 的 pack；题集、语料、模型/评分协议改变时默认不可比较。
- 只消费已配置的本地引擎或显式提供的评测 job 结果。不自动调用云端模型、不自动给其他任务发消息、不把旧答案改名当新答案。缺评分环境就报告 MISSING，软件功能验收可继续，最终 release 答题检查不能通过。

## 6 接口与资源上限

新应用服务位于 `goldmine/application.py`，纯请求/结果类型位于 `goldmine/contracts.py`。四个操作固定为 `search`、`get`、`context`、`status`，CLI 和 MCP 调同一实现。不能从 MCP 调 CLI 子进程再解析人类文本。

新增 CLI 开关固定：`search --mode fast|quality`、`--rerank off|local`、`--multi-query`；默认 fast/off/false。`--lexical` 仍不启动 embedding 模型；与 rerank=local 同用是参数错误，不悄悄覆盖。fast 不能启用重排或多查询；使用这些开关时要求显式 `--mode quality`。

MCP search 限制：原始 query 非空且 UTF-8 ≤8192 字节，limit 1..50，context 0..3，budget 512..32768；越界明确报 invalid_params。共享应用服务不把 MCP 的限制强加给旧 CLI：CLI 原有 `--limit 0` 合法并返回空列表、仅过滤器查询以及既有 budget/context clamp 行为均保持。新 quality 功能的重排/子查询资源上限对两个入口都生效。所有计划子查询继承同一份已解析过滤器，过滤条件不能重新混入可被改写的自然语言字符串。

MCP 只提供 `goldmine_search`、`goldmine_get`、`goldmine_context`、`goldmine_status`，stdio 单一传输。只从当前索引读取；get/context 使用索引返回的 opaque citation_id，不接受任意文件路径、shell 命令、URL 或 SQL。索引必须由服务启动参数显式选择；不存在时失败，不自动 init。

citation_id 固定为 `gm1:<index_uuid>:<segment_id>:<content_sha256前16位>`；index_uuid 第一次显式写入/升级索引时生成并保存，跨 Store 重开保持，复制备份保留。只读查询不为旧库补写 UUID。旧库缺 UUID 时工具明确提示做显式词法升级。`evidence_type` 固定为 content / name_metadata / vector_only；metadata get 返回已存元数据和明确的无正文状态，不把 folder_words 冒充正文。digest 统一由 contracts.py 生成：UTF-8 JSON 数组 `["gm-citation/v1",file.id,file.path,file.source,file.sha256,segment.id,segment.kind,segment.modality,segment.loc,segment.loc_end,evidence_type,file.info,stored_text]`，`ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False`。info 为解析后的存储 JSON，原文允许空；不计时间戳或查询分数。重新导入导致身份变化时旧 ID 返回 stale_citation，不回退到新段。

材料包沿用现有契约：完整材料正文含 header/citation/footer，不超过 budget；预算 tokenizer 的模型和 `token_mode=tokenize|estimate` 必须报告。不能声称 embedding tokenizer 的 4000 token 等于所有消费模型的 4000 token。预算是完整 `context_text` 的预算，完整出处表和可选 why 也放在其中计数；pack=true 时不再返回第二份 hit 正文/出处表，预算外仅返回≤2KiB的计数、实际设置、tokenizer和降级摘要。JSON-RPC协议封装另计。exact 模式计数未知时不能标 tokenize。长 query/path/why、零结果时固定头部或单条 citation 自身超过预算也必须返回 `budget_too_small` 或经过真实计数的有界空包；任何成功输出不得超预算。

调用上限：fast 默认 deadline 5s，quality 10s；候选重排最多 20 段，每段最多 384 个该重排模型 token；多查询最多 3 条，检索候选合并最多 60 段，仍受总 deadline。超时返回明确降级原因和实际执行模式；不能把降级结果归到 quality 成功统计。检索请求不执行下载、导入、迁移、外部打开或后台服务安装。

每个 search/get/context/status 在显式 SQLite 只读事务中读取身份、召回向量、段、正文、引用及 pack；search→pack 必须保持同一数据库快照。只读 URI 使用 `Path.resolve().as_uri()+"?mode=ro"`，特殊字符和相对路径不得打开或创建旁边的数据库。并发在召回、详情和 pack 阶段提交的测试不得混代。

一个 monotonic deadline 贯穿 SQLite busy_timeout、模型启动、RPC、重试、token count 与重排，每层仅取剩余时长。到期后禁止新调用、cold fallback 或延迟启动。请求拥有 Store 和临时 backend；会话拥有可复用 reranker（空闲 5min 释放），关闭只清理自有资源。受控阻塞/取消测试验证无后续调用、队列滞留和遗留进程。

## 7 数据库与兼容

按顺序升级：schema v5 增加词法派生索引及 `meta.lexical_version=lex.v1-cjk2`；v6 增加精确查找派生索引并设为 `lex.v2-exact`。不在 files 另造逐文件词法版本列；派生数据由迁移/显式词法重建和每次段事务保证完整。不要在不同任务竞争同一 schema 号。升级必须防护已经打开的旧 writer：v4 连接在另一连接升级后继续写入必须安全拒绝或保证完整派生更新，不能只依赖构造时 user_version 检查。实现可用连接级 writer 能力配合数据库触发器 fencing，必须测试真正升级前已开的旧连接；拒写需事务回滚且明确恢复操作。未来若实际结构需要再升级，先登记下一号及迁移测试，不改历史 migration。

新 CJK 索引固定为 `fts_cjk(tokens)`，rowid 与 segment_id 相同；在同一事务内随 fts/segments 增删。汉字、假名、韩文连续串采用相邻二元组；单字符查询允许单字 token，索引也保留单字 token。Latin 路径继续原 FTS，不通过空格拼接破坏原文。精确短语必须用原文归一化后连续匹配验证，不能把任意散落的二元组算短语命中。

词面 coverage 明确按查询单元计算：每个去重 Latin 内容词为一个单元；每个连续 CJK 串为一个单元，该单元得分为其去重查询二元组在候选文件的命中比例（单字串使用单字）；每个已验证精确引号短语为一个单元且不重复计其中 gram。coverage 为各单元得分之和/单元数，无单元时为0。CJK gram 只在召回中展开，不按每个 gram 再叠加融合奖励。两个 FTS 通道各最多取1000段，再按文件/段身份去重。

v5/v6 migration 只能处理已存储的 `fts.original/body`、文件路径和元数据，不读源文件、不调用 embedding、不改变向量 BLOB。迁移仍遵守现有单事务、备份、崩溃恢复和未来版本拒绝规则。只读旧库不触发迁移；报告词法能力缺失及明确修复命令。显式 `goldmine reindex lexical --db PATH` 是待新增维护命令，仅重建词法派生数据，不访问外部文件。

PDF 新切块若改变持久化段边界，将 chunker 版本升为 `chunk.v3-page`；旧文件标 stale，等用户显式 ingest 更新，不能伪标已升级。引用页码仍是从 1 开始的页码；不得把段序号、字符偏移或小数页码混入页码字段。

只读 API 不打开索引之外的文件内容；pack/get 使用存储原文，不用 excerpt 替代全文，不从源文件临时读取造成相同索引输出漂移。

## 8 性能门槛与环境

参考环境为同一台 macOS arm64、Python 3.12、同一模型/输入。热/冷各至少 30 次按相同查询交错执行，记录逐次 argv/退出码/JSON/时长，nearest-rank 计算 p50/p95。controlled residency 与真实未固定压力分别报告；MCP overhead 必须同请求配对直接/stdio，不能用两个独立 p95 相减。规模数据按 float32 分块生成、fp16 持久化，各规模/分布/臂独立进程，区分 fixture/search/model 峰值及 Darwin/Linux RSS 单位。性能运行先静置，记录真实内存压力、是否固定驻留、CPU、内存、模型 SHA 和缓存状态；正常保护机制测试不允许固定压力值。

| 项目 | 门槛 |
|---|---|
| 快速热搜索，参考小库，至少 30 次 fresh subprocess | p50 ≤300ms，p95 ≤500ms，且 p50 ≤冷启动/3 |
| 相同机器/库对任务前版本 | p50 不退步超过 20%；超过时必须有可复核功能成本解释且里程碑不通过 |
| quality warm，至少 30 题 | p95 ≤2500ms；超时/降级比例 ≤1% |
| 单模型本地重排新增峰值 RSS | ≤1.5GiB；5 分钟空闲后释放新增模型 |
| MCP 在 serve 已驻留时的额外开销 | p95 ≤100ms，对同一应用服务直接调用比较 |
| 增量重导入未变文件 | embedding/OCR/ASR 新调用数均为 0 |
| watch 正常供电，新文本文件 | 90s 内可搜，必须用 JSON 命中验证 |
| watch 4s 周期写入，运行 10min | embedding 后端启动数 ≤24，无子进程遗留 |

小库测量不能冒充 100k/500k 向量性能。250k 之前保留 SQLite + numpy 精确搜索；只有 P2-02 的触发条件满足才允许实现 mmap 向量 pack。ANN 和外部向量库不在本计划实现范围。

## 9 交付层级与检查配置

base hermetic offline 不依赖模型、BEIR 缓存、系统壁纸或可选 SDK；真实模型/公开集哈希归 assets，OCR/ASR/系统能力归 native，MCP/Foresight 有单独 optional SDK collection。每个 profile 验证其固定测试确实收集与通过，禁止全 skip 当绿。task-check 验证本项能力、已完成能力回归及 legacy；retrieval 为全阶段 dev；local_rc 为完整本地候选及一次联合 holdout 审计；hosted_release 在其上另要求真实 CI run 链接。当前无 remote，hosted 缺失不阻碍本地实现，但不得冒充已验证。release 判定只依赖原始检查和资产，完成后才派生任务/里程碑状态。

native tar 必须使用全新 staging、精确 revision/patch 与真实架构；打包动态依赖闭包或证明静态链接。离开源码/构建目录、清空 DYLD 覆盖后独立解压运行版本/OCR/真实模型请求，失败 nonzero。错 revision、未知 patch、缺 dylib、旧 staging 均有拒绝反例。
