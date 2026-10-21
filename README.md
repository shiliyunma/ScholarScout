# ScholarScout

## 项目简介

ScholarScout 是一个本地优先的**开放世界学术调研与学习辅助智能体**：你给出一个研究主题或一句问题，它会到公开来源检索、抓取并精读资料，把**带出处**的结论和摘要留在本机资料库，再用学习路线与间隔重复把读过的内容变成可复习的卡片——资料、会话、索引全部落在 `data/` 与 `run/state.db`，只有模型调用和资料抓取会出网。

## 功能特性

1. **开放世界检索 → 抓取 → 判权 → 带出处的回答**：模型可见工具 `research.search` / `source.fetch` / `source.grade`，实现见 `src/scholarscout/tools/research_search.py`、`tools/source_fetch.py`、`tools/source_grade.py`，出处拼接见 `tools/source_context.py`（工具清单与注册入口：`src/scholarscout/tools/README.md`、`tools/__init__.py`）。
2. **多格式正文抽取（含扫描版兜底）**：全项目唯一出网口 `src/scholarscout/fetch/http.py`（按 host 限速、指数退避、缓存、robots）；HTML→`fetch/html_text.py`，`.docx/.xlsx/.pptx`→`fetch/office.py`，`.epub/.rtf/.zip`→`fetch/formats.py`，PDF 先走本地 `pymupdf`，抽不出文字层再用 MinerU 兜底 `fetch/mineru.py`。
3. **本地资料库与全文检索**：SQLite（WAL）+ FTS5 单文件库 `run/state.db`，写库只经 `src/scholarscout/state/db.py`，去重入库在 `state/ingest.py`、检索在 `state/search.py`；正文不入库，库里只存摘要与 `raw_path`/`text_path`（`state/README.md`、`docs/02-数据模型.md`）。
4. **学习闭环：路线 + 掌握度 + 间隔重复**：`src/scholarscout/learning/srs.py`（SM-2 三档评价）、`learning/mastery.py`（类型化掌握度）、`learning/route_patch.py`（路线增量补丁）；落库与队列在 `state/learning.py` / `learning_cards.py` / `learning_routes.py`，对应工具 `route.plan` / `route.revise`。
5. **飞书渠道（websocket 长连接，无需公网回调）**：入站核在 `src/scholarscout/channels/feishu_ws.py` / `channels/feishu.py`，一键开机 `scripts/feishu_ws_up.py` 与 `飞书一键开机.bat`（本机主动外连，不需要隧道或回调地址；方案见 `docs/17-飞书渠道接入方案.md`）。

## 安装

**依赖要求**：Python 3.11（`pyproject.toml`：`requires-python = ">=3.11,<3.13"`；本机实测 3.11.5）、[uv](https://docs.astral.sh/uv/)；运行期第三方依赖只有 5 个——`httpx`、`pyyaml`、`pywebview`、`pymupdf`、`lark-oapi`（`deps-allowlist.txt` 与 `pyproject.toml` 一致）。

```bash
# 1) 建虚拟环境（3.11）
uv venv --python 3.11

# 2) 装依赖：脚本读 pyproject.toml，不装本包本身
.venv/Scripts/python.exe scripts/install_deps.py            # 运行期 + dev（pytest/ruff/mypy）
.venv/Scripts/python.exe scripts/install_deps.py --runtime  # 只装运行期 5 项

# 3) 配置：密钥进 .env，设置进 config.yaml（两者都不入库）
cp .env.example .env
cp config.example.yaml config.yaml
```

> **不要 `pip install -e .`**：项目路径含中文，editable 安装写入的 UTF-8 `.pth` 会被 CPython 用 locale（中文 Windows 上 GBK）读取，解释器直接启动失败。所以装依赖走 `install_deps.py`、跑 CLI 走 `scripts/scout.py`（`scripts/README.md`、`docs/03-目录结构与规则文件.md` §9）。

> 通用检索走自建 SearxNG（Docker 容器，见 `deploy/searxng/`）；Docker Desktop 重启后容器不会自己回来，用一条命令拉起：`.venv/Scripts/python.exe scripts/searxng_up.py`（`--status` 只看状态）。

## 使用

```bash
# 建库 / 迁移（幂等，可重复跑）
.venv/Scripts/python.exe scripts/scout.py --initdb

# 打开桌面窗口（pywebview，走系统 Edge WebView2）
.venv/Scripts/python.exe scripts/scout.py --desktop
```

最简示例——终端里直接对话（需要 `.env` 里配好模型密钥）：

```bash
.venv/Scripts/python.exe scripts/scout.py --chat
# 新会话 …｜模型 …｜工具集 research
# 你> 帮我查一下知识追踪最近的评测方法，给出处
```

其他入口（参数以 `scripts/scout.py --help` 为准）：`--version` 打印版本；`--setup-feishu` 飞书接入向导（密钥只写 `.env` 不回显）；`--rpc` 在 stdin/stdout 上跑 JSON-RPC；`--open-settings` 配合 `--desktop` 直接打开设置；`--db` / `--toolsets` 覆盖库路径与工具集。也可以双击项目根的 `ScholarScout.exe`（启动器，依赖本机 Python，改代码不用重打包）。

飞书渠道（长连接，无需隧道/回调）：

```bash
.venv/Scripts/python.exe scripts/feishu_ws_up.py --check   # 只做前提自检：SDK / 凭据 / 出站开关
.venv/Scripts/python.exe scripts/feishu_ws_up.py           # 起连接并阻塞收包（Ctrl-C 退出）
```

或直接双击 `飞书一键开机.bat`（等价）。退出码：0 正常 / 2 前提没满足 / 3 起连接失败；首次 `import lark_oapi` 本机约 116 秒。

## 许可证

MIT License，见仓库根 `LICENSE`（Copyright (c) 2026 shiliyunma）。注：`pyproject.toml` 目前未声明 `license` 字段，许可证以 `LICENSE` 文件为准。

详细文档见 `docs/00-总索引.md`。
