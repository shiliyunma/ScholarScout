---
name: vscode-runnable-workspace
description: "Use when 交付要能在 VS Code 里直接跑（F5/任务）。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [VS Code, 交付形态, 本地项目, 大浩, 中文, launch.json, tasks.json]
    category: software-development
---

# 把交付物配成"VS Code 里打开就能跑"

大浩的硬要求：**不要只给终端命令，要能在 VS Code 里点着跑**（原话就是"我希望能在 VS Code 里面打开，而不是通过原始的终端命令行输入"）。所以凡交付一个带 venv 和若干脚本的本地项目/工作台/工具，除了代码和文档，必须顺手把 `.vscode/` 配好、附一份使用指南、并用 `code` 打开工作区。

配套（用户自有，勿改，需要时提醒他 adopt）：源码学习工作台类交付看 `repo-learning-workbench`；MCP 服务器本身的开发与验证看 `mcp-server-development`。

## 触发

- "告诉我怎么用这些工具，我想在 VS Code 里打开，而不是敲命令行"
- 刚交付了一个本地项目/工作台，里面有 `.venv`、几个验证脚本、要人手动敲的长命令（`NO_PROXY=... python xxx.py`、`mcp dev ...`）

## Procedure

1. **先探现状**：`code --version`、已装扩展（`ls ~/.vscode/extensions` 过滤 `python|copilot|pylance|debugpy|ruff`）、用户级 settings 里的相关段。版本和扩展要写进指南页，下次不用重测。
2. **建 `.vscode/`**：`settings.json` / `launch.json` / `tasks.json` / `.env` / `extensions.json`（项目里有 MCP 服务器时再加 `mcp.json`）。**全部写纯 JSON**。
3. **每条"要跑的路径"给两条入口**：`launch.json` 一条 F5 配置 + `tasks.json` 一条任务。别只给其中一种。
4. **环境变量注入三处**（缺一处就会出现"本地连不上/包找不到"）：集成终端 `terminal.integrated.env.windows`、调试会话 `python.envFile` 指向的 `.vscode/.env`、以及宿主/服务器自己的 `env` 字段（`mcp.json` 里）。
5. **校验再交付**：跑 `scripts/verify_vscode_configs.py`（JSON 能解析 + 配置里引用的解释器和脚本真实存在），并**把每条任务/配置里的命令在终端等价跑一次**——写对 JSON 不等于跑得通。
6. **写一份 `<编号>-VSCode使用指南.md`**：怎么打开、三种不敲命令的跑法（运行面板 / 任务 / 编辑器右上角 ▶）、断点能断到哪、调试台怎么开、故障速查表、出处链接、状态实测值（版本/扩展/未装什么）。
7. **`code "<绝对路径>"` 真的打开工作区**，并在汇报里说清"已发出打开请求"与"哪些需要你点确认"。

## 关键配置（照抄改路径即可）

`settings.json` 最小可用集（解释器 + 环境变量 + 挡噪音 + 测试）：
```json
{
  "python.defaultInterpreterPath": "${workspaceFolder}\\.venv\\Scripts\\python.exe",
  "python.terminal.activateEnvironment": true,
  "python.envFile": "${workspaceFolder}\\.vscode\\.env",
  "python.testing.pytestEnabled": true,
  "terminal.integrated.env.windows": { "NO_PROXY": "127.0.0.1,localhost", "PYTHONIOENCODING": "utf-8" },
  "search.exclude": { "**/vendor": true, ".venv": true },
  "files.encoding": "utf8", "files.eol": "\n"
}
```
`.vscode/.env`（被 `python.envFile` 引用，给所有调试会话注入）：
```
NO_PROXY=127.0.0.1,localhost
no_proxy=127.0.0.1,localhost
PYTHONIOENCODING=utf-8
```
`launch.json` 每条配置的要点：`type: debugpy`、`program: ${workspaceFolder}/<脚本>`、`console: integratedTerminal`（中文不乱码）、`cwd: ${workspaceFolder}`、`justMyCode: false`（能进依赖栈）、`env` 显式重复环境变量；再加一条 `${file}`（当前打开的文件）。
`tasks.json` 每条：`label` 带 emoji、`options.env` 与终端一致、`presentation.panel: dedicated` + `reveal: always`、`problemMatcher: []`。值得配的固定任务：跑验证脚本、**打开图形调试台**（会拉浏览器、Ctrl+C 结束）、跑 pytest、跑自定义复核脚本。

### `.vscode/mcp.json`（把本地 MCP 服务器注册成工作区服务器）

官方格式是顶层 `servers` 对象：
```json
{ "servers": { "myServer": {
  "type": "stdio",
  "command": "${workspaceFolder}/.venv/Scripts/python.exe",
  "args": ["${workspaceFolder}/server.py"],
  "cwd": "${workspaceFolder}",
  "env": { "NO_PROXY": "127.0.0.1,localhost", "PYTHONIOENCODING": "utf-8" },
  "dev": { "watch": "**/*.py" }
} } }
```
- 服务器名用 camelCase，不带空格；`command` 用**绝对路径**（宿主不继承你的 shell 环境）。
- `dev.watch` = 改文件自动重启服务器（开发手感全靠它）；`dev.debug` 可给 stdio 服务器挂调试器（官方支持 Python/Node）。
- 操作：工作区打开时 Allow；`Ctrl+Shift+P` → `MCP: List Servers` 看状态/起停重启/**Show Output**（排查"看不到工具"第一站）。

## 改默认终端 + 装扩展（他常直接要求这两件事）

**把默认终端改成 Git Bash ≠ 双击 `.bat` 会用 bash。** 这是两个完全不同的设置，他会混淆：

- **VS Code 的 `terminal.integrated.defaultProfile.windows: "Git Bash"`** —— 只管 VS Code 集成终端面板里
  新开的那一个（同时在 `terminal.integrated.profiles.windows` 里定义好 `Git Bash` 条目）。
  改完让他在 VS Code 里新开终端用 bash 语法敲命令。
- **Windows 的「默认终端应用」**（注册表 `HKCU\Console\%%Startup`）—— 才管**双击 `.bat`/`.exe` 用哪个窗口**，
  Win11 默认是 Windows Terminal。**与 VS Code 的那个设置毫无关系**，而它还可能根本不存在（=从未改过）。
  要回经典黑底 cmd：`设置 → 系统 → 开发者选项 → 终端 → Windows 控制台主机`。
- **`.bat` 永远不该用 bash 打开** —— 它是 cmd 批处理语法（`@echo off` / `%VAR%` / `if errorlevel`），
  bash 读不懂。想「双击用 bash 跑」对应的是 `.sh`。被问到这个问题时先把这条说清楚，
  否则会去改一堆没用的设置、或者把 `.bat` 文件关联改坏。

**装扩展：先查已装，再去按 id 装缺的。**

```bash
ls ~/.vscode/extensions | grep -iE 'python|copilot|golang'
code --install-extension golang.go --install-extension mtxr.sqltools
```

按项目里实际有什么凑齐：语言本体（`golang.go`）、LSP 依赖的外部工具（见下）、
数据库/中间件客户端（`mtxr.sqltools` + `mtxr.sqltools-driver-pg`、`cweijan.vscode-redis-client`）、
配置类文件支持（`redhat.vscode-yaml`）、发请求（`humao.rest-client`）。

**语言工具链不在系统 PATH 上时，把它的 helper 二进制预装好，并显式告诉扩展根目录。**
本机 `go` 在 `E:\...\03-env\go\bin` 而不在 PATH：除了加 `go.goroot` 指过去，还要把
`gopls` / `dlv` / `staticcheck` 一个个 `go install` 进 GOPATH bin —— 否则扩展首次打开就卡在装这些工具，
或者每次开工作区都弹一次提示，他会以为工作区配坏了。

**数据库/中间件客户端的连接配置不要存密码。** 写进 `settings.json` 的连接用 `"askForPassword": true`，
把口令留在连接时的提示里；配置文件里出现明文口令等于把凭据写进了交付物。

## Pitfalls

- **写 `.json` 必须纯 JSON**：`write_file` 做严格语法校验，带 `//` 注释的 JSONC 会被直接拒（VS Code 自己接受注释）。解释文字放配套 `.md`，不要放配置里。
- **改用户已存在的用户级 `settings.json` 时，它多半是 JSONC**（VS Code 允许尾随逗号），于是 `patch` /
  `write_file` 都因严格 JSON 校验**拒绝写入**，而 `patch` 的报错只指向那个逗号 —— 很容易误判成文件坏了。
  正确做法是**用 Python 做文本级编辑**：先把尾随逗号正则清掉（`,(\s*[}\]])` → `\1`）让它变成严格合法 JSON，
  再 `json.loads` 读、加键、`json.dumps(indent=2)` 写回；结果同时满足严格 JSON 与 JSONC，不需要再引注释。
  三条纪律：① 动手前 `cp` 一份带时间戳的备份；② 写回后**逐项对比备份**，断言原有键一个没丢、原有值一个没改
  （只断言「解析成功」不够，那查不出删键）；③ 只加键，不重排、不删键。凡是动用户配置文件的地方都必须可回滚。
- **断点能不能命中取决于是不是同一进程**：内存直连的测试、常驻服务本身 → 能断到被调用的库/服务器代码；"客户端起子进程连服务"的脚本 → 断不到子进程那份代码。**把这件事写进配置名**（如"① 冒烟：内存直连（可断点）"），否则他断不上会以为是配置坏了。
- **宿主能力缺失时要直说，别写"配好即可用"**：VS Code 里让 AI 调用 MCP 工具依赖 GitHub Copilot + Copilot Chat（官方文档口径就是 Copilot 功能）；没装就给退路（用 `mcp dev` 拉起的浏览器调试台，无需账号；或已配好 MCP 的桌面端），并把这句写进指南页。
- **副本/依赖目录必须进 `search.exclude`**：工作区里放了源码副本、文档副本、`.venv` 后，`Ctrl+Shift+F` 会被淹掉，不排除等于搜索不可用。
- **别用 `write_file` 覆盖已有的 `.vscode/`**：先读后改（用户可能已有自定义设置/快捷键）。
- **指南页是交付物的一部分**：只给一堆 JSON，他下次还是不知道点哪里。要写"打开后立刻可做的三件事"，而不是配置清单。

## Verification

- `scripts/verify_vscode_configs.py .` 报 0 问题（JSON 可解析 + 引用的解释器/脚本存在）。
- 每条 F5 配置与每条任务里的命令，都在终端等价跑过并有真实输出。
- 指南页含：打开方式、三种跑法、断点范围、故障速查、**缺某宿主时的退路**、出处。
- 已用 `code "<绝对路径>"` 打开工作区（或明确说明未执行/失败原因）。
