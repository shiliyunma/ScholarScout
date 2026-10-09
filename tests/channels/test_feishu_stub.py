"""飞书渠道层「桩回调」自测：**不联网、无凭据**跑通整条链路。

覆盖 docs/17 §3.6 的四条断言：
  1. 起本地回调 → 发一条合造的飞书回调请求 → **消息进入会话路径**（真落库）；
  2. **回复回包格式正确**（HTTP 200 + `{"code":0,"msg":"ok"}`，且回复文本交给了发送器）；
  3. **非法签名 / 非法 token 被拒**（401）；
  4. **challenge 验证正确**（先过 token 再回显 challenge，token 错则 401）。

用真 `ThreadingHTTPServer` + `http.client` 打 HTTP，只桩掉"出网发送"（换成本地记录器）。
"""

from __future__ import annotations

import http.client
import json
import logging
from typing import Any

import pytest

from scholarscout.channels import feishu
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.tools import register_tools

from ..conftest import StubLlm, reply

VERIFICATION_TOKEN = "vt_stub_0001"
ENCRYPT_KEY = "ek_stub_0001"
CHAT_ID = "oc_stub_chat_0001"
STUB_REPLY = "这是桩模型对飞书消息的回复"


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_tools()


@pytest.fixture
def recorder() -> feishu.RecordingSender:
    return feishu.RecordingSender()


def _creds(*, with_secret: bool = True) -> feishu.FeishuCredentials:
    return feishu.FeishuCredentials(
        app_id="cli_stub_app",
        app_secret="secret_stub",
        verification_token=VERIFICATION_TOKEN if with_secret else "",
        encrypt_key=ENCRYPT_KEY if with_secret else "",
    )


def _message_payload(text: str, *, token: str = VERIFICATION_TOKEN, chat_id: str = CHAT_ID) -> dict:
    """一份合造的 `im.message.receive_v1` 回调体（形状照 Hermes adapter.py 的 webhook 路径）。"""
    return {
        "schema": "2.0",
        "header": {
            "event_id": "evt_stub_0001",
            "event_type": feishu.RECEIVE_EVENT,
            "token": token,
            "create_time": "1700000000000",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_stub_user"}},
            "message": {
                "message_id": "om_stub_msg_0001",
                "chat_id": chat_id,
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


def _sign_headers(
    body: bytes, *, nonce: str = "n1", timestamp: str = "1700000000"
) -> dict[str, str]:
    signature = feishu.compute_signature(timestamp, nonce, ENCRYPT_KEY, body)
    return {
        "Content-Type": "application/json",
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": signature,
    }


def _post(
    port: int, body: bytes, headers: dict[str, str], path: str = feishu.DEFAULT_PATH
) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, json.loads(raw.decode("utf-8"))
    finally:
        conn.close()


def _serve(
    ctx: AppContext, recorder: feishu.RecordingSender, *, with_secret: bool = True
) -> feishu.FeishuCallbackServer:
    server = feishu.build_server(
        ctx,
        port=0,
        credentials=_creds(with_secret=with_secret),
        sender=recorder,
        dispatcher=dispatch,
    )
    server.start()
    return server


# ------------------------------------------------------------------ 1+2：消息进会话 + 回包格式


def test_stub_callback_runs_turn_and_round_trips_reply(ctx: AppContext, recorder) -> None:
    ctx.llm = StubLlm(reply(STUB_REPLY))
    server = _serve(ctx, recorder)
    try:
        body = json.dumps(
            _message_payload("你好，帮我看下知识追踪入门"), ensure_ascii=False
        ).encode()
        status, payload = _post(server.address[1], body, _sign_headers(body))
    finally:
        server.stop()

    # 2. 回包格式正确（照飞书成功回包）
    assert status == 200
    assert payload == {"code": 0, "msg": "ok"}
    # 1. 消息进了会话路径：真的落库（session_key=chat_id，用户消息在库）
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions WHERE session_key = ?", (CHAT_ID,)) == 1
    session_id = ctx.db.query_one("SELECT id FROM sessions WHERE session_key = ?", (CHAT_ID,))["id"]
    users = ctx.db.query(
        "SELECT content FROM messages WHERE session_id = ? AND role = 'user'", (session_id,)
    )
    assert [r["content"] for r in users] == ["你好，帮我看下知识追踪入门"]
    # 回复交给了发送器（真实部署注入飞书 REST 发送；这里记下来）
    assert recorder.sent == [(CHAT_ID, STUB_REPLY)]
    print(f"[stub] 回包={payload}  会话={session_id}  发送器收到={recorder.sent}")


def test_same_chat_reuses_session(ctx: AppContext, recorder) -> None:
    ctx.llm = StubLlm(reply("第一轮"), reply("第二轮"))
    server = _serve(ctx, recorder)
    try:
        for text in ("第一句", "第二句继续"):
            body = json.dumps(_message_payload(text), ensure_ascii=False).encode()
            status, _ = _post(server.address[1], body, _sign_headers(body))
            assert status == 200
    finally:
        server.stop()

    # 同一个 chat_id → 只有一条会话（session.ensure 幂等）
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions WHERE session_key = ?", (CHAT_ID,)) == 1
    assert recorder.sent == [(CHAT_ID, "第一轮"), (CHAT_ID, "第二轮")]


# ------------------------------------------------------------------ 3：非法签名 / token 被拒


def test_bad_signature_rejected_401(ctx: AppContext, recorder) -> None:
    ctx.llm = StubLlm(reply(STUB_REPLY))
    server = _serve(ctx, recorder)
    try:
        body = json.dumps(_message_payload("这条签名是错的"), ensure_ascii=False).encode()
        headers = _sign_headers(body)
        headers["X-Lark-Signature"] = "deadbeef" * 8
        status, payload = _post(server.address[1], body, headers)
    finally:
        server.stop()

    assert status == 401
    assert payload["msg"] == "Invalid signature"
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions") == 0  # 没进会话路径
    assert recorder.sent == []


def test_bad_token_rejected_401(ctx: AppContext, recorder) -> None:
    server = _serve(ctx, recorder)
    try:
        body = json.dumps(_message_payload("坏 token", token="WRONG"), ensure_ascii=False).encode()
        status, payload = _post(server.address[1], body, _sign_headers(body))
    finally:
        server.stop()

    assert status == 401
    assert payload["msg"] == "Invalid verification token"
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions") == 0


# ------------------------------------------------------------------ 4：challenge 验证


def test_url_verification_returns_challenge(ctx: AppContext, recorder) -> None:
    server = _serve(ctx, recorder)
    try:
        body = json.dumps(
            {"type": feishu.CHALLENGE_EVENT, "challenge": "ch_stub_42", "token": VERIFICATION_TOKEN}
        ).encode()
        # challenge 在签名校验之前返回 → 不需要签名头
        status, payload = _post(server.address[1], body, {"Content-Type": "application/json"})
    finally:
        server.stop()

    assert status == 200
    assert payload == {"challenge": "ch_stub_42"}


def test_url_verification_bad_token_rejected(ctx: AppContext, recorder) -> None:
    server = _serve(ctx, recorder)
    try:
        body = json.dumps(
            {"type": feishu.CHALLENGE_EVENT, "challenge": "ch_stub_42", "token": "WRONG"}
        ).encode()
        status, payload = _post(server.address[1], body, {"Content-Type": "application/json"})
    finally:
        server.stop()

    assert status == 401
    assert payload["msg"] == "Invalid verification token"


# ------------------------------------------------------------------ 无密钥：不校签 + 告警


def test_no_secret_skips_verification_with_warning(ctx: AppContext, recorder, caplog) -> None:
    ctx.llm = StubLlm(reply(STUB_REPLY))
    with caplog.at_level(logging.WARNING, logger="scholarscout.channels.feishu"):
        server = _serve(ctx, recorder, with_secret=False)
        try:
            assert any("不校签" in r.message for r in caplog.records)
            body = json.dumps(
                _message_payload("没有密钥", token="irrelevant"), ensure_ascii=False
            ).encode()
            status, _ = _post(server.address[1], body, {"Content-Type": "application/json"})
        finally:
            server.stop()
    assert status == 200
    assert recorder.sent == [(CHAT_ID, STUB_REPLY)]


# ------------------------------------------------------------------ 纯函数（契约向量）


def test_compute_signature_matches_manual_sha256() -> None:
    import hashlib

    body = b'{"a":1}'
    expected = hashlib.sha256(b"1700000000" + b"n1" + b"ek" + body).hexdigest()
    assert feishu.compute_signature("1700000000", "n1", "ek", body) == expected


def test_parse_message_event_extracts_text_and_rejects_non_text() -> None:
    msg = feishu.parse_message_event(_message_payload("取文本"))
    assert msg is not None
    assert (msg.chat_id, msg.text, msg.message_id) == (CHAT_ID, "取文本", "om_stub_msg_0001")

    payload: dict[str, Any] = _message_payload("x")
    payload["event"]["message"]["message_type"] = "image"
    assert feishu.parse_message_event(payload) is None
    assert feishu.parse_message_event({"header": {"event_type": "other"}}) is None
