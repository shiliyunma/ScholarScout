# QUICKSTART — 10 分钟跑起 ScholarScout

三种跑法任选一种。**第一次用建议走 ②**（源码跑，出错时看得见现场），想省事就走 ①，
想放在服务器/群里当机器人就走 ③。

- 需要的东西：一个 DeepSeek（或任何 OpenAI 兼容）API key —— 不配也能启动，但模型不可用。
- 全部数据留在本机：`data/` 与 `run/`（路径见 [数据在哪里](#数据在哪里)），只有模型调用和资料抓取会出网。

---

## ① 桌面版（Windows，下载即用）

```bash
# 1) 打开仓库的 Releases 页，下载 ScholarScout-Portable-v<版本>.zip（旧版本也在那一页）
# 2) 解压到**不含中文的路径**（例如 D:\apps\ScholarScout），双击 ScholarScout.exe
# 3) 首次启动会生成 config.yaml 与 .env.example：把 .env.example 复制成 .env，填 LLM_API_KEY
# 4) 重开 exe → 左上角设置 → 填 key 也可以（写入 .env，界面上只显示掩码）
```

> **旧版本在哪找**：<https://github.com/shiliyunma/ScholarScout/releases>（每个 tag 挂着 zip 与
> `SHA256SUMS`）。本机只留当前版本 `releases/v<版本>/`，产物不进 git —— 规则与台账见
> [docs/29-发布产物管理.md](docs/29-发布产物管理.md)。

> Releases 页还没有桌面包时，用仓库自带的打包脚本自己出一个（需要本机有 Python 3.11）：
> `python scripts/release.py`（构建 → 打 zip → 算 SHA256 → 落 `releases/v<版本>/`，
> 只想要目录就 `python scripts/build_bundle.py`，产物在项目根的 `ScholarScout-Portable/`；
> 会带 `.env.example`，但**不带**你本地的 `.env` 与 `data/`）。

## ② 源码跑（Windows / macOS / Linux 通用）

```bash
uv venv --python 3.11
.venv/Scripts/python.exe scripts/install_deps.py     # macOS/Linux 换成 .venv/bin/python
cp .env.example .env      # 填 LLM_API_KEY=sk-...
cp config.example.yaml config.yaml   # 可选：不复制则全用默认值
.venv/Scripts/python.exe scripts/scout.py --initdb   # 建库（幂等，可重复跑）
.venv/Scripts/python.exe scripts/scout.py --desktop  # 打开窗口；没有图形界面就用 --chat
```

**不要 `pip install -e .`**，原因见 [FAQ](#常见问题) 第 1 条 —— 装了会连 `print(1)` 都跑不了。

## ③ Docker（无界面：只有 CLI / JSON-RPC / 飞书机器人）

```bash
cp .env.example .env            # 填 LLM_API_KEY
docker compose run --rm scout --initdb     # 建库（数据落在命名卷 scout-data 里）
docker compose run --rm scout --chat       # 终端对话
docker compose up -d                       # 后台常驻（默认命令就是 --chat）
```

**容器内没有 GUI，也没有 WebView2**：镜像故意不装 `pywebview`，`--desktop` 会明确回
“未安装 pywebview”。桌面窗口只能在宿主机上跑（路径 ① 或 ②）。

只想跑一次、不想用 compose：

```bash
docker build -t scholarscout:dev .
docker run --rm -it -v scout-data:/data scholarscout:dev --initdb
docker run --rm -it -v scout-data:/data --env-file .env scholarscout:dev --chat
```

国内拉不到 `python:3.11-slim` 时，基础镜像可用构建参数换源（默认仍是官方源）：

```bash
docker build --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim -t scholarscout:dev .
```

可选：给 `research.search` 起一个自建 SearxNG（返回结构化 JSON、免 key）。

```bash
docker compose --profile search up -d searxng     # http://localhost:8080
# 然后在 config.yaml 里写： search: { searxng_base_url: "http://localhost:8080" }
# 容器内的 scout 访问它用服务名：http://searxng:8080
```

---

## 必须配置的项

密钥只从 `.env`（或同名环境变量）读，设置只从 `config.yaml` 读 —— 两者都不入库。

| 配置项 | 位置 | 不给会怎样 |
| --- | --- | --- |
| `LLM_API_KEY` | `.env` | **唯一硬需求**。没有它：界面/窗口、建库、翻已收录资料都正常，但任何需要模型的动作（对话、摘要、判权、路线）会直接报“缺少模型密钥”，不会静默假装成功 |
| `LLM_BASE_URL` / `LLM_MODEL` | `.env`（或 `config.yaml` 的 `llm`） | 留空用 `config.yaml` 的默认值（默认指向 DeepSeek）。想跑本地 Ollama 就填 `http://localhost:11434/v1` + `modelscope.cn/Qwen/...`，key 随便写非空值 |
| `S2_API_KEY` | `.env` | 不填 → Semantic Scholar 来源不启用（无 key 时该接口恒回 429，是真机实测）；其余来源不受影响 |
| `TAVILY_API_KEY` / `SERPER_API_KEY` / `BRAVE_API_KEY` / `GOOGLE_CSE_KEY` | `.env` | 都不填 → 商用检索不启用，回落到自建 SearxNG / arXiv / OpenAlex / GitHub 等免 key 来源 |
| 飞书（`FEISHU_APP_ID` / `FEISHU_APP_SECRET` 等） | `.env` + `--setup-feishu` 向导 | 不填 → 只用本地 CLI/桌面，飞书渠道不启动。**不影响其他任何功能** |
| `MINERU_API_TOKEN` | `.env` | 不填 → 扫描版 PDF（没有文字层）抽取失败，普通 PDF 走本地 `pymupdf` 不受影响 |

## 数据在哪里

所有落盘路径都由 `src/scholarscout/paths.py` 拼，根目录是 `paths.home()`：源码跑=项目根，
桌面便携版=exe 所在目录，设了环境变量 `SCHOLARSCOUT_HOME` 就用它（Docker 里是 `/data`）。

```
data/raw/                   原件（PDF/HTML/Office…按 <会话>/<格式> 分目录，见 docs/19）
data/text/                  从原件抽出的纯文本（检索与摘要都读这里）
data/cache/kb-fts.sqlite3   kb.search 的 FTS5 索引，**可删可重建**（删了下次检索自动重建）
run/state.db                SQLite 库：来源、文档元数据、摘要、会话、消息、工具轨迹、学习卡
run/logs/scout.log          进程日志（轮转 + 密钥脱敏），起不来时先看这里
```

**设计上“正文不入库”**：库里只存摘要和 `raw_path` / `text_path` 两个指针，正文留在
`data/text/`。好处是库小、可以直接用 SQL 看元数据、备份时原件与库能分开搬；代价是
`data/text/` 丢了就得重新抽取（原件还在 `data/raw/` 时不会重新下载）。

整目录拷走就是完整迁移：`data/` + `run/` + `config.yaml` + `.env`。Docker 里对应命名卷
`scholarscout_scout-data`。

## 常见问题

1. **`pip install -e .` 直接把解释器搞崩（连 `print(1)` 都跑不了）** —— 项目路径含中文时，
   uv/pip 的 editable 安装写一个 UTF-8 的 `.pth`，而 CPython 的 `site.py` 用 locale 编码
   （中文 Windows 上是 GBK）读它，启动即崩。所以本项目**不做 editable 安装**：装依赖用
   `scripts/install_deps.py`，跑 CLI 用 `scripts/scout.py`（它把 `src` 显式插进 `sys.path`）。
   已经踩了：删掉 `site-packages` 里那个 `.pth` 或直接重建 venv。
2. **`export PYTHONPATH=src` 之后测试莫名变红** —— 本项目 `pyproject.toml` 的
   `[tool.pytest.ini_options] pythonpath = ["src"]` 已经处理了导入；外面再叠一个 `PYTHONPATH`
   会让 `src` 里的包和已安装副本打架（同一个模块被加载两次、`__file__` 指向不同副本）。
   **跑测试/门槛脚本时不要设 `PYTHONPATH`**。
3. **端口被占：`bind: address already in use`（最常见是 8080）** —— SearxNG 默认占 8080，
   而 8080 也常被别的开发服务占着。改 `docker-compose.yml` 里的 `ports: "8080:8080"` 左边那半
   （例如 `18080:8080`），再把 `config.yaml` 的 `searxng_base_url` 一起改掉。
4. **第一次开 `kb.search` 很久没反应** —— 索引是**懒建**的：第一次检索才扫 `data/text/` 建
   FTS5 索引，资料多时首次要几十秒到几分钟，之后是增量。它不阻塞启动，所以界面上看起来像“没反应”。
   等一次建完；想强制作废就删 `data/cache/kb-fts.sqlite3`。
5. **Docker 里点不出窗口 / `--desktop` 报“未安装 pywebview”** —— 不是 bug：容器没有显示器也没有
   WebView2，镜像故意不装 `pywebview`（只影响桌面模式）。容器里请用 `--chat` / `--rpc` /
   飞书渠道；要窗口请在宿主机跑路径 ① 或 ②。
6. **`.env` 提交不上去** —— 故意的：`.gitignore` 挡 `.env`、`config.yaml`、`data/`、`run/`。
   仓库里只有 `.env.example` / `config.example.yaml` 模板。**不要**为了“方便别人”把真 key 提交进去，
   一旦进了历史就只能改写历史 + 立刻吊销 key。
7. **飞书渠道第一次启动卡 1~2 分钟** —— 首次 `import lark_oapi` 本机实测约 116 秒（SDK 自身很大），
   不是死循环；用 `scripts/feishu_ws_up.py --check` 只看前提自检，不等真连接。

## 能力边界

- **没有 JS 引擎**：只抓服务端渲染的 HTML。纯前端渲染的站点（SPA、需要滚动/点击才出的正文）抓不到内容，
  通常会得到空正文或只有导航文字 —— 这类来源会被判为低质量，而不是假装读到了。
- **Office 只收 OOXML**：`.docx/.xlsx/.pptx` 可以；**老格式 `.doc/.xls/.ppt` 读不了**（OLE 复合
  二进制，不是 zip），需要先用 Office/LibreOffice 另存为新格式。
- **单文件 20MB 上限**：出网层有硬上限（`fetch.max_bytes`，默认 20MB），超了直接拒收；`archive.download`
  另有 20MB 抽取阈值（`fetch.large_file_mb`），超过只落盘不抽正文。
- **抓取遵守 robots.txt**：`fetch/http.py` 是唯一出网口，按 host 限速、指数退避，并遵守 robots
  （官方 API 主机除外，按它们的服务条款走）。被 robots 禁止的路径不会强抓。
- **不代你登录**：没有账号池，也不绕付费墙/验证码。需要登录才能看的资料请自己下载后放进 `data/raw/`。
- **不做“无出处的断言”**：结论要么带 refs，要么明确说没查到；它不是通用聊天模型，检索/精读/学习闭环才是主路径。

## 许可证与引用

MIT License（见仓库根 `LICENSE`，Copyright (c) 2026 shiliyunma）。可以商用、修改、再发布，
保留版权声明与许可文本即可。

如果你在论文/报告里引用本项目，建议写：

```text
张思浩. ScholarScout: 开放世界学术调研与学习辅助智能体 [CP/OL]. 2026.
https://github.com/shiliyunma/ScholarScout
```

更多细节：`README.md`（功能与架构入口）、`docs/00-总索引.md`（全部设计文档）、
`docs/01-接口契约.md`（RPC/工具契约）。
