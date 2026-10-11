# rpc/ —— 界面与后端之间唯一的 JSON-RPC 2.0 分发层

> 本文件按 **Chromium《Documentation Best Practices》** 对 `README.md` 的规定组织（该文要求
> 一个好的 README 回答四件事），只写**不随代码变的那部分**：这一层是干什么的、先看哪个文件、
> 哪些是 API、去哪看更多。
>
> **文件清单、行数、导出符号、依赖方向刻意不写在这里** —— 它们会随代码腐烂，
> 一律由 `scripts/make_code_map.py` 生成到 `docs/代码地图/00-符号清单.md`。

## 1. 这个目录放什么

桌面界面与渠道层**唯一**能用来调后端的入口：方法表 + 参数校验 + 流式事件（通知）+ 错误码映射。
界面不直读 SQLite、不直发 HTTP，一切经 RPC —— 单入口才可能把每次调用记进 `tool_traces`，指标才是真数。

**两种传输，同一张方法表**（分发逻辑只有一份）：stdio（一行一个 JSON 对象）与桌面壳子进程；
方法实现按域拆成同目录的多个模块，**import 即注册**（见 `methods.py`）。分层位置：
`desktop/` `channels/` → **`rpc/`** → `runtime/` → `tools/`（`docs/03-目录结构与规则文件.md` §2）。

## 2. 先看哪个文件

1. **`registry.py`** —— 基础设施：`AppContext`、`@method` 注册表、`dispatch`、参数校验工具。**先读它**，
   它只有机制、没有业务，是本层的骨架。
2. **`server.py`** —— stdio 传输：读一行 → 分发 → 写回；并发服务与通知上报。
3. **`methods.py`** —— 注册入口：import 各域模块触发注册；对外仍从这里导入 `AppContext`/`dispatch`/`method_names`。
4. **`errors.py`** —— 错误码 `Code` 与异常→码映射 `from_exception`。
5. **各域方法模块**：`session.py`、`research.py`、`archive.py`、`learning.py`、`settings.py`、`feishu.py`。
6. `__init__.py` 只是包标记。

## 3. 哪些是 API（外部可依赖的契约）

契约权威副本：`docs/01-接口契约.md`（§2 传输层、§3 方法表、§4 流式事件、§7 契约测试）；
改方法先改 `docs/01 §3` 再改实现（铁律 #1）。

- **基础设施 `registry.py`**：`AppContext`（`.require_llm()` / `.turn_context()` / `.close()`）、
  `method()`、`method_names()`、`dispatch()`、`llm_ok()`、`clamp_limit()`
  （参数校验工具：`_params` / `_require` / `_int_param`）。
- **传输 `server.py`**：`serve()`、`handle_line()`、`handle_message()`、`make_writer()`、`make_emitter()`、
  `ok_response()`、`error_response()`、`NOTIFICATION_METHODS`、`MAX_CONCURRENT_REQUESTS`。
- **`methods.py`**：re-export `AppContext` / `dispatch` / `method` / `method_names`（调用方与测试从这导入）。
- **错误 `errors.py`**：`Code`（`-32700` … `-32005`）、`RpcError`（`.to_error()`）、`from_exception()`。
- **方法名（按域，取自各模块 `@method`）**：`system.*`｜`config.*`｜`session.*`｜`chat.*`｜`archive.*`｜
  `topic.*`｜`research.*`｜`search.providers`｜`trace.list`｜`route.*`｜`note.*`｜`memory.*`｜
  `learner.profile`｜`feishu.*`｜`settings.*`。
  **完整方法清单、参数与返回以 `docs/01 §3` 为准**（本文件不复述，避免腐烂）。

## 4. 谁维护、去哪看更多

- 维护：项目作者本人（毕设项目，非团队协作）。
- 测试：`tests/rpc/`（`test_protocol.py`、`test_serve_concurrency.py`、`test_cancel_and_session_usage.py`、
  `test_session_methods.py`、`test_learning_methods.py`、`test_archive_actions.py`、`test_feishu_methods.py`）；
  契约测试在 `tests/contract/`（见 `docs/01 §7`）。
- 桌面壳侧的对端：`desktop/rpc_client.py`（子进程 + 一行一 JSON）、`desktop/bridge.py`（只转发）。
- 文档：`docs/01-接口契约.md` §2/§3/§4/§7；`docs/03-目录结构与规则文件.md` §2/§7。
- 符号与行数：`docs/代码地图/00-符号清单.md`（生成物，勿手改）。

## 改这个目录之前先知道

- **为什么必须经 rpc**：界面与渠道层都不许直读库、直发外网（`docs/01 §1` 硬规则 1）——
  渠道层是入口，与 `desktop/` 同级，直连下层会被 `tests/test_layering.py` 拦下。前端要展示什么，
  就在 `docs/01 §3` 加一个方法 + `ui/` 一页（铁律 #1）。
- **长任务必须能在真实传输层上取消**：一轮 `chat.send` 会阻塞 3–12 分钟，期间界面还要能点资料预览
  与「停止」。**单线程 `serve()` 会把它们排到队尾** —— 用户看到"点了没反应"，取消永远不生效。
  所以 `serve()` 用工作线程**并发**（读一行 → 线程分发 → 带锁写回），响应按 `id` 配对、允许乱序。
- **取消按 `session_id`**：整轮结束前前端拿不到 `turn_id`，只按 `turn_id` 取消则界面根本调不到；
  执行时机是**步间检查**（当前这一步结束才生效），界面必须如实提示。
- **分发只有一份**：stdio 与桌面壳走同一张方法表（`methods.py`）；改接口必须先单独提交 `docs/01`（铁律 #1）。
- **注册重复即报错**：`method()` 遇重复方法名抛 `ValueError`；`params` 非对象或类型不符报 `-32602`。
- **错误一律转契约码**：`from_exception()` 把 `LlmError`（按 `.kind` 分类）、`PathOutsideDataError`、
  `sqlite3.IntegrityError`、`KeyError/TypeError/ValueError` 映射成 `Code`；未知方法回 `-32601`，
  未分类回 `-32603`。
