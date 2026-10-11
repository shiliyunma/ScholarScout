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

1. **`feishu.py`** —— 回调核心。**先读 `FeishuWebhook`**：传输无关的 `handle(body, headers)` 做
   限速/限长/两类校验/`challenge`/事件解析/运行时拦截；`FeishuCallbackServer` + `build_server` + `run_listener`
   是它外面的 stdlib `http.server` 壳。
2. **`feishu_setup.py`** —— 飞书接入向导（`--setup-feishu` 落点）：编号问答 → 打印/打开创建入口 →
   填 App ID/Secret（只写 `.env`、不回显）→ 选连接模式 → 试拉起渠道层 → 生成配对码 → 校验 → 写白名单。
3. `__init__.py` 只是包标记，无逻辑。

## 3. 哪些是 API（外部可依赖的契约）

- **回调核心 `feishu.py`**：
  - `FeishuWebhook`（`handle(body_bytes, headers, *, remote_ip)` / `credentials` /
    `_decide` / `_try_claim` / `_run_turn` / `_check_rate`）；
  - `FeishuCallbackServer`（`start()` / `stop()` / `address` / `path`）、`build_server(ctx, *, ...)`、
    `run_listener(ctx, *, sender=, host=, port=, path=, ...)`；
  - 纯函数：`compute_signature(...)` / `signature_valid(...)` / `token_valid(...)` / `is_challenge(...)` /
    `challenge_body(...)` / `parse_message_event(...)`；数据结构 `FeishuMessage`、`FeishuCredentials`、
    `InterceptDecision`；判定 `bot_mentioned(...)` / `authorize_message(...)`；
  - 配置读取（都读 `.env`）：`credentials_from_env()`、`connection_mode_from_env()`、`group_policy_from_env()`、
    `dm_policy_from_env()`、`bot_open_id_from_env()`、`bot_name_from_env()`；
  - 向导辅助：`open_entry_url(domain)` / `launcher_url(user_code, *, ...)` / `probe_listener(...)`；
  - 默认发送器 `RecordingSender`（无凭据时**不假装发成功**，只记录 + 告警）。
- **接入向导 `feishu_setup.py`**：`run_setup(ctx, *, ask=, ask_secret=, say=, open_url=, dispatch_fn=, ...)`
  —— 所有 IO 可注入，所以能在**无凭据、无网络**下桩跑整条流程。
- **运行时拦截经的 RPC（渠道层只经这些说话）**：`feishu.access`（取白名单/临时放行/DM 策略）、
  `session.ensure`（`chat_id` → `session_id`，幂等）、`chat.send`（跑一轮）、
  `feishu.pairing.claim`（把未授权私聊的整条消息先当配对码登记）。

## 4. 谁维护、去哪看更多

- 维护：项目作者本人（毕设项目，非团队协作）。
- 设计与口径（**权威副本，改口径先改它们**）：
  - `docs/17-飞书渠道接入方案.md` §3（落点/校验/事件映射/监听/一键起飞）
  - `docs/18-飞书接入向导迁移方案.md`（向导与联网自动建应用）
  - `docs/01-接口契约.md` §3.8（`feishu.*` 方法表与三条不变量）、§4（流式事件、`stop_reason`）
  - `docs/02-数据模型.md` §8.5（配对码与白名单表结构）、`docs/03-目录结构与规则文件.md` §2（分层）
- 相邻落点（不在本目录，改这条链要一起看）：`rpc/feishu.py`（`feishu.*` 实现）、
  `state/pairing.py`（唯一碰 SQL 的配对/白名单）、`fetch/feishu_send.py`（出站发送器）、
  `fetch/feishu_onboard.py`（联网探测/建应用）、`scripts/feishu_up.py` + 根目录 `飞书一键起飞.bat`（一键链路）。
- 测试：`tests/channels/`（`test_feishu_stub.py`、`test_feishu_setup.py`）、`tests/rpc/test_feishu_methods.py`、
  `tests/state/test_feishu_pairing.py`、`tests/scripts/test_feishu_up.py`、`tests/fetch/test_feishu_onboard.py`。
- 符号与行数：`docs/代码地图/00-符号清单.md`（生成物，勿手改）。

## 改这个目录之前先知道

- **只经 `rpc/`，不读库/不出网**：白名单状态一律走 `feishu.access`；渠道层里出现 `sqlite3` / `httpx`
  或 `import runtime/tools/state/providers` 就是越界（`tests/test_layering.py::RULES["channels"]` 当场红）。
  取访问状态失败时**保守处理**（私聊按未授权、群聊丢弃），RPC 失败绝不能变成"放行"。
- **连接模式：本项目渠道层只实现 webhook**。代码里默认模式就是 `webhook`
  （`DEFAULT_CONNECTION_MODE`，可用环境变量 `FEISHU_CONNECTION_MODE` 覆盖；`SUPPORTED_CONNECTION_MODES`
  两种都认，但 `websocket` 仅在向导里被记录、不会真连）。webhook 需要**公网可达的回调地址**
  （内网穿透/反代），见 `docs/17` §3.8/§3.9 —— 这与 Hermes 侧默认走 websocket 不是一回事。
- **两类校验、密钥只从 `.env` 读**：verification token 与签名（`hmac.compare_digest`）都**有配才校**；
  **没配密钥时不校签并明确告警**（仅够本地桩自测，勿用于公网）。`challenge`（`url_verification`）必须
  **先过 token 再回**，防未授权者拿它证明自己控制了端点。
- **配对方向与本项目的取舍**：向导先生成"不带身份"的配对码 → 用户在飞书私聊把码发给机器人 →
  `feishu.pairing.claim` 回填 openid → `verify` 批准写白名单并**收回临时放行**。**明文码只在生成那一刻可见**，
  库里只存哈希（`state/pairing.py`）。**码不对不报错**（回 `claimed:false`）—— 机器人收到的普通聊天不该变成 RPC 异常。
- **运行时拦截在会话之前**：群聊按 `@提及` 策略（`mention` 仅被 @ 时响应 / `disabled` 关闭）、
  私聊按 DM 策略（默认 `pairing`：未授权**不跑任何会话**，只回配对提示或先试登记配对码）。
  `drop` 记日志丢弃；`run` 才真跑。
- **只绑本机回环、只收 JSON、限长限速**：监听默认只绑 `127.0.0.1`（公网交给穿透/反代）；非
  `application/json` 直接拒；超长/超频各有对应状态码。事件处理失败**也要回 200**（否则飞书会重试风暴）。
- **默认关**：回调监听与出站发送各由 `config.runtime.feishu_listener_*` / `feishu_reply_enabled` 开关控制，
  默认都关；关着时渠道层**一次外网都不发**，只告警、如实回 `ok:false`。
