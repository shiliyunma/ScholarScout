# QQ 通道断线后的自愈与恢复

> 适用：用户报「QQ 发消息你收不到」「机器人不出声」，且日志里没有 inbound。
> 与 SKILL.md「通道静默死亡」一节配套，这里放可复制的判读样本与恢复动作。

## 1. 先定位死亡时刻（30 秒）

```bash
# 状态文件里就有答案：state=disconnected 时，updated_at 即死亡时刻
python -c "import json;d=json.load(open(r'C:/Users/<user>/AppData/Local/hermes/gateway_state.json'));print(d['gateway_state'], d['pid']);print(d['platforms']['qqbot'])"
```

典型值：

```json
{"state": "disconnected", "error_code": null, "error_message": null,
 "updated_at": "2026-09-17T19:14:29.651791+00:00", "needs_attention": false, "retrying_since": null}
```

`updated_at` 是 **UTC**（+08:00 加 8 小时）；`needs_attention: false` 会误导人 —— 适配器已放弃重连，但它不认为自己“需要关注”。

## 2. 日志样本（按顺序出现即是“耗尽重连”）

```
WARNING … Reconnect failed: Failed to get QQ Bot access token: All connection attempts failed
WARNING … WebSocket error: WebSocket closed
INFO    … Reconnecting in 60s (attempt 99)...
WARNING … Reconnect failed: Failed to get QQ Bot access token: All connection attempts failed
INFO    … Reconnecting in 60s (attempt 100)...
ERROR   … Max reconnect attempts reached        ← 到此永久停止
…（之后只有业务侧的 "Not connected — waiting for reconnection (up to 15s)"）
```

`attempt N` 的计数 + 60 秒间隔能反推**断网起点**（`间隔 × N` 回退到开始重试的时刻），比翻更早的日志快。

## 3. 恢复动作

```bash
# 1) 先确认网络回来了（这一步区分「网络还没好」和「网络好了但适配器躺了」）
curl -sS -o /dev/null -w "%{http_code}\n" --max-time 15 https://bots.qq.com/app/getAppAccessToken   # 期望 200（不带参数也会回 200）

# 2) 重启（桌面/CLI 会话里可自行执行；QQ 会话里请交用户）
hermes gateway restart

# 3) 复查（应看到 connected）
tail -25 "$HERMES_HOME/logs/gateway.log" | grep -i "qqbot\|connect"
```

期望日志：`Access token refreshed` → `WebSocket connected to wss://api.sgroup.qq.com/websocket` → `Connected` → `✓ qqbot connected` → `Ready, session_id=…`。

## 4. 出站自测（证明发送侧真的通了）

用 `scripts/qq_dm_test.py`：读 `.env` / `config.yaml` 里的 app_id + client_secret + `QQ_ALLOWED_USERS`，取 token 后 `POST /v2/users/{openid}/messages`。
成功返回形如 `{"id": "ROBOT1.0_…", "timestamp": "…"}`；`40034105` 只对**群**主动消息成立，**私聊主动消息可用**，是唯一能自测的出站通路。

## 5. 防复发（三条，按性价比）

| 方案 | 做法 | 代价 |
|---|---|---|
| 调大重连上限 | `constants.py`: `MAX_RECONNECT_ATTEMPTS = 100 → 5000`，重启生效 | 改的是应用源码；升级可能被覆盖，升级后复查 |
| 用户侧看门狗 | 用户在 **Windows 任务计划程序** 挂每 N 分钟检查 `qqbot.state != connected` 就重启 | 需用户自己搭；agent 不能代建（防护 #30719 明确拦 cron 里的 gateway 生命周期命令） |
| 治网络 | 查路由器/光猫夜间重启、网卡节能（「允许计算机关闭此设备以节约电源」） | 无 |

**不要**建一个「每 N 分钟往 QQ 发心跳」的 cron 来保活：消息本身会打扰用户，而且 15 秒重连窗口只在**有发送动作**时开启，心跳间隔远大于窗口，救不了长时间断网。

## 6. 断网时的连带故障（同一根因，别分开排查）

网络不可达时不止 QQ 适配器受影响，**同时**会出现：
- 定时任务的模型调用失败（`RuntimeError: Connection error.`）→ 晨报/日报静默不送达；
- 定时任务投递失败（`delivery_outcome: failed`）。

所以「晨报没发出来」+「QQ 收不到消息」同时出现时，**先查通道状态和网络，再查任务本身**。用户问「XX 呢（怎么没发）」时，标准动作是：查 `cron/executions.db` 最近执行行的状态列 → 看 `gateway_state.json` → 补发内容并把失败原因写进回复（他要知道是偶发还是坏了）。
