---
name: mcp-server-development
description: "Use when 开发或学习 MCP 服务器（Python SDK v2）。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [MCP, Model Context Protocol, Python SDK, 工具开发, 大浩, 中文]
    category: software-development
---

# MCP 服务器开发（Python SDK v2）

大浩要"搞懂怎么开发一个 MCP 工具"时用这个。本地学习工作台在 `D:\AI+教育毕设毕设\MCP\`（六件套 + 91 篇官方文档副本 + SDK 源码副本 + 5 个跑通的 demo）。

## 事实基线（实测，别再猜）

- 2026-10 时 PyPI 上是 **mcp 2.3.0 + mcp-types 2.3.0**（一个 `mcp[cli]` 带出两个分发包，版本强绑定）。
- **v2 把 `FastMCP` 改名 `MCPServer`**：`mcp/server/fastmcp.py` 只剩十几行且 `raise ModuleNotFoundError`。正确写法：
  `from mcp.server import MCPServer` / `mcp = MCPServer("name")` / 装饰器 `@mcp.tool()` `@mcp.resource("scheme://{x}")` `@mcp.prompt()` / 入口 `mcp.run()`（默认 stdio）。
  网上绝大多数教程仍是 v1 的 `from mcp.server.fastmcp import FastMCP`——**先看版本再照抄**，或让项目 pin `mcp<2`。
- 协议分两个时代：`HANDSHAKE_PROTOCOL_VERSIONS`（≤2025-11-25，靠 `initialize`）与 `MODERN_PROTOCOL_VERSIONS = ("2026-07-28",)`（无状态 per-request envelope，靠 `server/discover`）。客户端默认 `mode='auto'` 先探 discover、被否再回落 initialize。
- `mcp dev <file>` 需要 npx/node（拉 MCP Inspector）；`mcp run <file>` 会阻塞不退出（正常）。

## 三步验证配方（每一步都可独立跑，别只做第一步）

1. **内存直连**（最快，替掉"装宿主"）：`async with Client(mcp, raise_exceptions=True) as c: await c.list_tools() / c.call_tool(...)`。官方类比 FastAPI 的 `TestClient`。
2. **stdio 子进程**（宿主视角）：`Client(stdio_client(StdioServerParameters(command=sys.executable, args=["server.py"])))`。**注意 `stdio_client` 收的是 `StdioServerParameters` 对象，不是 dict。**
3. **Streamable HTTP**（部署形态）：`mcp.run(transport="streamable-http", host=..., port=...)` + `Client("http://127.0.0.1:PORT/mcp")`。

## 四个真坑（都是实测踩出来的）

1. **本地 HTTP 测试 502**：机器开着系统代理（Windows 注册表 `Internet Settings\ProxyEnable=1`，如 `127.0.0.1:7890`）时，httpx/httpx2 会把 `127.0.0.1` 也送进代理 → 502 → 被传输层包装成 `MCPError`（如 -32603）。**症状特征：curl 能连、Python 客户端不能连。** 修法：`NO_PROXY=127.0.0.1,localhost`（或给 `streamable_http_client` 传 `trust_env=False` 的 httpx2.AsyncClient）。stdio 不受影响。
2. **docstring 的 `Args:` 段不会被解析成参数描述**：整段原文会被塞进工具 `description`，稀释重点。参数说明必须写 `Annotated[str, Field(description="…")]`（`Field(ge/le)` 会变成 schema 的 `minimum/maximum`，且**在函数执行前拦下非法参数**，错误回给模型让它重试）。
3. **`@mcp.tool` 漏括号** → `TypeError: Did you forget to call it?`；`@mcp.tool` 与 `@mcp.tool()` 必须成对。
4. **stdout 被协议占用**：服务器里任何 `print()` 都会破坏 stdio 帧（表现为宿主"看不到工具"）。日志走 `ctx.log/info` 或 stderr。

## 安全底线（写进任何要交给模型调用的工具）

- 文件类工具一律 `mcp.shared.path_security.safe_join(base, *parts)`，它拒绝对路径、空字节、越界拼接。
- HTTP 形态默认已开 DNS rebinding 防护（`TransportSecuritySettings.enable_dns_rebinding_protection`），仍要显式配 host/origin 白名单与鉴权。
- 检索类工具要能"查不到就拒答"，别用编造内容兜底。

## 抓官方文档的正确姿势（省时间，可复用）

MCP 文档站提供 `https://modelcontextprotocol.io/llms.txt`（全站索引），每页都有 `.md` 纯文本端点（如 `.../docs/<version>/develop/build-server.md`）。Python SDK 文档同理：`https://py.sdk.modelcontextprotocol.io/llms.txt`。
用 python urllib + `ThreadPoolExecutor(8)` 抓，91 篇约 50 秒；把每篇的来源 URL 写进文件头，再生成 `00-来源清单.md` 便于引用。**不要用 web_extract（本机后端不可取正文）。**

## 交付物形状（大浩的验收口径）

- 代码必须**实跑过**并把真实输出抄进进度笔记（带命令 + 输出片段）。
- 每个结论带 `文件:行号` 锚点，写完跑一次锚点复核脚本（`D:\AI+教育毕设毕设\MCP\_scripts\verify_anchors.py`，可改路径复用）。
- 接进宿主的配置片段要贴出来：Hermes 桌面端在 `%LOCALAPPDATA%\hermes\config.yaml` 的 `mcp_servers:` 下写 `command`（绝对路径，Windows 用正斜杠）+ `args`。
