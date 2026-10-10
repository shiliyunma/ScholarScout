"""飞书**出站发送器**：把一轮回答推回飞书（docs/17 §3.7）。

## 它在哪一层 / 为什么单独一个文件

这是**出网**能力，只能落在 `fetch/`（`docs/03 §2`：出网只走 `fetch/http.py`）。
本模块**不直接 import httpx**，所有请求都经项目的唯一出网口 `HttpClient`
（限速 / 退避 / UA / 超时 / 体积上限全在那一层生效）。

**渠道层（`channels/`）不许 import `fetch`**（`tests/test_layering.py::RULES["channels"]`），
所以这里提供的是**渠道层形状**的发送器工厂 :func:`build_sender`（`sender(chat_id, text) -> dict`）；
由**入口层**（`__main__.py` 的 `--feishu-listen`）把它注入 `channels/feishu.py::run_listener`。
渠道层只负责"一轮跑完调 `self._sender(...)`"这一处，不关心它是不是真出网。

## 接口契约（逐条带出处，不臆造）

发消息端点 / 字段（**实测来源**：本机 lark-oapi SDK 与本机 Hermes 适配器）：

- **端点** `POST {open_base}/open-apis/im/v1/messages`
  （SDK `lark_oapi/api/im/v1/model/create_message_request.py:25`：
  `create_message_request.uri = "/open-apis/im/v1/messages"`；HTTP 方法见同文件
  `create_message_request.http_method = HttpMethod.POST`；开放平台基址同
  `fetch/feishu_onboard.py::ONBOARD_OPEN_URLS`）。
- **query** `receive_id_type=chat_id`（SDK `create_message_request.py` 的
  `receive_id_type(...)` 把该值塞进 query；Hermes 亦按 `chat_id` 判定
  `"open_id" if chat_id.startswith("ou_") else "chat_id"`，`plugins/platforms/feishu/adapter.py:3602`）。
- **body**（JSON 对象）：`receive_id` / `msg_type` / `content` / `uuid`
  （SDK `create_message_request_body.py:8-12` 的 `_types`）。其中
  - `msg_type` 取 `"text"`；
  - **`content` 是一个 JSON 字符串**（不是对象）：`"{\"text\": ...}"`（本模块
    :func:`text_content`）；解析侧见 `channels/feishu.py::_text_from_content`；
  - `uuid` 是**幂等键**（SDK 同字段）：带上它，飞书对同一 uuid 去重，重试也不会重复投递。
- **鉴权** 头 `Authorization: Bearer <tenant_access_token>`
  （SDK `create_message_request.token_types = {TENANT, USER}`；Hermes 同一端点
  `self._client.im.v1.message.create`，`adapter.py:3607`）。token **复用**
  `fetch/feishu_onboard.py::fetch_tenant_token`（`POST /open-apis/auth/v3/tenant_access_token/internal`，
  已实测），**不重写**。
- **成功回包**：`{"code": 0, "msg": "success", "data": {"message_id": "om_..."}}`。
  Hermes 取 `response.data.message_id`（`adapter.py:3614-3616 / :3631`）；本模块按同形状解析。

## 两条工程口径（写清，不掩盖）

1. **失败明确、绝不抛**：取 token 失败 / 网络错误 / `code != 0` 都回 :class:`SendOutcome`
   （`ok=False` + `error`），**不抛异常** —— 渠道层的发送发生在"一轮跑完之后"，
   一次发送失败不该把会话或回调处理搞崩（`channels/feishu.py::handle` 也会兜一层）。
2. **发送不缓存**：`post_json(..., cache=False)`。发送是**写操作**，缓存会把"这次没发出去"
   钉死成"发过了"；而且同一段文本要能重复发（用户重问同一句）。
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from scholarscout.fetch.feishu_onboard import ONBOARD_OPEN_URLS, fetch_tenant_token
from scholarscout.fetch.http import FetchError, HttpClient

logger = logging.getLogger(__name__)

#: 发送消息路径（出处见模块顶注：SDK `create_message_request.py:25`）
SEND_MESSAGE_PATH = "/open-apis/im/v1/messages"
#: `receive_id_type`（本模块按 chat_id 发；形如 `oc_...` 的会话 id）
RECEIVE_ID_TYPE_CHAT = "chat_id"
#: 文本消息类型
MSG_TYPE_TEXT = "text"
#: 凭据环境变量名（键名照 Hermes `plugin.yaml:13-23` 与 `channels/feishu.py::credentials_from_env`）
APP_ID_ENV = "FEISHU_APP_ID"
APP_SECRET_ENV = "FEISHU_APP_SECRET"
#: 默认平台域名（feishu / lark，两套基址在 `fetch/feishu_onboard.py::ONBOARD_OPEN_URLS`）
DEFAULT_DOMAIN = "feishu"


@dataclass(frozen=True)
class SendOutcome:
    """一次发送的结果。`ok` 为真时 `message_id` 是飞书返回的消息 id。"""

    ok: bool
    message_id: str = ""
    code: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        """转成渠道层 `Sender` 约定的 dict 形状。"""
        return {
            "ok": self.ok,
            "message_id": self.message_id,
            "code": self.code,
            "error": self.error,
        }


def send_message_endpoint(domain: str = DEFAULT_DOMAIN) -> str:
    """发消息端点（开放平台基址 + `SEND_MESSAGE_PATH`）。未知域名回退 feishu（不抛）。"""
    base = ONBOARD_OPEN_URLS.get(domain, ONBOARD_OPEN_URLS["feishu"])
    return f"{base}{SEND_MESSAGE_PATH}"


def send_message_url(
    domain: str = DEFAULT_DOMAIN, *, receive_id_type: str = RECEIVE_ID_TYPE_CHAT
) -> str:
    """带 query 的完整发送 URL（`post_json` 只认 URL 本身带的 query）。"""
    return f"{send_message_endpoint(domain)}?receive_id_type={receive_id_type}"


def text_content(text: str) -> str:
    """飞书 `content` 字段是**一个 JSON 字符串**（`{"text": "..."}`），不是 JSON 对象。"""
    return json.dumps({"text": text}, ensure_ascii=False)


def credentials_from_env() -> tuple[str, str]:
    """从环境变量取 `(app_id, app_secret)`（`.env` 由 `config.load_dotenv` 载入 `os.environ`）。"""
    return (
        os.environ.get(APP_ID_ENV, "").strip(),
        os.environ.get(APP_SECRET_ENV, "").strip(),
    )


def send_text(
    http: HttpClient,
    *,
    app_id: str,
    app_secret: str,
    chat_id: str,
    text: str,
    domain: str = DEFAULT_DOMAIN,
) -> SendOutcome:
    """发一条文本消息到 `chat_id`。**失败回 `SendOutcome(ok=False)`，不抛异常。**

    流程：`fetch_tenant_token`（复用，已实测）→ `POST /open-apis/im/v1/messages?receive_id_type=chat_id`。
    任何一步失败（取 token 失败 / 传输错误 / 回包非 JSON / `code != 0`）都返回带 `error` 的结果。
    """
    if not (app_id and app_secret):
        return SendOutcome(ok=False, error="缺凭据：FEISHU_APP_ID / FEISHU_APP_SECRET 未配置")
    if not (chat_id or "").strip():
        return SendOutcome(ok=False, error="缺 chat_id：没有可发送的目标")

    token = fetch_tenant_token(http, app_id=app_id, app_secret=app_secret, domain=domain)
    if token is None:
        return SendOutcome(ok=False, error="取 tenant_access_token 失败（凭据不对或网络错误）")

    body: dict[str, Any] = {
        "receive_id": chat_id,
        "msg_type": MSG_TYPE_TEXT,
        "content": text_content(text),
        # 幂等键（SDK 同字段）：失败重发同一条不会重复投递
        "uuid": uuid.uuid4().hex,
    }
    try:
        result = http.post_json(
            send_message_url(domain),
            body,
            accept="application/json",
            cache=False,
            headers={"Authorization": f"Bearer {token.token}"},
        )
    except FetchError as exc:
        return SendOutcome(ok=False, error=f"发送请求失败（{exc.kind}）：{exc}")

    try:
        payload = json.loads(result.text)
    except (json.JSONDecodeError, ValueError):
        return SendOutcome(ok=False, error=f"发送接口返回的不是 JSON：{result.text[:200]!r}")
    if not isinstance(payload, Mapping):
        return SendOutcome(ok=False, error="发送接口返回的不是 JSON 对象")

    code = int(payload.get("code") or 0)
    if code != 0:
        return SendOutcome(ok=False, code=code, error=f"飞书返回 code={code}：{payload.get('msg')}")
    data = payload.get("data")
    message_id = ""
    if isinstance(data, Mapping):
        message_id = str(data.get("message_id") or "")
    return SendOutcome(ok=True, code=0, message_id=message_id)


def _no_network_sender(reason: str) -> Callable[[str, str], dict[str, Any]]:
    """「不出网」发送器：只告警、只回错误 —— **默认关 / 无凭据时用它**，绝不发任何外网。"""

    def _sender(chat_id: str, text: str) -> dict[str, Any]:
        logger.warning("[Feishu] 出站发送未启用（%s）：回复未发往飞书 chat_id=%s", reason, chat_id)
        return {"ok": False, "message_id": "", "code": 0, "error": f"未配置发送器：{reason}"}

    return _sender


def build_sender(
    http: HttpClient | None,
    *,
    enabled: bool = False,
    app_id: str = "",
    app_secret: str = "",
    domain: str = DEFAULT_DOMAIN,
) -> Callable[[str, str], dict[str, Any]]:
    """造一个**渠道层形状**的发送器 `sender(chat_id, text) -> dict`。

    - `enabled=False`（**默认**，对应 `runtime.feishu_reply_enabled=false`）→ 返回的发送器
      **一次外网都不发**，只告警并回 `{"ok": false, "error": ...}`（「默认关下不得发任何外网」）；
    - 凭据缺任一个、或没装配出网客户端 → 同样**不发外网**（只是原因不同）；
    - 否则每次调用走飞书 REST 发文本；**任何异常都被吞掉**并记日志（不崩会话）。
    """
    resolved_id = (app_id or "").strip() or os.environ.get(APP_ID_ENV, "").strip()
    resolved_secret = (app_secret or "").strip() or os.environ.get(APP_SECRET_ENV, "").strip()

    if not enabled:
        return _no_network_sender("未开启出站发送（runtime.feishu_reply_enabled=false）")
    if not (resolved_id and resolved_secret and http is not None):
        reason = (
            "出网客户端未装配" if http is None else "缺凭据（FEISHU_APP_ID / FEISHU_APP_SECRET）"
        )
        return _no_network_sender(reason)

    def _send(chat_id: str, text: str) -> dict[str, Any]:
        try:
            outcome = send_text(
                http,
                app_id=resolved_id,
                app_secret=resolved_secret,
                chat_id=chat_id,
                text=text,
                domain=domain,
            )
        except Exception as exc:  # 兜底：发送失败绝不把会话/回调搞崩
            logger.warning("[Feishu] 出站发送异常（已吞）：chat_id=%s err=%s", chat_id, exc)
            return {
                "ok": False,
                "message_id": "",
                "code": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        if outcome.ok:
            logger.info("[Feishu] 已发送回复 chat_id=%s message_id=%s", chat_id, outcome.message_id)
        else:
            logger.warning("[Feishu] 发送失败 chat_id=%s：%s", chat_id, outcome.error)
        return outcome.as_dict()

    return _send
