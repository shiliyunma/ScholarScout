---
name: rea-cli-reverse-engineering
description: "Use when 用 REA（rea-agents）分析无源码应用/逆向，或维护、排障、回滚本机 REA 安装。"
version: 1.0.0
platforms: [windows, linux, macos]
---

# REA（rea-agents）本地 CLI 用法

REA = npm 包 `rea-agents`（作者 morluto，MIT，官网 rea.tools，repo github.com/morluto/rea）：本地分析原生二进制 / JS & Electron / .NET / 网页 / Android，用于理解无源码应用、比较版本、反编译。**分析全在本机跑，不上传**。MCP 模式与 CLI 模式是两件独立的事（装 skill ≠ 挂 MCP）。

## 本机安装状态

- 全局装在 `E:\npm-global`（npm prefix 已设 E 盘，符合大浩「装东西默认 E 盘」要求），入口 `E:\npm-global\rea.cmd`；`rea` 已在用户 PATH。
- PATH 未生效时的绝对路径调用：`"/c/Program Files/nodejs/node.exe" "E:/npm-global/node_modules/rea-agents/scripts/rea.mjs" <command>`
- 官方讲义 skill：`%HERMES_HOME%\skills\reverse-engineer-anything\`（与包内逐字节一致）。
- **MCP 刻意不注册**（大浩 2026-10 选择 C）：零常驻进程、零工具注入。需要时用命令行跑，把结果和证据给他。

## 按需使用（不要预先跑 doctor）

1. `rea capabilities --json` 先看本机哪些操作可用（含 `effects` / `launch_process` / `available` / `reason`），比盲试快。
2. 按目标选命令：`analyze-javascript-application <path> --json`（JS/Electron，无需原生引擎）、`analyze <path>`、`inspect <path>`、`compare-application-versions`、`decompile` / `function`（需 Ghidra/IDA/Hopper）、`doctor [--client hermes]`。
3. 输出自带 Evidence 与 limitations：结论要带出处，`--json` 便于脚本化与后续聚合。
4. REA 官方讲义里的工具名是 snake_case（`analyze_javascript_application`），对应 CLI 的 kebab-case 命令。讲义明确说：优先用已安装的 `rea` CLI。

## 本机能力边界

无 Ghidra / IDA / Hopper → 原生二进制深分析不可用（`capabilities` 会给出 `available: false` + reason）。JS / Electron / 网页分析开箱可用。要补引擎：Ghidra 解压后设 `GHIDRA_INSTALL_DIR`。

## 坑（都是实测，别再踩）

- **`lazy: true` 对 REA 不生效**：REA 的 MCP SDK 在 `tools/list` 回 `ttlMs: 0`，而 Hermes `tools/mcp_schema_cache.py` 用 `elapsed_ms >= ttl_ms` 判过期 → `0` 恒为真 → 缓存每次启动都算失效 → 退回 eager 连接（实测 `_register_lazy_from_cache` → `eager=['rea']`；把 `ttl_ms<=0` 当无 TTL 才 `eager=[]`、lazy 注册 172 工具）。所以要「启动零进程」只能 CLI-only 或改 Hermes 源码；挂 MCP 的话就接受启动连一次 + `idle_timeout_seconds` 回收。
- `hermes config set` 是整文件重写（PyYAML），会**丢掉 config.yaml 的注释块**（`hermes-agent` skill 的 native-mcp.md 有记录）。改前先 `cp` 备份，事后可原样还原。
- `hermes mcp remove <name>` 会交互确认，回车默认 yes。
- REA 的 `setup --dry-run` 可先看它要改什么；注册命令是绝对路径 `node.exe + rea.mjs mcp`（不走 npx），并会写 `config.yaml.rea.backup`。它的 YAML 编辑只 upsert `mcp_servers.rea`，其余配置逐字保留（已核验）。

## 安装 / 卸载 / 回滚

```bash
# 装（用 Program Files 的 node/npm，避免污染 Hermes 自带 node）
"C:\Program Files\nodejs\npm" install -g rea-agents@6.4.0
# 只注册 MCP、不装 Hopper、只管 Hermes
rea setup --client hermes --dry-run && rea setup --client hermes
# 卸载
npm uninstall -g rea-agents && hermes mcp remove rea && rm -rf "%HERMES_HOME%\skills\reverse-engineer-anything"
# REA 自带（会连 skill 一起删）
rea uninstall --client hermes
```

## 验证清单（改完必做）

1. `rea --version` 且 `rea capabilities` 能出结果。
2. config.yaml：`yaml.safe_load` 通过 + 与改动前做**语义比对**（不只 diff 文本，因为 YAML 重写会改行尾/折行）。
3. 若挂了 MCP：用 Hermes venv 的 python + mcp SDK 手动 spawn 注册命令，`initialize` + `tools/list` 打印真实工具数（`mcp` 2.x 字段是 snake_case：`res.server_info`）。
4. `hermes mcp list` 确认状态；MCP 改动要重启 Hermes 才生效（不支持热加载）。
