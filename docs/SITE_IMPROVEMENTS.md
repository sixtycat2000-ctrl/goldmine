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

---
# 第二轮（2026-10-25 晚：交互升级 + 五领域对抗评审后）

> 评审团：产品 UX / 无障碍 WCAG 2.2 / 双语技术文案 / 性能与产线工程 / 产品策略。
> 交付 35 条发现。以下为**未在本轮修复**的遗留项（已修 25 条，摘要见
> COMMIT_DIGESTS.md）。

## 已知遗留（按优先级）

1. **learn.html 内联 JSON 仍未外置**（评审 PERF#3）：225KB 内联数据走关键
   路径。外置 fetch 方案已验证可行（.assetsignore/_headers 均生效），需要
   map.js 加载态骨架。留待下次。
2. **首页缺产品实拍动图**（STRAT#2，评审称为最高转化缺口）：需要一个真实
   的 search→open-at-timestamp 的 15s GIF/视频。素材要用真终端录。
3. **安装摩擦**（STRAT#9）：无 brew/PyPI 一行安装。发布到 PyPI 是产品任务，
   不属于站点。
4. **对比表第 7-8 行自证倾向**（STRAT#7）：'换模型保护/公开度量'是竞品
   没有的维度，评分表偏向自选赛道。建议每行链接到证据文件。
5. **learn 示例的可达性标注**（评审 P2#17）：部分"可跟算实例"依赖完整仓库
   + 真模型，应在内容层加"需完整仓库"范围标签（concepts.json 数据层改动）。
6. **sitemap 缺 lastmod、无 og:image 分享卡**（PERF#11 部分）：og:image 需
   1200×630 渲染图。
7. **Playwright 冒烟测试**（原 #8 仍未做）：window.__gm seam 已就位，脚本
   是体力活。
8. **hero 星空的 ignite 每帧 map+sort 已降频至 1/5 帧**，但仍是 O(n log n)；
   节点数再涨需换部分选择算法。

## 修复存档（本轮已修，备查）

P0：quickstart mkdir、关闭面板 inert/焦点归还/列宽归还、.assetsignore 内部
文件暴露、learn 安全头（CSP/HSTS/nosniff/DENY）。本地功能三大真凶：
setPointerCapture 异常杀点击（Safari 用户全灭）、shortcuts 面板 display:grid
压过 hidden 永久悬浮、rAF 冻结下 canvas 0×0 backing（时序竞态，ResizeObserver
治本）+ offsetX 在 capture/合成事件下不可靠（evPos 自算）。交互升级：懒投影
命中、命中容差 26/30px、目录视图切换、? 快捷键面板、Esc 五级链、Ctrl+wheel
还页缩放、RM 事件驱动重绘（不再 60fps 空转）、boot 一次性致意、诚实演示字幕、
doc-level 方向键入图、tabs/listbox ARIA、对比表语义化+脚注锚、GitHub 直链
审计索引、canonical/OG/robots/sitemap/404、拖动 307 消除（extensionless 链接）。
