---
name: qq-bot-group-monitoring
description: "Use when wiring a QQ bot into Hermes for group chat."
version: 1.0.0
metadata:
  hermes:
    tags: [qq, qqbot, gateway, group, messaging, allowlist]
---

# QQ 官方机器人 · 群消息接入（Hermes）

适用：让 QQ 机器人（腾讯官方 API，`api.sgroup.qq.com`）在群里收发消息、按群路由到不同工作目录。以下每条都是实测结论。

## 六个关键事实

1. **群消息默认被静默丢弃。** `group_policy` 默认 `pairing`，而 `_is_group_allowed` 只在 `open`/`allowlist` 时放行，`pairing`/`disabled` 直接 `return`，**连一行日志都不打**。症状：`channel_directory.json` 里只有 `dm`，日志里 0 条 `GROUP_AT_MESSAGE_CREATE`，看起来「消息根本没到」——其实可能到了被吞了。
2. **`group_policy: open` 会让网关拒绝启动**：`Refusing to start: qqbot has dm_policy/group_policy set to 'open' but neither GATEWAY_ALLOW_ALL_USERS nor QQ_ALLOW_ALL_USERS is enabled.`。要么在 `.env` 加 `QQ_ALLOW_ALL_USERS=true`，要么用 allowlist。**改平台配置前先确认新配置能启动，否则机器人直接掉线。**
3. **拿群 openid 不必先开 open**：`GET /v2/groups/{group_openid}/info` 直接返回 `group_name` / `group_member_num`。腾讯**没有**「列出机器人所在群」的接口（`/users/@me/groups` → `不支持的调用`）；guild 列表 `GET /users/@me/guilds` 是有的。
4. **群里只能被动回复**：发送必须带 `msg_id`（取自 `GROUP_AT_MESSAGE_CREATE` 事件的 `d.id`），**5 分钟内、每个 msg_id 最多 5 条**。不带 msg_id 就是主动消息，没有权限时返回 `40034105 主动消息失败, 无权限`。
5. **主动消息的能力与开关**：官方口径是开放的（群聊：未认证 30/qpm、单群每日 1000 条），但两个开关会挡住——① 机器人处于开发/沙箱、未上线；② QQ 客户端里收件人关掉了「允许主动发送」，关闭后一律失败。
6. **群内 sender id 与私聊 openid 相同**：网关日志里群消息的 `user=` 就是该用户的私聊 openid。因此 `.env` 的 `QQ_ALLOWED_USERS` 能同时管住私聊和群聊，可实现「只有本人能在群里指挥机器人」。Hermes 自身的群 ACL 只按**群**过滤（`_is_group_allowed(chat_id, sender_id)` 收了 sender_id 但判断里没用）。

## 配置模板（用 CLI，别手改 config.yaml）

```bash
# 1) 只放行指定群（值是逗号串，_coerce_list 接受逗号分隔）
hermes config set platforms.qqbot.group_policy allowlist
hermes config set platforms.qqbot.group_allow_from "openid1,openid2"

# 2) 只放行本人（写 .env）
QQ_ALLOWED_USERS=<你的 openid>          # 若用 open 临时排查：QQ_ALLOW_ALL_USERS=true

# 3) 按群注入不同提示词 → 天然实现「群 → 工作目录」路由
hermes config set "platforms.qqbot.channel_prompts.<群openid>" \
  "【模块：情报】本群对应 D:/path/1-情报/ 。收到消息后整理成条目写入该目录，回复要短。"

# 4) 生效
hermes gateway restart
```

## 坑

- `hermes config set` 会再次转义值里的反斜杠（`D:\a\b` 变成字面 `D:\\a\\b`）→ **提示词里的 Windows 路径用正斜杠**。
- 网关自检的「No env user allowlists configured」警告说明没有任何 `*_ALLOWED_USERS`，此时平台会拒绝未知发件人。
- 排查群消息是否真的到达：看 `channel_directory.json` 是否新增 `type: "group"` 条目，以及网关日志的 `inbound message: platform=qqbot user=<openid> chat=<群openid>`。
- 回复正文能发出去时日志里不会报错；只有失败才打 `Send failed` / `send retry n/3`。
- 机器人回复里出现 `主动消息失败` 时，先想 busy_ack / 状态类消息（并发多条入站时触发），别急着怀疑正文没发出去。

## 验证步骤

1. `hermes gateway status` + `gateway_state.json` 的 `platforms.qqbot.state == connected`。
2. 在群里 @ 一次机器人 → `channel_directory.json` 出现该群、日志出现 `inbound message`。
3. 看 `Sending response` 之后有没有 ERROR → 有 ERROR 且是 `主动消息失败` 说明这次发的是主动消息（多为 busy_ack/状态类），不是正文。
