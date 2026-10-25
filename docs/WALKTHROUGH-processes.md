# 第五讲：双常驻进程 — llama-server 与 goldmine-embed

> 轮讲第五讲（2026-10-25）。主入口：`goldmine/backends/llama.py`
> （llama-server 生命周期 + MediaEmbedder 边车）、`tools/embed/
> goldmine-embed.c`（1412 行 C11 静态 helper）、`goldmine/serve.py`、
> `goldmine/watch.py`。读完应能答文末十问。

## 0. 一张图：一个装满的 goldmine 跑着几个进程？

```
你的 goldmine 命令（一次性 CLI）
  ├─ llama-server（常驻，文本嵌入；冷启动时被 spawn，用完被杀——只杀自己的子进程）
  │    模型：embeddinggemma-2-Q8_0（768 维，池化 MEAN）
  └─ goldmine-embed（常驻边车，媒体嵌入；同生共死）
       静态链接 llama.cpp+mtmd；mmproj-embeddinggemma-2-Q8_0
       一次加载、反复嵌入：图片（预算制 70/140/280/560/1120 token）与音频 WAV 窗口

serve 守护进程（opt-in）：常驻 llama-server + embed 边车，亚秒级搜索
watch 守护进程（opt-in）：文件事件驱动增量摄取，复用同一后端加载纪律
```

## 1. 为什么媒体嵌入是一个独立的 C 程序（不是塞进 llama-server）

llama-server 的 HTTP 面没有暴露 mtmd（多模态解码）的嵌入接口——图片/
音频进不了它。两条路：给 vendored llama-server 加接口（维护一个永久
补丁，升级即地狱），或者**自己写一个 1412 行的纯 C11 helper 静态链上
libmtmd**。选了后者：无补丁、无运行时依赖（七个静态库 + 系统框架）、
协议简单到不会错——**每行一个 JSON**（请求带 path_b64 / wav_b64，回
一个 768 维向量）。同样的"像素管线"纪律：图片缩放用的是**逐位复刻
mtmd 自己的 Pillow 风格双三次**（22 位定点、a=−0.5、48px 网格取整），
测试用黄金表钉死——helper 和参考实现的缩放结果必须逐位一致，否则
向量家族就分裂了。

## 2. 边车纪律：和子进程共舞的四条命

MediaEmbedder（llama.py）用血泪换的四条规则，破坏任何一条都是死锁或
竞态：

1. **stdout 是协议信道，只有 stderr 开排空线程**——早期版本开两个排空
   线程，协议响应被 stderr 线程吃掉，请求永远等不到回包。
2. **裸 fd + os.read 轮询**——BufferedReader 的预读会把管道里的数据
   提前吃进自己的缓冲，level-triggered poll 永远不醒。
3. **EPIPE 吞掉 + SIGPIPE 忽略**——父进程先走时 helper 不能死于信号
   噪音。
4. **超长行丢弃保活**——一行坏输出不能毒死整条连接。

启动握手：`--version` 探活 + 模型 sha 校验（mmproj 的 sha 进指纹，第二
讲）；健康失败 → NotSupported/BackendStartupError（exit 2，第四讲的
诚实降级）。

## 3. 生命周期与预算

- 冻结的推理参数：ctx=ubatch=batch=2048（文本 512）、n_seq_max=16、
  kv_unified、池化 MEAN；**gemma-embedding2 没有 KV 记忆**（llama.cpp
  源码级事实），批次间只能清空重来——这就是"常驻"省的是**模型加载的
  ~1GB 内存搬运 + 逐次 spawn**，不是缓存计算图。
- 预算制的图片：photo 140 / screenshot 280 / pdf_page 280 /
  video_frame 70 token（合法集 70–1120）。预算在**指纹里**（第二讲），
  动预算 = 全量重嵌 = 不许悄悄动。

## 4. serve 与 watch：谁常驻、谁按需

- **serve**（opt-in 守护）：常驻后端 + 心跳 socket + linger 超时自动退；
  搜索从"每次起模型几秒"变"亚秒"。能力/指纹信息由**静态探针**合并
  （不起进程也能如实回答 visual_search——第四讲）。
- **watch**（opt-in 守护）：FSEvents 文件事件 → 静默窗口合并 → 增量
  摄取（只重做变化的文件）；full_period_s **21600（6 小时）**全量兜底
  扫；毒文件清单（delta 期反复失败的不再无限重试，全量扫时另计）；
  电源门控（电池模式不起后端）。watch 的后端加载走同一个
  backend_load_s 纪律：起不来不崩、记录原因、下次再试。

## 5. 腐烂风险清单

1. **升级 llama.cpp/mmproj**：先跑参考向量（锚点第 3 条）+ resize 黄金
   表——动了任何一位，指纹身份链（第二讲）必须跟上，不许混家族。
2. **边车协议改动**：四条命（§2）是设计约束不是实现细节；任何"顺手
   优化"读缓冲的改动先想想 poll 为什么用裸 fd。
3. **常驻进程的清理**：close() 只杀**自己 spawn 的**子进程（按 pid 记
   账）——杀别人的 llama-server 是上古事故。
4. **serve/watch 的能力宣称**：一切布尔由探针派生（第四讲锚点）。

## 十问

1. 一个装满媒体的 goldmine 最多同时养几个常驻进程？各自管什么？
2. 为什么媒体嵌入是独立 C 程序而不是给 llama-server 打补丁？
3. 边车协议一句话是什么？为什么 stdout 上不能开排空线程？
4. 为什么用裸 fd + os.read 而不是 BufferedReader？
5. "常驻"到底省了什么？（提示：不是计算图）
6. 图片预算有哪些档？为什么预算进指纹？
7. helper 的图片缩放为什么必须和 mtmd 逐位一致？用什么测试钉的？
8. watch 的 6 小时全量兜底和静默窗各解决什么问题？
9. 毒文件清单防的是什么？在哪两层分别处理？
10. close() 杀进程的纪律是什么？为什么"只杀自己的"是血的教训？
