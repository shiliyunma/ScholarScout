---
name: desktop-ui-render-check
description: Use when a desktop UI changed and it must be seen.
---

# 桌面界面渲染核对（桩桥 + 无头截图 + DOM 核对）

## 何时用

改了桌面应用的界面（新增面板/页签/列表），单元测试全绿，但**没人证明过它长出来是什么样**。
典型信号：你能说出"数据应该会显示"，但没看过像素。

这套做法在 ScholarScout（pywebview + 原生 HTML/CSS/JS）上抓到过两个真缺陷：
只在会话内解析引用来源 → 23 行里 15 行显示"没有可解析的来源"；枚举值直接显示英文原文。
**两个都不是测试能抓到的**（数据类型和取值都对，只是渲染出来不对/不全）。

## 步骤

1. **拷一份 UI 到临时目录**（不要改源文件）：`cp ui/{index.html,app.js,style.css,...} $TMP/probe/`
2. **取真实载荷**：用产品自己的代码路径生成 JSON（例如直接调那个只读 RPC 的构造函数），
   写进 `probe/payload.json`。**别手搓假数据** —— 假数据不会暴露"某个字段其实解析不到"这类问题。
3. **内联载荷 + 插桩脚本**：在拷来的 index.html 里把 JSON 放进
   `<script id="app-data" type="application/json">{...}</script>`（`file://` 下 `fetch` 会被拦，必须内联），
   并在业务脚本**之前**插入 `stub.js`。
4. **桩要模拟真实桥形状**（见下面最大的坑）。
5. **在 `load` 事件里直接指定状态并调渲染函数**（见第二个坑）。
6. **截图**：
   ```
   msedge --headless=new --disable-gpu --hide-scrollbars \
     --window-size=1500,1250 --virtual-time-budget=5000 \
     --screenshot="C:/path/out.png" "file:///C:/path/index.html"
   ```
7. **文字核对优先用 DOM 转储**（比看图确定、便宜）：
   ```
   msedge --headless=new --virtual-time-budget=5000 --dump-dom "file:///.../index.html" > dom.html
   ```
   再用正则把每个 `id="..."` 区块的标签剥掉、压空白，逐节打印文本 —— 一眼看出哪节是空的。
8. **版式核对用视觉**：截图后用 vision 看，**用 region 裁到目标栏/面板**（整页缩略图看不清小字）。
   问自己三件事：文字有没有被裁断？次要信息（来源/年份/强度）有没有挤在一起？留白是否异常？

## 三个必踩的坑（先看这里能省一轮）

- **坑 0：注入的脚本要用业务代码里**真实存在**的函数名。** 猜一个名字（例如以为评分函数叫
  `rateCard`）会在注入脚本里报 `ReferenceError`，而且**静默地中断整条异步链** ——
  表现是"前半段渲染了、后半段没有"，看起来像功能坏了。先 `grep '^\s*(async )?function'` 确认名字。
- **坑 1：不要覆盖业务脚本里的 `scout` 之类的包装对象。** 真正的桥是 `window.pywebview.api`，
  业务里的 `scout.call` 是包装器，内部读 `window.pywebview.api.rpc(method, paramsJson)`。
  只桩一个全局 `scout` 会报 `Cannot read properties of undefined (reading 'api')`。
  正确形状：`window.pywebview = { api: { rpc: async (m, p) => JSON.stringify({ok: true, result: payload}) } }`。
  返回值是**字符串**（业务侧 `JSON.parse(raw)`）。
- **坑 2：启动竞态。** 应用的 `boot()` 是异步的，在 `load` 里直接调渲染函数常拿到空状态
  （`state.sessionId` 还没设 → 提前 return，界面一片空，看起来像"渲染坏了"其实是没数据）。
  要么等 boot 完成，要么在注入脚本里**显式设状态**再调渲染函数。
  另外：顶层 `const state = {...}` 是全局词法绑定，**能按名字用**，但 `window.state` 是 `undefined`。

## 长清单的排版核对技巧

载荷里有几十行时，后面的面板会掉到折叠线以下。核对版式时**把行数裁到 2–3 行**
（在探针里裁，不动产品代码）——版式不依赖行数，但"能不能看到"依赖行数。

**顺手查窄栏换行**：桌面应用通常有可拖动的窄侧栏。加了一个按钮或一段状态文字后，
原来一行的控件会被挤成两行（汉字尤其明显："开始抽 背"）。核对时把截图**裁到那一栏**
看它有没有折行 —— 这是单元测试永远看不见的那类缺陷。

## 另外：从真库取载荷时别忘了迁移

直接 `Database(path)` 打开用户的库去构造载荷，如果没跑 `apply_migrations`，
新加的列会让查询报 `no such column`。应用自己是在启动时迁移的（不必担心线上），
但**你的载荷脚本不是**。先 `apply_migrations(db)` 再读。

## 反面教材

- 不要为了截图去改产品源码（默认激活页签等）——探针里改，产品保持原样。
- 不要把"测试通过"当成"界面能用"。数据形状对 ≠ 渲染对：解析不到、枚举没本地化、
  字段被截断成"…"，这些都只在像素上暴露。
- 核对完要**回头修产品代码**（这次修的是跨会话解析 + 枚举中文化），并补一条单测钉住。
