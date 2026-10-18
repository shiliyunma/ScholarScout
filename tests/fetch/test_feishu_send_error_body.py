"""发送失败的**飞书错误码**要一眼可见（修 `HTTP 400 {"code":99992361}` 只报“请求失败（400）”）。

真事故的形状（本机真打接口）：私聊发送被平台拒：
`HTTP 400 {"code":99992361,"msg":"open_id cross app"}`。
而 `fetch/http.py` 抛 `FetchError` 时只留状态码，正文被丢掉 —— 于是 `SendOutcome.code` 是 `0`、
日志里只有「发送请求失败（bad_request）：请求失败（400）」，**看不见真正的错因**。

本文件钉住三件事：

1. 4xx 回包里带 `{"code":...}` → `SendOutcome.code` 就是那个码、error 里带 `msg` 与人话提示；
2. 发送器日志里出现 `code=99992361`（**能在日志里一眼看到**，不用再去翻回包）；
3. 4xx 但正文不是 JSON → 仍然回 `code=0` + 原错误（**不编错误码、不崩**）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
import pytest

from scholarscout.fetch import feishu_send
from scholarscout.fetch.http import FetchError, HttpClient

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
TOKEN_OK = {"code": 0, "msg": "ok", "tenant_access_token": "t-" + "a" * 40, "expire": 7200}
#: 实测回包（真打接口时拿到的就是这个）
CROSS_APP = {"code": 99992361, "msg": "open_id cross app"}


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> HttpClient:
    return HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))


def _handler(rejected: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    """换 token 成功、发消息被拒（**只打两次，不重试**：400 不在可重试状态里）。"""

    def handle(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in str(request.url):
            return httpx.Response(200, json=TOKEN_OK)
        return rejected

    return handle


# ================================================== ① 400 + 平台错误码


def test_http_400_carries_feishu_code_and_msg_into_send_outcome() -> None:
    http = make_client(_handler(httpx.Response(400, json=CROSS_APP)))

    out = feishu_send.send_text(
        http, app_id="cli_x", app_secret="sec_x", chat_id="ou_wrong_app", text="会被拒"
    )

    assert out.ok is False
    assert out.code == 99992361  # 修之前这里是 0
    assert "99992361" in out.error and "open_id cross app" in out.error
    assert "跨应用" in out.error  # explain_code 的人话提示（能指导下一步）
    assert "HTTP 400" in out.error  # 状态码也留着，别丢证据


def test_http_layer_keeps_the_error_body_for_the_caller() -> None:
    """`FetchError` 留证：上层能从错误对象里取回 `(code, msg)`（唯一出网层的职责）。"""
    http = make_client(_handler(httpx.Response(400, json=CROSS_APP)))

    out = feishu_send.send_text(
        http, app_id="cli_x", app_secret="sec_x", chat_id="ou_wrong_app", text="会被拒"
    )
    assert out.code == 99992361

    try:
        http.post_json("https://open.feishu.cn/open-apis/im/v1/messages", {"x": 1})
    except FetchError as exc:
        assert exc.status == 400 and exc.platform_code() == (99992361, "open_id cross app")
    else:  # pragma: no cover - 400 必然抛
        raise AssertionError("400 应该抛 FetchError")


# ================================================== ② 日志里能一眼看到码


def test_private_sender_log_mentions_the_feishu_code(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec_x")
    http = make_client(_handler(httpx.Response(400, json=CROSS_APP)))

    sender = feishu_send.build_private_sender(http, open_id="ou_wrong_app")
    assert sender is not None
    with caplog.at_level(logging.WARNING, logger="scholarscout.fetch.feishu_send"):
        result = sender("提醒：今晚该复习了")

    assert result["ok"] is False and result["code"] == 99992361
    # 日志形状：`[Feishu] 私聊提醒发送失败 open_id=ou_wrong_app code=99992361：飞书返回 code=99992361：open_id cross app｜…`
    assert any("code=99992361" in record.getMessage() for record in caplog.records)
    assert any("open_id cross app" in record.getMessage() for record in caplog.records)


# ================================================== ③ 4xx 但正文不是 JSON


def test_http_400_without_json_body_falls_back_without_inventing_a_code() -> None:
    http = make_client(_handler(httpx.Response(400, text="<html>bad gateway</html>")))

    out = feishu_send.send_text(
        http, app_id="cli_x", app_secret="sec_x", chat_id="oc_1", text="会被拒"
    )

    assert out.ok is False and out.code == 0  # 不回包的码就不编
    assert "请求失败（400）" in out.error
