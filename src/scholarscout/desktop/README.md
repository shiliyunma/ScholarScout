# desktop/ —— 桌面壳：pywebview 窗口 + 一行一 JSON 的后端子进程

> 本文件按 **Chromium《Documentation Best Practices》** 对 `README.md` 的规定组织（该文要求
> 一个好的 README 回答四件事），只写**不随代码变的那部分**：这一层是干什么的、先看哪个文件、
> 哪些是 API、去哪看更多。
>
> **文件清单、行数、导出符号、依赖方向刻意不写在这里** —— 它们会随代码腐烂，
> 一律由 `scripts/make_code_map.py` 生成到 `docs/代码地图/00-符号清单.md`。
> 超时值、最小尺寸等具体数字也不在这里复述（"Duplication is evil"），权威副本见代码本体与 `docs/01`。

## 1. 这个目录放什么

**桌面壳（入口层）**：用 pywebview 开一个原生窗口，把同一个 `python -m scholarscout --rpc`
后端拉成**子进程**，然后在窗口（HTML 界面）与后端之间转发 JSON-RPC。

分层位置（单向，见 `docs/03-目录结构与规则文件.md` §2）：
**`desktop/`** `channels/` → `rpc/` → `runtime/` → `tools/` → `{providers, fetch, grading, library, state}`。

壳里**不含业务逻辑**：起进程 → 建窗口 → 转发。它刻意**不 import 任何业务模块**
（`runtime`/`tools`/`state`/`providers`）；分发只有一份（`rpc/methods.py`），桌面与 CLI 走同一条必经之路
（`docs/01-接口契约.md` §2）。

## 2. 先看哪个文件

1. **`app.py`** —— 壳入口：`launch()` 起窗口 + 起后端子进程，处理窗口几何/图标/无边框窗控。**先读它**。
2. **`rpc_client.py`** —— 子进程与"一行一个 JSON"的同步客户端：按 `id` 配对、通知转发、超时、退出。
3. **`bridge.py`** —— 暴露给 JS 的方法（`rpc`/`info`/`reveal`/`open_url`/`shutdown` + 无边框窗控），只转发。
4. **`icon.py`** —— Windows 窗口/任务栏图标与进程任务栏身份（ctypes）。
5. **`window_state.py`** —— 窗口大小/位置的记住与恢复（可离线测）。
6. **`assets.py`** —— `ui/` 静态资源校验（门槛用）。
7. `__init__.py` 只是包标记，无逻辑。

## 3. 哪些是 API（外部可依赖的契约）

- **壳入口 `app.py`**：`launch(*, db_path=, toolsets=, debug=, open_settings=) -> int`、
  `main(argv=)`、`window_icon()`、`build_parser()`。
- **子进程通道 `rpc_client.py`**：`RpcClient`（`start()` / `call(method, params=, *, timeout=)` /
  `close()` / `alive` / `on_event=` 回调）、`default_command(*, db_path=, toolsets=)`、`default_env()`、
  `RpcCallError`、`ProcessLike`（测试用假进程形状）。
- **桥 `bridge.py`**：`Bridge`（`rpc(method, params_json)` / `info()` / `reveal(rel_path)` /
  `open_url(url)` / `shutdown()` / `attach()` / `on_event()` / `flush_pending()`；
  无边框窗控 `window_mode()` / `win_minimize()` / `win_toggle_maximize()` / `win_close()` /
  `win_move(x, y)` / `win_resize(width, height)`）、`ok_payload()`、`error_payload()`、
  `MIN_WINDOW_SIZE`（最小尺寸**只有这一处定义**）、`JS_EVENT_FUNCTION`（前端事件入口名）。
- **图标 `icon.py`**：`set_app_user_model_id(app_id=)`、`apply_window_icon(title, ico_path, *, ...)`、
  `APP_USER_MODEL_ID`。
- **窗口几何 `window_state.py`**：`load(path, *, screens=)`、`save(*, width, height, x, y, maximized, path=, screens=)`
  （**写前必过 `normalize`**：脏值/最小化不写、几何没变不重写）、`clear(path=)`、`config_path(path=)`、
  `normalize(raw, *, screens=)`、`same(left, right)`、`MIN_SIZE`。
- **资源校验 `assets.py`**：`verify_ui(ui=)`（返回问题清单，空=通过）、`main()`、`ui_dir()`、`called_methods(text)`。

调用方：`__main__.py`（`--desktop` / `--check-ui`）、`scripts/scout.py`、`tests/desktop/`。

## 4. 谁维护、去哪看更多

- 维护：项目作者本人（毕设项目，非团队协作）。
- 口径与设计依据（**权威副本，改口径先改它们**）：
  - `docs/01-接口契约.md` §2（传输层：壳暴露的方法、为何壳不 import 业务、错误码）
  - `docs/03-目录结构与规则文件.md` §2（模块边界）、§7（真机坑清单）、§9（Windows 中文路径）
  - `AGENTS.md` / `docs/项目规则.md` §5（关键路径）
- 测试：`tests/desktop/`（`test_assets.py`、`test_bridge.py`、`test_bridge_window_controls.py`、
  `test_rpc_client.py`、`test_rpc_concurrency.py`、`test_window_icon.py`、`test_window_state.py`、
  `test_markdown_js.py`，界面断言辅助在 `tests/desktop/_ui.py`）。
- 符号与行数：`docs/代码地图/00-符号清单.md`（生成物，勿手改）。

## 改这个目录之前先知道

- **壳里不许出现业务模块的 import**：`desktop/*` 只经 `rpc/server.py` 通信（`tests/test_layering.py`）。
  理由：分发只有一份，测试覆盖的就是用户跑的那条；**后端崩了窗口还在**，界面能把错误显示给人看
  （启动失败时界面页头显示"连接后端失败"，见 `app.js::boot`）。壳里也不能出现 `sqlite3` / `httpx`。
- **中文路径与编码坑挡在子进程里**：`default_env()` 给后端子进程强制 `PYTHONUTF8=1` /
  `PYTHONIOENCODING=utf-8`（本项目路径含中文，见 `docs/03` §9）；读一侧用 `errors="replace"`
  —— 一个坏字节不该让读线程退出、界面误报"后端进程已退出"。
- **一行一 JSON、按 `id` 配对、可并发**：每个请求自增 `id`，收行线程把响应投进 `id -> Queue` 的信箱；
  调用方只等**自己那个信箱**（不互相排队）。没有 `id` 的行是通知 → 转 `on_event`；
  解析不了的行直接忽略（后端被第三方库日志污染也不能把界面搞崩）。
- **打包（frozen）后两条硬规则**：①**不许**把 `src/` 挂进 `PYTHONPATH`（否则子进程 import 到源码副本、输出退回 GBK）；
  ②后端命令要**拉起自己**（`sys.executable --rpc`），不能再拼 `-m scholarscout`（PyInstaller 引导器不认 `-m`）。
  两条的真机症状都是"界面能开、页头红字连接失败"。见 `rpc_client.py` 的 `default_env` / `default_command`。
- **链接点击必须拦下来**：pywebview 的窗口本身就是浏览器，正文里直接点 `<a>` 会把整个界面顶掉；
  前端 `preventDefault` 后调 `bridge.open_url`（只放行 http/https，其余拒）。见 `bridge.open_url` 注释。
- **图标不走 `webview.start(icon=...)`**：该参数在 Windows 上被忽略；`icon.py` 等窗口出现后对 HWND 发
  `WM_SETICON`，并用 `SetCurrentProcessExplicitAppUserModelID` 给进程独立任务栏身份。64 位下
  `LoadImageW` 必须显式 `restype = c_void_p`，否则句柄被截断、`SendMessage` 静默失败。
- **无边框窗控自己实现**：无边框后端不响应标题区拖动、也没有系统边框；拖动/缩放由界面算坐标打
  `win_move` / `win_resize`，最小尺寸在**桥这一侧**统一夹住（`MIN_WINDOW_SIZE` 单处定义）。
- **窗口几何一律存逻辑像素**：`window.width/height` 报物理像素、`create_window(width=…)` 收逻辑像素，
  不换算会越开越大；且**不要用"尺寸≈屏幕"猜最大化**（单位不一致会误判，代价是尺寸干脆不记了，见 `window_state.py`）。
