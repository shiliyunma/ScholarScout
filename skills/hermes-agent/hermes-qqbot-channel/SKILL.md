---
name: hermes-qqbot-channel
description: "Use when 配置或排查 Hermes 的 QQ 机器人通道。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [hermes, qqbot, 群消息, gateway, 排查]
    category: hermes-agent
---

# Hermes QQ 机器人通道

接入与排查 Hermes 的 QQ 官方机器人（q.qq.com 的 App ID / App Secret）通道：群消息权限、消息形态限制、安全边界。
凭证探测、`/users/@me`、加群分享链接与“被动/主动消息”的发送机制见 `references/qqbot-api-probes.md`。

## When to Use

- “能不能收某个群的消息并自动处理”、把机器人拉进群、开/关某平台
- “群里 @ 了机器人但没反应”这类投递失败排查
- 评估把机器人放开给多人使用时的风险

## 能力边界（官方 API 决定，绕不过去）

- 支持：私聊（C2C）、**群 @消息**（事件 `GROUP_AT_MESSAGE_CREATE`）、频道/公会消息、图片/语音/文件、语音转写
- **群消息只在群里 @机器人 时下发**，读不到群内其它消息；要全量读群消息只能用非官方协议（封号风险），Hermes 无内置适配器
- 合并转发的“聊天记录”卡片往往只给**截断的预览文本**且 `attachments=None`，卡片内图片拿不到，这种消息可能根本不进会话 → 让用户改发**单张图片**或**复制成纯文本**
- **卡片正文丢了也能捞回来**：会话上下文里只显示空卡片（`[群聊的聊天记录]` + `attachments=None`、发送者后面什么都没有）时，**别急着让他重发** —— 先 `grep -a "inbound message" $HERMES_HOME/logs/gateway.log | tail -10` 看原始 `content='...'`。他常常在转发卡片的同一秒另发了一条正文消息，那条可能没进你的上下文却已经在日志里（本次就是这样发现真实指令的）。日志里能看到被截断前的完整文本。
- 图片/多媒体 URL 带 `rkey`、有时效；要留存就**立刻下载**（普通 urllib + 浏览器 UA 即可，无需额外鉴权）

## 配置

- 凭证：`.env` 里 `QQ_APP_ID` / `QQ_CLIENT_SECRET`，或 `platforms.qqbot.extra.app_id / client_secret`
- 私聊权限：`platforms.qqbot.extra.dm_policy` + `allow_from`；环境变量 `QQ_ALLOWED_USERS` / `QQ_ALLOW_ALL_USERS`
- 群权限：`platforms.qqbot.group_policy` = `open | allowlist | disabled | pairing`（**默认 pairing**）+ `group_allow_from: [群 openid]`（值写 YAML 列表、JSON 列表字面量或**逗号串** `"id1,id2,id3"` 都行 —— `gateway/platforms/qqbot/utils.py` 的 `coerce_list` 三种都吃）
  - 实测 `hermes config set platforms.qqbot.group_policy open` 会直接写进 config.yaml 的 `platforms.qqbot` 下（不带 `extra` 也行）。两种层级都写过，**`hermes config get` 的回显不能证明适配器读到了它** —— 改完必须在群里真 @ 一次、用日志确认生效。
- 群消息的第二道闸在网关鉴权层：`QQ_GROUP_ALLOWED_USERS`（群放行）+ `extra.group_allowed_chats`
- 一律用 `hermes config set <点分键> <值>` 改，**不要手改 config.yaml**；改完 `hermes gateway restart`
- **按会话注入提示词 = 把每个群绑到各自的职责/目录**：`platforms.qqbot.channel_prompts.<chat_id>`（键就是群 openid 或频道 id，精确匹配）在该会话的每一轮注入一段临时提示词。多群多模块的场景用这个，不必改代码。
  - ⚠️ `hermes config set` 会**对值里的反斜杠再转义一层**（`D:\a\b` 落成字面 `D:\\a\\b`）→ **提示词里的 Windows 路径一律写正斜杠**（`D:/找工作找工作/1-情报/`），再确认无转义残留。
  - 回显不可信：用 `python -c "import yaml;d=yaml.safe_load(open('config.yaml'));print(d['platforms']['qqbot']['channel_prompts'])"` 看解析后的真实值。

## 关键坑

- **默认 `pairing` 会把群消息在入口静默丢弃**：放行条件只有 `open` 或 `allowlist` 命中（`gateway/platforms/access_policy_mixin.py` 的 `_is_group_allowed`）。表现就是“群里 @ 了没反应”，日志里连 inbound 都没有。
- **拿群 openid 的办法**（官方 API 没有“列出机器人所在群”的端点，实测 `/users/@me/groups`、`/v2/users/@me/groups`、`/v2/groups` 全部返回 `不支持的调用`，只能靠一条真实群消息带出来）：
  1. `.env` 里补 `QQ_ALLOW_ALL_USERS=true`。**漏掉这一步会直接把机器人搞下线**：`group_policy: open` 会让网关拒绝启动（`Refusing to start: qqbot has dm_policy/group_policy set to 'open' but neither GATEWAY_ALLOW_ALL_USERS nor QQ_ALLOW_ALL_USERS is enabled`），而用户侧只看到“机器人不说话了” 。只有 `open` 有这个防呆，`allowlist` 正常启动。
  2. `hermes config set platforms.qqbot.group_policy open` → `hermes gateway restart` → **先确认 `gateway_state.json` 是 `running` 且 `platforms.qqbot.state=connected`，再让人 @**。
  3. 在**自己能控制的群**里 @ 一次 → 抄 `channel_directory.json` 里 `"type": "group"` 的条目（或日志 `chat=` 后面那串）。多个群可以一次性全拿到。
  3b. **拿到 openid 后反查群名**：`GET /v2/groups/{group_openid}/info` 返回 `group_name` / `group_member_num`。这个端点**存在**，别和上面那些“列出机器人所在群”的 404 端点混为一谈。群名是把目录/模块对齐到群组的唯一依据 —— 本地工作目录直接按群名命名（本次四个群就叫 情报 / 简历 / 面试 / 刷题），用户一眼能对上。
  4. 立刻改回 `allowlist` + `group_allow_from`，**并从 `.env` 删掉 `QQ_ALLOW_ALL_USERS`**，再 restart + 复查。
- **适配器放行 ≠ 进会话**：群消息的发送者是 `member_openid`，与单聊的 `user_openid` 是**两个不同的 ID**（QQ openid 分场景下发），所以"私聊已经配好对"不代表群里能用。群这一侧的开关是环境变量 `QQ_GROUP_ALLOWED_USERS`（`gateway/authz_mixin.py` 的 `_GROUP_CHAT_ENV`）—— **它装的是群 openid**，名字里的 USERS 是误导（`_chat_scoped_grant` 拿它比对 `source.chat_id`）；config 里的等价项是 `extra.group_allowed_chats`。
- **被丢弃的群消息零痕迹**：INFO 级别下，被 `_is_group_allowed` 挡下的消息一行日志都不写。要分清"没送到"和"送到了被丢"，先 `hermes config set logging.level DEBUG`（原始 dispatch 事件是 debug 级），@ 一次复现，抓完改回 INFO。
- **重启要看当前会话走哪条通道**：会话本身就是通过 gateway 在聊天（在 QQ/Telegram 里对话）时，`hermes gateway restart` 会提前揭示掉你正在生成的回复 —— 把命令交给用户执行。桌面 App / CLI 会话是**独立进程**，实测可以在会话里自己跑 `hermes gateway restart`，回复照常送达。
- **改完权限必查服务起没起来**：`hermes gateway status` + `$HERMES_HOME/gateway_state.json` 的 `gateway_state` / `exit_reason` / `platforms.qqbot.state`。策略值不合法时网关会“启动失败”直接退出，而**聊天侧零提示** —— 用户只会觉得机器人死了。
- **安全边界**：qqbot 平台的工具集是全量核心工具（`toolsets.py` 里 `hermes-qqbot` 绑的是核心工具集，含 `terminal`/`write_file`/`patch`/`browser`/`computer_use`）→ **群里任何人 @ 一下就能在主机上执行命令**。**适配器层无法按人放行**：`_is_group_allowed(chat_id, sender_id)` 收了 sender 但判断只用 `chat_id`（只有 Telegram 才有配对的 `TELEGRAM_GROUP_ALLOWED_USERS`），所以放行一个群 = 该群全群可用。只给完全信任的群开 allowlist；必要时收窄 `platform_toolsets.qqbot` 兜底。
- **想在群这一侧按人收窄，走网关层的 `QQ_ALLOWED_USERS=<openid>`**：实测群消息日志里 `user=` 打出来的**就是单聊那个 openid**（同一 app 下 QQ 两个场景给的是同一个值），而 `_chat_scoped_grant` 只在 `QQ_GROUP_ALLOWED_USERS` 被设置时才整群放行 —— 不设它，群消息就落到按人白名单上。⚠️ 这条**目前只有日志证据，尚未端到端验证**：配上之后必须让用户在群里 @ 一次确认，不通过就退回 chat 级放行。
- 沙箱模式的机器人与正式发布的可达范围不同；C2C / 群 @消息 / 频道 这几个 intent 要在 q.qq.com 后台确认已启用。
- **频道（guild）接入与群不一样，要改两处、且判两个不同的 id**：频道消息走 `_handle_guild_message`，`_ingest(chat_id=channel_id, qq_chat_type="guild", user_id=author["id"], chat_type="group")`。① 适配器层 `_is_group_allowed(guild_id or channel_id, author_id)` → **`platforms.qqbot.group_allow_from` 里要加 guild_id**（没有 guild_id 时才回退 channel_id），不在名单里就是静默丢弃。② 频道作者的 `user_id` 是**频道内用户 id**，与 QQ `user_openid` **不是同一个 id 空间** → 现有的 `QQ_ALLOWED_USERS` 按人白名单放不进来，必须在 `.env` 加 `QQ_GROUP_ALLOWED_USERS=<channel_id>`（网关层按 `source.chat_id` 比对，对已有群是**纯追加**：不在名单里会继续落回按人白名单）。拿 id 的办法：`GET /users/@me/guilds`（存在）→ `GET /guilds/{guild_id}/channels`，见本 skill 的 `scripts/qq_guild_probe.py`（从 `.env` 读凭据、只读、不打印 secret）。
  - **机器人不能自助进频道**，必须频道主授权 —— 没有任何接口能让它自己加入。`GET /guilds/{guild_id}` 在未授权时返回 `11264 频道未对机器人授权`（**不是**“不存在”），所以顺手能验证用户给的候选 id 是不是真频道。
  - 给用户的落地入口：`POST /v2/generate_url_link`（body 可带 `guild_id` 绑定到该频道）→ 返回短链 `https://bot.q.qq.com/s/xxxx`，让他在 QQ 里点开授权。不带参数也能生成通用的加群/加频道链接（`/users/@me` 的 `share_url` 是另一条路）。
- **群回复必须带 `msg_id`（被动回复）**：腾讯只允许在收到消息后的短窗口内、对同一个 `msg_id` 回有限条数；不带 `msg_id` 的“主动消息”要单独申请权限，没权限就是 `[400] /v2/groups/<id>/messages: 主动消息失败, 无权限`。Hermes 的正文回复会用 `_reply_anchor_for_event` 锚定入站 id，能发出去；但**忙确认/状态提示（`busy_ack`，`gateway/run_busy.py`）没有锚点**，在群里必然报这个 400 —— 日志有 ERROR，**正文照常送达，属噪音**。连发多条、上一轮没跑完时最容易触发；嫌吵就 `.env` 加 `HERMES_GATEWAY_BUSY_ACK_ENABLED=false`。
- **主动消息的能力矩阵（实测，同一个机器人同一天）**：C2C 私聊主动消息 **可用**；群主动消息 **不可用**。验证办法：建两个一次性 cron 任务（`schedule: "in 2m"`），一个 `deliver: origin`（群）、一个 `deliver: qqbot:<user_openid>`（私聊），跑完读 `$HERMES_HOME/cron/executions.db` 的 `delivery_outcome` 列 —— 群那条 `failed`、私聊那条 `delivered`（比看日志可靠：成功不发日志）。**所以定时推送/主动播报一律 `deliver: qqbot:<user_openid>` 走私聊，不要投群。** 这挡在腾讯后台权限上，本机改配置绕不过去；要群主动消息得在 q.qq.com 申请。
  - **频道（guild）是主动推送的第三条路，配额比群明确得多**：官方文档写“主动推送消息，默认每天往每个子频道可推送 20 条；每个频道每天可以往 2 个子频道推送；不论主动还是被动，一个子频道每秒最多 5 条”。所以群主动消息被 `40034105` 挡住时，若对方自己有频道，**优先提议改用频道推**（前提：频道主先授权机器人进频道）。这条配额**未实测**，加进频道后要先用一条测试消息验证。
  - 官方口径（`/wiki/develop/api-v2/server-inter/message/overview.html`）是**能力开放但有配额**：被动回复 5 分钟窗口、同一 `msg_id` 最多 5 条；主动消息未认证机器人 30/qpm、单群每日上限 1000 条。所以 `40034105` 不等于“这个能力永远没有”，文档给的两条可查原因是：① 机器人处于开发/沙箱、未上线；② 收件人在 QQ 客户端关掉了「允许主动发送」开关。**这两条未实测**，排查时当作给用户的线索，别当结论。
- **`clarify` 提示在群里发了等于没发**：它不带 `msg_id` 锚点，群里必然 `[400] 主动消息失败,无权限`，注册被清掉、用户在界面上什么都看不到（工具只回 `[clarify prompt could not be delivered]`）。在群会话里问用户就用普通正文，别用 `clarify`。
- **cron 任务的 `script` 参数只收 `~/.hermes/scripts/` 下的相对文件名**：传绝对路径被拒（`Script path must be relative to ~/.hermes/scripts/`）。脚本放 `$HERMES_HOME/scripts/`，cron 里写 `daily_xxx.py`；脚本 stdout 会以 `## Script Output` 注入 agent 提示词，是给无上下文的定时 agent 喂数据的正路。
- **改推送时间要四处同步**，漏一处就自相矛盾：① cron 的 `name`（含时段后缀，如 `-0830`）② `schedule` ③ **prompt 正文里写死的时间文案**（如「现在是晚上 21:30」）④ 文档里引用该时间的文字（总索引 / 交接说明 / 事项台账 / 相关准备卡）。改完先把「改了哪几处」列出来给用户看。
  - ⚠️ **绝不用全局 find/replace 去改时间或数字**：同一个数字在不同文件里语义不同（`09:00` 既是某场宣讲开始时间、也可能是推送时间；`22:00` 既是考试结束时间、也可能是推送时间），存档、引用原文、历史时间戳更不该动。做法：先 `search_files` 把所有命中列出来**逐条判语义**，只改正主的那几处；改完复扫确认 0 残留；用户改主意说「撤销」时按清单**定点还原**（全局反向替换会把本来就正确的值改坏）。

## 通道静默死亡：断网超时后不会自愈

**症状**：用户说「QQ 里发消息你收不到」，日志里没有任何 inbound，机器人不出声；`gateway_state.json` 的 `platforms.qqbot.state` = `disconnected`，它的 `updated_at` **就是死亡时刻**（先看这个时间戳，再回日志找对应行）。

**根因（实测）**：`$HERMES_HOME/hermes-agent/gateway/platforms/qqbot/constants.py` 里
`RECONNECT_BACKOFF = [2, 5, 10, 30, 60]`（退避封顶 60 秒）+ `MAX_RECONNECT_ATTEMPTS = 100`
→ **只能扛约 1.5 小时的断网**；耗尽后打一行 `Max reconnect attempts reached` 就彻底放弃，**之后永不重连**。
这两个常量**没有 env / config 覆盖口**（整个文件里只有 `QQ_PORTAL_HOST` 是可覆盖的）。

**日志判读（别把网络问题当凭证问题查）**：
- `Failed to get QQ Bot access token: All connection attempts failed` = **网络层不可达**（连拿 token 的请求都发不出去）→ 先 `curl -sS -o /dev/null -w '%{http_code}' https://bots.qq.com/app/getAppAccessToken` 确认网络，**不要**去翻 secret / app_id。
- `Not connected — waiting for reconnection (up to 15s)` / `Still not connected after 15s` = 有发送动作时适配器会开一个 15 秒重连窗口 → 网络已恢复时，**一次投放尝试就可能自愈**（这也是为什么「让它发一条消息」有时能救活通道）。
- **`SSL: CERTIFICATE_VERIFY_FAILED ... self-signed certificate`（取 token 或投递时报）= HTTPS 被中间人替换**，既不是网络不可达、也不是凭证错。常见来源：梯子/VPN（TUN 模式或带 TLS 拦截的客户端）、安全软件的 HTTPS 扫描、校园网客户端。用 `scripts/tls_probe.py` 判读：直连 `bots.qq.com` / `api.sgroup.qq.com` / 模型接口并打印**实际证书的 issuer** —— 出现真证书（GlobalSign / TrustAsia）说明**此刻已恢复**（这类故障常是瞬时的，下一次退避重试就跑通了，日志会自己出现 `✓ qqbot reconnected successfully`，不必重配）；出现自签 / 未知 issuer 才需要动网络侧，顺手让用户把 `*.qq.com` 加进梯子的**直连规则**。
- **`getaddrinfo failed` / `Blocked request - DNS resolution failed for: <host>` = 本机 DNS 解析失败**（不是凭证错、也不是「网络不可达」）：日志形如 `Cannot connect to host ilinkai.weixin.qq.com:443 ssl:default [getaddrinfo failed]`、`Failed to get QQ Bot access token: Blocked request - DNS resolution failed for: bots.qq.com`。**它会同时打到所有通道与 cron 的模型调用**（cron 表现为 `RuntimeError: Hermes can't reach the model provider. You may be offline.`）→ 症状是「通道全挂 + 定时任务失败」同时发生。先 `nslookup bots.qq.com` 确认是否能解析，再用 `Get-DnsClientServerAddress -AddressFamily IPv4` 看在用哪些 DNS（校园网常见 `10.70.0.16` / `150.204.1.2`）——校园 DNS 抖动时会整片域名解析失败。修复：给在用网卡加公共 DNS 做备份（**校园 DNS 留在最前**，避免影响校内系统）`Set-DnsClientServerAddress -InterfaceAlias "WLAN" -ServerAddresses ("<校园DNS>","223.5.5.5","119.29.29.29")` + `ipconfig /flushdns`。**网络自行恢复后适配器会在下一次 60 秒退避重试里自愈**（日志 `✓ qqbot reconnected successfully`），不必重配凭证或改常量。
- **判通道死活要看全部平台**：`gateway_state.json` 的 `platforms` 下可能同时挂着 `qqbot` 与 `weixin`（`ilinkai.weixin.qq.com`）——一个挂了另一个可能正常，两个都要各自看 `state` 与各自平台的报错行，别只看 qqbot。

**先分流：启动阶段失败 ≠ 运行中掉线（重试责任不同，决定你等还是重启）**

| 场景 | 日志特征 | 谁在重试 | 会不会永久死 |
|---|---|---|---|
| **启动时**连不上 | `Connecting to qqbot...` → `qqbot connect timed out after 30s` → `Gateway started with no connected platforms — N platform(s) queued for retry` → `Reconnecting qqbot (attempt N)` | **网关级 reconnection watcher**，每 60 秒一次 | ❌ 不会，会一直重试 |
| **运行中**掉线 | `WebSocket error: WebSocket closed` → `Reconnecting in Ns (attempt k)` | 适配器自己重连，受 `MAX_RECONNECT_ATTEMPTS` 限制 | ✅ 会 —— 耗尽后永久死，网关不接手 |
| **进程没了**（重启后没起来 / 被别的实例挤掉） | `hermes gateway status` 打 **`✗ No gateway process detected`**；`gateway.log` 停在死亡前最后一行，之后**再无任何记录** | **没人重试**（进程不在了） | ✅ 会 —— 必须手动 `hermes gateway restart` |

**进程级死亡（第三种，先跟网络问题分开）**

- 一眼分流：`hermes gateway status` 打 `✗ No gateway process detected` = 进程没了；打 `✓ Gateway process running` 但 qqbot 不 connected = 前面两种。
- ⚠️ **别信 `gateway_state.json`**：进程死后文件会**停在最后一次写入**（`platforms.qqbot.state` 还写着 `connected`、`writer_pid` 指向已消失的 PID）。判断活着要同时看 `hermes gateway status` 的进程检测 + 日志里的 `✓ qqbot connected` / `Ready, session_id=...`，或核对 `writer_pid` 是否等于当前 PID。
- 最典型诱因：**整机重启后网关与桌面 App 抢启动** —— 桌面 App 先起来，网关那一路启动后退出，聊天侧零提示（用户只说“又挂了”）。
- 什么时候死的：`$HERMES_HOME/logs/gateway-exit-diag.log`（逐行 JSON）里 `{"tag":"gateway.previous_unclean_exit", ...}` 带 `prior_pid` / `prior_started_at` / **`last_heartbeat_at`**（= 该进程停止工作的时刻）；同文件 `{"tag":"gateway.start","pid":...}` 列出每次启动，`gateway-stdio.log` 是当前进程的 stdout。心跳文件 `cron/ticker_heartbeat` 也能反推。
- 修复：**`hermes gateway restart`**（没有自动重试兜底）。预防：**开机后别立刻开桌面 App**，等约 1 分钟或先跑一次 `hermes gateway status` 确认。

所以「把常量调大」只解后者；**排查时先看日志落在哪一段再决定**：启动阶段失败先等 60~90 秒（常见于开机、网络刚恢复、梯子刚切换），别急着重启。

**修复**：网络恢复后 `hermes gateway restart` 即可。重启后 `gateway_state.json` 回到 `gateway_state: running` + `platforms.qqbot.state: connected`，日志出现 `WebSocket connected` → `✓ qqbot connected` → `Ready, session_id=...`。

**重启命令谁跑**：桌面 App / CLI 会话是独立进程，可以在会话里自己跑；**QQ 会话本身走网关，跑 restart 会掐掉正在生成的回复 → 把命令交给用户**（同上文「重启要看当前会话走哪条通道」）。

**别指望用 cron 兜底**：agent 建含「重启网关」动作的 cron 会被防护拦死（`Blocked: cron job contains a gateway lifecycle command … #30719`，防止自动重启循环）——**连脚本正文里出现该命令字符串都会被拦**。要自动化只有两条路，都要用户点头：
1. 把 `MAX_RECONNECT_ATTEMPTS` 调大（一行常量、重启生效；Hermes 升级可能覆盖，升级后复查）。
   - **本机现状（2026-09-18 用户指定）**：`100 → 600`（`[2,5,10,30,60]` 退避下 ≈ **9.9 小时**容忍）。改前先备份 `constants.py.bak-<日期>`，删 `__pycache__/constants*.pyc`，再 `hermes gateway restart` 验证 `connected`。
   - **调大前先跟用户算清代价**：联网正常时**零开销**（重连只在断开态触发）；断网期间最多**每分钟 1 次**尝试（异步协程等 socket，CPU/内存/电量可忽略）；唯一真实代价是**日志**——每次失败 1~2 行、实测每行约 150 字节 → 1440 行/天 ≈ 0.2 MB/天。
   - **不建议设成无限**：若是凭证失效（appsecret 被重置等）会永远刷日志，现有的次数上限顺手保护了这个场景。取“用户能接受的断网时长 ÷ 60 秒”即可。
2. 由**用户自己**在系统层（Windows 任务计划程序）挂一个检测 `qqbot.state != connected` 就重启的看门狗。

细节、日志样本与出站自测脚本见 `references/qqbot-reconnect-recovery.md`、`scripts/qq_dm_test.py`。

## 回传文件（MEDIA 附件）

- 正文里写一行 `MEDIA:<绝对路径>` 即可发附件：图片走原生图片消息，其它文件走分片上传（`chunked_upload`）。
- **成功怎么认**：日志依次出现 `Delivering N non-image MEDIA attachment(s)` →
  `Chunked upload start: file=<名> size=...` → `Prepared: upload_id=...` → `All 1 parts uploaded, completing…`，
  且之后没有 ERROR，就是已发出。
- **文件消息比文本消息晚几秒到十几秒才到**。用户说“发给我 / 没收到”时**先 grep 日志确认再决定是否重发**，
  立刻重发只会把同一份附件灌两遍（他往往只是还没看到）。
- 报“已发送”前用 `Delivering N ...` 那行核对条数，别凭记忆说发了几份。
- **关键推送要先落盘再投递**：定时任务在生成正文**之前**，先把它追加到工作目录下的存档文件（如 `<模块>\晨报存档-YYYY-MM.md`）。通道断时投递必然失败，但内容不丢、随时能取回重发。⚠️ cron 上下文里 **`execute_code` 会被安全防护拦**（无人在场无法批准），**`terminal` 可用** —— 落盘动作写成 terminal 命令才跑得通。
  - **错过的定时任务会被补跑一次**：机器宕机/重启后，`hermes cron status` 会打 `⚠ 1 job(s) last fired late (missed-fire catch-up)` 并给出实际执行时间（如「scheduled 08:30, ran 08:47」）。所以断网/重启期间错过的晨报不是丢了，而是迟到重跑 —— 但**补跑时通道可能仍未恢复，投递照样失败**，这正是落盘要挡的场景：去存档文件里取原文手动补发，比重新生成快。
  - **手动补发要带「临时提示词」**：`cronjob(action='run', job_id=<id>, prompt='【本次为补发】现在是 <准确日期时间> …')` —— 任务自己的 prompt 是日期无关的，直接重跑会照旧上下文写（时间/日程全错）。补发提示词必须自带三样：**① 声明补发 + 当前准确时间；② 今天的时间线（正在进行的场次、硬截止点）；③ 刚新增的关键事项**。跑完看 `hermes cron runs <id>` 的 `status` 与投递目标，别只看返回值。
  - **补发前先确认“真没送到”**：同一天的推送既可能“没跑成”（`status=failed`，如模型不可达 / DNS 挂）也可能“跑成了但投递失败”（`last_delivery_error`）—— 只读得到前者才能安全重发；已送达还补发，用户会收到两份同样的日报。
  - **别凭日志就宣布「推送失败」**：通道断 ≠ 那条推送失败 —— 补跑或后续重试可能已经送达。说「没送到」之前先查任务记录：`hermes cron list` 的 `last_status` / `last_delivery_error`，或 `hermes cron runs <job_id>`、`cron/executions.db` 的 `delivery_outcome`。让用户先翻一下聊天记录再决定是否补发（重复推同一份比少推一次更扰人）。
- **同一份材料补一份图片版**：打印店、只收图片的上传框都只吃图。把 PDF 每页用 PyMuPDF 渲染成 JPG
  （`fitz.Matrix(2.2, 2.2)` 约等于 160 DPI，A4 两页约几百 KB，可直接发）。注意 `fitz` 这类项目依赖
  **只在 terminal 的 python 里**，`execute_code` 的沙箱解释器没有 → 写成脚本交给 `terminal` 跑。

## 排查顺序

1. `$HERMES_HOME/logs/gateway.log` 搜 `C2C message` / `GROUP_AT_MESSAGE_CREATE` / `inbound message` → 先分清“没送到”还是“送到了被过滤”
2. 有事件行但没有 `inbound message` 行 = 被 ACL 拦了 —— **两道闸都要查**：适配器层 `group_policy`（拦得干净，无日志）和网关层 `QQ_GROUP_ALLOWED_USERS` / `extra.group_allowed_chats`
3. 会话里到底有没有 → 查 `state.db` 的 `messages` 表 `role='user'`（事件到达适配器 ≠ 进了会话）
4. 需要看实现时读本机源码副本：`$HERMES_HOME/hermes-agent/gateway/platforms/qqbot/adapter.py`、`gateway/platforms/access_policy_mixin.py`

## Verification

1. **服务起没起来**：`hermes gateway status` + `gateway_state.json` 的 `gateway_state: running`、`exit_reason: None`、`platforms.qqbot.state: connected`。改策略后这步不能跳。
2. **收**：在目标群 @ 一次，`gateway.log` 出现 `inbound message: platform=qqbot user=<openid> chat=<群openid>`；同时 `channel_directory.json` 里能看到该群条目。
3. **发**：紧随其后是 `Sending response (N chars) to <群openid>`，且后面**没有** `send retry` / `Send failed`（成功不打日志，所以“没报错”就是成功）。
4. **让用户回报他看到了什么** —— “机器人到底回没回话”只有用户的眼睛算数，日志只是证据。
