# channels/ —— 渠道层（入口）：把外部消息汇成一次会话

> 本文件按 **Chromium《Documentation Best Practices》** 对 `README.md` 的规定组织（该文要求
> 一个好的 README 回答四件事），只写**不随代码变的那部分**：这一层是干什么的、先看哪个文件、
> 哪些是 API、去哪看更多。
>
> **文件清单、行数、导出符号、依赖方向刻意不写在这里** —— 它们会随代码腐烂，
> 一律由 `scripts/make_code_map.py` 生成到 `docs/代码地图/00-符号清单.md`。
> 阈值、超时、配对码规格等具体数字也不在这里复述，权威副本见 `docs/01` §3.8 与代码本体。

## 1. 这个目录放什么

**渠道层（入口层，与 `desktop/` 同级）**：都是"外面来一条消息 → 汇进去跑一轮 → 把回复送回去"的前门。
`desktop/` 是桌面窗口这个前门，`channels/` 是**消息平台**这个前门。

**它只经 `rpc/` 说话**（进程内 `dispatch`）：不读库、不出网、不 import `runtime`/`tools`/`state`/`providers`
—— 这条由 `tests/test_layering.py::RULES["channels"]` 钉死（违反当场红）。

分层位置（单向，见 `docs/03-目录结构与规则文件.md` §2）：
**`channels/`** `desktop/` → `rpc/` → `runtime/` → `tools/` → `{providers, fetch, grading, library, state}`。

当前只有一个渠道：**飞书**（把 Hermes 的飞书回调做法拍扁进本项目，方案见 `docs/17`）。

## 2. 先看哪个文件

1. **`feishu.py`** —— **传输无关的入站核**。**先读 `FeishuMessageHandler`**：`handle_event(payload)` 做
   事件解析 / 运行时拦截（群 `@提及`、私聊 `pairing|all|allowlist`）/ `session.ensure` + `chat.send` 跑一轮 / 回包
   （回复交给**注入的发送器**）。它**不碰任何 socket**，所以桩事件（甚至一个 dict）就能把整条链路测到底。
2. **`feishu_files.py`** —— **「要文件」意图识别**（纯规则、零 IO；docs/17 §3.12）：一条消息 →
   `FileRequest`（`path` / `artifact` / `library`）或 `None`。查库、导出、上传、发送都在主进程
   （`channel.send_file`）；`feishu.py` 仍重导出 `FileRequest` / `parse_file_request`。
3. **`feishu_ws.py`** —— **入站传输层（唯一一条）**：官方 SDK `lark-oapi` 的 **websocket 长连接**
   （本机主动外连，**不需要公网地址 / 回调 URL / 隧道**，也不绑任何端口）。它只做两件事：收事件 → 归一成报文
   （`event_to_payload`）→ 转交给上面那个核。
4. **`feishu_setup.py`** —— 飞书接入向导（`--setup-feishu` 落点）：编号问答 → 打印/打开创建入口 →
   填 App ID/Secret（只写 `.env`、不回显）→ 生成配对码 → 校验 → 写白名单（**连接方式不做二选一：只有 websocket**）。
5. `__init__.py` 只是包标记，无逻辑。

## 3. 哪些是 API（外部可依赖的契约）

- **入站核 `feishu.py`**：
  - `FeishuMessageHandler`（`handle_event(payload)` / `handle_message(message)` / `decide(message)` /
    `run_turn(message)`；构造时注入 `credentials` / `sender` / `dispatcher`）；
  - 纯函数：`parse_message_event(...)` / `bot_mentioned(...)` / `authorize_message(...)` /
    `_text_from_content(...)` / `_parse_mentions(...)`；数据结构 `FeishuMessage`、`FeishuCredentials`、
    `InterceptDecision`；常量 `RECEIVE_EVENT`、`PAIRING_PROMPT`；
  - 配置读取（都读 `.env`）：`credentials_from_env()`、`connection_mode_from_env()`
    （**只认 websocket**，配了别的值含历史 `webhook` 会告警并回落）、`group_policy_from_env()`、
    `dm_policy_from_env()`、`bot_open_id_from_env()`、`bot_name_from_env()`；
  - 向导辅助：`open_entry_url(domain)` / `launcher_url(user_code, *, ...)`；
  - 默认发送器 `RecordingSender`（无凭据时**不假装发成功**，只记录 + 告警）。
- **「要文件」识别 `feishu_files.py`（纯函数，零 IO）**：`parse_file_request(text)` → `FileRequest | None`
  （`kind ∈ {path, artifact, library}`）；`FileRequest(kind, query, raw)`。`feishu.py` 重导出这两个名字。
- **传输层 `feishu_ws.py`**（websocket 长连接）：
  `build_client(ctx, *, sender=, dispatcher=, sdk=)` → `FeishuWsClient`
  （`build_client()` / `start(*, block=True, say=)` / `stop()` / `handle_event(event)` / `running` / `client`）；
  纯函数 `event_to_payload(event)` / `plainify(value)`；
  `sdk_available()` / `probe_sdk()`（**只查规格、不 import** —— import 本机首次约 116 s）；
  `load_sdk()`（全项目**唯一** import `lark_oapi` 的地方）与 `WsSdk`（桩测的替身注入点）。
- **接入向导 `feishu_setup.py`**：`run_setup(ctx, *, ask=, ask_secret=, say=, open_url=, dispatch_fn=, ...)`
  —— 所有 IO 可注入，所以能在**无凭据、无网络**下桩跑整条流程。
- **运行时拦截经的 RPC（渠道层只经这些说话）**：`feishu.access`（取白名单/临时放行/DM 策略）、
  `session.ensure`（`chat_id` → `session_id`，幂等）、`chat.send`（跑一轮）、
  `feishu.pairing.claim`（把未授权私聊的整条消息先当配对码登记）。

## 4. 谁维护、去哪看更多

- 维护：项目作者本人（毕设项目，非团队协作）。
- 设计与口径（**权威副本，改口径先改它们**）：
  - `docs/17-飞书渠道接入方案.md` §3（落点 / 长连接传输 / 事件映射 / 一键开机 / 人工后台步骤）
  - `docs/18-飞书接入向导迁移方案.md`（向导与联网自动建应用）
  - `docs/01-接口契约.md` §3.8（`feishu.*` 方法表与三条不变量）、§4（流式事件、`stop_reason`）
  - `docs/02-数据模型.md` §8.5（配对码与白名单表结构）、`docs/03-目录结构与规则文件.md` §2（分层）
- 相邻落点（不在本目录，改这条链要一起看）：`rpc/feishu.py`（`feishu.*` 实现）、
  `state/pairing.py`（唯一碰 SQL 的配对/白名单）、`fetch/feishu_send.py`（出站发送器）、
  `fetch/feishu_onboard.py`（联网探测/建应用）、`scripts/feishu_ws_up.py` + 根目录 `飞书一键开机.bat`（一键开机）。
- 测试：`tests/channels/`（`test_feishu_stub.py`、`test_feishu_ws_stub.py`、`test_feishu_setup.py`）、`tests/rpc/test_feishu_methods.py`、
  `tests/state/test_feishu_pairing.py`、`tests/scripts/test_feishu_ws_up.py`、`tests/fetch/test_feishu_onboard.py`。
- 符号与行数：`docs/代码地图/00-符号清单.md`（生成物，勿手改）。

## 改这个目录之前先知道

- **只经 `rpc/`，不读库/不出网**：白名单状态一律走 `feishu.access`；渠道层里出现 `sqlite3` / `httpx`
  或 `import runtime/tools/state/providers` 就是越界（`tests/test_layering.py::RULES["channels"]` 当场红）。
  取访问状态失败时**保守处理**（私聊按未授权、群聊丢弃），RPC 失败绝不能变成"放行"。
- **连接模式：本项目渠道层只实现 websocket 长连接**。默认与唯一支持值都是 `websocket`
  （`DEFAULT_CONNECTION_MODE` / `SUPPORTED_CONNECTION_MODES`；`FEISHU_CONNECTION_MODE` 配了别的值
  —— 含历史遗留的 `webhook` —— 会**告警并回落到 websocket**）。长连接由本机主动外连飞书，
  **不需要公网地址 / 回调 URL / 隧道**，也没有本地监听端口；取舍（依赖最小化 vs 接入便捷）见 `docs/17` §1.3 —— 与 Hermes 侧默认一致。
- **长连接不校签、密钥只从 `.env` 读**：报文走 SDK 的**已认证长连接**
  （`lark_oapi` 的 `EventDispatcherHandler._do_without_validation`），所以 verification token / 签名（HMAC）/
  `url_verification` 挑战这一套**没有意义、已随 webhook 一并删除**（`FEISHU_VERIFICATION_TOKEN` /
  `FEISHU_ENCRYPT_KEY` 两个键也没了）。凭据（`FEISHU_APP_ID` / `FEISHU_APP_SECRET`）仍**只从 `.env` 读、只写不回读**。
  访问控制靠**运行时拦截**（下一条），不靠传输层校验。
- **配对方向与本项目的取舍**：向导先生成"不带身份"的配对码 → 用户在飞书私聊把码发给机器人 →
  `feishu.pairing.claim` 回填 openid → `verify` 批准写白名单并**收回临时放行**。**明文码只在生成那一刻可见**，
  库里只存哈希（`state/pairing.py`）。**码不对不报错**（回 `claimed:false`）—— 机器人收到的普通聊天不该变成 RPC 异常。
- **运行时拦截在会话之前**：群聊按 `@提及` 策略（`mention` 仅被 @ 时响应 / `disabled` 关闭）、
  私聊按 DM 策略（默认 `pairing`：未授权**不跑任何会话**，只回配对提示或先试登记配对码）。
  `drop` 记日志丢弃；`run` 才真跑。
- **没有本地监听面**：长连接是**出站**连接（不绑端口、不暴露任何入站面），所以没有"只绑回环 / 只收 `application/json` /
  限长限速 / 事件失败也回 200"这一层 —— 那些都是旧 webhook 的 stdlib `http.server` 壳，已随路线删除。
  新口径是：事件处理异常在 `feishu_ws.on_message_event` 里**只记日志、绝不上抛**（上抛会顺着 SDK 收包线程让整条链路静默失聪）。
- **入站没有开关、出站默认关**：入站由 `scripts/feishu_ws_up.py` 显式拉起（停进程即停，**没有配置键**）；
  出站由 `config.runtime.feishu_reply_enabled` 控制，**默认关**；关着时渠道层**一次外网都不发**，只告警、如实回 `ok:false`。
