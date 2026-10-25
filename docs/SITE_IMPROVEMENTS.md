# 建站过程发现的改进点（site-001，2026-10-25）

> 规约：过程中发现的 goldmine 本体可改进点如实记录于此；本文件只记录、
> 不顺手修（每条注明建议与证据）。按发现顺序编号。

## 产品/文档

1. **JOURNEY.md 与第五讲对 ASR 引擎的表述不准**：文档写 whisper 由
   llama.cpp 运行；实际是独立的 whisper-cli 二进制（`goldmine/ingest/asr.py`
   可证，silero VAD 以 `--vad -vm` 参数挂接）。教学数据制作时核实。
   建议改两处文档（JOURNEY B 组词条、WALKTHROUGH-processes §1）。
2. **A2 的 deleted_filtered 计数只在 quality 模式 --json 可见**：fast 模式
   --json 不输出 settings（commands/search.py 只在 quality 渲染 settings）。
   建议统一：fast 模式 --json 也带 settings，或人读输出在过滤发生时给一行
   stderr 提示。
3. **硬编码的套件计数会漂移**：ANCHORS.md 第 6 条与首页写了"3,200+/
   除 2 个存量失败"；套件增长后需要手工同步三处（锚点卡、首页
   verify 注释、摘要档案）。建议锚点卡改为"以当日 pytest 尾行为准"的
   引用式写法，或从回执文件生成。
4. **estimate 的速率仍是 DigUp 先验**（已如实标注"goldmine 校准待做"）。
   建议的校准法：在 eval 语料上实测本机各类文件耗时，落一个
   rates.local.json 覆盖层。
5. **仓库无 favicon**：站点三页共享无图标（Cloudflare 上标签页为默认）。
   建议：logo-glyph 方形旋转菱形出一版 32/180px SVG favicon。

## 站点工程（记录给下次迭代）

6. **learn.html 数据内联 225KB（gzip 129KB）**：为 file:// 无 fetch 约束
   所迫。可接受；若未来概念数破 150，考虑 build 时拆为
   `assets/graph.js`（JS 变量而非 fetch，仍无 CORS/file:// 问题）。
7. **浏览器兼容坑记录**：WebKit 的 IntersectionObserver 拒绝负百分比
   rootMargin（"0 0 -6% 0" 抛 SyntaxError，整页揭示失效）——已改像素值。
   任何新页面用 IO 时沿用像素 margin。
8. **canvas 地图的自动化测试缺位**：交互链（搜索→选中→下探→面包屑）
   目前靠人工浏览器 QA。建议补一个 Playwright 冒烟脚本进 CI
   （headless cdp 后端已具备）。
9. **对比表的时效纪律**：首页对比表基于 2026-10 公开行为，每行有
   verify 注释；建议每次发布前人工过一遍，避免静默过期。
10. **并行会话撞车再现**：本任务期间工作树仍有另一会话的未提交改动
    （build 期间 README/SPEC 等持续变动）。AGENTS.md"一会话一分支"
    已立规但未被执行环境强制；hosted CI 是真正的强制点（仍未跑通）。
