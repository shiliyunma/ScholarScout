# QQ 机器人 API 探测与发送机制

只读探测：确认“机器人是谁”、拿到加群链接、判读发送失败的原因。

## 用凭证探测机器人身份

凭证在 `$HERMES_HOME/.env`：`QQ_APP_ID` / `QQ_CLIENT_SECRET`。App ID 也能直接看日志的 tag：`[QQBot:1905618517]`。

```python
# 1) 取 access_token（约 7200s 过期）
POST https://bots.qq.com/app/getAppAccessToken
body: {"appId": <QQ_APP_ID>, "clientSecret": <QQ_CLIENT_SECRET>}

# 2) 取机器人身份
GET  https://api.sgroup.qq.com/users/@me
header: Authorization: QQBot <access_token>
```

`/users/@me` 返回 `id` / `username` / `share_url`。`share_url` 形如

```
https://qun.qq.com/qunpro/robot/qunshare?robot_uin=<uin>&robot_appid=<appid>&biz_type=0
```

—— 这就是**把机器人加进群**的链接，直接给用户（他在后台找不到这个）。

脚本里直接从 `.env` 读值，**不要把 secret 打进终端输出**（只打印长度或布尔）。所有 `api.sgroup.qq.com` 请求都要带 `Authorization: QQBot <token>`。

## 已知不存在的端点（别再浪费轮次）

- `/users/@me/groups`、`/v2/users/@me/groups`、`/v2/groups`、`/v2/me/groups` → 全部 `{"message":"不支持的调用","code":11001,"err_code":40011002}`
- 所以**没有任何接口能列出机器人所在的群**，群 openid 只能从一条真实群消息里拿
- `/users/@me/guilds` **存在**，返回机器人所在的频道列表（`[]` = 不在任何频道）。日志里的 `Interaction: scene=guild` 不代表它在频道里，以这个接口为准。

## 发送：被动消息 vs 主动消息

- 发文本：`POST /v2/{users|groups}/{openid}/messages`，body 带 `content` / `msg_type` / `msg_seq`；**被动回复还要带 `msg_id`**（= 触发那条入站消息的 id）。
- Hermes 侧实现：`gateway/platforms/qqbot/adapter.py` 的 `_send_text_to()` 只在 `reply_to` 非空时才写 `body["msg_id"]`；`_send_typing()` 是 **C2C 专属**（群里直接 return，不会发任何东西）。
- 不带 `msg_id` = 主动消息 = 需要单独申请的产品权限，默认没有 → `[400] .../messages: 主动消息失败, 无权限`。这是产品规则，不是网络问题、也不是权限没配好。
- `_last_msg_id[chat_id]` 保存每个会话最后一条入站消息 id（`_ingest` 里写入），发提示类消息时用它当锚点。

## 日志判读速查

| 日志 | 含义 |
|---|---|
| `inbound message: platform=qqbot user=<id> chat=<id>` | 消息已被接受并进入会话 |
| `C2C message: id=... content=...` | 私聊入站（群入站没有对应的 INFO 行） |
| `Sending response (N chars) to <id>` 后直接接下一件事 | 发送成功（成功不打日志） |
| 同上但后面跟 `send retry k/3` / `Send failed: ...` | 发送被拒，看 API 报错文案 |
| `Refusing to start: ... open policy without allow-all opt-in` | 配置导致网关拒绝启动，机器人已下线 |

## 存在但容易漏掉的端点

```
GET  /v2/groups/{group_openid}/info   → {group_name, group_member_num}
     群名唯一来源（本地目录/模块按群名命名时用它）

POST /v2/generate_url_link           body 可空，或 {"guild_id": "<频道id>"}
     → {"retcode":0,"data":{"url":"https://bot.q.qq.com/s/xxxx"}}
     给用户的“把机器人加进群/频道”入口（频道必须频道主授权，机器人不能自进）

GET  /guilds/{guild_id}              未授权 → 11264 频道未对机器人授权
GET  /guilds/{guild_id}/channels     （**不是**“不存在”，可用来验证候选 id）
```

**频控**：连着快速重复调同一接口会返回 `400 {"message":"接口调用超过频率限制","code":100017,"err_code":40023001}` —— 探测循环里每次调用之间留间隔，同一个值只探测一次，别把 5 个变体一口气打完。

## 官方文档导航（查规则用）

- 文档站：`https://bot.q.qq.com/wiki/develop/api-v2/`；全站页面列表 `https://bot.q.qq.com/sitemap.xml`（约 400+ 条，`grep` 关键词找页）。
- **消息规则权威出处**：`/wiki/develop/api-v2/server-inter/message/overview.html`（被动/主动定义与配额表）。
- 错误码页 `/wiki/develop/api-v2/gateway/error/error.html` **靠 JS 渲染**，`curl` 抓下来只剩几行，而且**不含 `40034105`** —— 别指望它解释主动消息被拒，那条只能靠自己实测 + 概述页的规则反推。
