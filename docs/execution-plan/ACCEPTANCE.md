# Goldmine 验收矩阵与拒收规则

所有任务先满足 [CONTRACTS.md](CONTRACTS.md)，再满足自身步骤和下面的证据要求。这里区分已有命令与未来命令，避免执行 agent 把不存在的脚本、空 profile 或 skip 当完成。

## 1 运行隔离

每次新验收会话在仓库根目录准备自己的临时目录，不改变 HOME，不接触已有用户配置。下面这组环境设置现在即可使用：

```bash
PLAN_RUN="$(mktemp -d "${TMPDIR:-/tmp}/goldmine-acceptance.XXXXXX")"
export GOLDMINE_HOME="$PLAN_RUN/home"
export GOLDMINE_CONFIG="$PLAN_RUN/config.toml"
mkdir -p "$GOLDMINE_HOME"
```

环境变量仅对本次进程树有效；模型位置显式传入或使用项目已验证的模型解析，不让 tmp config 覆盖真实配置。创建的测试服务记录 PID、启动时间和子进程 receipt，结束时仅清理自身进程。

报告存入 CONTRACTS 的运行目录；原始日志不能被 `tail`/token 输出截断代替。大文件不入 Git，小型 manifest 与结果摘要可提交。全量性能、真实重排和10min watch 测试只在对应任务或最终阶段运行，不对每个小修改反复执行。

## 2 当前已有命令

这些命令在计划起点已存在。eval_real --help 是 P0-01 要修复的已知失败，不能把其当前失败称为新回归。

```bash
python3 -m pytest tests/ -q -m 'not live'
python3 scripts/eval_real.py --backend llama --gate --no-fold-audit
python3 scripts/eval_real.py --backend llama --include-needs asr --include-needs ocr --include-needs exif --no-fold-audit --json
python3 scripts/eval_public.py --backend llama --offline --arm lexical --arm semantic --arm rrf --arm hybrid --json
python3 scripts/bench.py --gate
GOLDMINE_LIVE=1 python3 -m pytest tests/test_live_serve.py::test_live_latency_bench_f1 -q -rP
```

真实命令依赖的模型、索引、BEIR cache 和 native tools 必须预先准备。`eval_public --offline` 表示不联网，仍然需要 cache；它与下节不依赖模型/缓存的 hermetic offline profile 是两件事。默认 legacy 不能验证全部媒体能力。当前 public gate 为 advisory，退出0不能替代 CONTRACTS 新的公开集回归判定。现有 latency 测试只有22次且没有断言 p95，升级前不能用它满足新合同。

## 3 待实现的统一命令

P0-06 实现后，把 [inputs.example.json](inputs.example.json) 复制为本次运行的 inputs.json，填入已构建/已校验资产的真实路径与 SHA。以下示例假定 `$PLAN_RUN/inputs.json` 已填完整；不得把模板中的 null 或不存在路径当作实际输入。

```bash
python3 scripts/acceptance.py run --profile offline --out "$PLAN_RUN/offline"
python3 scripts/acceptance.py run --profile task-check --task P1-02 --inputs "$PLAN_RUN/inputs.json" --out "$PLAN_RUN/P1-02"
python3 scripts/acceptance.py run --profile assets --inputs "$PLAN_RUN/inputs.json" --out "$PLAN_RUN/assets"
python3 scripts/acceptance.py run --profile native --inputs "$PLAN_RUN/inputs.json" --out "$PLAN_RUN/native"
python3 scripts/acceptance.py run --profile optional-mcp --inputs "$PLAN_RUN/inputs.json" --out "$PLAN_RUN/sdk"
python3 scripts/acceptance.py run --profile retrieval --db evals/eval.sqlite --inputs "$PLAN_RUN/inputs.json" --out "$PLAN_RUN/retrieval"
python3 scripts/acceptance.py run --profile agent --db evals/eval.sqlite --inputs "$PLAN_RUN/inputs.json" --out "$PLAN_RUN/agent"
python3 scripts/acceptance.py run --profile local_rc --inputs "$PLAN_RUN/inputs.json" --out "$PLAN_RUN/local-rc"
python3 scripts/acceptance.py validate --report "$PLAN_RUN/local-rc/manifest.json"
# 仅在已有remote/runner且有真实run证据时，另行运行：
python3 scripts/acceptance.py run --profile hosted_release --inputs "$PLAN_RUN/inputs.json" --out "$PLAN_RUN/hosted-release"
```

这里 `--db evals/eval.sqlite` 仅选择 legacy/media 参考库，必须与 inputs 中所固定的参考库身份一致；不得通过该开关覆盖候选身份。product.v4 的参考与候选库、答题 run、发行包位置由固定 profile 输入 manifest 选择。schema 2 的所有相对路径按 inputs 中显式的绝对 `path_base` 解析；不依赖当前 shell 目录。文件使用 `{path, sha256}`，run/证据使用 `{manifest_path, sha256}`，manifest 的原始文件再用相对路径与SHA逐一绑定。缺路径/哈希或任一身份不符均 MISSING/exit2，不默认拿同一份候选充当 reference。正式成对实验拒绝同一run；诊断性的 self-compare 单列，不能作为改善证据。

`task-check --task ID` 展开固定注册表中的本项检查、已交付能力回归和历史回归，只读取dev（旧train是兼容别名）；不得隐式运行 product holdout，也不要求后续任务尚未实现的质量门槛。它只能证明本项局部实现，不代表里程碑通过。其他 profile 上的 `--task ID` 只追加本项检查，不减少基础必需项。retrieval/agent日常验证dev；全部算法/参数/接受规则冻结后，local_rc消费一次联合发布审计的sealed holdout原始结果。其他profile若消费正式结果必须引用同一个审计artifact，不重复运行。记录冻结代码/依赖/参数SHA、时刻和exposure ledger；若正式确认失败，该holdout已经暴露，不能调参后重新声称其未见，后续发布质量声明需独立未见来源。

所有目标读取 `evals/acceptance_targets.json`；原始指标始终保留，即使未达标。检查注册表由版本控制中的实现固定，输入报告不能传入任意命令、添加PASS项、移除必需项或改写门槛。未实现的必需适配器返回 MISSING，不能用空占位检查通过。

| Profile | 必需输入 | 必需检查 | 通过条件 |
|---|---|---|---|
| offline | 源码、锁定基础 dev 依赖、临时目录 | hermetic collection、确定性fixture测试、历史哈希/文档一致性 | 无网络/模型/cache/原生工具也可运行，必需nodeid全passed |
| task-check | task ID、本项注册所需的dev证据 | 本项局部测试、既有行为/已交付能力回归 | 只判本项，不消费holdout，不伪称里程碑通过 |
| assets | 显式模型文件、BEIR缓存、来源manifest | 真实文件bytes/SHA和完整性 | 缺资产nonzero；不能把mock哈希当真实资产 |
| native | 声明平台、已准备native工具/fixture | OCR/ASR/PDF/ffmpeg真实调用和结果 | 平台声明范围内必需nodeid全passed |
| optional-mcp / optional-foresight | 已锁定MCP/Foresight extras、独立环境 | 各extra固定collection、base缺extra降级、真实SDK握手 | 各声明子集完整，无importorskip冒充通过 |
| retrieval | offline/assets/native证据；legacy/media/product两版库与dev结果；公开四臂；冻结候选/分割 | 逐题成对回归、product dev门槛、公开集、warm/cold各≥30次交错测量 | 所有身份一致、分母完整、dev质量/性能过合同 |
| agent | retrieval证据；optional-mcp；真实包tokenizer；两套答题run与预登记 | SDK协议、越界/陈旧ID、CLI/MCP一致、预算/引用、dev答题比较、配对MCP时延 | 无协议/越界错误，预算与预登记实际退步门槛通过 |
| local_rc | agent证据；同候选sealed holdout联合审计；wheel/sdist/native tar；安装/watch/serve/distill/本地CI原始结果 | 一次联合发布质量审计、源码外安装、升级/恢复、资源清理、功能事实/文档、CI同脚本与失败注入 | 全部本地必需检查通过；不要求尚不存在的hosted run |
| hosted_release | local_rc证据、已配置runner的真实run URL及下载artifact | hosted矩阵、run/commit与artifact身份、真实CI结果 | 所声明hosted平台全通过；缺remote/runner/run则MISSING |

P2-08 使用独立 comparison 输入与报告，不混入 release profile，也不能用竞品未安装阻止正常离线开发。

`release` 如为兼容保留，固定为 `local_rc` 的别名且在报告中记录 canonical profile；不能根据环境偷偷改为hosted范围。local_rc检查原始证据，不检查P2-07或M3是否已经VERIFIED；成功后才派生P2-07/M3状态。P2-05的本地实现/验证与hosted验证分别记录，后者MISSING不影响已完成的本地证据，也不能被写成GitHub已通过。

offline / native / assets / optional-mcp / optional-foresight 使用显式markers和平台/依赖子集注册；原有nodeid移动到新分类必须有迁移表。机器collection的丢失或模块级importorskip必须被发现；平台例外只允许逐nodeid、原因和适用平台的allowlist。模型/BEIR磁盘实物检查移入assets，系统壁纸等偶然文件改为可搬运fixture；不得删掉验证责任。native和optional依赖的缺失只影响自己的profile，不能让hermetic offline依赖它们。

### 原始证据与校验

每次command另存不可截断的stdout、stderr、machine receipt以及JUnit或指标JSON；receipt记录run_id/check_id、argv数组、cwd、允许列出的环境摘要、真实exit_code、开始/结束时间和产物SHA。报告只引用这些文件，不信任自报status。validate从固定注册表重建完整检查集合、核对nodeid及JUnit outcome、重算指标和阈值、对照receipt退出码，拒绝重复/未知/缺失检查、错suite、非有限数、被编辑摘要、缺原文日志和路径越界。内部artifact必须在该run目录内，拒绝`..`及symlink逃逸；外部输入只能通过显式inputs引用并校验SHA。

哈希提供一致性与来源关联，不提供对能够同时改写整套本地文件的攻击者的认证。不得声称报告防伪。报告格式、实际解析器版本、检查注册表SHA、目标文件SHA和输入文件SHA都应保留；validate不得用报告自带的旧注册表替代当前调用者选择的可信注册表。

## 4 每项任务的最小验收清单

下列新测试文件尚需对应任务创建；若未创建，检查应失败。每一行的局部测试通过后，仍需该阶段的质量或 live 证据，不能把单元测试当完整验收。

| 任务 | 局部测试或检查 | 真实或外部验收 | 特别拒收条件 |
|---|---|---|---|
| P0-01 | test_eval_entrypoints；六文件 SHA；nodeid 对照 | 可用条件下复现 legacy | 修改历史数据来凑快照 |
| P0-02 | test_answer_calibration；test_eval_answer | 80题四组有效评分、来源与预登记阈值 | SHA变化算质量变化；无独立null证据声称误报受控 |
| P0-03 | test_answer_compare；来源不变量/变量验证 | 基准/候选独立新答案与比较 | pack改了但沿用旧答案 |
| P0-04 | test_eval_real/test_coverage/test_eval_harness | full-media完整164+5行 | 默认跳掉缺能力题并缩分母 |
| P0-05 | test_product_corpus/test_eval_product | 420题、审计、两目录重建、真模型参考 | 模板凑题、跨fold重复来源 |
| P0-06 | test_acceptance_runner六类反例 | 一份完整offline报告 | 任一必需项skip或缺失仍exit0 |
| P1-01 | test_application；CLI/JSON/pack对照 | 真模型旧检索gate | adapter拷贝评分逻辑或读时写库 |
| P1-02 | test_lexical_cjk/test_lexical_migration | cjk dev达标与向量hash；local_rc统一审计holdout | 只有语义能搜中文；迁移重嵌入 |
| P1-03 | test_exact_lookup；v5→v6 | lookup/noise dev联合回归；local_rc统一审计holdout | 硬编码ID；alias掩盖原串冲突 |
| P1-04 | test_pdf_evidence；长页/多证据 | dev页位置指标与公开集 | 放宽页约束；重复文件刷榜 |
| P1-05 | test_media_evidence；ASR/OCR既有测试 | dev三种media-text子能力与真工具 | name命中冒充内容；自动云caption |
| P1-06 | test_search_admission | 冻结dev选择；最终local_rc一次联合holdout审计 | 反复扫holdout；全不相关仍强制返回 |
| P1-07 | test_rerank | off/local对照、时延/RSS/释放 | 默认加载；降级冒充quality成功 |
| P1-08 | test_query_plan；过滤器/预算 | compound事实覆盖与单query对照 | LLM拆题；expected facts喂planner |
| P1-09 | test_mcp；真实SDK子进程 | 真模型CLI/MCP同ID、额外开销 | shell包装假MCP；新增写工具 |
| P1-10 | test_pack_contract/test_agent_workflow | exact token/引用全检；LIST/PACK比较 | 隐藏超预算、失效引用或伪答对率 |
| P2-01 | test_module_boundaries；全部既有测试 | wheel导入、行为/性能对照 | 用重构顺带调参或破坏import |
| P2-02 | test_scale_bench；缓存/崩溃反例 | 四规模三分布、同机时间/RSS | 小库冒充大库；无触发引入ANN |
| P2-03 | pipeline/watch/serve/identity测试 | 90s落地、10min占空比、SIGKILL | 只看stdout词命中；清理他人进程 |
| P2-04 | distill/eval_distill既有门禁 | 100篇8主题、真实purity/增量/引用 | mock purity宣称真实效果 |
| P2-05 | workflow命令本地演练与故障注入 | local_rc用本地同脚本；hosted_release另需run链接与artifact | 无runner声称CI绿；自动更新baseline |
| P2-06 | test_distribution；build/twine check | 两种extra源码外安装；native tar | 源码/vendor掩盖缺文件或依赖 |
| P2-07 | 文档链接、示例、版本一致性 | 完整local_rc、升级/回滚；通过后再更新自身状态 | 状态自循环；把本地RC称已发布 |
| P2-08 | 三方输出adapter fixture | 同题QMD/Recoll/goldmine完整报告 | 不同分母、隐藏转换、只报赢家 |

## 5 报告中的数值门槛

数值源只有 CONTRACTS 和 P0-06 生成的 acceptance_targets.json。实现者不得在 task 测试里另写更宽松常数。验收至少导出：

- product dev及最终sealed holdout分列：各主类与三种 media-text 子能力的 n、命中数、success@1/5、MRR、nDCG；noise n/正确空/误命中；compound 的全部事实覆盖。
- legacy/media：每题参考/候选 hit/位置/分数与成对差异；不能只给均分。
- public：数据SHA、四臂、三个集、同 qrels 和每集 nDCG 变化。
- answer：60可答/20trap、四组评分来源、p_score稳定性诊断、预登记阈值（默认0.10）、均分与trap拒答；LIST/PACK按source_group整组bootstrap的成对差异与CI。无独立null run审计时明确`calibration_status=uncalibrated`、`false_positive_bound=null`；不要求calibration_valid=true，不从p_score生成误报概率。
- budget/citation：每次预算、计数tokenizer、实际token、是否estimate、每个citation回读结果。
- latency/resources：每次样本、p50/p95、cache/压力/驻留状态、RSS、真实模型spawn与释放结果。

延迟统一使用nearest-rank `sorted[ceil(p*n)-1]`；fast warm/cold各至少30次成功fresh subprocess，按相同query交错轮换，记录JSON形状与真实退出码。MCP开销按同query/同结果配对计算每对`stdio_ms-direct_ms`再取p95，不用两个独立p95相减。失败/超时/降级保留在分母；controlled与真实保护机制运行分列。规模测试每个规模/分布/版本用全新进程测量，分开生成、查询、子模型RSS；Darwin `ru_maxrss`为bytes、Linux为KiB时显式统一为bytes，不能比较同进程累积高水位。

比例按分子/分母计算。n=25、阈值0.85时至少22题命中；不能把21/25=0.84四舍五入到一位小数0.8或0.9后判断。某子能力 n<8 或某类 holdout n<25 是输入不完整，不是提高分数后可以豁免的条件。

## 6 任务交付模板

每项在自己的报告目录写 `result.md`，使用下面格式；机器状态写回 tasks.json 及 manifest。文档和 JSON 不一致时任务未完成。

```text
任务 ID：
状态：IMPLEMENTED / VERIFIED / BLOCKED / EXPERIMENT_REJECTED
起点 commit 与最终 commit/diff SHA：
依赖任务及其状态：
修改文件与目的：
完成的验收条目：逐条列对应原始证据路径
执行命令：精确 argv、退出码、日志 SHA
指标：基准 / 候选 / n / 变化 / 门槛 / 判定
输入身份：corpus / queries / split / model / tokenizer / settings SHA
未通过或缺失项：逐条列原因、已知失败样例、下一步
副作用清理：自有进程、临时文件、用户目录未触碰的验证
是否改变协议、索引版本或默认行为：具体说明
```

填写“通过全部测试”但没有命令和日志，填写“无回归”但没有基准/候选对照，均拒收。实验拒绝报告必须有实际尝试与对照；缺模型只属于 BLOCKED/MISSING，不属于 EXPERIMENT_REJECTED。

## 7 不达标时的处理

1. 保留原始失败，不删测试、不改阈值、不改标签、不自动重建参考 baseline。
2. 先判断是实现错误、数据身份不一致、缺依赖还是目标未达；分别记 FAIL/NOT_COMPARABLE/MISSING。
3. 实现错误可在任务范围内修复并重跑被影响检查；没有新变化或新疑点，不重复已通过的大型检查。
4. 指标未达时保留最多3组预登记dev候选的全部结果，选不出合格候选就停止调参，记录根因。不得读取sealed holdout继续搜索参数；正式审计后暴露的数据不能重新包装为未见确认集。
5. 外部依赖无法获得时把当前任务标BLOCKED，推进不依赖它的独立任务；M0–M4仍不能提前通过。不能因计划规模大或耗时长就把剩余项默认关闭。
6. 只有用户明确修改目标或范围，才能修订合同。规格修改必须保留旧目标与理由，不能悄悄替换为更易通过的数字。

## 8 最终交付核对

- [ ] 24 个任务有状态；任何未完成项明确列出。
- [ ] 本地必需原始检查全部通过后再派生M0–M3与任务终态；P2-07不要求自己的VERIFIED作为验收输入。允许的实验拒绝保留完整证据。
- [ ] local_rc与hosted_release分列；未配置hosted环境时明确MISSING，不能声称GitHub CI已通过。
- [ ] 真实检索、答题、媒体、性能、MCP、安装、watch、卡片的必要检查无缺失。
- [ ] P2-08 完成才可宣称M4和竞品实测结论；未完成不冒充已比较准确率。
- [ ] 历史baseline字节保留，新reference与candidate独立，模型/题目/分割身份可核查。
- [ ] 文档、包版本、功能状态、命令示例与测量报告一致。
- [ ] 只有本地候选资产；任何发布或推送动作按后续明确授权执行。
