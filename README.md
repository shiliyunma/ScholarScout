# ScholarScout

面向开放世界的学术调研与学习辅助智能体（本科毕业设计，2027 届）。

**一句话**：你说"我想入门 XX 方向"，它去外网找资料、判断来源权威性与相关性、把原件归档到本地、给出带出处的学习路线，并陪你把这条路线走完。资料和会话全在本地。

---

## 这是什么 / 不是什么

| 是 | 不是 |
|---|---|
| 开放世界检索：现去外网找，找到什么用什么 | 已知知识库检索（**本课题不做 RAG**） |
| 判断来源权威性与相关性，并给出可解释的理由 | 把搜索结果直接丢给模型总结 |
| 原始资料本地归档（原件 + 纯文本 + 元数据） | 云端存一份、本地只留 URL |
| 学习路线规划 / 总结 / 格式化输出 / 辅助学习 | 通用聊天机器人 |

**为什么不做 RAG**（答辩必问，先把答案写在这）：本课题解决的是"这个方向我该怎么入门、哪些资料可信"，输入是**未知的开放世界**，没有可预先建库的语料；语义检索解决的是"已知语料里哪几段最相关"，与本课题的问题定义不匹配。因此检索走 FTS5 关键词 + 来源分层规则，把工程重心放在**发现、判权、归档、路线**四个环节。

## 架构（四层）

```
桌面壳 pywebview ──JSON-RPC 2.0/stdio──> rpc/ ──> runtime/ ──> tools/ 6 个主线工具
                                                          │
                                          providers/ + fetch/（出网唯一入口）
                                                          │
                                          state/（SQLite + FTS5）  data/（原件归档）
```

主线工具：`research.search` / `source.fetch` / `source.grade` / `topic.build_map` / `route.plan` / `note.save`
降级通道（默认关，仅作论文消融对照）：`kb.search`

## 技术栈（与 Hermes Agent 同构的下半身，前端降级）

| 面 | 选型 | 为什么 |
|---|---|---|
| 后端 | Python 3.11 + uv | 与既有技术积累一致 |
| 桥 | JSON-RPC 2.0 over stdio | 照 Hermes `tui_gateway` 的分层思路，自己实现 |
| 桌面壳 | pywebview（Edge WebView2） | 纯 Python，无 Node 工具链 |
| 前端 | 原生 HTML/CSS/JS，**无构建步骤** | Hermes 桌面端是 57 万行 TS —— 已评估，成本过高，否决 |
| 存储 | SQLite(WAL) + FTS5 | 照 Hermes `state.db`：FTS 由触发器维护 |
| 中文检索 | FTS5 `tokenize='trigram'` + 短查询 LIKE 兜底 | 实测 trigram 对 <3 字的中文查询返回 0 条，必须兜底；见 `docs/02 §4.1` |

## 决策记录

| 日期 | 决策 | 理由 |
|---|---|---|
| 2026-10-05 | 主线改为**开放世界调研 + 学习辅助**；知识库通道降级为可插拔次要通道 | 问题定义是"未知世界的入门"，不是"已知语料的复查" |
| 2026-10-05 | **不做 RAG / 向量检索** | 与问题定义不匹配；避免为手段而手段 |
| 2026-10-05 | AIhot 只作架构参照，不部署、不跑 | 它是新闻聚合/成刊项目，没有 query 检索能力，当不了检索引擎 |
| 2026-10-05 | 复用 Hermes 技术栈，废弃旧 1680 行脚手架 | 上层架构值得学，旧代码可弃；教训（arXiv 限速等）继承 |
| 2026-10-05 | 原件存本地目录 + 纯文本进 SQLite FTS + 元数据进表 | 备份/迁移/可读三种需求分开满足 |
| 2026-10-05 | 桌面壳不 import 业务模块，改为拉起 `--rpc` 子进程 | 分发只有一份（CLI/桌面/测试同路径）；后端崩了窗口还在；中文路径编码坑挡在子进程里 |
| 2026-10-05 | 检索来源分三层：学术（arxiv）/ 通用（SearxNG 自建、商用 API）/ 抓取（默认关） | 抓搜索结果页既违反 robots（Bing 禁 `/search`）又质量差（匿名请求返回词典类结果，实测） |
| 2026-10-05 | `providers: ["auto"]` —— 只带"现在真能用的"来源 | 填了 key 就自动生效；没配的来源不刷 `missing_api_key`；抓取/需梯子的来源不带上，免得每次检索都撞超时 |

## 检索来源（通用搜索 / 商用搜索）

| 来源 | 类型 | 要什么 | 怎么开 |
|---|---|---|---|
| `arxiv` | 学术预印本 | 免 key | 默认开（≥3s 限速，只索引英文） |
| `openalex` | 期刊/会议论文与综述（带被引数、DOI） | 免 key | 默认开（实测 1.6s 返回真综述；robots 是 `Allow: /`） |
| `github` | 代码/官方实现/教程 | 免 key（`.env` 配 `GITHUB_TOKEN` 可提额） | 默认开（Search API 未认证 10 次/分钟 → 间隔 6s） |
| `searxng` | **通用检索（推荐）** | 免 key，需自建实例 | `cd deploy/searxng && docker compose up -d`，再把 `search.searxng_base_url` 配成 `http://localhost:8080` |
| `bing` / `duckduckgo` | 通用（抓结果页） | 免 key | **默认关**：需显式开 `search.allow_searchpage_scraping`（Bing robots 禁 `/search`；DDG 国内需梯子） |
| `tavily` / `serper` / `brave` / `google_cse` | 商用 API | `.env` 里的 key（`google_cse` 还要 `search.google_cse_cx`） | 填 key 即自动生效（Brave/Google CSE 有免费额度） |

**通用检索挂了怎么办**：它跑在 Docker 里（`deploy/searxng/`），Docker Desktop 关掉后容器不会自己回来 ——
检索会报连接失败。一条命令修好（会自动拉起 Docker Desktop + 起容器 + 等引擎加载 + 用真 provider 自检）：

```bash
.venv/Scripts/python.exe scripts/searxng_up.py           # 起服务 + 自检
.venv/Scripts/python.exe scripts/searxng_up.py --status  # 只看状态
```

SearxNG 在国内要绕三个坑（全部实测过，详见 `deploy/searxng/settings.yml` 注释）：

1. **Docker Hub 不可达** → `docker pull docker.m.daocloud.io/searxng/searxng:latest` 再 `docker tag` 回官方名
2. **默认引擎表在境内全 timeout**（google/ddg/brave/wikipedia）→ `keep_only` 收窄
3. **`keep_only` 只是"保留"，不等于"启用"** —— 保留的引擎默认仍是 `disabled`，
   `/config` 会报"启用引擎数: 0"、聚合查询恒 0 条 → 必须再写 `engines: [{name: ..., disabled: false}]`

实测可用的引擎：`360search`、`sogou`（英文查询 16 条、中文查询 14 条）；
`baidu` 要验证码、`bing` 返回空、`quark` 崩溃、`chinaso`/`mojeek` 空 —— 都不进配置。
容器 restart 后**别立刻查**（加载引擎要十几秒，太早查会得到 `results: []`）。

```bash
# 真机自检：逐个来源打一次真请求，缺 key / 被拦 / 解析失效都会当场说出来
.venv/Scripts/python.exe scripts/check_search_sources.py --all
# 走真工具路径（注册表 → 工具 → 来源 → 入库），端到端验一次
.venv/Scripts/python.exe scripts/check_search_sources.py --via-tool --searxng-base-url http://localhost:8080
```

## 文档

| 文档 | 内容 | 状态 |
|---|---|---|
| `docs/00-总索引.md` | 导航 + 版本演进 + 未决项 | v0.1 |
| `docs/01-接口契约.md` | RPC 方法表、工具 schema、错误码 | v0.1 待冻结 |
| `docs/02-数据模型.md` | 全部 DDL 的唯一权威来源 | v0.1 待冻结 |
| `docs/03-目录结构与规则文件.md` | 目录树、模块边界、门槛、依赖登记 | v0.1 |
| `docs/项目规则.md` | 开发规则正文（AI 协作 8 条铁律） | v0.1 |
| `docs/04-摘要提取方案.md` | 摘要口径 + MinerU 接入（含真机契约） | v0.1 |

## 快速开始

```bash
# 1) 建虚拟环境 + 装依赖（已实测可用）
bash scripts/bootstrap.sh

# 2) 配置（密钥进 .env，设置进 config.yaml）
cp config.example.yaml config.yaml
cp .env.example .env            # 填 LLM_API_KEY

# 3) 建库（幂等，可重复跑）
.venv/Scripts/python.exe scripts/scout.py --initdb

# 4) 门槛（lint + 类型 + 测试 + 三项校验，一个不过就不合）
bash scripts/check.sh

# 5) 打开桌面窗口（需要 pywebview，已在依赖白名单里）
.venv/Scripts/python.exe scripts/scout.py --desktop

# 5b) 想要"双击即用"？编译一个启动器 exe（用 Windows 自带 csc，不装新依赖）
.venv/Scripts/python.exe scripts/build_exe.py
#   → ScholarScout.exe（项目根，带图标）+ ScholarScout.launcher.ini（指向项目根）
#   双击即可；排查用 ScholarScout.exe --diagnose

# 6) 跑评测（离线集秒级，报告落 eval/reports/）
.venv/Scripts/python.exe eval/run_eval.py --set all --ablation
.venv/Scripts/python.exe eval/run_eval.py --set e3 --live   # 端到端，需要 .env 里的密钥与网络
```

### 双击启动（项目根的 `ScholarScout.exe`）

exe 就放在**项目根**（跟 `README.md` 同级），双击最顺手 —— 它是个"入口"，不是构建中间产物
（想收进子目录：`python scripts/build_exe.py --out-dir dist`）。

这个 exe **不是**把应用打包进去的成品，而是一个**启动器**：找项目根 → 找 `.venv` 的解释器 →
拉起 `scripts/scout.py --desktop`。所以**改了代码也不用重编它** —— 这正是"不会因版本更替失效"的做法
（打包成一体的 exe 才要每次改完重打包）。

| 场景 | 它怎么做 |
|---|---|
| 项目就在默认位置 | 直接跑（exe 在项目根，或放进任意子目录都会往上找到根） |
| 项目移动了 | 改同目录的 `ScholarScout.launcher.ini`（一行 `project=...`），或把 exe 放进项目里 |
| 想指到别处 | `ScholarScout.exe --project D:\别的\ScholarScout` |
| 双击没反应 | `ScholarScout.exe --diagnose` 打印它找的项目/解释器/pywebview 状态，并存一份 `launcher-diagnose.txt` |
| 缺依赖 / 缺 pywebview | 弹窗给出要跑的命令（`scripts/install_deps.py`），不是静默失败 |

> exe / `.ini` / `launcher-diagnose.txt` 都不入库（构建产物 + 本机绝对路径，见 `.gitignore`）；
> 换机器 clone 后跑一次 `scripts/build_exe.py` 就有。

图标：`assets/icon-source.png` 是设计源图，`scripts/make_icon.py` 生成多尺寸 `assets/icon.ico`；
exe 图标（csc `/win32icon:`）与窗口标题栏/任务栏图标（见下）用的是同一份。

> ⚠️ **`webview.start(icon=...)` 在 Windows 上无效**（pywebview 文档写明 `Supported only on GTK/QT`），
> 它的 winforms 后端是从 `sys.executable`（也就是 python.exe）取图标的 —— 所以默认会显示 **Python 的图标**。
> 正确做法见 `src/scholarscout/desktop/icon.py`：窗口出现后对 HWND 发 `WM_SETICON`
> （ICON_BIG 给任务栏、ICON_SMALL 给标题栏左上角），再 `SetCurrentProcessExplicitAppUserModelID`
> 给进程一个自己的任务栏身份，否则任务栏按钮会按 python.exe 归组。

> ⚠️ **不要用 `pip install -e .`**：项目路径含中文，editable 安装写的 `.pth` 会让解释器启动失败
> （`UnicodeDecodeError: 'gbk' codec ...`，连 `print(1)` 都跑不了）。装依赖用 `scripts/install_deps.py`，
> 跑 CLI 用 `scripts/scout.py`。原因链见 `docs/03 §9`。
>
> 当前状态：**M1–M8 主线已闭环**（含通用/商用搜索、GitHub、OpenAlex 两路学术索引、MinerU 兜底接入）；
> `check.sh` 全绿；剩下的见 `docs/00-总索引.md` 的待办。

## 桌面壳怎么工作

```
pywebview 窗口（ui/，原生 HTML/CSS/JS，无构建、离线）
    │  window.pywebview.api.rpc(method, params)   ← 只有这一条通道
    ▼
desktop/bridge.py（只转发 + 包错误）  ← 壳里没有业务逻辑
    │  JSON-RPC 2.0，一行一个 JSON，stdio
    ▼
python -m scholarscout --rpc 子进程  ← 与 CLI/测试走同一个分发器
```

界面能做到的三件事：**问**（流式显示工具调用与出处）、**查**（资料库搜索、看摘要与原件位置）、**收**（学习路线、主题地图、抽背卡的展示与导出）。

### 回答正文按 Markdown 渲染

模型返回的是 Markdown（标题、表格、代码块、列表），界面上由 `ui/markdown.js` 渲染，
不用 `textContent` 直接铺出来。要点：

- **零依赖、无构建**：Hermes 桌面端用的是 streamdown + shiki + katex + dompurify（React + 打包），
  我们这套 UI 是无构建的原生 JS，所以只搬它的**设计决策** —— 围栏代码内不做行内转换、
  裸 URL 自动链接、表格、`> [!NOTE]` 提示块、代码块带语言标签与复制按钮、先剥掉 ` thinking` 推理块。
- **安全靠构造**：模型文本只经 `textContent` 落地，从不拼 `innerHTML` —— 不引 dompurify 也不会被注入。
  链接只放行 http/https。
- **流式渲染有节流**：增量每 120ms 重画一次（末次结果直接落），否则每条增量都重排 DOM 会卡。
- **链接必须拦截**：pywebview 的窗口就是浏览器，直接点 `<a>` 会把界面顶掉 → 统一交给
  `bridge.open_url`（系统默认浏览器打开）。
- 校验：`python scripts/scout.py --check-ui` 会校验界面文件齐全、**只调真实存在的 RPC 方法**、
  无任何外网资源，并额外检查 markdown.js 存在、在 app.js 之前引入、且 app.js 真的用了 `ScoutMarkdown`；
  解析层的 22 条用例在 `tests/desktop/markdown_probe.js`（用 node 真跑产品文件，没装 node 时跳过）。

### 上下文成本：缓存策略与三条瘦身策略

**先说清缓存边界**（用户问过"缓存为什么没效果"）：

| 层 | 有没有缓存 | 说明 |
|---|---|---|
| 网页抓取（`fetch/http.py`） | ✅ 有，6 小时 | 落 `data/cache/`，只缓存"200 且有内容"的**内容抓取** |
| 检索请求 | ❌ 故意不缓存 | 时点查询，缓存会把一次瞬时空响应钉 6 小时（踩过） |
| **模型调用（LLM）** | ❌ **完全没有** | **这是费用的主体，出网缓存对它一点用都没有** |

**钱花在哪（2026-10-05 实测）**：一轮对话模型被调用 ~10 次，而**每次都把前面所有工具结果重发一遍**。
系统提示词 393 tokens + 工具声明 1,562 tokens 是每轮固定开销；单条工具结果最长 9,583 字符 ≈ 3,194 tokens；
库里 38 条工具结果合计 ≈ 32,442 tokens。于是同一段 JSON 在 10 轮里被计费 5～9 次。

**`runtime/context_budget.py` 三条策略**（都对着这个账单）：

1. **去重**：本回合已发过的 `source_id`，第二次出现只回锚点，摘要不重复发
   （多次检索常捞回同一批来源，摘要被反复计费是最大一笔浪费）；
2. **截断**：`snippet` 截到 200 字符、整条结果上限 4,000 字符，超出留一行说明；
3. **折叠**：更早的工具结果（默认只保留最近 3 条全文）压成一行占位符。

**不能省的**：`source_id` 一个都不能丢 —— 它是引用锚点，丢了模型就会编 id（守卫会拦，但那是拿成本换质量事故）。

界面上每轮会显示真实用量：`token 输入 12.3k ／ 输出 0.4k`（`chat.done` 的 `usage`），
所以"贵不贵"以后有数字可查，不靠体感。

## 评测（论文第 5 章的数字从这里来）

三套题集 + 一条取数口径（`docs/05-评测方案.md`）：

| 集 | 规模 | 需要模型 | 现状 |
|---|---|---|---|
| E1 判权一致性 | 24 条 | 否 | tier 一致率 **100%**（κ 1.000）、verdict 一致率 58.3%（κ 0.444），10 条分歧如实列出 |
| E2 反幻觉守卫 | 12 条 | 否 | 守卫行为符合预期 **12/12**；关掉守卫时出处覆盖率虚高 96%→79% |
| E3 端到端 | 6 条 | **是** | 待配置模型密钥后运行 |

对照臂 A_no_rules（撤掉规则表）：tier 一致率从 100% 掉到 **16.7%** —— 规则表确实在干活。
所有调用类指标（成功率、P50/P95 时延、token）**只从 `tool_traces` / `usage_records` 取数**，不另造一套埋点。

## 本地化与隐私

- 资料、会话、索引全部在 `data/` 与 `run/state.db`，**零上传**
- 只有模型调用会出网（走你自己配置的 API）；抓取请求只发向资料源本身
- `.env` 存密钥、`config.yaml` 存设置，两者分离；两者都不入库

## 许可

毕业设计代码，未定许可证。（`LICENSE` 待补）
