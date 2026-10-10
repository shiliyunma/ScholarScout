"""飞书接入相关的 RPC：`feishu.*`（契约见 docs/01 §3.8）。

分工：**配对码与白名单落在 `state/pairing.py`**（唯一碰 SQL 的地方），这里只做
参数校验 + 中文错误映射 + 读写 `.env` 里的渠道层开关。渠道层（`channels/`）与
CLI 向导都只经这些方法说话（不直接读库）。
"""

from __future__ import annotations

import os
from typing import Any

from scholarscout.fetch import feishu_onboard
from scholarscout.fetch.http import HttpClient
from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.registry import AppContext, _params, method
from scholarscout.state import pairing

#: 连接模式默认值（与 `channels/feishu.py` 的同名默认保持一致；键名照 Hermes `adapter.py:1311`）
DEFAULT_CONNECTION_MODE = "webhook"
#: DM 策略默认值（配对审批）
DEFAULT_DM_POLICY = "pairing"
#: 允许的连接模式
CONNECTION_MODES = ("webhook", "websocket")
#: 允许的 DM 策略（对齐参照体验的三选一：配对审批 / 放行全部 / 仅白名单）
DM_POLICIES = ("pairing", "all", "allowlist")
#: 允许的平台域名（照 Hermes `_ONBOARD_ACCOUNTS_URLS` 的两个键，adapter.py:198-201）
DOMAINS = ("feishu", "lark")
#: 允许的群聊策略（对齐参照体验的二选一）
GROUP_POLICIES = ("mention", "disabled")
#: 群聊策略默认值
DEFAULT_GROUP_POLICY = "mention"


def _env_mode() -> str:
    mode = (os.environ.get("FEISHU_CONNECTION_MODE") or DEFAULT_CONNECTION_MODE).strip().lower()
    return mode if mode in CONNECTION_MODES else DEFAULT_CONNECTION_MODE


def _env_dm_policy() -> str:
    policy = (os.environ.get("FEISHU_DM_POLICY") or DEFAULT_DM_POLICY).strip().lower()
    return policy if policy in DM_POLICIES else DEFAULT_DM_POLICY


def _env_group_policy() -> str:
    policy = (os.environ.get("FEISHU_GROUP_POLICY") or DEFAULT_GROUP_POLICY).strip().lower()
    return policy if policy in GROUP_POLICIES else DEFAULT_GROUP_POLICY


def _domain_param(params: dict[str, Any]) -> str:
    domain = str(params.get("domain") or "feishu").strip().lower()
    if domain not in DOMAINS:
        raise RpcError(Code.INVALID_PARAMS, f"domain 只能是：{'、'.join(DOMAINS)}")
    return domain


def _require_http(ctx: AppContext) -> HttpClient:
    """取出网客户端；没装配就如实报「未配置」，不去直接 import httpx。"""
    if ctx.http is None:
        raise RpcError(Code.NOT_CONFIGURED, "出网客户端未装配（应用上下文缺少 HttpClient）")
    return ctx.http


@method("feishu.status")
def feishu_status(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """飞书接入状态：**只报"是否已配置"与白名单身份，绝不回传任何密钥值**。"""
    app_id_set = bool(os.environ.get("FEISHU_APP_ID", "").strip())
    secret_set = bool(os.environ.get("FEISHU_APP_SECRET", "").strip())
    return {
        "configured": app_id_set and secret_set,
        "app_id_set": app_id_set,
        "secret_set": secret_set,
        "connection_mode": _env_mode(),
        "dm_policy": _env_dm_policy(),
        "listener_enabled": bool(ctx.config.runtime.feishu_listener_enabled),
        "listener_port": int(ctx.config.runtime.feishu_listener_port),
        # P0 不联网探测 bot 名 → 明确给 None，不编
        "bot_name": None,
        "temp_open": pairing.temp_open(ctx.db),
        "pending": pairing.pending_count(ctx.db),
        "allowlist": pairing.approved_users(ctx.db),
    }


@method("feishu.access")
def feishu_access(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """运行时拦截取数口（渠道层专用，**渠道层不读库**，docs/17 §3.4）。

    给一个 `open_id`，回三件渠道层做放行判定需要的最小事实：
    - `approved`：该 open_id 是否在白名单（误匹配一律 False）；
    - `temp_open`：当前是否处于「临时放行」（还没批准任何身份时渠道层先放行）；
    - `dm_policy`：当前 DM 策略（`pairing`/`all`/`allowlist`）。

    **只回判定所需的最小事实**，不回身份明细、不回码 —— 渠道层拿这些就够了。
    `open_id` 缺省/为空按未授权处理（不回 `approved:true`）。
    """
    params = _params(params)
    open_id = str(params.get("open_id") or "").strip()
    return {
        "approved": pairing.is_approved(ctx.db, open_id) if open_id else False,
        "temp_open": pairing.temp_open(ctx.db),
        "dm_policy": _env_dm_policy(),
    }


@method("feishu.dm_policy")
def feishu_dm_policy(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """设置私聊授权策略（写 `.env` 的渠道层开关，键名照参照体验）。"""
    policy = str(params.get("policy") or "").strip().lower()
    if policy not in DM_POLICIES:
        raise RpcError(Code.INVALID_PARAMS, f"policy 只能是：{'、'.join(DM_POLICIES)}")
    from scholarscout import paths, settings_io

    settings_io.write_env(paths.home() / ".env", {"FEISHU_DM_POLICY": policy})
    os.environ["FEISHU_DM_POLICY"] = policy
    if policy == "all":
        pairing.set_temp_open(ctx.db, False)
    return {"ok": True, "policy": policy}


@method("feishu.pairing.create")
def feishu_pairing_create(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """生成一枚配对码（**明文只在此刻可见**），并打开「临时放行」。"""
    params = _params(params)
    user_name = str(params.get("user_name") or "")
    code = pairing.create_code(ctx.db, user_name=user_name)
    if code is None:
        raise RpcError(
            Code.INVALID_PARAMS,
            "暂时不能再生成配对码：待批已达上限（3 枚）或平台处于锁定状态（连续错码 5 次锁 1 小时）",
        )
    return {
        "code": code,
        "code_length": pairing.CODE_LENGTH,
        "ttl_seconds": pairing.CODE_TTL_SECONDS,
        "temp_open": True,
    }


@method("feishu.pairing.claim")
def feishu_pairing_claim(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """把 openid 绑到码上（渠道层收到"把码发给机器人"的那条私信时调）。

    码不对**不报错**，只回 `claimed:false` —— 机器人收到的普通聊天不该被当成 RPC 异常。
    """
    params = _params(params)
    for key in ("code", "open_id"):
        if not str(params.get(key) or "").strip():
            raise RpcError(Code.INVALID_PARAMS, f"缺参数：{key}")
    claimed = pairing.claim_code(
        ctx.db,
        str(params["code"]),
        str(params["open_id"]),
        user_name=str(params.get("user_name") or ""),
    )
    return {"ok": True, "claimed": claimed}


@method("feishu.pairing.verify")
def feishu_pairing_verify(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """校验配对码：成功则**写白名单 + 收回临时放行**（docs/18 §③）。"""
    params = _params(params)
    code = str(params.get("code") or "").strip()
    if not code:
        raise RpcError(Code.INVALID_PARAMS, "缺参数：code")
    result = pairing.verify_code(ctx.db, code)
    if not result.ok:
        raise RpcError(Code.INVALID_PARAMS, _VERIFY_MESSAGES.get(result.reason, "配对失败"))
    return {
        "ok": True,
        "user_id": result.user_id,
        "user_name": result.user_name,
        "allowlist": pairing.approved_users(ctx.db),
        "temp_open": pairing.temp_open(ctx.db),
    }


#: 失败原因 → 中文提示（**要能指导下一步**，不能只说"不行"）
_VERIFY_MESSAGES = {
    "not_found": "配对码不对或不存在（8 位大写字母数字；0/O、1/I 不在字母表里）",
    "expired": "配对码已过期（有效期 1 小时），请重新生成",
    "unclaimed": "还没收到你的 open_id——请先在飞书里把这条码私聊发给机器人，再回来校验",
    "locked_out": "平台因连续错码被临时锁定，请稍后再试",
}


@method("feishu.pairing.list")
def feishu_pairing_list(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """待批与已批准列表（**待批只给 entry_id 与绑定身份，绝不回传码本身**）。"""
    del params
    return {
        "pending": pairing.list_pending(ctx.db),
        "approved": pairing.approved_users(ctx.db),
        "temp_open": pairing.temp_open(ctx.db),
    }


@method("feishu.pairing.revoke")
def feishu_pairing_revoke(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """从白名单移除（误批准要能撤）。"""
    params = _params(params)
    user_id = str(params.get("user_id") or "").strip()
    if not user_id:
        raise RpcError(Code.INVALID_PARAMS, "缺参数：user_id")
    return {"ok": True, "removed": pairing.revoke_user(ctx.db, user_id)}


# --------------------------------------------------------------------- 联网自动建应用（docs/18 §③）

#: `begin` 里 `archetype` 等固定参数的出处（照 Hermes adapter.py:3931-3933）。
#: 这里不复述常量，直接引用 `fetch/feishu_onboard.py` 的定义，避免两处漂移。


@method("feishu.onboard.begin")
def feishu_onboard_begin(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """发起设备码注册：回 `qr_url`（飞书服务端签发的 `verification_uri_complete` + `from`/`tp`）。

    **不伪造设备码**：`qr_url` / `user_code` 都来自飞书接口的真实回包
    （照 Hermes `_begin_registration`，adapter.py:3929-3942）。`from`/`tp` 可配置。
    """
    params = _params(params)
    domain = _domain_param(params)
    from_ = str(params.get("from") or feishu_onboard.DEFAULT_REGISTRATION_FROM)
    tp = str(params.get("tp") or feishu_onboard.DEFAULT_REGISTRATION_TP)
    try:
        begin = feishu_onboard.begin_registration(
            _require_http(ctx), domain=domain, from_=from_, tp=tp
        )
    except feishu_onboard.OnboardError as exc:
        raise RpcError(Code.SERVICE_UNAVAILABLE, str(exc)) from exc
    return {
        "device_code": begin.device_code,
        "qr_url": begin.qr_url,
        "user_code": begin.user_code,
        "interval": begin.interval,
        "expire_in": begin.expire_in,
    }


@method("feishu.onboard.poll")
def feishu_onboard_poll(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """轮询注册结果：成功则**把凭据直接写进 `.env`**，只回非密钥字段。

    **密钥只写不回读**：`client_secret` 由本方法落 `.env`（+ 同步 `os.environ`），
    **绝不放进返回值**；返回值里只有 `app_id`（用户自己的应用标识，非密钥，Hermes 也会打印）
    与 `open_id`。未完成/取消/超时 → `{ok:false, reason}`（不抛异常，便于向导回退手工）。
    """
    params = _params(params)
    device_code = str(params.get("device_code") or "").strip()
    if not device_code:
        raise RpcError(Code.INVALID_PARAMS, "缺参数：device_code")
    domain = _domain_param(params)
    interval = int(params.get("interval") or 5)
    expire_in = int(params.get("expire_in") or 600)
    result = feishu_onboard.poll_registration(
        _require_http(ctx),
        device_code=device_code,
        interval=interval,
        expire_in=expire_in,
        domain=domain,
    )
    if result is None:
        return {"ok": False, "reason": "未在有效期内完成创建（用户取消或超时）"}
    _write_registration_credentials(result)
    return {
        "ok": True,
        "app_id": result.app_id,
        "domain": result.domain,
        "open_id": result.open_id,
    }


def _write_registration_credentials(result: feishu_onboard.RegistrationResult) -> None:
    """把自动创建拿到的凭据写进 `.env`（App Secret 只写不回读）。"""
    from scholarscout import paths, settings_io

    values = {
        "FEISHU_APP_ID": result.app_id,
        "FEISHU_APP_SECRET": result.app_secret,
        "FEISHU_DOMAIN": result.domain,
    }
    settings_io.write_env(paths.home() / ".env", values)
    for key, value in values.items():
        if value:
            os.environ[key] = value
