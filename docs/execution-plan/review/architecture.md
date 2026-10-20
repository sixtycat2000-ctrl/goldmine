# 架构与权限边界对抗审查

审查对象为 2026-10-21 的 P0–P2 执行计划，以及当前 Store、迁移、检索、pack、CLI 和模型 RPC 的真实约束。审查采用数据库/检索/MCP 边界视角和 [thermo-nuclear-code-quality-review](/Users/yul/.codex/skills/thermo-nuclear-code-quality-review/SKILL.md) 的结构标准。此报告的 ACCEPTED 表示计划修订已接纳，不表示相应实现或验收已经完成。没有把真实持牌专家或付费工具的评审作为事实。

审查阶段只运行临时目录内的 Python/SQLite/NumPy 反例；没有修改生产源码、用户索引、模型或外部配置。以下行号指审查时的代码，后续重构可能移动。

## ARCH-01 请求缺少统一 SQLite 读快照

- 严重度：P1；处置：ACCEPTED，落在 P1-01，P1-09/P1-10 复用。
- 证据位置：`goldmine/search.py:319` 读取向量后，`417–455` 再读取段、文件和正文；原 P1-01 只要求只读连接。
- 破坏场景：watch/ingest 在上述步骤之间提交替换，SQLite 复用整数段 ID；随后输出旧向量的评分和新段正文，citation 甚至可能按新正文计算而看似有效。
- 已实跑：临时 25 文件、768维向量库；注入 backend 的 embed_texts 在另一连接调用 replace_segments。原段 ID 25 被复用；返回 `cosine=1.0`、`segment_id=25`，正文为 `NEW unrelated text`。全程无用户文件参与。
- 最小修订：每个 search/get/context/status 的相关读取在同一显式只读事务内；search→pack/citation 不能跨快照。只读连接和向量缓存失效不能替代事务快照。
- 验收：在取向量、取正文、打包之前分别插入另连接的替换/删除；每个结果所有内容来自同一 generation，后续新请求看到已提交新内容；读调用不写 schema/元数据。

## ARCH-02 SQLite 只读 URI 路径未转义

- 严重度：P1；处置：ACCEPTED，落在 P1-01。
- 证据位置：`goldmine/store.py:308` 使用 `file:{path}?mode=ro`。
- 破坏场景：POSIX 合法文件名中的 `?`/`#` 被解释为 URI 部分，打开错误文件，甚至使 mode=ro 失效而创建空库。
- 已实跑：创建 `index?literal.sqlite` 后调用 connect_index(ro=True)；PRAGMA database_list 指向旁边的 `index`，user_version=0，目录多出 `index` 文件。
- 最小修订：将显式选择路径解析为绝对 Path，再调用 as_uri()，然后附加 mode=ro；只读构造不 mkdir、不执行 DDL、不迁移。
- 验收：`?`、`#`、`%`、空格、中文、相对路径和不存在路径均验证；比对打开的 database_list、原库 UUID/内容及父目录文件集合。

## ARCH-03 固定包头本身可超过预算

- 严重度：P1；处置：ACCEPTED，落在 P1-10，并成为 P1-01 共享服务错误边界。
- 证据位置：`goldmine/pack.py:503–532`；原合同允许8192字节 query 和512 token预算，未定义不可容纳包头的结果。
- 已实跑：`PackSettings(query="x"*8192, budget=512)`、空 hits、估算计数；返回正文长度8527字符、used_tokens=2131，预算512，records=0。
- 最小修订：完整 context_text 的包头、查询、出处、why、footer 均计数。无证据可放入时仍检查空包；连必需有界空包都放不下则明确返回 budget_too_small，不能返回超预算正文。
- 验收：最小预算与最大合法 query、长路径、长出处、长 why 的组合；exact 和 estimate 模式分别检查，不能把未知 token 数当0。

## ARCH-04 升级前已打开的旧 writer 可破坏派生索引

- 严重度：P1；处置：ACCEPTED，落在 P1-02/P1-03。
- 证据位置：`goldmine/store.py:337–367` 仅构造时检查 schema；`402–458`、`524–539`、`671–686` 后续写入不重新核验当前 writer 协议。
- 破坏场景：旧 watch 持有 v4 连接，另一进程升级 v5/v6。旧连接随后只维护旧 fts，新的 fts_cjk/lookup_terms 静默失配；meta.lexical_version 仍声称完整。SQLite user_version 不会自动撤销旧连接写权限。
- 最小修订：数据库级 writer 版本栅栏，并在新连接注册明确版本函数；升级后已经打开的旧连接也被拒绝。进一步反例验证发现 SQLite 禁止在 FTS shadow table 上建触发器，普通触发器调用旧连接不存在的函数又只会中止当前语句。最终采用持久 files/segments/meta 无条件 RAISE(ROLLBACK) 栅栏；新 writer_scope 在 BEGIN IMMEDIATE 的写锁内核验版本，暂移栅栏、写入、恢复后才提交，未提交 DDL 不对其他连接可见。嵌套 Store 调用不 commit。该实现位于 storage_writes.py，Store.write_transaction() 提供唯一原始写入口。没有增加通用锁/daemon 框架。
- 验收：保留升级前 v4/v5 writer 活连接，升级后分别执行 upsert、replace、delete、failed cleanup；要么维护全部新派生数据，要么事务完整拒写。重点检查旧路径先删 FTS 后被拒绝时不会留下半状态；向量 BLOB 不变，FK与完整性检查通过。

## ARCH-05 metadata 与正文 citation 混淆

- 严重度：P1；处置：ACCEPTED，落在 P1-01，并约束 P1-03/P1-05/P1-09。
- 证据位置：`goldmine/store.py:756–765` 的 segment_text 只取 fts.original/body；`1098–1108` 的 name body 是 Latin folder words，并非文件名/正文，可能为空。
- 破坏场景：精确文件名命中关联 name segment，但 get 根据原文契约返回空字符串，或执行 agent 为了满足“原文”补造正文。另一缺口是只哈希正文，存储 info/EXIF 改变后旧引用仍被接受。
- 最小修订：Citation evidence_type 固定 content/name_metadata/vector_only，媒体细类另记；get 可返回已存元数据和明确无正文状态，不读取源文件。digest 唯一 canonical 输入为：

```text
["gm-citation/v1", file.id, file.path, file.source, file.sha256,
 segment.id, segment.kind, segment.modality, segment.loc, segment.loc_end,
 evidence_type, parsed_file_info, stored_text]
```

使用 UTF-8 JSON，ensure_ascii=false、sort_keys=true、separators=(",", ":")、allow_nan=false；对完整字节 SHA256，ID 取前16位。parsed_file_info 为已存 JSON 或 null，stored_text 为 coalesce(original,body)，没有 FTS 时 null，空字符串不转换成 null。不含分数、indexed_at、时间或PID。UUID标识索引谱系，备份副本保留同一 UUID。

- 验收：name body为空、纯中文路径、metadata-only照片、无FTS向量段；修改 info/文本/位置后旧引用失效；不相关库拒绝，保留原身份的未变备份可读；各入口共享相同 digest 代码与测试。

## ARCH-06 deadline 与资源所有权不足以约束底层调用

- 严重度：P2；处置：ACCEPTED，落在 P1-01/P1-07/P1-09。
- 证据位置：`goldmine/store.py:308–315` 的60秒 busy timeout；`goldmine/backends/serve.py:472–558` 的多层重试和 cold fallback；`goldmine/backends/llama.py` 的独立 startup/request timeout。
- 破坏场景：MCP 外层 timeout 已返回，工作线程仍在等待、重试或启动冷模型，串行队列被占据。单请求统一 close 与 reranker“五分钟空闲后释放”也没有确定持有者。
- 最小修订：单个 monotonic deadline 将排队和每次阻塞计入；连接、模型启动、RPC、重试、token count、rerank 使用剩余时间，耗尽后不再开始新调用。请求拥有 Store/临时 backend；可复用 reranker 由 MCP 会话显式拥有，绝不共享跨线程 Store。
- 验收：受控阻塞/重试、取消/EOF/SIGTERM、超时后无后续请求、自有进程清零、共享 serve 未被杀；会话 reranker 释放与单次 CLI backend 关闭分别验证。

## 结构处置

MCP 基础协议只依赖共享服务和 index_uuid，不等待可选重排/多查询实验；planner 的基础实现也不依赖 reranker 成功。最终组合能力仍在 jointrelease 一起验收。搜索 adapter 在 P1-01 的服务边界移出大 CLI，其他命令由 P2-01完成；不增加通用框架或无意义传递包装。所有日常产品质量检查只使用 train/dev，最终 holdout 在组合候选冻结后一次开启。

## 实现验证记录

P1-01 的共享服务/连接/引用/deadline 边界已有离线实现；focused 81项通过。P1-02/P1-03 的 Unicode检索、raw/alias排序、迁移与旧writer拒写实现后，连同受影响既有测试运行375项全部通过（98.33秒）。补充“文件名编号不能伪装正文位置”反例后，相关65项通过（1.06秒）。这些测试集合有重叠，不相加为总测试数量；它们均不替代真实模型、产品 dev/reference 或最终 holdout 的任务验收。相应任务在真实质量证据补齐前仍为 IMPLEMENTED。

新增 test_lexical_migration.py 实跑了：v4→v5→v6向量字节/原文/UUID保持；升级前已打开v4连接先删FTS、再被触发器拒写后整个事务回滚，旧commit也不能发布半状态；v5连接在v6升级后被拒绝；嵌套写异常恢复所有栅栏；SIGKILL中断回填后仍是完整v4，再运行可升级至v6。没有修改历史检索/答题基线或用户索引。
