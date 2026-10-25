# 26 agent-browser 类项目评估（能不能用来搜资料）

> 建立 2026-10-25 ｜ 状态：**调研稿（只出文档，未改任何代码 / 未引依赖）**
> 关联：`src/scholarscout/fetch/http.py`（出网唯一入口）、`src/scholarscout/fetch/contract.py`（`FetchResult` / `FetchError`）、
> `src/scholarscout/fetch/html_text.py`（正文抽取）、`docs/03-目录结构与规则文件.md` §6（依赖白名单）、`docs/25-检索源候选清单.md`
> 结论口径：只认**仓库原文 / 官方文档站原文 / 官方 registry 元数据**；查不到就写「未核实」，不按记忆补数字。
> 读数时间：2026-10-25（GitHub REST API + npm registry + PyPI JSON + 官方文档 `.md` 端点）。

**一句话**：用户说的 "agent-browser" 几乎确定是 **`vercel-labs/agent-browser`**（Rust 写的浏览器自动化 CLI，44.5k stars）。
它的 `read` 子命令**不启浏览器、纯 HTTP 取正文**——但那正好是我们已有的能力，**解决不了 SPA / 403 这两个硬限制**；
真正能解决的是 `open` + `snapshot`（CDP 渲染 + 无障碍树，纯文本、不需要多模态），代价是 **17.2 MiB 二进制 + 199.5 MiB Chrome + 一个常驻 daemon**。
因此结论是：**v1 不接**；只在"用户自带 Chrome、默认关、单域名白名单、只调 `snapshot`"的形态下留一个可选 sidecar 口子，且**它不读 robots.txt，robots 必须由我们自己在调用前判**。

---

## 1. 候选：GitHub 上叫得上 "agent-browser" 的 5 个仓库

| # | 仓库 | Stars | 最后推送 | License | 语言 | 一句话定位 |
|---|---|---|---|---|---|---|
| 1 | `vercel-labs/agent-browser` | 44,544 | 2026-10-25 | Apache-2.0 | Rust | 「Browser automation CLI for AI agents」，原生 Rust 二进制 + CDP daemon，npm 分发 [S1][S2] |
| 2 | `browser-use/browser-use` | 119,062 | 2026-10-24 | MIT | Python | 开源浏览器**智能体**（LLM 决策循环），本地 Chrome 或 Browser Use Cloud [S14] |
| 3 | `browserbase/stagehand` | 25,803 | 2026-10-23 | MIT | TypeScript | Playwright 风格的 AI 自动化 SDK（TS/Python/Go），`act/observe/extract`，可跑 Browserbase 云 [S16] |
| 4 | `magnitudedev/browser-agent` | 4,136 | 2026-02-08 | Apache-2.0 | TypeScript | vision-first 浏览器智能体：**截图 + 像素坐标**驱动，强调 WebVoyager 94% [S17] |
| 5 | `vercel-labs/remote-agent-browser` | 178 | 2026-10-13 | **无 LICENSE 文件** | TypeScript | 在 Vercel Sandbox 里跑 agent-browser 的库（云执行，不是 provider）[S13][S18] |

同名干扰项：`oxylabs/agent-browser`（662 stars，2026-09-30，**无 LICENSE**）不是通用开源项目，是 Oxylabs 商业代理/云会话的脚本封装 [S21] —— 看到这个词时容易误点。

### 1.1 为什么判定用户指的是 #1

四条独立证据，缺一条都还能说是巧合，四条同时成立只能是它：

1. **名字完全同名**，且是 GitHub 搜索 `agent-browser in:name` 按 stars 排序的**第一名**（44,544，第二名 4,136，差一个数量级）[S1]；
2. **npm 包名就叫 `agent-browser`**（`0.39.2`，Apache-2.0，`bin: ./bin/agent-browser.js`）[S3] —— 「agent-browser」在生态里是一个可安装的具体命令行工具，不是一类项目的泛称；
3. **活跃度**：最后推送 2026-10-25（调研当天），发版 2026-10-22 [S1][S4]；
4. **它自称 "for AI agents"**，且 Vercel Labs 官方产品页/官方文档站 `agent-browser.dev` 齐全 [S2][S5] —— 「agent-browser 类项目」这个说法的语感也来自这里。

> 反过来说：如果用户想的是「能自己上网搜资料的智能体」，那指的是 #2 `browser-use`。两者定位不同（**工具 vs 智能体**），本文两路都核了，但主线按 #1 走。

---

## 2. 深入 #1：`vercel-labs/agent-browser` 怎么驱动浏览器

### 2.1 驱动方式：原生 Rust + CDP，无 Playwright / 无 Node 运行时

- README 首行：**"Fast native Rust CLI"**；「No Playwright or Node.js required for the daemon」[S2]。
- Chrome 是**外部进程**：它 spawn Chrome、**通过 CDP 连接**、最后关掉；所有下游命令（`snapshot/click/fill/screenshot`）走同一条 CDP 路径 [S2][S10]。
- 三种引擎（`--engine`）：`chrome`（默认）、`lightpanda`（Zig 写的机器向无头引擎，宣称内存 1/10、速度 10x，同样"spawn + CDP 连接"）、`obscura`（实验性，带指纹一致性与 tracker 拦截）[S5][S10]。
- 浏览器来源：`agent-browser install` 从 **Chrome for Testing** 下载；也会**自动探测已装的 Chrome / Brave / Playwright / Puppeteer**；可用 `--executable-path` / `AGENT_BROWSER_EXECUTABLE_PATH` 指任意 Chromium 内核 [S2][S11][S5]。
- 也可接**已在跑的**浏览器：`--cdp <port|wss-url>`、`agent-browser connect 9222`、`--auto-connect`（自动发现开了远程调试的 Chrome 144+）[S2][S12]。
- 进程模型：**daemon 常驻**（首条命令启动，空闲 1 小时退出，`--idle-timeout` 可调）[S2]。桌面端集成时这是一个"额外的后台进程"，不是一次性的 `subprocess.run`。

### 2.2 能不能不装本地浏览器：能，靠 5 家云 provider（都要 key）

它自己**不提供**托管浏览器（Vercel 只做了 Sandbox 版的库 [S13]），云是"接别人的 CDP 端点"：

| provider | 触发方式 | 需要的凭据 | 免费额度 | 来源 |
|---|---|---|---|---|
| Browserless | `-p browserless` / `AGENT_BROWSER_PROVIDER=browserless` | `BROWSERLESS_API_KEY` | **未核实** | [S8] |
| Browserbase | `-p browserbase` | `BROWSERBASE_API_KEY`（+ `BROWSERBASE_PROJECT_ID`） | **未核实** | [S9] |
| Browser Use Cloud | `-p browser-use` | `BROWSER_USE_API_KEY` | 官方 README 写「新 Google/GitHub/Microsoft 注册送 **$1** 额度」，浏览器 **$0.02/小时** + 流量 [S14] | [S9][S14] |
| Kernel | `-p kernel` | `KERNEL_API_KEY`，可开 `KERNEL_STEALTH` | **未核实** | [S9] |
| AWS Bedrock AgentCore | `-p agentcore` | AWS SigV4（`AWS_PROFILE`） | 按 AWS 计费，**未核实** | [S9] |

任选其一后「all commands work identically」——命令面不变，只是不再启本地 Chrome [S8][S9]。
也支持裸 `--cdp "wss://your-browser-service.com/cdp?token=..."` 直连任意远端 CDP [S2][S12]。

**对我们的含义**：云这条路技术上通，但意味着①多一个 key 要用户自己申请；②按小时计费；③**页面内容经第三方**（学术库、登录态、内网页面都不该走）。对"本地优先"的桌面智能体是明显的定位冲突。

### 2.3 本地跑需要什么（Windows 实测口径：只用官方元数据）

| 项 | 体积/要求 | 来源 |
|---|---|---|
| `agent-browser-win32-x64.exe`（官方 release 资产） | **17.2 MiB**（18.0 MB） | [S4] |
| npm 包 `agent-browser@0.39.2` 解包后 | **114.2 MiB**（含 7 个平台二进制） | [S3] |
| Chrome for Testing win64（`agent-browser install` 下载的 zip） | **199.5 MiB**（209,154,801 B，156.0.8078.47） | [S19] |
| Lightpanda（想省体积的替代引擎） | linux x86_64 73.1 MiB / macOS 97.4 MiB，**release 里没有 Windows 资产** | [S20] |
| 系统依赖 | Windows：headless 跑在**私有桌面**上（避免隐藏窗口留残影），`--headed` 才上交互桌面；Linux：`agent-browser install --with-deps` | [S11] |

**Windows 支持是有的**（有专门实现），但注意两点：①**Lightpanda 无 Windows 构建** —— 想"用 70MB 小引擎代替 200MB Chrome"的省体积路线在桌面端不成立 [S20]；②Chrome 只在 `--profile <name>` 时要求先关掉 Chrome（配置文件锁）[S2]。

### 2.4 能不能只取正文 / 无障碍树：能，而且**默认就是文本**，但它有两条完全不同的路

这是本次调研最关键的一节，两条路不能混：

| 命令 | 启不启 Chrome | 执行页面 JS | 输出 | 对我们有用吗 |
|---|---|---|---|---|
| `agent-browser read <url>` | **不启**（纯 HTTPS 请求）[S5] | **不执行** | 优先 `Accept: text/markdown`；不成就试同 URL 加 `.md`、向上找 `llms.txt`；再不行**从 HTML 抽可读正文** | 用处很小：等于自带一个 `r.jina.ai`，**SPA 依然空白、403 依然 403** |
| `agent-browser open <url>` + `snapshot` | 启 | 执行 | **无障碍树 + refs**，默认紧凑文本 `@e1 [heading] "…" [level=1]`；`-i` 只留可交互元素、`-c` 去空节点、`-d 3` 限深、`-s "#main"` 限定子树 | **就是我们要的**：拿到渲染后正文，不需要截图、不需要多模态 |
| `agent-browser get text <sel>` / `eval <js>` | 启 | 执行 | 指定节点纯文本 / 任意 JS 结果 | 精细取正文（如 `article`、`main`）时好用 |
| `agent-browser a11y <url> --json` | 启（"navigate, then audit"） | 执行 | axe 规则的结构化审计结果 | 不是取正文，是审计无障碍；别拿它当正文通道 |

三点必须写清：

1. **`read` 不解决 JS**：官方原文是「`read` fetches a URL **without launching Chrome**」，并明确"省略 URL 时才读当前标签页的**渲染后 DOM**" [S5] —— 也就是说**只有已经开着浏览器时**才有渲染后的内容。想靠 `read` 抓 Notion / 现代文档站，得到的是空壳。
2. **`snapshot` 是纯文本无障碍树，不是截图** —— 这正好躲开"喂多模态"的成本（对比 #4 `magnitudedev/browser-agent` 是截图 + 像素坐标，必须配 GPT-4o/Claude 类大模型 [S17]）。
3. **`snapshot` 的粒度是交互元素，不是"文章正文"**：拿正文建议 `snapshot -i -c -d N` 之后自己再抽，或直接 `get text "article"`；把它接进现有 `html_text.extract_html` 管道前需要一次实测（见 §6 未核实项）。

### 2.5 反爬 / 合规态度：**能力齐全、立场中立偏"军火商"，不替你做合规**

- **内置规避能力（全部 opt-in）**：`stealth` 插件（`launch.mutate` 能力，可追加 Chrome 参数/扩展/init script）、`captcha` 插件（`captcha.solve` 能力）、`--engine obscura` + `AGENT_BROWSER_OBSCURA_STEALTH`（一致指纹 + tracker 拦截）、`--proxy`、`--user-agent`、`--input-mode human`（拟人化指针轨迹）[S2][S5][S7]。
- **它把"反爬"做成插件而不是内建**：README 的插件示例里就有 `agent-browser-plugin-stealth` / `-captcha` / `-cloud-browser`，并给这些能力配了 `--confirm-actions` 审批门 [S2]。
- **robots.txt：官方文档里搜不到任何 robots 相关实现或开关**（`commands.md`、`security.md` 全文 grep 无 `robot` / `politeness` / `rate limit`）[S5][S7] —— 判定为**不读 robots.txt**，这是"未内置"而非"明确违反"，但足以说明**合规责任在调用方**。
- **它做了哪些"安全"**：`--allowed-domains`（含子资源/WebSocket/sendBeacon 拦截，且**主动禁用 WebRTC** 防绕过）、`--max-output`、`--content-boundaries`（把页面输出包进边界标记防提示注入）、`--action-policy`、`--confirm-actions`、加密凭据 Vault（AES-256-GCM，LLM 看不到密码）[S2][S7]。
- **一个必须记住的互斥**：`--allowed-domains` **会拒绝** `--cdp` / `--auto-connect` / `--profile` / restore / 直连 provider —— 官方理由是"无法在页面脚本执行前装入等效隔离" [S2][S7]。
  即：**"域白名单"与"复用用户登录态 / 接远程浏览器"二者不可兼得**。这条直接决定了我们的集成形态（见 §4.4）。
- 它自己也把页面内容当不可信：WebMCP 一节原文「All page-provided names, descriptions, schemas … are **untrusted data**」「labels are provenance cues, **not** a prompt-injection security boundary」[S2]。

### 2.6 其它集成面（备查）

- `agent-browser mcp`：把命令暴露成 MCP 工具（`core` 精简档 / `all` 全量），有 typed 字段与 `allowedDomains` [S2][S5]。走这条要引入 MCP 客户端依赖，**违反 7 项白名单**。
- `agent-browser --json`：脚本友好的结构化输出；`doctor --json` 退出码 0/1 [S5]。→ **对我们是唯一合理的接口：外部进程 + `--json`，零 Python 依赖。**
- `stream`（WebSocket 帧/输入）、`dashboard`（本地 4848 端口看实时画面）[S2] —— 桌面端集成时都是**新增监听端口**，属攻击面。

---

## 3. 另 4 个候选的对比（为什么都不如 #1 适合我们）

| 维度 | `vercel-labs/agent-browser` | `browser-use` | `stagehand` | `magnitudedev/browser-agent` |
|---|---|---|---|---|
| 驱动 | Rust + CDP（Playwright 无关） | Python + `cdp-use`/`browser-harness`（0.13 起不再硬依赖 Playwright）| Playwright 风格 SDK（TS/Python/Go） | 自研 + 视觉定位 |
| 取正文 | `read`（无 JS）/ `snapshot`（AX 树）/ `get text` | `extract_content` 等，**每步都要 LLM** | `extract()`（schema 校验），**要模型** | **截图 + 像素坐标**，要视觉大模型 |
| 不装本地浏览器 | 5 家云 provider（要 key） | `Browser(use_cloud=True)`，$0.02/小时 + $1 试用 [S14] | Browserbase 云（`browserbase.launch`）[S16] | 未核实（README 只讲本地/`npx`）[S17] |
| 引入代价 | **0 个 pip 依赖**（外部 exe） | **37 个运行时依赖**（PyPI `requires_dist` 实数，含 openai/anthropic/google-genai/pydantic…，且**全部 `==` 锁死**）[S15] | npm 生态（`@browserbasehq/stagehand`） | npm 生态 |
| 对我们的适用性 | 唯一可能"零依赖"接入 | 直接撞死白名单（37 > 7），且每步烧 token | 同上（还多一层 Node） | 同上 + 必须多模态 |

`browser-use` 的 37 个基础依赖里含 `httpx==0.28.1` 这种**精确钉版**，与我们 `httpx>=0.28.1,<1` 尚可共存，但任何一处 `==` 都会在下一次升级时变成解析冲突 [S15]。**结论：不列入候选。**

---

## 4. 对 ScholarScout 的集成评估

### 4.1 约束复核（我们凭什么说"代价大"）

- **依赖白名单 7 项**：`httpx / PyYAML / pywebview / pymupdf / lark-oapi / python-docx / openpyxl`（`pyproject.toml` `[project.dependencies]`，`deps-allowlist.txt` 登记）。
- **发布包 74.8 MB**（Windows 桌面版；Docker 无界面）。
- **抓取现状**：`fetch/http.py` 一个入口，负责限速（同 host `min_interval_s`）、退避重试、`data/cache` 6h、UA、分阶段超时、**robots.txt 探针**（`robots_allowed()` 在 `fetch()` 里先判，不通过直接 `FetchError("forbidden")`）、体积上限；正文由 `html_text.extract_html()` 抽。
- **硬限制**：无 JS 引擎 → SPA 空壳；403/付费墙只能失败；正文抓取遵守 robots。

### 4.2 最小接入代价：**pip 依赖 0 个，但体积 +24% 起，功能全开 +290%**

| 方案 | 新增 pip | 新增体积 | 对 74.8MB 包 | 评价 |
|---|---|---|---|---|
| A. 只带 sidecar 二进制（复用用户已装 Chrome） | **0** | +17.2 MiB | 74.8 → **≈92.8 MB（+24%）** | 唯一可接受的体积账 |
| B. 连 Chrome 一起带（Docker / 无 Chrome 机器） | 0 | +199.5 MiB | ≈**292 MB** | 与"绿色版"定位冲突 |
| C. 带 npm 包（含 7 平台二进制） | 0（但是 Node 生态） | +114.2 MiB | ≈190 MB | 荒谬：为一个平台带 7 个 |
| D. 云 provider | 0 | 0 | 不变 | 要 key、按小时计费、内容过第三方 |

**"依赖白名单"不被破坏**（没有 Python 包），但"74.8MB 发布包"被破坏 —— 这是两种不同的红线，前者能守、后者守不住。

### 4.3 能不能做成可选插件：**能，而且这是唯一干净的路**

理由：它是**独立可执行文件**，不需要 import，不需要 Python 绑定。

形态建议（只描述，不实施）：

1. **不进 wheel、不进发布包**：`fetch/render.py` 只做「探测 + 调用」。探测顺序 `AGENT_BROWSER_BIN` 环境变量 → `PATH` 上的 `agent-browser` → `%LOCALAPPDATA%\ScholarScout\bin\agent-browser.exe`；探不到就 `raise FetchError("network", "渲染引擎未安装")` 或直接走老路，**默认行为与今天完全一致**。
2. **默认关**：`config.yaml` 加 `fetch.render.enabled: false`；UI 里是"高级 → 需要 JS 的页面（实验）"，点开才提示要装 18MB 二进制。
3. **调用面只开两个**：`snapshot -i -c -d 6 --json` 和 `get text "article"`。**不暴露** `eval` / `click` / `fill` / `auth` / `--profile`。
4. **插件侧也关掉军火库**：显式禁用 `stealth` / `captcha` / `obscura` / `--proxy`；`--user-agent` 用我们自己的固定 UA（与 `http.py` 一致），不用随机 UA。
5. **robots 由我们判**：调用前先用现有 `HttpClient.robots_allowed(url)`，不通过就不交给渲染引擎（agent-browser 自己不读 robots [S5][S7]）。
6. **超时/配额由我们管**：复用 `deadline_s` 思路包一层 `subprocess` 超时；daemon 用 `--idle-timeout 2m` 收敛，别让它常驻一小时。

### 4.4 与 `fetch/http.py` 的接口怎么接：**只做 fallback，不做新 provider**

- **不要**新增 `providers/*`：provider 是"检索源"，不是"抓取引擎"，塞进去会破坏 `providers/` 的语义。
- **也不要**替换 `HttpClient`：它承载限速/缓存/robots/体积上限，`read` 那条路（不启浏览器）等于把这些**全部绕过**，收益为零。
- 建议位置：`tools/source_fetch.py` 在 `extract_html()` 之后判「正文过短/空壳」（例如 `< 200 字` 或 `main/article` 为空），且 `config.fetch.render.enabled`，才转 `fetch/render.py`。
- 返回值**必须复用 `FetchResult`**（`kind="html"` + `text=渲染正文` + `url=最终 URL`），错误映射到既有 `FetchError.KINDS`（`timeout` / `network` / `forbidden` / `too_large`），这样 `rpc/errors.py` 的契约码不用动。
- 抓到的 `snapshot` 文本不是 HTML，`extract_html()` 不能直接吃 → 需要一个 `kind="text"` 分支或直接返回纯文本（**这一点要实测再定**，见 §6）。

### 4.5 风险清单（按"会不会让我们被告/被封"排序）

| 风险 | 具体机制 | 处置 |
|---|---|---|
| **凭据/登录态外泄** | `--profile <name>` 会**复制**用户 Chrome 配置到临时目录（只读快照），里面是全部 cookie；`auth save` 存密码（AES-256-GCM，key 在 `~/.agent-browser/.encryption-key`）[S2][S7] | **不开放** `--profile` / `auth` / `--cdp`；只允许匿名会话 |
| **越权访问** | 渲染引擎能执行任意页面 JS（`eval`），页面可发任意请求（含内网） | 只开 `snapshot`/`get text`；关闭 `eval`；`--allowed-domains` 我们自己在调用前做 URL 校验（因为开了它就**不能**用 profile/cdp，正好与"不开放登录态"一致）[S2] |
| **被 ban / IP 污染** | 无 robots、无默认限速；`stealth`/`captcha` 一旦启用就是明确的规避行为 | 保持 `min_interval_s`；**不启用 stealth/captcha**；失败就报"该站不支持"，不重试绕 |
| **版权 / ToS** | 渲染后可拿到原本 403/付费墙后的正文（若页面只是 JS 门禁而非真鉴权） | 与现有口径一致：只抓公开可访问内容；付费墙/登录墙**直接放弃**，不做"渲染绕过" |
| **本地攻击面** | daemon 常驻 + `dashboard`（默认 4848）+ `stream` WebSocket [S2] | 不启 dashboard/stream；`--idle-timeout` 收敛；`--namespace` 隔离 |
| **发布包失控** | 一旦有人把 Chrome 打进包，74.8MB 变 292MB | sidecar 走"首次使用再下载"，构建脚本不打包 |

---

## 5. 两个集成方案与明确结论

### 方案甲（可选插件 / sidecar）——**推荐作为唯一备选形态**

- 默认不装、默认关、不进发布包；用户自己在设置里点"安装渲染引擎"（下 17.2 MiB 二进制；有 Chrome 就不再下 199.5 MiB）。
- 只暴露 `snapshot` / `get text`；禁 `eval`/`profile`/`auth`/`stealth`/`captcha`/`proxy`。
- 触发条件三重：`enabled` + 正文过短 + **URL 在用户点名的域名白名单内**。
- robots 前置判定沿用现有 `robots_allowed()`。
- 体积影响：**0（默认）→ +24%（装了的用户）**；pip 依赖 **0**。

### 方案乙（默认集成）——**否决**

理由：①默认装就得 +24%，带 Chrome 就 +290%，与 74.8MB 绿色包直接冲突；②默认开等于默认给所有用户装一个"能执行任意页面 JS 的浏览器"，而它不懂 robots、不读 robots [S5][S7]，我们的合规口径会被它抹平；③收益只覆盖"少部分 SPA 页面"，却把最难的三个问题（凭据、被 ban、版权）一次性引入。

### 结论（明确）

1. **v1 不接 `agent-browser`。** 不写代码、不加配置项、不进发布包。
2. **若接，只能是方案甲**：默认关的外部 sidecar，只调 `snapshot`/`get text`，只对用户显式放行的域名开，robots 由我们判。**不能**做成默认 fallback，也**不能**接进 `HttpClient`。
3. **它的 `read` 子命令不值得接**：`Accept: text/markdown` + `.md` 探测 + `llms.txt` 查找 + HTML 可读性抽取，这四件事我们**自己用 `httpx` 写 30~50 行就能做**（零依赖、仍走限速/缓存/robots），而且不引入外部二进制。**这是本轮最便宜的收益，建议优先于接 agent-browser。**
4. **替代方案（按性价比排序）**：
   - ① **自己实现 `Accept: text/markdown` 协商 + `llms.txt` 查找**（零依赖，直接提升文档站可读性）；
   - ② **`r.jina.ai` 这类阅读器代理**（一个 URL 前缀，httpx 就能用；免 key 有速率限制，付费要 key；**它对 SPA 也是渲染后才返回，效果等同"别人的 Chrome"**，但内容过第三方）；
   - ③ **用户手动粘贴正文 / 上传 PDF**（现有 `fetch/office.py`、`pymupdf` 已能处理，零新增成本，零合规风险）；
   - ④ 真有硬需求再上方案甲。

---

## 6. 未核实项与存疑（不猜）

1. **`snapshot` 拿到的正文质量**：官方只给"无障碍树 + refs、`-i/-c/-d/-s` 过滤"的形态说明 [S5][S6]，**没有**给"文章正文抽取"的对比数据。对我们这种"要正文"的用途，`snapshot -i -c`（只留可交互元素）**可能反而丢掉段落文本**，需实测 `snapshot` 全量 vs `get text "article"` 两种取法的字数/信噪比。**未实测。**
2. **Edge 是否能被自动探测**：README/安装页只写探测 Chrome / Brave / Playwright / Puppeteer、以及 `--executable-path` 可指任意 Chromium [S2][S11]，**没写 Edge**。Windows 机器普遍只有 Edge，这决定"零下载"能否成立。**未核实。**
3. **Lightpanda 的 JS 兼容度**：官方只说"Zig 写的机器向引擎、内存 1/10、速度 10x" [S10]，**没说**支持到什么程度的现代 JS/SPA 框架；且**无 Windows 资产** [S20]。**未核实。**
4. **云 provider 的免费额度**：只有 Browser Use 的 "$1 试用 + $0.02/小时"有官方 README 原文 [S14]；Browserless / Browserbase / Kernel / AgentCore 的免费额度**未核实**（本次未逐个读定价页）。
5. **`agent-browser` 是否在任何地方声明尊重 robots.txt / ToS**：官方 `commands` 与 `security` 文档全文无相关表述 [S5][S7]，**只能判定"未内置"**，不能判定"官方反对"。它的 ToS/合规立场文档**未核实**。
6. **npm 包 114.2 MiB 的构成**：registry 只给 `unpackedSize` 总数 [S3]，未逐文件核对（推断为 7 个平台二进制 ≈ 7×16~18 MiB，与 release 资产吻合 [S4]，但**未解包验证**）。
7. **`vercel-labs/remote-agent-browser` 无 LICENSE 文件** [S18]：默认"保留所有权利"，**不可**直接用于分发；只是备查，不影响结论。

---

## 7. 本轮边界

- 只新增本文；**未改** `src/`、`tests/`、`pyproject.toml`、`config.yaml`、`.env`，**未引任何依赖**，**未安装 agent-browser**（所有体积/行为均为官方元数据 + 官方文档原文，未做本地实测）；
- 所有 star / 推送时间 / license 均为 2026-10-25 GitHub API 单次读数，会随时间变化；
- 下一轮若要动手，建议顺序：**①自研 markdown 协商（30~50 行，零依赖）→ ②实测 3 个目标站（Notion 公开页 / 某文档站 / arXiv abs 页）的 `snapshot` 取正文质量 → ③再决定方案甲是否值得做**。

---

## 参考（URL 全部为本轮实际访问）

- [S1] `vercel-labs/agent-browser` 仓库 — https://github.com/vercel-labs/agent-browser ｜ API 读数 https://api.github.com/repos/vercel-labs/agent-browser
- [S2] 仓库 README（原文，2057 行）— https://github.com/vercel-labs/agent-browser/blob/main/README.md
- [S3] npm registry 元数据（`0.39.2`，`unpackedSize` 119,712,496）— https://registry.npmjs.org/agent-browser/latest
- [S4] Release `v0.39.2`（资产体积）— https://github.com/vercel-labs/agent-browser/releases/tag/v0.39.2
- [S5] 官方命令文档（含 `read` 原文）— https://agent-browser.dev/commands ｜ Markdown 源 https://agent-browser.dev/commands.md
- [S6] 官方 snapshots 文档 — https://agent-browser.dev/snapshots
- [S7] 官方安全文档 — https://agent-browser.dev/security ｜ https://agent-browser.dev/security.md
- [S8] Browserless provider — https://agent-browser.dev/providers/browserless
- [S9] 其它云 provider — https://agent-browser.dev/providers/browserbase ｜ https://agent-browser.dev/providers/browser-use ｜ https://agent-browser.dev/providers/kernel ｜ https://agent-browser.dev/providers/agentcore
- [S10] Lightpanda 引擎 — https://agent-browser.dev/engines/lightpanda
- [S11] 安装与 Windows 说明 — https://agent-browser.dev/installation
- [S12] CDP 模式 — https://agent-browser.dev/cdp-mode
- [S13] Remote Agent Browser（Vercel Sandbox）— https://agent-browser.dev/providers/remote-agent-browser
- [S14] `browser-use/browser-use` 仓库 + README（$1 试用、$0.02/小时）— https://github.com/browser-use/browser-use
- [S15] PyPI `browser-use` 元数据（`requires_dist` 37 基础 / 61 总）— https://pypi.org/pypi/browser-use/json
- [S16] `browserbase/stagehand` — https://github.com/browserbase/stagehand
- [S17] `magnitudedev/browser-agent` — https://github.com/magnitudedev/browser-agent
- [S18] `vercel-labs/remote-agent-browser`（无 LICENSE 文件）— https://github.com/vercel-labs/remote-agent-browser
- [S19] Chrome for Testing 版本与下载清单 — https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json
- [S20] Lightpanda nightly release 资产（无 Windows）— https://github.com/lightpanda-io/browser/releases/tag/nightly
- [S21] `oxylabs/agent-browser`（同名不同物，商业代理）— https://github.com/oxylabs/agent-browser
