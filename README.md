# ScholarScout

## 项目简介

ScholarScout 是一个本地优先的**开放世界学术调研与学习辅助智能体**：你给出一个研究主题或一句问题，它会到公开来源检索、抓取并精读资料，把**带出处**的结论和摘要留在本机资料库，再用学习路线与间隔重复把读过的内容变成可复习的卡片——资料、会话、索引全部落在 `data/` 与 `run/state.db`，只有模型调用和资料抓取会出网。

## 功能特性

1. **开放世界检索 → 抓取 → 判权 → 带出处的回答**：模型可见工具 `research.search` / `source.fetch` / `source.grade`，实现见 `src/scholarscout/tools/research_search.py`、`tools/source_fetch.py`、`tools/source_grade.py`，出处拼接见 `tools/source_context.py`（工具清单与注册入口：`src/scholarscout/tools/README.md`、`tools/__init__.py`）。
2. **多格式正文抽取（含扫描版兜底）**：全项目唯一出网口 `src/scholarscout/fetch/http.py`（按 host 限速、指数退避、缓存、robots）；HTML→`fetch/html_text.py`，`.docx/.xlsx/.pptx`→`fetch/office.py`，`.epub/.rtf/.zip`→`fetch/formats.py`，PDF 先走本地 `pymupdf`，抽不出文字层再用 MinerU 兜底 `fetch/mineru.py`。
3. **本地资料库与全文检索**：SQLite（WAL）+ FTS5 单文件库 `run/state.db`，写库只经 `src/scholarscout/state/db.py`，去重入库在 `state/ingest.py`、检索在 `state/search.py`；正文不入库，库里只存摘要与 `raw_path`/`text_path`（`state/README.md`、`docs/02-数据模型.md`）。
4. **学习闭环：路线 + 掌握度 + 间隔重复**：`src/scholarscout/learning/srs.py`（SM-2 三档评价）、`learning/mastery.py`（类型化掌握度）、`learning/route_patch.py`（路线增量补丁）；落库与队列在 `state/learning.py` / `learning_cards.py` / `learning_routes.py`，对应工具 `route.plan` / `route.revise`。
5. **飞书渠道（websocket 长连接，无需公网回调）**：入站核在 `src/scholarscout/channels/feishu_ws.py` / `channels/feishu.py`，一键开机 `scripts/feishu_ws_up.py` 与 `飞书一键开机.bat`（本机主动外连，不需要隧道或回调地址；方案见 `docs/17-飞书渠道接入方案.md`）。

完整上手（三条路径逐步版、必配项、真踩过的坑）见 **[QUICKSTART.md](QUICKSTART.md)**。

## 快速上手

三条路径任选一条，都能在 5 行内跑起来。**通用前提**：一个 DeepSeek（或任何 OpenAI 兼容）
API key；不配也能启动，但模型不可用。

### ① 桌面版（Windows，下载即用）

```bash
# 从 Releases 下载 ScholarScout-Portable-*-win64.zip → 解压到不含中文的路径 → 双击 ScholarScout.exe
# 首次启动生成 config.yaml / .env.example；把它复制成 .env 填 LLM_API_KEY，重开即用
# Releases 还没有包时自己打：python scripts/build_bundle.py  →  ScholarScout-Portable/
```

### ② 源码跑（Windows / macOS / Linux）

**依赖要求**：Python 3.11（`requires-python = ">=3.11,<3.13"`；本机实测 3.11.5）、[uv](https://docs.astral.sh/uv/)；
运行期第三方依赖 7 项：`httpx`、`pyyaml`、`pywebview`、`pymupdf`、`lark-oapi`、`python-docx`、`openpyxl`
（`pyproject.toml` 与 `deps-allowlist.txt` 一致）。

```bash
uv venv --python 3.11
.venv/Scripts/python.exe scripts/install_deps.py     # macOS/Linux 用 .venv/bin/python
cp .env.example .env && cp config.example.yaml config.yaml   # config.yaml 可省，省了全用默认值
.venv/Scripts/python.exe scripts/scout.py --initdb            # 建库 / 迁移（幂等）
.venv/Scripts/python.exe scripts/scout.py --desktop           # 没有图形界面就换 --chat
```

> **不要 `pip install -e .`**：项目路径含中文，editable 安装写入的 UTF-8 `.pth` 会被 CPython 用
> locale（中文 Windows 上 GBK）读取，解释器直接启动失败。所以装依赖走 `install_deps.py`、
> 跑 CLI 走 `scripts/scout.py`（`scripts/README.md`、`docs/03-目录结构与规则文件.md` §9）。

### ③ Docker（无界面：只有 CLI / JSON-RPC / 飞书机器人）

```bash
cp .env.example .env                              # 填 LLM_API_KEY
docker compose run --rm scout --initdb            # 建库（数据在命名卷 scout-data）
docker compose run --rm scout --chat              # 终端对话
docker compose up -d                              # 后台常驻（默认命令就是 --chat）
```

**容器内没有 GUI**：镜像基于 `python:3.11-slim`、以非 root 用户 `scout` 运行、`SCHOLARSCOUT_HOME=/data`
且声明了 `VOLUME ["/data"]`。`pywebview` 是桌面壳专用依赖，镜像**故意不装**（代码里它是函数内延迟导入，
只影响桌面模式），所以 `--desktop` 在容器里会明确回“未安装 pywebview”。国内拉不到基础镜像时：
`docker build --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim -t scholarscout:dev .`

可选起一个自建 SearxNG 给通用检索用：`docker compose --profile search up -d searxng`（`http://localhost:8080`）。

## 必须配置的项

密钥只从 `.env`（或同名环境变量）读，设置只从 `config.yaml` 读，两者都不入库。

| 项 | 不给会怎样 |
| --- | --- |
| `LLM_API_KEY`（DeepSeek） | 唯一硬需求。没有它：窗口、建库、翻已收录资料都正常，任何要模型的动作会明确报“缺少模型密钥”，不会静默假装成功 |
| `S2_API_KEY` | Semantic Scholar 来源不启用（无 key 时该接口恒回 429，真机实测）；其余来源不受影响 |
| `TAVILY_API_KEY` / `SERPER_API_KEY` / `BRAVE_API_KEY` / `GOOGLE_CSE_KEY` | 商用检索不启用，回落到 SearxNG / arXiv / OpenAlex / GitHub 等免 key 来源 |
| 飞书 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`（`--setup-feishu` 向导写入） | 飞书渠道不启动，不影响本地 CLI/桌面 |
| `MINERU_API_TOKEN` | 扫描版 PDF（无文字层）抽不出正文；普通 PDF 走本地 `pymupdf` 不受影响 |

## 数据在哪里

根目录是 `paths.home()`：源码跑=项目根，便携版=exe 所在目录，设了 `SCHOLARSCOUT_HOME` 就用它（Docker 里是 `/data`）。

```
data/raw/                  原件（按 <会话>/<格式> 分目录，docs/19）
data/text/                 抽出的纯文本（检索与摘要读这里）
data/cache/kb-fts.sqlite3  kb.search 的 FTS5 索引，可删可重建
run/state.db               SQLite（WAL）：来源/文档元数据/摘要/会话/消息/工具轨迹/学习卡
run/logs/scout.log         进程日志（轮转 + 密钥脱敏）
```

**正文不入库**：库里只存摘要与 `raw_path` / `text_path` 指针，正文留在 `data/text/` —— 库小、元数据可直接
用 SQL 查、备份时原件与库能分开搬；代价是 `data/text/` 丢了要重新抽取（原件还在就不必重新下载）。
整个 `data/` + `run/` + `config.yaml` + `.env` 拷走就是完整迁移。

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

## 能力边界

- **没有 JS 引擎**：只抓服务端渲染的 HTML，纯前端渲染的 SPA 抓不到正文（会得到空正文或只剩导航，判为低质量，而不是假装读到了）。
- **Office 只收 OOXML**：`.docx/.xlsx/.pptx` 可以，**老格式 `.doc/.xls/.ppt` 读不了**（OLE 复合二进制，不是 zip），需先另存为新格式。
- **单文件 20MB 上限**：出网层硬上限 `fetch.max_bytes`（默认 20MB）超了直接拒收；`archive.download` 另有 20MB 抽取阈值（`fetch.large_file_mb`），超了只落盘不抽正文。
- **抓正文遵守 robots.txt**：唯一出网口 `fetch/http.py` 按 host 限速、指数退避并遵守 robots（官方 API 主机按各自服务条款）。
- **不代你登录**：没有账号池，不绕付费墙/验证码；需登录的资料请自行下载后放进 `data/raw/`。
- **不做无出处的断言**：结论要么带 refs，要么明说没查到；它不是通用聊天模型，检索/精读/学习闭环才是主路径。

常见问题（`pip install -e .` 崩、`PYTHONPATH` 污染、端口占用、首次建索引慢、容器无 GUI、`.env` 不入库）
逐条写在 [QUICKSTART.md](QUICKSTART.md#常见问题)。

## 许可证

MIT License，见仓库根 `LICENSE`（Copyright (c) 2026 shiliyunma）。注：`pyproject.toml` 目前未声明 `license` 字段，许可证以 `LICENSE` 文件为准。

引用方式与更多细节见 [QUICKSTART.md](QUICKSTART.md#许可证与引用)；全部设计文档见 `docs/00-总索引.md`。
