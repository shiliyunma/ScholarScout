"""飞书出站发送器的桩测（**不真联网**，在 `fetch/http.py` 那层用 MockTransport 打桩）。

覆盖 docs/17 §3.7「四种桩测」：**成功回包 / 非零 code / 网络异常 / 无凭据**，全用真实回包形状。

真实回包形状的出处（本机实测 / SDK）：
- 取 token：`{"code":0,"msg":"ok","tenant_access_token":"<42字符>","expire":7200}`
  （`fetch/feishu_onboard.py::fetch_tenant_token`，已实测）；
- 发消息成功：`{"code":0,"msg":"success","data":{"message_id":"om_..."}}`
  （SDK `lark_oapi/api/im/v1/model/create_message_response_body.py` 的 `data.message_id`；
  Hermes 取 `response.data.message_id`，`plugins/platforms/feishu/adapter.py:3614-3616 / :3631`）。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from scholarscout.fetch import feishu_send
from scholarscout.fetch.http import HttpClient

#: 实测的取 token 回包
TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
TOKEN_OK = {"code": 0, "msg": "ok", "tenant_access_token": "t-" + "a" * 40, "expire": 7200}
#: 发消息成功回包（真实形状：code/msg/data.message_id）
MESSAGE_OK = {"code": 0, "msg": "success", "data": {"message_id": "om_sent_0001"}}


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> HttpClient:
    """把出网口限定在 MockTransport 上（不真联网、不查 robots）。"""
    return HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))


def _handler(
    seen: list[httpx.Request], message_response: httpx.Response | Callable[[], httpx.Response]
) -> Callable:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "tenant_access_token" in str(request.url):
            return httpx.Response(200, json=TOKEN_OK)
        return message_response() if callable(message_response) else message_response

    return handle


# --------------------------------------------------------------- 1. 成功回包


def test_send_text_success_round_trips_message_id_and_exact_request() -> None:
    seen: list[httpx.Request] = []
    http = make_client(_handler(seen, httpx.Response(200, json=MESSAGE_OK)))

    out = feishu_send.send_text(
        http, app_id="cli_x", app_secret="sec_x", chat_id="oc_chat_1", text="你好，这是回答"
    )

    assert out.ok is True and out.message_id == "om_sent_0001" and out.code == 0
    # 只打两次：先换 token，再发消息（复用 feishu_onboard.fetch_tenant_token，不重写）
    assert [str(r.url) for r in seen] == [TOKEN_URL, MESSAGE_URL + "?receive_id_type=chat_id"]
    send = seen[1]
    assert send.method == "POST"
    assert send.headers["authorization"] == "Bearer " + str(TOKEN_OK["tenant_access_token"])
    body = json.loads(send.content.decode("utf-8"))
    assert body["receive_id"] == "oc_chat_1"
    assert body["msg_type"] == "text"
    # content 是**一个 JSON 字符串**，不是 JSON 对象
    assert isinstance(body["content"], str)
    assert json.loads(body["content"]) == {"text": "你好，这是回答"}
    assert isinstance(body["uuid"], str) and body["uuid"]  # 幂等键


def test_send_text_url_uses_chat_id_receive_id_type() -> None:
    assert feishu_send.send_message_endpoint("feishu") == MESSAGE_URL
    assert (
        feishu_send.send_message_url("feishu")
        == "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    )
    assert feishu_send.send_message_endpoint("lark").startswith("https://open.larksuite.com/")
    assert feishu_send.send_message_endpoint("nope") == MESSAGE_URL  # 未知域名回退 feishu


# --------------------------------------------------------------- 2. 非零 code


def test_send_text_nonzero_code_is_a_clear_failure() -> None:
    seen: list[httpx.Request] = []
    http = make_client(
        _handler(seen, httpx.Response(200, json={"code": 99991663, "msg": "chat not found"}))
    )

    out = feishu_send.send_text(
        http, app_id="cli_x", app_secret="sec_x", chat_id="oc_bad", text="会被拒"
    )

    assert out.ok is False
    assert out.code == 99991663
    assert "99991663" in out.error and "chat not found" in out.error


def test_send_text_non_json_response_is_a_failure_not_a_crash() -> None:
    seen: list[httpx.Request] = []
    http = make_client(_handler(seen, httpx.Response(200, text="<html>oops</html>")))
    out = feishu_send.send_text(http, app_id="a", app_secret="s", chat_id="c", text="x")
    assert out.ok is False and out.error


# --------------------------------------------------------------- 3. 网络异常


def test_send_text_swallows_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in str(request.url):
            return httpx.Response(200, json=TOKEN_OK)
        raise httpx.ConnectError("boom")

    out = feishu_send.send_text(
        make_client(handler), app_id="cli_x", app_secret="sec_x", chat_id="oc_1", text="会连不上"
    )
    assert out.ok is False
    assert "网络" in out.error or "发送请求失败" in out.error


def test_send_text_token_endpoint_network_error_is_a_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    out = feishu_send.send_text(
        make_client(handler), app_id="cli_x", app_secret="sec_x", chat_id="oc_1", text="x"
    )
    assert out.ok is False and "tenant_access_token" in out.error


# --------------------------------------------------------------- 4. 无凭据 / 默认关（不发任何外网）


def test_build_sender_disabled_never_sends_even_with_credentials(monkeypatch) -> None:
    """**默认关**：`enabled=False` 时，即便凭据齐全也一次外网都不发。"""
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec_x")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调用
        seen.append(request)
        return httpx.Response(200, json=MESSAGE_OK)

    sender = feishu_send.build_sender(make_client(handler), enabled=False)
    result = sender("oc_1", "关着的时候不该出网")

    assert result["ok"] is False
    assert "未开启出站发送" in result["error"]
    assert seen == []  # 默认关：一次外网都不发


def test_build_sender_without_credentials_never_touches_network(monkeypatch) -> None:
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调用
        seen.append(request)
        return httpx.Response(200, json=MESSAGE_OK)

    http = make_client(handler)
    sender = feishu_send.build_sender(http, enabled=True)

    result = sender("oc_1", "没有凭据，不该出网")

    assert result["ok"] is False
    assert "凭据" in result["error"]
    assert seen == []  # 开着的但没凭据：一样一次外网都不发


def test_build_sender_without_http_client_never_sends(monkeypatch) -> None:
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec_x")
    sender = feishu_send.build_sender(None, enabled=True)
    result = sender("oc_1", "没出网客户端")
    assert result["ok"] is False and "出网客户端" in result["error"]


def test_build_sender_with_credentials_sends_through_http_client(monkeypatch) -> None:
    monkeypatch.setenv("FEISHU_APP_ID", "cli_env")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec_env")
    seen: list[httpx.Request] = []
    http = make_client(_handler(seen, httpx.Response(200, json=MESSAGE_OK)))

    sender = feishu_send.build_sender(http, enabled=True)
    result = sender("oc_env", "来自环境凭据")

    assert result["ok"] is True and result["message_id"] == "om_sent_0001"
    assert [str(r.url) for r in seen][1] == MESSAGE_URL + "?receive_id_type=chat_id"


def test_build_sender_swallows_unexpected_exceptions(monkeypatch) -> None:
    """兜底：发送里冒出的任何异常都被吞掉（绝不把会话/回调搞崩）。"""
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec_x")

    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("unexpected")

    sender = feishu_send.build_sender(make_client(handler), enabled=True)
    result = sender("oc_1", "会炸")  # 不抛
    assert result["ok"] is False and result["error"]
