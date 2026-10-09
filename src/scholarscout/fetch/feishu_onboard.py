"""飞书「联网自动建应用」的设备码注册客户端（照 Hermes 的真实接口，docs/18 §③）。

## 它在哪一层 / 为什么要单独一个文件

这是**出网**能力，只能落在 `fetch/`（`docs/03 §2`：出网只走 `fetch/http.py`）。
本模块**不直接 import httpx**，所有请求都经项目的唯一出网口 :class:`HttpClient`
（限速 / 退避 / UA / 超时 / 体积上限全在那一层生效），由 `rpc/feishu.py` 调用后
回给 CLI 向导（`channels/feishu_setup.py`）—— `channels/` 不许 import `fetch`
（`tests/test_layering.py::RULES["channels"]`），所以向导只经 RPC 走这一条路。

## 接口契约（逐条来自本机 Hermes 源码，带 `文件:行号`，不臆造）

源文件 `plugins/platforms/feishu/adapter.py`（4308 行）：

- 注册端点 `POST {accounts_base}/oauth/v1/app/registration`
  （路径常量 `_REGISTRATION_PATH="/oauth/v1/app/registration"` `:203`；
  accounts 基址 `_ONBOARD_ACCOUNTS_URLS = {"feishu":"https://accounts.feishu.cn",
  "lark":"https://accounts.larksuite.com"}` `:198-201`）。
- **请求体是表单**（`application/x-www-form-urlencoded`，不是 JSON）——
  `_post_registration` 用 `urlencode(body)` + `Content-Type: application/x-www-form-urlencoded`
  （`:3899-3904`）。所以走 :meth:`HttpClient.post_form`，不走 `post_json`。
- 三个动作 `action ∈ {init, begin, poll}`：
  - `init`：`{"action":"init"}`，读 `supported_auth_methods` 判断支不支持 `client_secret`
    （`_init_registration` `:3918-3926`）；
  - `begin`：`{"action":"begin", "archetype":"PersonalAgent", "auth_method":"client_secret",
    "request_user_info":"open_id"}`（`_begin_registration` `:3929-3933`），回
    `device_code` 与 `verification_uri_complete`（`:3934-3937`）；Hermes 再给这个 URL
    **追加** `&from=hermes&tp=hermes`（`:3937-3938`）——本项目把 `from`/`tp` 做成可配置参数。
  - `poll`：`{"action":"poll", "device_code": ..., "tp":"ob_app"}`（`:3954-3955`），
    成功回 `client_id` → app_id、`client_secret` → app_secret、`user_info.open_id` → openid
    （`:3971-3977`）；`user_info.tenant_brand == "lark"` 时把域名切到 lark（`:3967-3970`）；
    `error ∈ {access_denied, expired_token}` → 放弃（`:3979-3984`）；其它（如
    `authorization_pending`）继续轮询（`:3985`）。超时由 `expire_in` 兜底（`:3952/3987-3990`）。
- 请求超时 Hermes 用 10s（`_ONBOARD_REQUEST_TIMEOUT_S = 10` `:204`）。
- Bot 名探测：`GET /open-apis/bot/v3/info` 取 `bot.app_name`（`probe_bot` / `_probe_bot_http`
  / `_parse_bot_response`，`:4013-4068`）。

## 两条工程取舍（写清楚，不掩盖）

1. **轮询里 4xx 不细分**：飞书把 `authorization_pending` 当 400 返回，Hermes 靠"解析 4xx 的
   body"区分 pending 与 denial（`:3900/3908-3914`）。本项目的出网口把 4xx 直接抛
   :class:`FetchError`（不携带 body），所以这里**把 4xx 一律当"还没好，继续等"**，
   由 `expire_in` 兜底超时返回 `None`。denial 最终也返回 `None`（与 Hermes 一致），只是慢一点。
2. **轮询不会死循环**：`deadline = clock() + expire_in`，每次失败/未就绪都 `sleep(backoff)`，
   退避有上限（`max_interval_s`）；到点返回 `None`。`clock`/`sleep` 都可注入，桩测不真等。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from scholarscout.fetch.http import FetchError, FetchResult, HttpClient

#: 注册/探测用的 accounts 基址（照 Hermes `_ONBOARD_ACCOUNTS_URLS`，adapter.py:198-201）
ONBOARD_ACCOUNTS_URLS: dict[str, str] = {
    "feishu": "https://accounts.feishu.cn",
    "lark": "https://accounts.larksuite.com",
}
#: 开放平台基址（bot/v3/info 在这上面，照 Hermes `_ONBOARD_OPEN_URLS`，adapter.py:202）
ONBOARD_OPEN_URLS: dict[str, str] = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}
#: 注册路径（照 Hermes `_REGISTRATION_PATH`，adapter.py:203）
REGISTRATION_PATH = "/oauth/v1/app/registration"
#: 请求超时（照 Hermes `_ONBOARD_REQUEST_TIMEOUT_S`，adapter.py:204）
ONBOARD_TIMEOUT_S = 10.0
#: begin 的三个固定参数（照 Hermes `_begin_registration`，adapter.py:3931-3933）
DEFAULT_ARCHETYPE = "PersonalAgent"
DEFAULT_AUTH_METHOD = "client_secret"
DEFAULT_REQUEST_USER_INFO = "open_id"
#: poll 的固定参数（照 Hermes `_poll_registration`，adapter.py:3954）
DEFAULT_POLL_TP = "ob_app"
#: `from`/`tp` 的默认值。Hermes 固定 `hermes`；本项目默认 `scholarscout`，
#: 且**可配置**（源码里这两个参数的真实语义未读到，docs/18 §② 标 unknown）。
DEFAULT_REGISTRATION_FROM = "scholarscout"
DEFAULT_REGISTRATION_TP = "scholarscout"


class OnboardError(Exception):
    """注册流程的协议错误（端点没回 device_code / 回的不是 JSON 对象）。"""


@dataclass(frozen=True)
class RegistrationBegin:
    """`begin` 的结果（字段名与 Hermes 返回值一致，adapter.py:3939-3942）。"""

    device_code: str
    qr_url: str
    user_code: str
    interval: int
    expire_in: int


@dataclass(frozen=True)
class RegistrationResult:
    """`poll` 成功后拿到的凭据（字段来自 adapter.py:3971-3977）。"""

    app_id: str
    app_secret: str
    domain: str
    open_id: str = ""


def registration_endpoint(domain: str = "feishu") -> str:
    """注册端点 URL（accounts 基址 + 路径）。"""
    base = ONBOARD_ACCOUNTS_URLS.get(domain, ONBOARD_ACCOUNTS_URLS["feishu"])
    return f"{base}{REGISTRATION_PATH}"


def _json_object(result: FetchResult) -> dict[str, Any]:
    """把回包解成 JSON 对象；不是对象就抛 :class:`OnboardError`（不静默吞）。"""
    try:
        data = result.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise OnboardError(f"注册接口返回的不是 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise OnboardError(f"注册接口返回的不是 JSON 对象：{type(data).__name__}")
    return data


def init_registration(http: HttpClient, *, domain: str = "feishu") -> list[str]:
    """`action=init`：回 `supported_auth_methods`（照 adapter.py:3918-3926）。"""
    result = http.post_form(registration_endpoint(domain), {"action": "init"})
    payload = _json_object(result)
    methods = payload.get("supported_auth_methods") or []
    if not isinstance(methods, list):
        return []
    return [str(m) for m in methods]


def begin_registration(
    http: HttpClient,
    *,
    domain: str = "feishu",
    from_: str = DEFAULT_REGISTRATION_FROM,
    tp: str = DEFAULT_REGISTRATION_TP,
) -> RegistrationBegin:
    """`action=begin`：取设备码与 `verification_uri_complete`，再追加 `from`/`tp`。

    照 Hermes `_begin_registration`（adapter.py:3929-3942）逐字实现；区别只有
    `from`/`tp` 可配置（Hermes 写死 `hermes`）。
    """
    result = http.post_form(
        registration_endpoint(domain),
        {
            "action": "begin",
            "archetype": DEFAULT_ARCHETYPE,
            "auth_method": DEFAULT_AUTH_METHOD,
            "request_user_info": DEFAULT_REQUEST_USER_INFO,
        },
    )
    payload = _json_object(result)
    device_code = str(payload.get("device_code") or "")
    if not device_code:
        raise OnboardError("飞书注册接口没有返回 device_code（无法继续轮询）")
    qr_url = str(payload.get("verification_uri_complete") or "")
    separator = "&" if "?" in qr_url else "?"
    qr_url += f"{separator}from={from_}&tp={tp}"
    return RegistrationBegin(
        device_code=device_code,
        qr_url=qr_url,
        user_code=str(payload.get("user_code") or ""),
        interval=int(payload.get("interval") or 5),
        expire_in=int(payload.get("expire_in") or 600),
    )


def poll_registration(
    http: HttpClient,
    *,
    device_code: str,
    interval: int = 5,
    expire_in: int = 600,
    domain: str = "feishu",
    tp: str = DEFAULT_POLL_TP,
    clock: Any = time.monotonic,
    sleep: Any = time.sleep,
    max_interval_s: float = 60.0,
) -> RegistrationResult | None:
    """轮询直到用户完成创建（→ :class:`RegistrationResult`）或放弃/超时（→ ``None``）。

    照 Hermes `_poll_registration`（adapter.py:3945-3990）：域名可自动切到 lark；
    `access_denied`/`expired_token` 放弃；其余未就绪就退避后继续。**有超时上限，不死循环。**
    """
    deadline = clock() + expire_in
    current_domain = domain
    domain_switched = False
    backoff = float(max(1, interval))
    while clock() < deadline:
        try:
            result = http.post_form(
                registration_endpoint(current_domain),
                {"action": "poll", "device_code": device_code, "tp": tp},
            )
            payload = _json_object(result)
        except (FetchError, OnboardError):
            # 网络/协议错误：退避后重试（飞书的 pending 也走 400，落在 FetchError 里）
            sleep(min(backoff, max_interval_s))
            backoff = min(backoff * 2, max_interval_s)
            continue

        user_info = payload.get("user_info")
        user_info = user_info if isinstance(user_info, dict) else {}
        if user_info.get("tenant_brand") == "lark" and not domain_switched:
            current_domain = "lark"
            domain_switched = True

        if payload.get("client_id") and payload.get("client_secret"):
            return RegistrationResult(
                app_id=str(payload["client_id"]),
                app_secret=str(payload["client_secret"]),
                domain=current_domain,
                open_id=str(user_info.get("open_id") or ""),
            )

        error = str(payload.get("error") or "")
        if error in {"access_denied", "expired_token"}:
            return None

        sleep(min(backoff, max_interval_s))
        backoff = min(backoff * 2, max_interval_s)
    return None
