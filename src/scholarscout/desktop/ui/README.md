# ui/ —— 无构建、离线的桌面界面（原生 HTML / JS / CSS）

> 本文件按 **Chromium《Documentation Best Practices》** 对 `README.md` 的规定组织（该文要求
> 一个好的 README 回答四件事），只写**不随代码变的那部分**：这一层是干什么的、先看哪个文件、
> 哪些是 API、去哪看更多。
>
> **文件清单、行数、导出符号刻意不写在这里** —— Python 侧符号由 `scripts/make_code_map.py`
> 生成到 `docs/代码地图/00-符号清单.md`；`ui/` 的 js/html/css **不在这份清单里**，
> 前端自检见 `desktop/assets.py`（`--check-ui`）与 `tests/desktop/`。

## 1. 这个目录放什么

**桌面界面的全部本体**：`index.html` 一个入口 + 若干原生 `.js` + `style.css`。
**无构建、无 npm、无框架、无打包器** —— 每个 `.js` 都是全局声明（`var`/`function`），靠 `index.html`
里的 `<script>` 顺序加载、跨文件直接按名字互相调用。

它**只经桥调 RPC**：`scout.call(method, params)` → `window.pywebview.api.rpc(...)` → `desktop/bridge.py`
→ 后端子进程（`docs/01-接口契约.md` §2）。界面**不读库、不发外网**。

**离线是硬要求**：界面文件里**不许出现 `https?://` 外链**（答辩现场可能没网），由 `--check-ui` 当门槛拦。

## 2. 先看哪个文件

1. **`index.html`** —— 骨架 + **加载顺序**：先 `markdown.js`，再 `api.js` / `sources.js` / `learning.js` /
   `board.js` / `profile.js`，最后 `app.js`。**先读它**，一切接线从这里看。
2. **`api.js`** —— 底座：桥调用、DOM/格式小工具、主题、布局、`window.scoutEvent`（后端事件入口）。
3. **`app.js`** —— 应用主体：全局状态、会话与消息、启动（`boot`）与接线；定义全局 `scout.call`。
4. **`sources.js`** —— 资料库：列表、摘要区、主题地图。
5. **`learning.js`** —— 学习路线（含时间线/日历）、抽背卡、笔记。
6. **`board.js`** —— 研究看板（证据/缺口/时间线）。
7. **`profile.js`** —— 个人中心（画像/跨会话记忆）与设置面板。
8. **`markdown.js`** —— 自写的极小 Markdown 渲染器 `ScoutMarkdown`（安全靠构造：模型文本只经 `textContent`）。
9. **`style.css`** —— 全部样式。

## 3. 哪些是 API（外部可依赖的契约）

- **调后端 `scout.call(method, params)`**（`app.js` 定义）：返回 `result`，失败抛带 `code` 的 `Error`
  （桥统一回 `{ok,result}` / `{ok,error:{code,message}}`）。`method` 必须是**真实存在的 RPC 方法名**
  （取自 `rpc.methods.method_names()`）。
- **后端事件入口 `window.scoutEvent(event)`**（`api.js`）：壳把 `{method, params}` 推给它；
  它处理 `chat.tool_call` / `chat.tool_result` / `chat.delta` / `chat.error` 与 `session.updated`。
- **直接打桥 `bridgeCall(name, ...)`**（`api.js`）：绕开 RPC 直调桥方法（窗控用；后端忙时也要点得动关闭）。
- **渲染器 `ScoutMarkdown`**（`markdown.js`）：`renderInto(el, text)` / `render(text)`；
  纯函数 `parseBlocks` / `parseInline` 可被 node 直接 `require` 做单测。
- **关键页面元素 id**（`index.html`，JS 靠 `el(id)` 取）：
  `status`、`session-list`、`chat-title`、`messages`、`input`、`composer`、`btn-send`、`btn-stop`、
  `btn-new`、`btn-settings`、`btn-profile`、`session-usage`；
  右栏 tab 页 `pane-sources` / `pane-routes` / `pane-notes` / `pane-cards` / `pane-board`；
  浮层 `settings` / `profile` / `practice` / `route-zoom` / `textmodal`；
  无边框把手 `resize-grip`、窗控件 `win-min` / `win-max` / `win-close`。
- **本机界面偏好键（localStorage）**：`scout:theme`、`scout:layout`、`scout:uiScale`、
  `scout:sidebarCollapsed`、`scholarscout.calendarRoutes`。
- **桥侧被调用的方法名**（`desktop/bridge.py`）：`rpc` / `info` / `reveal` / `open_url` / `shutdown`
  与无边框窗控 `win_move` / `win_resize` 等。

## 4. 谁维护、去哪看更多

- 维护：项目作者本人（毕设项目，非团队协作）。
- 设计与口径（**权威副本，改口径先改它们**）：
  - `docs/01-接口契约.md` §2（传输层：壳暴露的方法、事件）、§3（RPC 方法表）、§4（流式事件）
  - `src/scholarscout/desktop/assets.py`（`verify_ui`：文件齐全 / 方法名存在 / 无外链；`--check-ui` 的实现在这里）
  - `AGENTS.md` §2（命名）、`docs/03-目录结构与规则文件.md` §4（门槛）
- 测试与自检：`tests/desktop/`（`test_markdown_js.py` 用 node 跑渲染器用例，`test_assets.py` 校验资源，
  `_ui.py` 是界面断言辅助，`markdown_probe.js` / `feishu_card_probe.js` 是探针脚本）。
- 符号与行数：`docs/代码地图/00-符号清单.md`（生成物，勿手改；只覆盖 Python）。

## 改这个目录之前先知道

- **无构建 / 离线是门槛**：`python scripts/scout.py --check-ui`（→ `desktop/assets.verify_ui`）会扫全部
  前端 js/css/html，出现 `https?://` 直接判不合。**注释里也别写真实 URL**（正则分不清注释与代码）。
- **加载顺序有强制约束**：`markdown.js` 必须**在 `app.js` 之前**引入（`app.js` 用 `ScoutMarkdown`）；
  每个 js 都要被 `index.html` 引到（否则是死文件）；除 `markdown.js` 外都要**排在它之后**。
  `ui/` 下**只允许 `index.html` 一个入口页**。这三条都在 `assets.py` 里有对应断言。
- **只调真实存在的 RPC 方法**：`scout.call("x")` 的 `x` 必须在 `rpc.methods.method_names()` 里 ——
  `assets.py` 会把两者做差集，写错方法名的按钮当场判不合。新增界面动作 = 先在 `rpc/` 加方法。
- **链接点击必须 `preventDefault` 再走桥**：pywebview 的窗口本身就是浏览器，正文里直接点 `<a>` 会把
  整个界面顶掉（窗口变成那个网页、回不来）。渲染器统一拦下改调 `bridge.open_url`，**只放行 http/https**。
- **文件之间靠全局同名函数/变量互联**（没有 `import`、没有模块作用域）：拆文件时保持全局声明，
  跨文件调用才和拆分前一致（`api.js` / `sources.js` / `board.js` / `profile.js` 顶部注释都记了这条）。
- **窗控不走 RPC**：最小化/最大化/关闭/拖动/缩放直接打桥（`bridgeCall`）—— 后端正忙时也点得动关闭。
- **设置面板字段名必须与后端白名单一一对应**（`settings_io.FIELDS`）：后端对未知键直接报 `-32602`，
  界面写错字段名会在保存时被拒（`app.js` 设置段有对应注释）。
