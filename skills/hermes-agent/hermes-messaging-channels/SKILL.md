---
name: hermes-messaging-channels
description: "Use when enabling or securing a Hermes messaging channel."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes, gateway, messaging, group-chat, access-control, security, qqbot]
    category: hermes-agent
---

# Hermes Messaging Channels Skill

Turn group and DM traffic on for a Hermes messaging platform without handing the host to strangers. Covers the shared access-policy surface (`open | allowlist | disabled | pairing`), how to read the effective policy from the code instead of guessing defaults, how to obtain a chat id, and the tool-scope gate that must be checked before any group is granted access. Per-platform depth: `references/platform-adapters.md`.

## When to Use

- "Can you receive messages from group X / answer in this group?"
- Group messages are ignored: nobody gets a reply and the user suspects the connection
- Before granting group access, to decide what that group can make the agent do
- Any edit to `platforms.<name>.extra` policy keys or `platform_toolsets.<name>`
- 要让 bot **主动发（outbound）**一条消息或一个文件给某人（推送 / 脚本 / cron）—— `hermes send`，做法与投递验证见「主动外发」

## Procedure

1. **Establish the platform's inbound limits first.** Most official bot APIs deliver group messages only when the bot is @-mentioned (QQ: `GROUP_AT_MESSAGE_CREATE`). If the user wants every group message read, that is a different protocol path (unofficial clients, account-ban risk) — say so before designing anything else. Never promise full-chat monitoring on an official bot API.

2. **Read the effective policy, do not recall it.** `dm_policy` and `group_policy` both default to `pairing`, and `_is_group_allowed` in `gateway/platforms/access_policy_mixin.py` returns True only for `open` or `allowlist` — `pairing`, `disabled` and any unknown value drop group traffic at intake. Keys live at `platforms.<name>.extra.{dm_policy,allow_from,group_policy,group_allow_from}`; some platforms mirror them as env vars (e.g. `QQ_GROUP_ALLOWED_USERS`). Config is canon, env is a mirror.

3. **Grant narrowly.** Never `open` for anything but a chat whose every member the user fully trusts:

   ```bash
   hermes config set platforms.<name>.extra.group_policy allowlist
   hermes config set platforms.<name>.extra.group_allow_from "<chat_id1>,<chat_id2>"
   ```

   Use `hermes config set` — never hand-edit `config.yaml`, and never move a non-secret setting into `.env`.
   Pass the allowlist as a **comma-separated scalar**, never a JSON array literal: adapters parse it with a
   `coerce_list`-style helper that accepts a YAML list or a comma-separated string, and a JSON literal
   silently lands as ONE entry (`["a","b"]` → `'["a"'`, `'"b"]'`), so the allowlist matches nothing and
   every message is dropped with no error.

4. **Get the chat id empirically.** There is no "list my chats" endpoint on most official bot APIs (QQ: `/users/@me/groups` returns `不支持的调用`, while `/users/@me/guilds` works and lists guilds), so a real inbound message is the only source. Temporarily set `group_policy` to `open`, have someone @ the bot exactly once, then read `$HERMES_HOME/channel_directory.json` — entries appear as `{"id", "name", "type": "dm"|"group", "thread_id"}` under `platforms.<name>[]`. Copy the id, switch back to `allowlist`, restart.

   **`open` does not boot on its own.** The gateway refuses to start with an `open` policy unless the
   allow-all opt-in is present in `.env`: `GATEWAY_ALLOW_ALL_USERS=true`, or the platform-scoped
   `<PLATFORM>_ALLOW_ALL_USERS=true` (QQ: `QQ_ALLOW_ALL_USERS`). The log says
   `Refusing to start: <platform> has dm_policy/group_policy set to 'open' but neither ... is enabled`;
   without it the gateway exits and the bot simply goes offline — no chat shows any error. `open` is the
   only policy value with this guard; `allowlist` boots normally. Add the opt-in, restart, **confirm the
   service is up before asking anyone to send the test message**, then drop the opt-in once the id is
   captured.

5. **Restart the gateway.** The adapter builds its ACL at construction, so a config edit does nothing to a running process: `hermes gateway restart`, then `hermes gateway status`. On a foreground `hermes gateway run` install, restart that terminal instead. A desktop-app or CLI session survives the restart (the gateway is a separate process) and can run the command itself; a session whose own transport *is* the gateway (chatting on QQ/Telegram) hands the command to the user.

6. **Verify service state, then the round trip.** After every restart read `$HERMES_HOME/gateway_state.json` and require `gateway_state: running`, `exit_reason: None`, `platforms.<name>.state: connected`. A policy mistake surfaces here as `startup_failed` — the bot is dead and no chat will tell you. Only then verify with a real inbound message in the target chat, and confirm the reply lands *there* and not in the DM. Report the verified round trip — a written config is not the deliverable.

7. **Check tool exposure before the first grant** — see the gate below.

## 主动外发（`hermes send`）：消息、附件、以及“真的发出去了吗”

入站策略管完了，出站是另一条路：

```bash
hermes send --to <platform>[:<chat_id>[:<thread_id>]] "文本"      # 也可 --file <正文文件> / --list / --json
```

- **附件写在正文里**：`MEDIA:<绝对路径>`（图片与文档都走这条）；`--file` 只送正文。一次可带多个 `MEDIA:` 行。
- bot-token 类平台不要求 gateway 在跑；weixin 这类走本机已登录的 gateway 凭据。
- **目标 id 从哪来**：`$HERMES_HOME/channel_directory.json` → `platforms.<name>[]` 的 `{id,name,type,thread_id}`。
  weixin 的本人 DM id 形如 `<openid>@im.wechat` —— 别与 bot **自身** id（`weixin/accounts/<account_id>.json` 里的 `<id>@im.bot`）搞混。

**证明它真发出去了（不要只看 `success` 字样）：**

- `--json` 返回 `success` + `message_id`。weixin 侧 `gateway/platforms/weixin.py::_deliver_direct` 的判定是硬的：
  **正文或任一附件**发送失败都会改返回 `{"error": ...}` —— 所以拿到 `success` + `message_id` 说明**附件那一步也成功了**。
- `hermes send` 的结果**不一定进 `gateway.log`**：日志里没有这条**不等于**没发出去，别拿“日志没写”当失败证据。
- 日志里可能有接收侧轮询的 `poll error … getaddrinfo failed`，而**发送仍然成功**：先验证再下结论
  （`python -c "import socket;print(socket.getaddrinfo('<host>',443)[0][4])"`），不要凭日志里的旧错误说频道挂了。
- 你看不到对方手机：`success` 之外**请用户确认收到**，不要把“接口返回成功”说成“你已收到”。主动推送只走私聊（群的主动消息多数平台不让发）。

`platform_toolsets.<platform>` maps a messaging platform to a named toolset (`hermes-qqbot`, `hermes-discord`, ...) defined in `toolsets.py` as a "full access" bundle, i.e. the full core toolset: `terminal`, `process_manage`, `write_file`, `patch`, `search_files`, `browser_*`, `computer_use`, `cronjob_manage`, `delegate_task`. Anyone who can @ the bot in a group gets that on the host — shell, file writes, browser, scheduled jobs. So:

- `group_policy: open` is acceptable only when every member of that group is trusted with the machine.
- Otherwise offer to narrow `platform_toolsets.<platform>` to what the use case needs (`web`, `file`) so the bot can look things up without touching the system.
- Confirm the bundle's tool list in `toolsets.py`; do not promise a scope from memory.
- Sessions are per-chat (and per-user in groups via `group_sessions_per_user: true`), so a group session does not inherit the DM conversation — but it is the same machine and the same credentials.

## Pitfalls

- **One platform's policy mistake kills EVERY platform.** The `open`-without-opt-in guard (`gateway/run.py`, `_open_policy_without_optin`) aborts startup for the whole gateway, so an `open` policy on a platform the user just enabled takes down every other adapter too (qqbot + weixin both offline). Symptom from the user: "我的 X 和 Y 都不能用了". Read `$HERMES_HOME/gateway_state.json` → `exit_reason` FIRST (`<platform>: open policy without allow-all opt-in`); the matching `Refusing to start` line is in `logs/gateway.log`. Do not debug the platforms one by one.
- **Feishu defaults to websocket — websocket needs NO public URL.** 别默认说“飞书必须公网回调地址”：先看 `FEISHU_CONNECTION_MODE`（不设即 websocket，`adapter.py:1311`），只有 `webhook` 模式才要公网地址 + `FEISHU_VERIFICATION_TOKEN`/`ENCRYPT_KEY`。引导式建应用（launcher 设备码）与配对码机制见 `references/feishu-onboarding.md`。
- **Silent drop.** With the default `pairing` policy, group messages are discarded at intake with no error, no log line, no reply. Debug order: policy → bot membership → platform intents → websocket. Do not start by chasing the connection.
- **Weixin (iLink) needs `group_policy: disabled` in practice.** QR-login accounts are iLink bot identities (`<id>@im.bot`) that cannot receive ordinary WeChat group events, so `open` buys nothing while tripping the guard above (and logs a startup WARNING for any non-`disabled` value). The owner's Weixin user id for the DM allowlist is the `user_id` field of `$HERMES_HOME/weixin/accounts/<account_id>.json` (format `<openid>@im.wechat`) — set `WEIXIN_DM_POLICY=allowlist` + `WEIXIN_ALLOWED_USERS=<that id>` and confirm with `inbound from=<id> type=dm` → `Sending response` in the log.
- **`~/.hermes/.env` is refused by the `patch`/`write_file` tools** ("protected system/credential file"). Edit it with a small python script run through `terminal` (read text with `newline=""` to keep CRLF), or `hermes config set platforms.<name>.extra.<key>`; back the file up first.
- **Membership is a prerequisite only the user can satisfy.** Someone with the rights must invite the bot to the group, and the group-message intent must be enabled in the platform's bot console. Sandbox-mode bots reach only the console's test channel.
- **Restart is mandatory** after policy changes: the gateway reads user YAML raw while constructing adapters.
- **Never touch another profile's channels.** Every profile owns its own `platforms/` pairing state, and profile clones deliberately omit messaging channels.
- **Scope repo searches.** A repo-wide `grep -rn` inside the local Hermes checkout can blow the 180 s foreground timeout (tens of thousands of test files). Use `search_files` scoped to `gateway/platforms`, or read the specific adapter file.
- **Docs site unreachable?** The docs tree ships in the checkout at `$HERMES_HOME/hermes-agent/website/docs/` (Chinese mirror under `website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/`) — same pages as the website.

## Where the code lives

- Shared ACL: `gateway/platforms/access_policy_mixin.py` — `_is_dm_allowed`, `_is_dm_intake_allowed`, `_is_group_allowed`
- In-tree adapters: `gateway/platforms/<name>.py`, `gateway/platforms/<name>/adapter.py`; plugin adapters: `plugins/platforms/<name>/adapter.py`
- Toolset map: `toolsets.py` (`hermes-<platform>` entries) plus `platform_toolsets` in `config.yaml`
- Per-platform policy surface, QQ Bot specifics and pairing state files: `references/platform-adapters.md`
- Feishu / Lark 引导式接入（`hermes gateway setup` → launcher 设备码链接 → `hermes pairing approve <platform> <CODE>`）、websocket vs webhook、凭据验活与 pitfall：`references/feishu-onboarding.md`

## Working with this user

Give the exact command lines and file paths, and let them run the change; offer to apply it yourself but confirm first — config edits and gateway restarts are theirs to approve. Reply in Chinese, lead with the constraint (what the platform cannot deliver), then the steps, then the open questions. When the answer depends on Hermes internals, read the adapter or mixin rather than answering from memory.
