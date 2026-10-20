# 只读 stdio MCP

安装可选依赖后，显式选择一个已导入的索引：

```sh
pip install 'goldmine[mcp]'
goldmine mcp --db /absolute/path/index.sqlite
```

也可用 `--index NAME` 显式选择命名索引。服务不创建索引、不导入资料、不修改客户端配置。旧库缺 opaque citation 身份时，先显式运行 `goldmine reindex lexical --db PATH`。维护命令完成前，相关工具会返回 missing_capability。

只提供 `goldmine_search`、`goldmine_get`、`goldmine_context`、`goldmine_status`。工具 schema 拒绝未知字段；query 最多8192个UTF-8字节，search limit为1–50，context半径0–3，材料包预算512–32768。`filters` 接受 kinds/under/site/phrases数组及after/before日期字符串；同一个过滤字段不能同时在线内语法与对象中给出。

get/context只接收搜索返回的 `gm1:...` 引用，不接收路径、URL或SQL。文本、位置与元数据在一个SQLite读快照中返回；引用内容变化会得到 stale_citation。照片名称/元数据明确标注为name_metadata，不伪装成图像描述。

响应为一个TextContent中的JSON；`structured_content`为空，防止正文重复。pack/context响应只有一次context_text及简短计数/设置；完整出处在计入预算的正文内。token_mode区分tokenize与estimate。工具失败设置is_error并返回明确kind。

默认fast deadline为5秒，quality为10秒（具体可选能力仍需实际实现与实验验收）。模型操作串行，最多两项排队；排队也计入同一deadline。客户端取消传播到同步工作线程的检查点，底层阻塞操作受剩余时长限制；请求清理自有backend，不终止共享serve。

实现锁定官方[mcp 2.0.0](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0)。协议测试使用真实[官方客户端](https://py.sdk.modelcontextprotocol.io/client/)与stdio transport；该固定版本的Client接收`stdio_client(StdioServerParameters(...))`。其中一组测试显式选择legacy mode验证initialize握手，另有现代进程内调用与取消检查。依赖锁记录在requirements/mcp.lock。

```sh
python3 -m pytest tests/test_mcp.py -q
```

上面的协议测试使用mock向量验证边界；它不能代替真实模型的排序、延迟或最终产品质量验收。
