# DigUp 实证挖掘 → goldmine 优化计划

Status: PARTIALLY EXECUTED 2026-10-25 · Date: 2026-10-25(挖掘+审计)· 同日执行
**执行结果见 reports/execution/digup-plan-001/SUMMARY.md**。速览:P0-1/P0-2 PASS
落地;P1-1+P1-2 A/B 零改善按预注册 REJECT(测量基建保留);P1-3 实现绿但撞冻结
基线按规则 REJECT(配方保留);P1-4 前提部分证伪 DEFERRED(read_end≡read_to,
真正特性是响应性两段式);P2(私钥/生成/压缩/ipynb)PASS 落地;§3.9 三决策点已
预注册未实现。
Method: 5 个只读探索代理读毕 DigUp 全部 51 个 Swift 文件(14,746 行),产出 27 起
真实事故(A1–A27)与 36 条防御规则(B1–B36),全部带 file:line 与实测数字;随后对
goldmine 逐项核实继承状态。DigUp 仓库只有单个 squash 的 Initial commit,git 史不可用,
代码中的带日期注释是唯一的历史记录——本计划中的每条教训都可回溯到 DigUp 源码原文。

Review record(2026-10-25,三个独立审计代理):
1. **DigUp 证据核查**:24 项引用中 21 项逐字核实(含全部实测数字),3 项仅行号
   漂移,1 处实质错误(电池索引默认方向)——已修正;
2. **goldmine 差距核查**:14 项断言中 10 项 GAP-CONFIRMED,3 项 ALREADY-HANDLED
   (模型下载契约、弱匹配折叠、watch 电源门控),1 项前提有误(P1-4)——已修正;
3. **多模态可行性研究**:总体 FEASIBLE,vendor/llama.cpp@b11461 含全部所需 C API
   符号(mtmd_*/llama_batch_ext_*),但 helper 必须常驻而非一次性,验收标准与
   套件规模(128 非 169)已修订。

---

## §0 已核实:goldmine 已经继承的部分(不需要动)

这些 DigUp 教训在 goldmine 已正确落地(逐项核实过代码),列出以防重复劳动:

| DigUp 教训 | DigUp 证据 | goldmine 现状(已核实) |
|---|---|---|
| OCR 文字向量让截图挤掉更相关结果(2026-10-08) | Indexer.swift:509-513;迁移 2→3 | `extraction.py:486` `_ocr_seg` 的 `cap.image` 门控;llama 后端下 `ocr_text_vector` 配置默认给文字向量(正确的降级) |
| 向量身份不键在 llama.cpp build 上(迁移 3→4) | IndexStore.swift:156-158 | fingerprints 记录 build 但不进指纹(`llama_build` 仅为 provenance) |
| z-score 只做分模态均值、全局 std(2026-10-08 反转) | Searcher.swift:306-310 | `search.py:589` `zscores()` 分模态 centering + 全局 std + 0.02 floor |
| FTS tokenizer 需 `M*` 类别(孟加拉/阿拉伯语拆词,迁移 1→2) | IndexStore.swift:25-27 | 同款 tokenizer DDL |
| 数字占比过低的文本不给向量(970k 字符数字堆 = 607 个垃圾向量) | SearchText.swift:33-43 | `extraction.py:112` `_has_words` 闸门 |
| xattr 截图检测,文件名前缀仅兜底 | Rules.swift:139-144 | `rules.py:179` ctypes getxattr |
| dataset 目录检测(300+ 同类媒体、70% 机器命名) | Rules.swift:164-188 | `rules.py:325` `dataset_folders` |
| TCC/不可读根不得当成"空"(F8) | main.swift:121-126 | `walk.py:83-87` PermissionError → surfaced;**且 `pipeline.py:158-184`(F10 phantom roots)在 ingest 开始时 stat 每个根,开始时已消失的根会作为 per-root failure 排除,不会进删除计算** |
| `is_lookup`:数字/代码查询词重、含义轻;数字长串(≥4 位)计入 | Searcher.swift:265-273 | `search.py:572` 且做了自有 A/B 演进(ANY-term 规则,lookup MRR 0.594→0.609) |
| MPEG-TS sniff(.ts 是 TypeScript 不是视频) | Rules.swift:129-137 | rules/walk 已有 |
| 向量与行同库,崩溃不会 desync | IndexStore.swift:17-19 | 同设计 |
| fp16 存储(70k 向量 108MB,查询 +2ms) | Vectors.swift:23 | 同设计 |
| **模型下载契约(.partial+Range 续传、2/5/15/30s 重试阶梯、200 应答即重头来、短读检测、200MB 余量预检、大小+sha256 校验通过才 rename)** | ModelDownload.swift:7-13, 111, 305-307, 336, 355-359 | **已实现于 `model.py`**(269, 274-290, 295-310, 357-381, 401-445),`tests/test_model.py:285-400` 覆盖;init 共用同一路径。残余仅一项:未校验 206 `Content-Range` 起始偏移(错误偏移会追加垃圾,靠 sha256 门兜底后重来)——可选加固 |
| **弱匹配折叠(0.5×top,26.2→5.7 条/查询,从未折掉预期答案)** | Searcher.swift:275-277 | **已实现**:`render.py:147-158,196-197`(human 模式无条件折叠,`--json` 保留全量),另加近重复折叠(cos 0.97) |
| **watch 电源门控(thermal 暂停全部、内存压力 ≥2 推迟嵌 pass、电池模式 delta pass ≤50 文件 + 120s 间隔)** | PowerPolicy.swift:54-63(注:电池索引默认开启,仅用户关闭时才阻塞 backfill) | **已实现**:`watch.py:771-792` pmset 探测 + `config.py:129-136`;低电量模式有意只报告不阻塞(H12) |

§0 的残余差异(非缺口,记录设计差异):watch 是自适应轮询(5s→120s)而非 FSEvents
事件;settle 2.0s;全库单一 `--code` 旗标,无按代码根的 60s/600s 静默窗与 codeNoise
列表(见 P2)。

---

## §1 P0 — 正确性/数据安全

### P0-1 根目录**中途**消失不得当成"全部删除"

DigUp 事故 A18:移动硬盘爬取中途弹出,该根看起来"空了",sync 险些把整盘文件当
"已删除"清掉。守卫在两处:
- DigUp Indexer.swift:118-120 — "A root that went away while it was being walked
  (a drive ejected mid-crawl) looked emptier than it is: keep its rows rather than
  take that for 'deleted'."
- DigUp IndexService.swift:22-23 — 每次 sync 前重查可用性:"That's checked
  before every sync, not once."

goldmine 现状(审计后精确化):**开始时的守卫已存在**——`pipeline.py:158-184`
对每个根 stat,失败者作为 per-root failure 排除出 `resolved_roots`,其行不会进
`compute_deletions`。**真正的缺口是运行中途**:开始 stat 成功后、`compute_deletions`
(`walk.py:247-268`,调用点 `pipeline.py:313-318`)执行前根消失/变不可读 → 该根
所有行按 `os.path.exists` 判删。另一个入口:子目录 `scandir` OSError 仅告警并静默
返回(`walk.py:150-152`),该子树不产出任何行,已索引行随后在删除计算时全部
`exists=False` 被剪。

动作:
1. `compute_deletions` 删减前逐根重新 stat(存在+可读);不可达根 → 该根行保留,
   报告明确列出 "root unavailable, N rows kept";
2. 子目录 OSError/PermissionError 的子树同样按"不可达"处理(行保留+报告),不按
   "已删除";
3. 回归测试:摄取 tmp 根 → 运行中途重命名根目录/子目录(模拟拔盘)→ 再 ingest →
   断言行仍在、报告有 kept 标记;根恢复后正常续作。

### P0-2 参考向量钉死(ReferenceVectorTests 纪律)

DigUp 事故 A3 的完整教训有两半:指纹不含 build(已继承);另一半是
DigUp LlamaEmbedder.swift:18-22 — "every llama.cpp update must reproduce the
reference vectors, or come with a new version here (then every index embeds
again)". llama.cpp 更新时用**钉死的参考向量测试**判断向量是否漂移:复现 → 指纹
不动,不 reindex;不复现 → vectorVersion +1,宣布 reindex。DigUp 的测试形态:
`Tests/LlamaRuntimeTests/ReferenceVectorTests.swift`,钉 `vector_version` 标注的
`Reference/vectors.json`,阈值 cosine ≥ 0.999,输入含 prompt 文本(约 1800 token
长文档、bn/ar 查询)+ screen.png@280 + photo.jpg@140/@70 + 16kHz WAV;无模型时
跳过,仅 `DIGUP_WRITE_REFERENCE=1` 可再生。

goldmine 现状(已核实):无任何参考向量测试(`tests/test_live_llama.py:62-83` 只
查形状/范数/prompt 差异);build 变化只发 note 建议 reindex(`pipeline.py:343-348`);
`docs/BUILDING.md:3` 钉 b11461 拒绝其他版本,但无向量复现流程。

动作:
1. 选 8–10 条参考输入(短/长文本、中英混合、代码、查询 prompt 与文档 prompt
   各若干),把 embeddinggemma-2-Q8_0 @ b11461 的输出向量存为钉死的期望
   (sha256 或逐向量 cosine ≥ 0.999 判定);
2. 测试放在 native/llama profile(需要真模型),hermetic 套件跳过;提供显式
   再生开关(环境变量),杜绝意外覆盖;
3. 升级流程写入 docs/CI.md:先跑参考向量测试,复现 → 直接换 build;不复现 →
   指纹 preprocessing/vector_version 变更 + reindex 通告。
4. 多模态落地后(§3)参考输入扩图像/音频各 2 条。**注意**:参考向量必须由
   goldmine 自己的管线生成(sips 缩略图 → helper),不能照抄 DigUp 的
   vectors.json——第一阶段像素管线不同(sips vs ImageIO,见 §3.2)。

---

## §2 P1 — 实测支持的检索改进(小改动,带 A/B 数字)

### P1-1 code 块标题用仓库相对路径(范围:仅 code 文件)

DigUp 实测(CodeExtractor.swift:15-17,2026-10-09 real-code eval,27 queries):
文件名做 title 22/27 top-5、MRR 0.69;仓库相对路径(`repo 名开头`)24/27、
MRR 0.79。

goldmine 现状(审计后确认):**code 文件的 title 确为文件名 stem**
(`extraction.py:137-153` 的 code 路径不带 title → `commit.py:253`
`title = outcome.title or Path(cand.path).stem`)。但**文档类已有语义标题**:
HTML `<title>`(`extract_html.py:106-119`)、邮件 Subject(`extract_mail.py:91`)、
markdown 首个 `#`(`extract_text.py:101-111`)、docx/epub DC title
(`extract_text.py:137-153`)——本项只改 code 文件,不动文档路径。
`rules.PROJECT_MARKERS`(`rules.py:114-118`)已存在,向上找 repo root 成本低。

注意:改文档向量 → 指纹 prompts/preprocessing 变化 → 与 P1-2 合并成同一批次
reindex,先在代码评测集 A/B 再采纳。

### P1-2 code 查询提示词分离

DigUp 实测(Model.swift:92-95,同上 eval):`"task: code retrieval | query: {}"`
对代码查询 24 vs 22 top-5、MRR 0.79 vs 0.75(对比 search prompt)。

goldmine 现状(已核实):单一 `QUERY_PROMPT = "task: search result | query: {}"`
(`backends/base.py:19`,唯一查询点 `search.py:368`),无 "code retrieval" 字样,
search CLI 也无 `--code` 作用域。查询侧不影响已存向量,但 fingerprint 的 prompts
字段需记录双提示词(指纹版本+1,与 P1-1 同批)。fingerprints 已键入查询 prompt
(`fingerprints.py:161,223`),机制就绪。

### P1-3 移动检测(device+inode)

DigUp IndexStore.swift:261-269 + Indexer.swift:108-112:同 device+inode+size
+mtime 的新路径 = 改路径不重嵌("Screenshot tools move screenshots into project
folders all the time")。

goldmine 现状(已核实):files 表无 inode/device 列(`storage_connections.py:23-40`,
schema v6,`path UNIQUE` 为身份);`st_ino/st_dev` 只用于 walk 防环
(`walk.py:91-93`)。改名 = 新行全量重嵌 + 旧行 prune。加列小迁移(schema v7),
sync 时对"消失行"做一次 inode 反查。

### P1-4 长文件续读(finishLongFiles 等价物)

DigUp 事故 A4:首段步进把 >50 页 PDF 截在 30 页(标 partial)、**40-chunk 文档**
截断且误标 done;修复 = 显式 read_to/read_end + 后置续读,每步一事务
(Indexer.swift:169-209),排序理由:"a book's last pages matter less than
everything else's first ones"(0.04–0.2 s/页)。

goldmine 现状(审计后修正):**撞 caps 的文件标 `state='done'` + `info['partial']=True`
并被 `_skip_unchanged`(`pipeline.py:441-442`)跳过——不会从头重做,也不会重试**
(原稿"每次从头重做"是错的;`store.unchanged()` 仅测试使用)。PDF 侧已记录
`read_to/read_end`(`pdf_evidence.py:74-76`),音频/ASR 侧有 `read_end`
(`extraction.py:236-238`,`asr.py:372`)。**真正的缺口:没有任何代码消费
read_to/read_end——撞 caps 的文件永远无法越过 caps 获得更多内容;无 pending 优先、
无逐步续读事务。**

动作:引入显式续读 pass(一切 pending 清零后,从 info.read_to 续读到 read_end,
每步单事务);设计上保留 done+partial 标记的现有语义(不做"不得是 done"的
反转——那是现状的有意设计),新增的是"显式 `--finish-long` 或自动后置 pass"。

---

## §3 P1/P2 — 多模态落地(DigUp 生产验证的完整规格;审计结论:可行,按下列修订执行)

目标:接通图像/音频嵌入,翻转 `visual_search=false`。DigUp 用 goldmine 钉死的
同一份 mmproj(bfcd298,字节级相同——双方 manifest 逐 sha256 核对一致)在生产
跑通。可行性研究确认:vendor/llama.cpp@b11461 含全部所需 C API 符号
(`mtmd_init_from_file` mtmd.h:133、`mtmd_tokenize` :302、`mtmd_encode_chunk` :326、
`mtmd_get_output_embd` :332、`llama_process` llama.h:1088、
`llama_get_embeddings_seq` :1165、`llama_batch_ext_*` 家族 :1014-1080),
后端抽象的接缝(`base.py` 的 `embed_image/embed_audio_wav`、mock 已实现的
多模态测试链路、commit 路径的调用点)全部就绪。规格如下:

1. **架构(修订)**:**常驻子进程** helper(照 `llama.py` 的 spawn/health/kill
   模式),**不是** goldmine-ocr 的一次性模式——审计测算:5,000 张照片在
   常驻模式下约 10–13 分钟;若每 wave(16 文件)重启一次 ~1 GB 模型,加 50–100%
   开销;若每次调用重启则约 2.8 小时纯加载,不可行。实现语言推荐**纯 C 单文件**
   (对 llama.h+mtmd.h,clang 链接),比 Swift+modulemap 简单;构建照抄 DigUp
   `scripts/build-llama.sh` 的 recipe 并入 `build_native.py`:cmake 目标加
   `llama mtmd`(当前只建 `llama-server`,vendor 树**尚无 libmtmd.a**)→
   `xcrun libtool -static` 合并为 libllamacpp.a → 资产打包/校验沿用现有框架。
   全量加载 ~1 GB,文本 only ~0.25 GB;查询侧仍走 llama-server 纯文本。
   helper 上下文参数(DigUp LlamaEmbedder.swift:60-70):ctx=batch=ubatch=2048
   (文本 only 512)、pooling=mean、kv_unified、n_seq_max=16。
2. **图像缩放必须在 goldmine 侧精确实现**(DigUp 事故 A26,ImageSizing.swift:1-8,
   22-bit 定点在 :28-29):mtmd 只对超范围图像缩放、不填满预算;照抄 mtmd = 向量
   与参考实现漂移。移植 targetSize(patch 16、pooling 3,按预算填满 soft tokens,
   边长 48px 取整)+ Pillow bicubic 内核(a=−0.5),并设
   `image_min_tokens=1, image_max_tokens=1120`。**验收标准(修订)**:第二阶段
   (targetSize+bicubic)可逐位对齐;"cosine ≥ 0.999" 必须对照 **goldmine 自己
   生成的参考向量**(P0-2 扩展),因为第一阶段缩略图管线不同(goldmine 用 sips
   `media.py:259-377`,DigUp 用 ImageIO)——不引入 Pillow 依赖,端口放 helper 内。
3. **预算常量**(实测):photo 140 / screenshot 280 / PDF page 280 / frame 70;
   合法集 [70,140,280,560,1120];560 = "2× slower, no better on real shots"
   (DigUp main.swift:35)。预算值与**按类路由规则**从第一天起作为冻结常量写进
   `fingerprint_fields()`(DigUp Model.swift:84 同粒度;goldmine 的 embed
   identity 是全索引级——任何预算变化 = 全库 embed-stale,与 DigUp 一致,可接受)。
4. **抽取参数**(实测,第一阶段 goldmine 已对齐 sips 版):照片 1024px JPEG、
   截图 1536px PNG、minSide 256 拒绝、视频关键帧 3s/aHash≤4bit/maxFrames 1200、
   音频 30s 窗/25s hop/16kHz mono/RMS>0.003 静音闸/尾部 <2s 丢弃。
5. **政策自动就位**:多模态后端一接入,`_ocr_seg` 的 `cap.image` 门自动关掉
   OCR 文字向量(§0 已核实);thin/扫描 PDF 页 → 渲染页图向量 + 乱序文本
   "含义专用"向量(实测 R@1 0.75 vs 0.73,Extractors.swift:108-111)。
   **新增待决(审计发现的政策冲突)**:语音转写后不给原始音频向量已钉死
   (docs/v2/02:222),但哪些音频拿原始向量(音乐?ASR 失败的?RMS 过闸但无
   语音的窗口?)未定义——需在实现前定规,并写入指纹。
6. **迁移纪律**:mmproj sha256 + 预算常量进 fingerprint extras——机制已就绪
   (`fingerprints.py:62-76` extras 自动并入指纹与 embed identity,
   `live_embed_identity` :305-330 自动产生逐文件 staleness);exit-3 拒绝链
   (`pipeline.py:339` → `watch.py:869` → `cli.py:158-165`)不用动。
7. **验收闸门(修订)**:盘上全媒体套件实际为 **128 题**(real.jsonl 78 +
   media_queries.jsonl 50,其中 19 题 needs-gated),README 的 "169" 已过时——
   先重新基线化(或补齐缺题)再作为正式闸;EXIF 半侧已完成(corpus v5,
   d23c3bf),OCR/ASR 出处需 `build_eval_corpus.py --backend llama` 真跑。
   注意:preflight 只查文本/元数据证据,图像向量经由检索指标间接检验——正式
   gate 与 `visual_search` 翻转相关但不等同。
8. **z-score 安全**:图像/音频向量直接进现有分模态均值+全局 std
   (`search.py:589-604` 零改动;同模态基线差异 0.75–0.86 vs 0.67–0.78,
   DigUp Model.swift:30-31)。
9. **新增风险决策(审计补)**:
   - **双模型常驻**:媒体 ingest 期间 llama-server(文本 ~0.25GB)+ embed
     helper(~1GB)同时驻留、共占 Metal(合计 ~1.3GB)。选项:接受 / 文本
     暂走 helper(会改 `pooling:"server:mean"` 指纹字段 → 强制 reindex)。
     默认建议:接受;
   - **watch 逐 pass 加载**:`watch.py:853-859` 每 pass 新建 backend,媒体
     pass 每次付 ~1GB 加载。选项:接受 / keep-alive 窗口 / 走 serve 路由;
   - **helper 契约含 EPIPE/SIGPIPE 纪律**(DigUp main.swift:7,
     IndexService.swift:331-336):对已关闭管道的写出吞错,SIGPIPE ignore;
   - **serve.py 线协议**:若媒体嵌入未来经 serve 提供,需新增 ops;查询侧
     保持纯文本则 serve 仅影响 describe/capability 诚实上报;
   - **vector_cache 无破坏**(已核:模式校验已含 image/audio,`_vector_cache_files.py:64-66`)。

---

## §4 P2 — 运维与产品细节(DigUp 实证,按价值挑;审计后增删)

| 项 | DigUp 证据 | 动作(审计后) |
|---|---|---|
| `goldmine estimate` | Estimator.swift:5-16(365 文件 160s@M4 Pro;截图 0.32s 含 OCR、PDF 0.09s/页、音频 0.0089/s、代码 0.037s/stretch) | 只读元数据估算首段时间;init/大根摄取前的预期管理 |
| watch 按代码根的静默窗 | Engine.swift:382,395,435-445;codeQuiet 60s 默认在 AppSettings.swift:39-40,77;codeLongestWait 600 在 Engine.swift:110 | 增补:goldmine watch 若引入按根 code 模式,加 60s 静默窗 + codeNoise 列表;电源门控已实现(§0),不重做 |
| 代码规则补全 | CodeExtractor.swift:35-63;Rules.swift:57-64, 100-104;ipynb 在 CodeExtractor.swift:150-163 | **2026-10-25 已执行+用户修正**:(a) ipynb 按 cell 读、输出永不读 — 已落地(此前 base64 输出进索引);(b) 内容级 minified + 生成文件标记拒读 — 已落地(质量判断);(c) ~~`PRIVATE KEY-----` 内容扫描~~ — **用户否决(纯本地,无此硬规则),已移除并用测试反向钉死**;扩展名级 is_secret 未动;(d) `.git` 为文件已不读;vendored/build 目录表为扩展既有 `SKIP_FOLDERS` 而非新建,待办 |
| iCloud dataless 跳过 | Crawler.swift:85-91(SF_DATALESS 0x40000000 + 非 current) | 未下载占位 → 跳过并报原因,绝不触发下载(goldmine 现状未核到等价物,需查 walk 的 st_flags) |
| EPIPE 收尾(窄化) | IndexService.swift:331-336;main.swift:7 | 审计:serve.py/rerank.py/ocr.py **已安全**(连接循环 OSError 静默/子进程 stdout 落文件/父侧捕获);残余两处:`mcp_server.py:94-119` stdio 无 BrokenPipe 吞错;`cli.py:246` 无 BrokenPipeError 捕获(`goldmine search \| head` 走通用 handler) |
| 停机不停读(毒文件) | Indexer.swift:483-492 `stopReading` | 中途失败的文件保留已读内容、记 done。**须与 P1-5 反陈旧文本规则调和**(`pipeline.py:557-559`:_record_failed 删段是防"搜索服务于已不能抽取的文件文本"的有意设计;watch 已有 delta-pass 毒文件清单 `watch.py:950-976`,sweep 仍无限重试)。建议:仅"读入中途 IO 错误"保留段,抽取器明确拒绝仍走现行删段 |
| A19 查询期 fileExists 过滤 | Engine.swift:749-760 | goldmine 检索路径无磁盘存在性检查(`search.py:441-494`);已删文件滞留到下次 ingest/watch sweep(full_period_s 21600)。移植时注意 URL 行豁免(与 `compute_deletions` 语义一致) |
| mmap 向量缓存 | Vectors.swift:22-27(60MB 矩阵 free 后滞留) | Python 侧 malloc 行为不同;10 万+向量级先测 np.memmap 再定 |

已从 P2 移除(审计确认已实现):模型下载契约(§0)、watch 电源门控(§0)、
弱匹配折叠(§0)。

---

## §5 P3 — 明确不采(带测量依据的拒绝)

| 项 | DigUp 实测 | 决定 |
|---|---|---|
| RRF 融合 | 输给加法融合:R@1 0.88 vs 0.73 synthetic / 0.86 vs 0.82 real(Searcher.swift:168,2026-10-07) | 保持加法融合;`--scorer rrf` 仅作消融基线 |
| 分模态 std 缩放 | "inflated the narrow ones and buried PDF pages under pictures"(91→94/105,Searcher.swift:306-310,2026-10-08) | 保持分模态均值+全局 std |
| 长文件页参与 z-score 尺度 | 书的几百页拉低**文本模态均值**(均值按模态分算,Searcher.swift:312-315,334-350;2026-10-09) | 多模态/长文件落地时评估 rowSetsScale 等价物;纯文本现状影响小,暂缓 |
| F16 模型文件 | "Never the F16 files: the model's activations overflow float16"(ModelFiles.swift:4-6) | 已只钉 Q8_0;写进 manifest 注释 |
| OCR .fast 模式 | 省 8% 时间、少读 9% 字符、丢精确命中(Extractors.swift:75-78,2026-10-07) | 保持 .accurate(现状一致,记录依据) |
| GUI 层产物(App Nap、按键预嵌、QuickLook 定时、embedAhead、QoS) | SearchPanel/Engine 各处 | CLI 不适用;serve/watch 若做交互式前端再回头 |
| **code 仓库路径标题 + code 查询提示词(P1-1/P1-2)** | DigUp 实测为正(24 vs 22 / MRR 0.79 vs 0.69、0.79 vs 0.75);**goldmine 自测为零改善** | **2026-10-25 按 goldmine 自己的 A/B(85 文件语料、21 题、真 llama)REJECT**:hybrid 的词汇精确匹配已使 s@5 饱和于 1.000,DigUp 先验未迁移。回执 reports/execution/digup-plan-001/p1-code-title-prompt/;基建保留,无新证据不重提 |

---

## §6 执行顺序与闸门

1. **P0-1 / P0-2**(守卫 + 参考向量测试):不动向量、无 reindex,先行;
2. **P1-1 + P1-2 合并一个指纹批次**:代码评测集 A/B,赢了才采纳,一次 reindex;
3. **P1-3 / P1-4**(move 检测、长文件续读):独立小迁移,可并行;
4. **多模态(§3)**:独立里程碑,`goldmine-embed` 常驻 helper + ImageSizing 精确
   移植 + 指纹迁移 + 全媒体套件重新基线化(128 题)后转正;先落 §3.9 的三个
   决策(双常驻、watch 加载、音频向量政策);
5. **P2 按顺手插入**(ipynb 修复与私钥内容扫描优先——它们是当下的正确性缺口,
   不只是优化)。

所有采纳以 goldmine 自己的 acceptance profile 为闸:DigUp 的数字是先验,不是证明。
每项落地时在代码注释中回引 DigUp 的 file:line 出处,保持本仓库"教训带出处"的
记录纪律。

---

## 附录:DigUp 事故索引(A = 真实事故,B = 预防规则;摘要)

与 goldmine 相关的 A 类事故(完整清单见挖掘报告):

- **A1** FTS 默认 tokenizer 拆散孟加拉/阿拉伯词 → `M*` 类别(已继承)
- **A2** OCR 文字向量挤掉更优结果 → 关键词专用(已继承,含门控降级)
- **A3** 向量身份键在 build → vectorVersion + ReferenceVectorTests(P0-2 半继承)
- **A4** 长文件截断(40-chunk 文档误标 done;>50 页 PDF 本就 partial)→ partial + 续读(P1-4 未继承)
- **A5** 分模态 std 放大窄模态 → 全局 std(已继承)
- **A6** 长文件后段污染文本模态 z-score 尺度 → rowSetsScale(暂缓,多模态时重评)
- **A7** code 词权重过高 → docs 2.0 / code 1.0(待 A/B;goldmine 现状全局 1.0/2.0
  (lookup 0.5/4.0),无种类区分,且 KIND_WORDS 无 code 词条——code 文件连
  +1.0 kind hint 都拿不到)
- **A8** RRF 输给加法融合(P3 记录)
- **A9** 定义查找全量跑 → 前 10 结果内(560→100ms;代码检索深化时一并考虑)
- **A10** 数字查询沉底 → is_lookup 扩展(已继承)
- **A11** OCR .fast 丢精确命中 → .accurate(已继承)
- **A12** malloc 缓存滞留 60MB → mmap(P2 评估)
- **A13** Vision 模型缓存 +77MB 常驻 → helper 进程退出(goldmine-ocr 模式,已继承)
- **A14** EPIPE 崩溃 → 吞错(P2 窄化至 mcp/cli 两处)
- **A15** QuickLook displayState 时序(GUI,不采)
- **A16** 970k 数字堆 = 607 垃圾向量 → hasWords(已继承)
- **A17** 孟加拉 glyph-order / 阿拉伯连字丢失 PDF → 页图 + OCR 替换(多模态一并)
- **A18** 拔盘误删(P0-1:开始时守卫已有,中途缺口未补)
- **A19** 已删文件滞留结果 → 查询期 fileExists 过滤(P2,含 URL 豁免)
- **A20/A21** App Nap/GPU 闲置(GUI,不采)
- **A22/A23** 下载续传的 URL size 缓存与 Range 丢失(已由 model.py 实现,残余 Content-Range 偏移校验)
- **A24** SwiftUI 内 resize(GUI,不采)
- **A25** F16 文件毒性(已规避,记录)
- **A26** mtmd 缩放与参考不一致 → 自行精确缩放(多模态关键规格)
- **A27** A/B 调参记录(codeStretch 1800、shot-tokens 280、标题/提示词等,已散入各节)
