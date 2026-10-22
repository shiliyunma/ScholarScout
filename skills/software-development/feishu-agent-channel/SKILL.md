---
name: feishu-agent-channel
description: "Use when 给自建 Agent 接入飞书渠道（收/发消息）。含隧道、回调、白名单、出站发送全流程与坑。"
version: 1.0.0
tags: [feishu, lark, messaging, channel, webhook, tunnel, agent]
---

# 给自建 Agent 接入飞书渠道

把飞书当成自建 Agent 的入口/出口：用户私聊机器人 → 事件推到本机 → Agent 跑一轮 → 回复推回飞书。
适用于**本地单用户、不引第三方 SDK**的 Agent（本项目叫 ScholarScout，端到端已跑通）。

## 何时使用

- 要给自己的 Agent/机器人加飞书私聊通道
- 飞书事件收不到、收到了不回复、回复发不出去
- 免费隧道地址变来变去，需要固定地址

## 关键前提（先讲清，否则白忙）

1. **必须有一个飞书自建应用**（`app_id` / `app_secret`）。后台操作只能人工点，没有开放 API 能替你创建应用。
2. **回调要公网可达**。本机在内网 → 只有两条路：
   - **webhook + 内网穿透**（零依赖，推荐）
   - **websocket 长连接**（无需公网地址，但飞书长连接是自定义 protobuf 协议，要么引 `lark-oapi` 等第三方 SDK，要么手写，风险高）
3. **加密密钥必须留空**。若在后台设了 Encrypt Key，飞书推的是**密文**，标准库解不了（AES 要密码学依赖）。只填 Verification Token 即可（可选）。

## 全流程（每条命令都可直接跑）

### 1. 起本地监听

```bash
cd <项目根> && export PATH="$PWD/.venv/Scripts:$PATH"
# 先在 config.yaml 打开开关：
#   runtime.feishu_listener_enabled: true
#   runtime.feishu_listener_port: 8765
#   runtime.feishu_listener_path: /feishu/webhook
<venv>/python scripts/scout.py --feishu-listen    # 用后台方式跑，日志 tee 到文件
```

启动成功的标志（日志原文）：
```
[Feishu] listening on 127.0.0.1:8765/feishu/webhook
```
本地自测（不需要飞书）：
```bash
curl -s -X POST http://127.0.0.1:8765/feishu/webhook -H "Content-Type: application/json" \
  -d '{"type":"url_verification","challenge":"probe"}'
# 期望：{"challenge": "probe"}
```

### 2. 起公网隧道

```bash
# 方案A：cloudflared（winget install --id Cloudflare.cloudflared --silent）
"/c/Program Files (x86)/cloudflared/cloudflared.exe" tunnel --url http://127.0.0.1:8765 --no-autoupdate 2>&1 | tee run/tunnel.log

# 方案B：ssh（无需安装，本机已有 ssh）
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ServerAliveInterval=30 \
    -R 80:localhost:8765 nokey@localhost.run 2>&1 | tee run/tunnel.log
```
**必须 tee 到文件**：隧道把 URL 打在 banner 里，只用输出预览会漏掉它；事后 `grep -o "https://[a-z0-9.]*\.\(trycloudflare\.com\|lhr\.life\)" run/tunnel.log | tail -1` 取最新地址。

### 3. 后台填回调地址

飞书后台 → 该应用 → **事件订阅/回调** → 填 `https://<隧道域名>/feishu/webhook`（**路径不能少**）
→ 同时勾上**接收消息**类事件（`im.message.receive_v1` 之类）。
保存时飞书会立刻打一次 `url_verification`；我们的监听会回 `{"challenge": ...}`，所以应直接通过。

### 4. 放行你自己（否则只收到配对提示、不跑会话）

授权策略默认 `pairing`（未授权者不跑会话）。**白名单在库里，不在 .env**：
```bash
sqlite3 run/state.db "insert or ignore into feishu_paired_users(user_id,user_name,approved_at,source) values('ou_xxx','名字',strftime('%s','now'),'manual');"
```
你的用户 openid 怎么拿：**找个已配好的通道**（例如 Hermes 的 `hermes pairing approve <platform> <code>` 输出里就带着 `ou_...`）；或走一次配对流程。
注意区分：`ou_...`（用户）≠ bot 自己的 open_id。

### 5. 打开出站发送（不开就只跑不回）

```yaml
# config.yaml
runtime:
  feishu_reply_enabled: true
```
**改完必须重启监听**（配置只在启动时读）。日志里不再出现 `出站发送未启用…未发往飞书 chat_id=...` 才说明开关生效。

### 6. 验证（要证据，不要感觉）

```bash
# 1) 公网地址还活着吗（200 = 活）
curl -s -o /dev/null -w "%{http_code}\n" -X POST "https://<域名>/feishu/webhook" -H "Content-Type: application/json" -d '{"type":"url_verification","challenge":"x"}'
# 2) 事件到底有没有进来：看库里有没有新会话/新消息
sqlite3 run/state.db "select created_at from sessions order by rowid desc limit 1;"
# 3) 有没有跑完但没发出去
_grep '出站发送未启用' run/feishu-listen.log
```
日志里出现 `回复未发往飞书 chat_id=oc_...` 就等于：**事件到了、会话跑了、只差发送开关** —— 这个 chat_id 顺手记下。

## 固定隧道地址（免费隧道会变，且会自己重连换域名）

实测：cloudflared quick tunnel 30 分钟内失效、第二次请求 `api.trycloudflare.com` 直接超时（部分网络环境下不可用）；ssh 的 `localhost.run` 隧道会自动重连并**换一个子域名**。
永久/长期方案（择一）：

| 方案 | 固定地址 | 代价 |
|---|---|---|
| localhost.run **注册账号 + 上传 SSH 公钥** | 长期子域名（它自己的原话："create an account and add your key for a longer lasting domain name"，见 https://localhost.run/docs/forever-free/） | 免费；具体步骤以它文档为准 |
| Cloudflare **named tunnel** | 完全固定 | 需要 Cloudflare 账号 + 一个域名 |
| ngrok 账号（白嫖静态域名） | 固定 | 需要账号；以后台当前政策为准 |
| 小 VPS + Nginx/Caddy | 完全固定、最稳 | 月付；适合答辩演示 |

**判断标准**：只要需要在演示/长期使用中填一次就永久有效 → 必须上固定方案；临时调试用 quick tunnel 即可。
**注意**：换隧道方案后**后台地址要重填一次**（因为域名变了）。

## 坑（都是踩出来的）

1. **免费 quick tunnel 会自己重连换域名** → 后台里那条旧地址会在某刻变成 502。症状是"用户说发了消息没反应"。**先探地址存活**再查其他。
2. **加密密钥留空**：设了就是密文，标准库解不了。
3. **监听只在启动时读 `.env` / `config.yaml`**：改了密钥或开关必须重启监听。
4. **白名单在数据库表里**（`feishu_paired_users`），不在 env；改完**无需重启**（每次请求重查）。
5. **默认策略 pairing 会静默拒绝**：没放行的人只会收到配对提示；若出站开关也关着，就表现为"发了消息什么都没发生"。
6. **隧道 URL 必须带路径** `/feishu/webhook`，漏了会 404。
7. **隧道进程的输出要 tee 到文件**，否则取不到 URL（输出预览会截掉 banner）。
8. **别同时起两条隧道指同一端口**：多一份公网暴露、日志还会互相干扰。停的时候按 PID 精准停。
9. **Windows/git-bash 下 `taskkill //F //PID` 会被拒**（报无效参数）→ 用 `MSYS_NO_PATHCONV=1 taskkill /F /PID <pid>`。
10. **不要把 `//F` 之类 MSYS 转义写法用在原生程序参数上**；路径带空格/括号要整体加引号。
11. **SQLite 时间列是 ISO 字符串时，别拿整数比大小**：`where created_at > <epoch>` 会把所有行都算成"最近"（字符串永远大于整数），差点据此误报"事件进来了"。要比就算字符串或换成真实时间戳。
12. **提交代码时不要动运行中的进程与 `config.yaml`**：那会把刚跑通的链路弄断（`.env`/`config.yaml`/`run/` 都不在 git 里）。
13. **飞书后台的操作无法用 API 代替**：创建应用、勾事件、填回调都得人工点；能自动化的只是"拿到链接后在飞书里确认"那段（设备码轮询）。

## 与 Hermes 做法的对照（写论文/写文档可用）

Hermes 的便捷（默认长连接、零公网）来自**按需安装官方 SDK `lark-oapi` + websockets/aiohttp**（`plugins/platforms/feishu/adapter.py`，lazy_deps 机制）。若项目有**严格依赖白名单**，就走本文这条：标准库 http.server + HMAC 校验 + 内网穿透。
**这是一组真实取舍：依赖最小化 vs 接入便捷**，两者各自付代价（前者要处理隧道稳定性，后者要引入并维护第三方 SDK）。

## 相关文件（ScholarScout 作为参考实现）

- 渠道层：`src/scholarscout/channels/feishu.py`（解析/签名/challenge/拦截/回包）
- 监听入口：`scripts/scout.py --feishu-listen`
- 出站：`src/scholarscout/fetch/feishu_send.py`
- 向导：`--setup-feishu`；配对码：`state/pairing.py` + 迁移 `0017`
- 方案文档：`docs/17-飞书渠道接入方案.md`、`docs/18-飞书接入向导迁移方案.md`
- 实测端点：`POST /open-apis/auth/v3/tenant_access_token/internal` → `{code,tenant_access_token,expire}`；`GET /open-apis/bot/v3/info` → `bot.app_name` / `bot.open_id`；发送 `POST /open-apis/im/v1/messages?receive_id_type=chat_id`
