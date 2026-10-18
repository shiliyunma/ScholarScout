"""渠道中心统一命名空间的 RPC：`channel.*`（契约见 docs/21 §5.1）。

## 为什么另开一套而不是把 `feishu.*` 改名

`feishu.*` 已经把"状态 / 配对 / 探测"跑通了（`docs/18 §⑤`）。新增 `channel.*` 是**把 platform
提成参数**，不是重写：`feishu.*` 在本模块之上原样保留（双写期，见 `docs/01 §3.8`），
`docs/21 §5.1` 里那几条只对飞书有意义的（`guide` / `start` / `stop` / `test` / `set_credentials`）
在 `rpc/feishu.py` 里补了同名别名，指向这里的同一份实现。

## 本轮只做飞书

微信 / QQ **本轮不写任何通道代码**（`docs/21 §8` 的第 8/9 步是从零接）。因此
`platform` 只认 `feishu`，传别的值**明确报错**（不假装支持）：这是用户拍板的范围。

## 四条硬约定（都有测试钉住）

1. **只写不回读**：`channel.set_credentials` 只调 `settings_io.write_env`（原子写 `.env`），
   回包**只有布尔 + 尾 4 位**（`written` 是键名，不是值）；任何返回值里都搜不到密钥全文。
2. **不认识的键直接报错**：白名单之外的凭据键一律 `INVALID_PARAMS`（静默忽略 = 用户以为改了、其实没改）。
3. **没在跑时 `stop` 不假装成功**：回 `{ok: false, reason: "not_running"}`（照 `chat.cancel` 的既有铁律）。
4. **平台硬限如实报**：`platform_limits` 把 `docs/21 §2.1` 的结论做成机器可读摘要
   （没有重置 Secret 的 API、发布要人工点…），界面据此显示"哪条做不了"。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from typing import Any

from scholarscout import paths, settings_io
from scholarscout.channels import feishu as feishu_channel
from scholarscout.channels import feishu_ws, supervisor
from scholarscout.fetch import feishu_onboard, feishu_send
from scholarscout.fetch.http import HttpClient
from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.registry import AppContext, _params, method

#: 本轮支持的平台（与 `channels/supervisor.PLATFORMS` 同源，**只有飞书**）
PLATFORMS: tuple[str, ...] = supervisor.PLATFORMS
#: 平台 → 展示名
LABELS: dict[str, str] = {"feishu": "飞书"}
#: 平台 → 允许写入的凭据键（逻辑名 → `.env` 键名）。**白名单之外一律报错**。
CREDENTIAL_KEYS: dict[str, dict[str, str]] = {
    "feishu": {"app_id": "FEISHU_APP_ID", "app_secret": "FEISHU_APP_SECRET"},
}
#: 允许的域名（飞书 / Lark）
DOMAINS: tuple[str, ...] = ("feishu", "lark")

#: 飞书后台的人工步骤（**文案沿用 `docs/17 §3.10`**：四步，且**不出现"填回调地址"这一步**）。
#: `probe` 说明这一步的"完成"是靠什么探测出来的 —— 探测不到的一律如实标 `manual`。
FEISHU_STEPS: tuple[dict[str, str], ...] = (
    {
        "id": "create_app",
        "title": "① 建「自建应用」，拿到 App ID / App Secret",
        "detail": "点右边的按钮打开飞书开放平台。可以点「自动创建应用」让飞书签发凭据"
        "（自动写入 .env，界面只显示「已配置」）；也可以手工建应用后把两个值填进来。",
        "probe": "credentials",
    },
    {
        "id": "enable_bot",
        "title": "② 给应用开启「机器人」能力",
        "detail": "在应用详情里找到「应用能力 / 添加应用能力」→ 添加机器人。"
        "完成状态按“收到过私聊”判定：探测不到时如实显示未完成。",
        "probe": "message_received",
    },
    {
        "id": "subscribe_event",
        "title": f"③ 事件订阅勾选 {feishu_channel.RECEIVE_EVENT}",
        "detail": "「事件与回调 → 事件配置」里订阅「接收消息」事件。"
        "完成状态同样按“收到过消息”判定（收得到就说明订阅生效）。",
        "probe": "message_received",
    },
    {
        "id": "no_callback",
        "title": "④ 不用填回调地址",
        "detail": "长连接由本机**主动外连**飞书服务器，飞书侧没有公网回调要访问 —— 这一步没有动作。",
        "probe": "none",
    },
    {
        "id": "start_channel",
        "title": "⑤ 回应用里点「启动」",
        "detail": "启动后渠道在独立子进程里跑：关掉窗口也继续收消息；"
        "停止请点「停止」（没在跑时会明确告诉你“当前未运行”）。",
        "probe": "state",
    },
)


def _platform(params: dict[str, Any]) -> str:
    name = supervisor.normalize_platform(params.get("platform"))
    if name not in PLATFORMS:
        raise RpcError(
            Code.INVALID_PARAMS,
            f"platform 只能是 {'、'.join(PLATFORMS)}（本轮只做飞书；微信/QQ 见 docs/21 §8 后续步骤）",
        )
    return name


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _tail(secret: str) -> str:
    """密钥尾 4 位（脱敏展示用）。**少于 4 位就返回全长的位数提示，绝不补齐**。"""
    text = (secret or "").strip()
    return text[-4:] if len(text) >= 4 else ("" if not text else "****")


def _credentials_view(platform: str) -> dict[str, Any]:
    """凭据的**只读视图**：两个布尔 + 尾 4 位（照 docs/21 §6 的"永不回读"口径）。"""
    keys = CREDENTIAL_KEYS[platform]
    app_id = _env(keys["app_id"])
    secret = _env(keys["app_secret"])
    return {
        "app_id_set": bool(app_id),
        "secret_set": bool(secret),
        "tail": _tail(secret),
    }


def _configured(platform: str) -> bool:
    view = _credentials_view(platform)
    return bool(view["app_id_set"] and view["secret_set"])


def _domain(params: dict[str, Any]) -> str:
    domain = str(params.get("domain") or "feishu").strip().lower()
    if domain not in DOMAINS:
        raise RpcError(Code.INVALID_PARAMS, f"domain 只能是：{'、'.join(DOMAINS)}")
    return domain


def _require_http(ctx: AppContext) -> HttpClient:
    if ctx.http is None:
        raise RpcError(Code.NOT_CONFIGURED, "出网客户端未装配（应用上下文缺少 HttpClient）")
    return ctx.http


def _reply_enabled(ctx: AppContext) -> bool:
    """出站开关（`runtime.feishu_reply_enabled`，**默认关**：关着一次外网都不发）。"""
    runtime = getattr(ctx.config, "runtime", None)
    return bool(getattr(runtime, "feishu_reply_enabled", False))


def _platform_limits(ctx: AppContext, platform: str) -> dict[str, Any]:
    """平台硬限的机器可读摘要（`docs/21 §2.1` 的结论；界面直接显示"哪条不能做"）。"""
    del platform
    return {
        # F1：重置 App Secret **没有** API，只能去后台人工重置
        "secret_reset_api": False,
        "secret_reset_url": f"{feishu_channel.open_entry_url()}/app",
        # F2：设备码注册是**半自动**（链接由飞书签发，"点一下确认"仍由人做）
        "auto_create_app": "semi_automatic",
        # F3：开机器人能力 / 勾事件 / 发布，都只能在后台人工点
        "bot_capability_api": False,
        "publish_version_api": False,
        # F4：连接能程序化起，**断开没有公开接口**（进程退出即断开）
        "connection_start_api": True,
        "connection_stop_api": False,
        # 长连接：不需要公网地址 / 回调 URL / 隧道
        "callback_url_required": False,
        "public_url_required": False,
        # 出站发送受项目开关控制（默认关）
        "reply_enabled": _reply_enabled(ctx),
        "dm_only": True,  # 项目口径：只私聊不发群（docs/20 §6.3），**是决策不是平台硬限**
    }


# ------------------------------------------------------------------ 只读接口


@method("channel.list")
def channel_list(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """每张卡片的一行数据。**不含任何密钥值**（只有布尔与尾号）。"""
    del params
    channels: list[dict[str, Any]] = []
    for platform in PLATFORMS:
        snap = supervisor.snapshot(platform)
        channels.append(
            {
                "platform": platform,
                "label": LABELS.get(platform, platform),
                "configured": _configured(platform),
                "enabled": bool(snap["running"]),
                "state": snap["state"],
                "last_error": snap["last_error"],
            }
        )
    return {"channels": channels, "platforms": list(PLATFORMS)}


@method("channel.status")
def channel_status(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """单个平台的完整状态行（凭据 / 进程 / 连接 / 重连 / 最后消息 / 最后错误 / SDK）。"""
    platform = _platform(_params(params))
    snap = supervisor.snapshot(platform)
    view = _credentials_view(platform)
    sdk_ready = feishu_ws.sdk_available()
    detail = ""
    if not sdk_ready:
        detail = feishu_ws.probe_sdk()[1]
    return {
        "platform": platform,
        "label": LABELS.get(platform, platform),
        "configured": bool(view["app_id_set"] and view["secret_set"]),
        "credentials": view,
        "state": snap["state"],
        "connected": bool(snap["connected"]),
        "reconnects": int(snap["reconnects"]),
        "last_message_at": snap["last_message_at"],
        "last_message_from": snap["last_message_from"],
        "last_error": snap["last_error"],
        "sdk_ready": sdk_ready,
        "sdk_detail": detail,
        "pid": snap["pid"],
        "log_path": snap["log_path"],
        "reply_enabled": _reply_enabled(ctx),
        "platform_limits": _platform_limits(ctx, platform),
    }


@method("channel.guide")
def channel_guide(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """申请过程引导页：逐条人工步骤 + 每步的可点后台深链 + 后端探测出的完成状态。

    `done` 的判据写在每一步的 `probe` 里（`credentials` / `message_received` / `state` / `none`），
    **探测不到的一律如实显示未完成**，不假装勾上。
    """
    platform = _platform(_params(params))
    if platform != "feishu":  # pragma: no cover - PLATFORMS 目前只有飞书
        raise RpcError(Code.INVALID_PARAMS, "本轮只做飞书引导页")
    view = _credentials_view(platform)
    snap = supervisor.snapshot(platform)
    sdk_ready = feishu_ws.sdk_available()
    received = bool(snap["last_message_at"])
    console = f"{feishu_channel.open_entry_url()}/app"

    steps: list[dict[str, Any]] = []
    for spec in FEISHU_STEPS:
        probe = spec["probe"]
        if probe == "credentials":
            done = bool(view["app_id_set"] and view["secret_set"])
        elif probe == "message_received":
            done = received
        elif probe == "state":
            done = snap["state"] == "running"
        else:
            done = True  # "不用填回调地址"这一步本来就没有动作
        step: dict[str, Any] = {
            "id": spec["id"],
            "title": spec["title"],
            "detail": spec["detail"],
            "done": done,
            "probe": probe,
        }
        # 深链：三步后台动作都给（同一个开发者后台），"不用填回调"这一步也给（去看一眼即可），
        # 最后一步是应用内动作，没有后台页面可开。
        if spec["id"] != "start_channel":
            step["deeplink"] = console
        steps.append(step)

    blockers: list[str] = []
    if not (view["app_id_set"] and view["secret_set"]):
        blockers.append("凭据未配置：填 App ID / App Secret，或点「自动创建应用」")
    if not sdk_ready:
        blockers.append(feishu_ws.probe_sdk()[1])
    if snap["state"] != "running":
        blockers.append("渠道未启动：点「启动」起独立子进程（关窗后仍会继续收消息）")
    if snap["last_error"]:
        blockers.append(f"最后一次错误：{snap['last_error']}")
    return {
        "platform": platform,
        "steps": steps,
        "sdk_ready": sdk_ready,
        "credentials_configured": bool(view["app_id_set"] and view["secret_set"]),
        "state": snap["state"],
        "blockers": blockers,
    }


@method("channel.report_state")
def channel_report_state(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """渠道**子进程**的状态上报口 —— 子进程不写状态文件，**父进程是唯一写者**。

    为什么开这一条（真机 bug 的根治手段）：`run/channels/<platform>.state.json` 原本被两个进程
    写（父进程的 `start`/`stop`/桥线程收尾 + 子进程自己）。Windows 上 `os.replace` 在目标被
    另一个进程的句柄打开着时瞬时 `PermissionError: [Errno 13]`，而进程内锁跨不了进程 ——
    症状就是"偶发、单跑不复现"。让子进程改走这条 RPC，写者只剩父进程一个。

    - `patch` 必须是对象；`state` 若给了必须落在 `supervisor.STATES` 里
      （拼错的状态值会让界面把 error 显示成"未连接"，静默失真比报错更难查）；
    - **回包只有 `ok` / `platform` / `state`**：不含任何凭据（父子两侧都不需要）；
    - 顺手把 `channel.state` 通知推给界面，状态点跟手更新。
    """
    params = _params(params)
    platform = _platform(params)
    patch = params.get("patch")
    if not isinstance(patch, dict):
        raise RpcError(Code.INVALID_PARAMS, "缺参数：patch（状态字段对象）")
    state = patch.get("state")
    if state is not None and str(state) not in supervisor.STATES:
        raise RpcError(
            Code.INVALID_PARAMS,
            f"patch.state 只能是：{'、'.join(supervisor.STATES)}（拿到 {state!r}）",
        )
    supervisor.report_state(platform, patch)
    snap = supervisor.snapshot(platform)
    supervisor.emit_state(
        ctx,
        {"platform": platform, "state": snap["state"], "connected": bool(snap["connected"])},
    )
    return {"ok": True, "platform": platform, "state": snap["state"]}


# ------------------------------------------------------------------ 凭据写入


@method("channel.set_credentials")
def channel_set_credentials(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """把凭据写进 `.env`（原子写）。**只写不回读**：回包只有布尔 + 尾 4 位。

    - 白名单之外的键 → **直接报错**（不静默忽略）；
    - 传空串 = 清除该键（从 `.env` 删行 + 清进程环境）；
    - `restart_required`：渠道**正在跑**时为真（配置只在子进程启动时读，改了要重启）。
    """
    del ctx
    params = _params(params)
    platform = _platform(params)
    keys = CREDENTIAL_KEYS[platform]
    fields = params.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise RpcError(Code.INVALID_PARAMS, '缺参数：fields（形如 {"app_id": "cli_xxx"}）')
    unknown = sorted(str(key) for key in fields if str(key) not in keys)
    if unknown:
        raise RpcError(
            Code.INVALID_PARAMS,
            f"不认识的凭据键：{'、'.join(unknown)}（{platform} 只接受：{'、'.join(sorted(keys))}）",
        )

    dotenv = paths.home() / ".env"
    writes: dict[str, str] = {}
    cleared: list[str] = []
    for logical, value in fields.items():
        env_name = keys[str(logical)]
        text = "" if value is None else str(value).strip()
        if text:
            writes[env_name] = text
            os.environ[env_name] = text
        else:
            # 空串 = 清除：**删行**（留一个 `KEY=` 会让人以为还配着什么）
            settings_io.drop_env_key(dotenv, env_name)
            os.environ.pop(env_name, None)
            cleared.append(env_name)
    if writes:
        settings_io.write_env(dotenv, writes)

    view = _credentials_view(platform)
    running = bool(supervisor.snapshot(platform)["running"])
    return {
        "ok": True,
        "platform": platform,
        "written": sorted(writes),
        "cleared": sorted(cleared),
        "configured": view,
        "restart_required": running,
        "note": "已写入 .env（原子写）；值不回读、不回显"
        + ("；渠道正在跑，改完请点「重启」或停止后重新启动" if running else ""),
    }


# ------------------------------------------------------------------ 自检 / 出站


def _probe_bot(ctx: AppContext, platform: str, domain: str) -> dict[str, Any]:
    """联网探测机器人名与 token 有效期。**失败也回结构完整的结果**（不抛异常）。"""
    keys = CREDENTIAL_KEYS[platform]
    app_id = _env(keys["app_id"])
    app_secret = _env(keys["app_secret"])
    if not (app_id and app_secret):
        return {
            "ok": False,
            "error": "凭据未配置：先填 App ID / App Secret（或点「自动创建应用」）",
        }
    if ctx.http is None:
        return {"ok": False, "error": "出网客户端未装配，无法联网探测"}
    probe = feishu_onboard.probe_bot(ctx.http, app_id=app_id, app_secret=app_secret, domain=domain)
    if probe is None:
        return {"ok": False, "error": "探测失败（网络错误或 code!=0）—— 不影响接入，可稍后重试"}
    return {
        "ok": True,
        "bot_name": probe.app_name,
        "bot_open_id": probe.open_id,
        "token_expire": probe.token_expire,
        "error": None,
    }


def _send_text(
    ctx: AppContext, platform: str, chat_id: str, text: str, domain: str
) -> dict[str, Any]:
    """发一条文本（经项目统一的出站层；**默认关**时一次外网都不发）。"""
    keys = CREDENTIAL_KEYS[platform]
    sender = feishu_send.build_sender(
        ctx.http,
        enabled=_reply_enabled(ctx),
        app_id=_env(keys["app_id"]),
        app_secret=_env(keys["app_secret"]),
        domain=domain,
    )
    outcome = sender(chat_id, text)
    return (
        outcome if isinstance(outcome, dict) else {"ok": False, "error": "发送器返回了非字典结果"}
    )


@method("channel.test")
def channel_test(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """自检：`probe` = 只探测凭据/连接（联网取机器人名）；`send` = 真发一条测试消息。

    失败**不抛异常**（照 `feishu.probe` 的非阻塞口径）：界面要能显示原因而不是整块卡片消失。
    """
    params = _params(params)
    platform = _platform(params)
    mode = str(params.get("mode") or "probe").strip().lower()
    if mode not in ("probe", "send"):
        raise RpcError(Code.INVALID_PARAMS, "mode 只能是：probe、send")
    domain = _domain(params)
    started = time.perf_counter()

    if mode == "probe":
        result = _probe_bot(ctx, platform, domain)
        latency_ms = int((time.perf_counter() - started) * 1000)
        payload: dict[str, Any] = {
            "ok": bool(result["ok"]),
            "mode": mode,
            "platform": platform,
            "latency_ms": latency_ms,
            "bot_name": result.get("bot_name") or "unknown",
            "error": result.get("error"),
        }
        if result.get("token_expire"):
            payload["token_expire"] = result["token_expire"]
        return payload

    chat_id = str(params.get("to") or params.get("chat_id") or "").strip()
    text = str(params.get("text") or "ScholarScout 渠道自检消息").strip()
    if not chat_id:
        raise RpcError(Code.INVALID_PARAMS, "mode=send 时必须给 to（目标 chat_id）")
    outcome = _send_text(ctx, platform, chat_id, text, domain)
    message_id = str(outcome.get("message_id") or "")
    return {
        "ok": bool(outcome.get("ok")),
        "mode": mode,
        "platform": platform,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "message_id_tail": _tail(message_id),
        "error": outcome.get("error"),
    }


@method("channel.send")
def channel_send(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """渠道**子进程**的出站口：子进程不出网（`docs/21 §4`），回复经这里由主进程发。

    回包只有 `ok` / `message_id`（尾号在 `channel.test` 里给）/ `code` / `error` —— 不含任何凭据。
    """
    params = _params(params)
    platform = _platform(params)
    chat_id = str(params.get("chat_id") or "").strip()
    text = str(params.get("text") or "").strip()
    if not chat_id or not text:
        raise RpcError(Code.INVALID_PARAMS, "缺参数：chat_id / text")
    outcome = _send_text(ctx, platform, chat_id, text, _domain(params))
    return {
        "ok": bool(outcome.get("ok")),
        "platform": platform,
        "message_id": str(outcome.get("message_id") or ""),
        "code": int(outcome.get("code") or 0),
        "error": outcome.get("error"),
    }


# ------------------------------------------------------------------ 起 / 停


@method("channel.start")
def channel_start(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """起渠道**子进程**（幂等：已在跑回 `already_running`，绝不起第二个实例）。"""
    platform = _platform(_params(params))
    return supervisor.start(ctx, platform)


@method("channel.stop")
def channel_stop(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """停渠道子进程。**没在跑就如实回 `{ok: false, reason: "not_running"}`**，不假装成功。"""
    platform = _platform(_params(params))
    result = supervisor.stop(platform)
    if result.get("ok"):
        supervisor.emit_state(ctx, {"platform": platform, "state": "stopped"})
    return result


# ------------------------------------------------------------------ 兼容别名用的薄封装


def alias_targets() -> dict[str, Callable[..., Any]]:
    """给 `rpc/feishu.py` 的双写期别名用（`feishu.start` → `channel.start` 等）。

    返回**函数本身**而不是再写一遍逻辑：别名与主名行为一致才是别名，抄一份就是两套。
    """
    return {
        "guide": channel_guide,
        "test": channel_test,
        "set_credentials": channel_set_credentials,
        "start": channel_start,
        "stop": channel_stop,
        "status": channel_status,
    }


def debug_dump(result: Any) -> str:  # pragma: no cover - 仅供手工排障
    return json.dumps(result, ensure_ascii=False, indent=2)
