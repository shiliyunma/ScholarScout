# Feishu / Lark 通道接入（实测记录）

## 引导式接入（用户在终端实际跑过的流程）

入口是 **`hermes gateway setup`**（不是 `hermes setup`），菜单里选 **`12. Feishu / Lark`**，然后二选一：

- `1. Scan QR code to create a new bot automatically (recommended)`
- `2. Enter existing App ID and App Secret manually`

选 1 后终端打印 `Connecting to Feishu / Lark... done.` 并给出链接（在手机飞书里打开即自动建机器人）：

```
https://open.feishu.cn/page/launcher?user_code=Z2BC-3GAP&from=hermes&tp=hermes
```

- `user_code` 是**飞书服务端签发的**设备码（4 位大写字母数字 + `-` + 4 位），不是本地生成；Hermes 只是把服务端返回的 `verification_uri_complete` 拼上 `&from=hermes&tp=hermes` 再打印（`plugins/platforms/feishu/adapter.py:3937-3938`）。`from`/`tp` 的确切语义未核实。
- 随后终端打印 `Fetching configuration results....` → `✓ Bot created: <名字>` ——**它是在轮询等飞书侧完成，不是让用户再输入**。
- 顺手提示：`Tip: pip install qrcode`（二维码只是可选增强）。

## 授权策略与配对码（关键，否则机器人“没反应”）

默认 DM 策略是 **`pairing`**：未授权用户**不执行任何会话**。设置向导里三选一：`1. Use DM pairing approval (recommended)` / `2. Allow all direct messages` / `3. Only allow listed user IDs`；群聊默认只回 `@提及`。

配对流程（用户在飞书里给机器人发一句话，拿到码，回终端输入）：

```bash
hermes pairing approve feishu KMHSZQXF
# → Approved! User ou_aa206fb174402ae72345a7fe39a51ae2 on feishu can now use the bot~
```

- 配对码 **8 位大写字母数字**，字母表 `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`；**只存 `sha256(salt+code)`**；TTL 3600s；每平台待批 ≤3；错 5 次锁 3600s（`gateway/pairing.py`）。
- 批准后写平台白名单（feishu 写 `FEISHU_ALLOWED_USERS`），写入的身份就是 **`ou_` 开头的用户 openid**。
- 这条“配对码写白名单”比自己造“临时 open + 自己发一条 + 读 channel_directory.json”干净得多——**取用户 id 优先用配对**。

## 连接模式：websocket vs webhook

- **默认 websocket（`FEISHU_CONNECTION_MODE` 不设即 websocket，`adapter.py:1311`）→ 不需要公网地址**；不要一上来就说“必须内网穿透”。
- webhook（`FEISHU_CONNECTION_MODE=webhook`）才需要 `FEISHU_VERIFICATION_TOKEN` / `FEISHU_ENCRYPT_KEY` + **公网可达回调地址**（默认监听 `127.0.0.1:8765`，路径 `/feishu/webhook`，见 `adapter.py:149-151`）。
- 成功日志：`[Feishu] Connected in <mode> mode`（`adapter.py:1446`）；失败会直说 `FEISHU_APP_ID or FEISHU_APP_SECRET not set` / `lark-oapi not installed`。
- 插件是**懒装**的：`install_hint` 提示跑 `hermes setup` 装依赖（`adapter.py:4300-4304`）。

## 直接用凭据验活（不建通道也能测）

```bash
# 1) 取 tenant token（成功即证明 app_id/secret 配对正确）
POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
     body {"app_id":"cli_xxx","app_secret":"..."}
  → {"code":0,"tenant_access_token":"<42字符>","expire":7200}

# 2) 探机器人自己（拿到名字，用于“接入成功”回显）
GET https://open.feishu.cn/open-apis/bot/v3/info   (Authorization: Bearer <token>)
  → {"code":0,"bot":{"app_name":"...","open_id":"ou_..."}}
```

Hermes 自己用的就是 `/open-apis/bot/v3/info`（`adapter.py:3348`）；名字字段是 **`bot.app_name`**。注意区分：`bot.open_id` 是**机器人自己**的 id，**用户** openid（白名单要的）来自配对批准那一步。

## Pitfalls

- **一上来就说“必须公网地址”是错的**：先确认连接模式，websocket 不需要。
- 飞书**没有**“创建自建应用”的公开 API（未核实，但引导流程是 launcher 网页）；程序只能给出链接，后台那一下必须人点。
- 同一个自建应用**只能有一条长连接**：不要拿给 Hermes 用的那个 app 再去跑另一个 websocket 客户端。
- 密钥只进 `.env`；若凭据曾被贴进对话/截图，提醒用户去后台**重置 App Secret**（成本很低）。
