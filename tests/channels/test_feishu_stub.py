"""飞书渠道层「桩回调」自测：**不联网、无凭据**跑通整条链路。

覆盖两批断言：
  A. docs/17 §3.6 的四条传输层断言（起本地回调 → 打 HTTP）：
     1. 消息进入会话路径（真落库）；2. 回包格式正确；3. 非法签名/token 被拒（401）；
     4. challenge 验证正确（先过 token 再回显）。
  B. 运行时拦截（docs/17 §3.4，默认 `pairing`）：
     未授权私聊**不执行任何会话**、只回配对提示；`allowlist` 名单外**直接丢弃并记日志**；
     群聊遵守 `@提及` 策略；临时放行、报文 @ 判定、配对码登记。

用真 `ThreadingHTTPServer` + `http.client` 打 HTTP，只桩掉「出网发送」（换成本地记录器）。
渠道层不读库：白名单状态一律经 `feishu.access` RPC 取（`tests/rpc/test_feishu_methods.py` 钉住它）。
"""

from __future__ import annotations

import http.client
import json
import logging
import os
from typing import Any

import pytest

from scholarscout.channels import feishu
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state import pairing
from scholarscout.tools import register_tools

from ..conftest import StubLlm, redirect_home, reply

VERIFICATION_TOKEN = "vt_stub_0001"
ENCRYPT_KEY = "ek_stub_0001"
CHAT_ID = "oc_stub_chat_0001"
STUB_REPLY = "这是桩模型对飞书消息的回复"
SENDER = "ou_stub_user"
BOT_OPEN_ID = "ou_stub_bot"
BOT_NAME = "大浩的AI毕设"

#: 渠道层读到的环境变量（运行时拦截依据）；每个用例进出都清干净
_ENV_KEYS = ("FEISHU_DM_POLICY", "FEISHU_GROUP_POLICY", "FEISHU_BOT_OPEN_ID", "FEISHU_BOT_NAME")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """`.env` 重定向到 tmp；清掉渠道层环境变量（拦截判定直接读 os.environ）。"""
    redirect_home(monkeypatch, tmp_path)
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    for key in _ENV_KEYS:
        os.environ.pop(key, None)


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_tools()


@pytest.fixture
def recorder() -> feishu.RecordingSender:
    return feishu.RecordingSender()


def _approve(ctx: AppContext, open_id: str = SENDER) -> None:
    """把发送者写进白名单（模拟「已完成配对」）。"""
    pairing.approve_user(ctx.db, open_id, user_name="Stub")


def _creds(*, with_secret: bool = True) -> feishu.FeishuCredentials:
    return feishu.FeishuCredentials(
        app_id="cli_stub_app",
        app_secret="secret_stub",
        verification_token=VERIFICATION_TOKEN if with_secret else "",
        encrypt_key=ENCRYPT_KEY if with_secret else "",
    )


def _message_payload(
    text: str,
    *,
    token: str = VERIFICATION_TOKEN,
    chat_id: str = CHAT_ID,
    chat_type: str = "",
    mentions: list[dict[str, Any]] | None = None,
    sender: str = SENDER,
) -> dict:
    """一份合造的 `im.message.receive_v1` 回调体（形状照 Hermes adapter.py 的 webhook 路径）。"""
    message: dict[str, Any] = {
        "message_id": "om_stub_msg_0001",
        "chat_id": chat_id,
        "message_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    if chat_type:
        message["chat_type"] = chat_type
    if mentions is not None:
        message["mentions"] = mentions
    return {
        "schema": "2.0",
        "header": {
            "event_id": "evt_stub_0001",
            "event_type": feishu.RECEIVE_EVENT,
            "token": token,
            "create_time": "1700000000000",
        },
        "event": {
            "sender": {"sender_id": {"open_id": sender}},
            "message": message,
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


def _send(server: feishu.FeishuCallbackServer, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload, ensure_ascii=False).encode()
    return _post(server.address[1], body, _sign_headers(body))


# ================================================================== A. 传输层

# ------------------------------------------------------------------ 1+2：消息进会话 + 回包格式


def test_stub_callback_runs_turn_and_round_trips_reply(ctx: AppContext, recorder) -> None:
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _approve(ctx)  # 默认 pairing：授权后的私聊才跑会话
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
    _approve(ctx)
    server = _serve(ctx, recorder)
    try:
        for text in ("第一句", "第二句继续"):
            status, _ = _send(server, _message_payload(text))
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
    _approve(ctx)
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


# ================================================================== B. 运行时拦截（反例 + 正例）


def test_default_pairing_unapproved_only_gets_prompt_no_session(
    ctx: AppContext, recorder, caplog
) -> None:
    """**反例**：默认 pairing 下未授权私聊 → 只回配对提示，绝不执行会话。"""
    ctx.llm = StubLlm(reply(STUB_REPLY))  # 备着；真跑了会消耗它
    with caplog.at_level(logging.WARNING, logger="scholarscout.channels.feishu"):
        server = _serve(ctx, recorder)
        try:
            status, payload = _send(server, _message_payload("喂，在吗"))
        finally:
            server.stop()

    assert status == 200 and payload == {"code": 0, "msg": "ok"}
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions") == 0  # 没进会话路径
    assert recorder.sent == [(CHAT_ID, feishu.PAIRING_PROMPT)]  # 只回配对提示
    assert ctx.llm.calls == []  # 模型一次都没被叫


def test_access_rpc_reports_unapproved_unknown_sender(ctx: AppContext) -> None:
    """`feishu.access` 是渠道层唯一取数口：未知 sender 回未授权、未临时放行、当前策略。"""
    assert dispatch(ctx, "feishu.access", {"open_id": SENDER}) == {
        "approved": False,
        "temp_open": False,
        "dm_policy": "pairing",
    }
    # 空 open_id 不回 approved:true（防漏判）
    assert dispatch(ctx, "feishu.access", {})["approved"] is False
    pairing.approve_user(ctx.db, SENDER)
    assert dispatch(ctx, "feishu.access", {"open_id": SENDER})["approved"] is True


def test_paired_dm_runs_turn(ctx: AppContext, recorder) -> None:
    """**正例**：已授权私聊 → 跑会话。"""
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _approve(ctx)
    server = _serve(ctx, recorder)
    try:
        status, _ = _send(server, _message_payload("我是白名单用户"))
    finally:
        server.stop()
    assert status == 200
    assert recorder.sent == [(CHAT_ID, STUB_REPLY)]


def test_temp_open_lets_unapproved_run(ctx: AppContext, recorder) -> None:
    """「临时放行」开着时，未授权也能先跑（配对码就是在这期间被发出去的）。"""
    ctx.llm = StubLlm(reply(STUB_REPLY))
    pairing.set_temp_open(ctx.db, True)
    server = _serve(ctx, recorder)
    try:
        status, _ = _send(server, _message_payload("临时放行期间"))
    finally:
        server.stop()
    assert status == 200
    assert recorder.sent == [(CHAT_ID, STUB_REPLY)]


def test_allowlist_denies_unlisted_and_logs(ctx: AppContext, recorder, caplog) -> None:
    """**反例**：`allowlist` 下名单外 → 直接丢弃并记日志（不回、不跑会话）。"""
    os.environ["FEISHU_DM_POLICY"] = "allowlist"
    ctx.llm = StubLlm(reply(STUB_REPLY))
    with caplog.at_level(logging.WARNING, logger="scholarscout.channels.feishu"):
        server = _serve(ctx, recorder)
        try:
            status, _ = _send(server, _message_payload("我是名单外的"))
        finally:
            server.stop()

    assert status == 200  # 回包仍是 ok（飞书不该重试）
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions") == 0
    assert recorder.sent == []
    assert any("消息被丢弃" in r.message and "not_allowlisted" in r.message for r in caplog.records)


def test_allowlist_allows_listed(ctx: AppContext, recorder) -> None:
    """**正例**：`allowlist` 下名单内 → 跑会话。"""
    os.environ["FEISHU_DM_POLICY"] = "allowlist"
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _approve(ctx)
    server = _serve(ctx, recorder)
    try:
        status, _ = _send(server, _message_payload("我是名单内的"))
    finally:
        server.stop()
    assert status == 200
    assert recorder.sent == [(CHAT_ID, STUB_REPLY)]


def test_group_without_mention_is_dropped(ctx: AppContext, recorder, caplog) -> None:
    """**反例**：群聊没 @ 机器人 → 丢弃并记日志。"""
    os.environ["FEISHU_BOT_OPEN_ID"] = BOT_OPEN_ID
    ctx.llm = StubLlm(reply(STUB_REPLY))
    with caplog.at_level(logging.WARNING, logger="scholarscout.channels.feishu"):
        server = _serve(ctx, recorder)
        try:
            status, _ = _send(
                server, _message_payload("大家好", chat_type="group", chat_id="oc_grp_1")
            )
        finally:
            server.stop()
    assert status == 200
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions") == 0
    assert recorder.sent == []
    assert any("group_no_mention" in r.message for r in caplog.records)


def test_group_with_mention_runs(ctx: AppContext, recorder) -> None:
    """**正例**：群聊 @ 到机器人（按 open_id 认）→ 跑会话。"""
    os.environ["FEISHU_BOT_OPEN_ID"] = BOT_OPEN_ID
    ctx.llm = StubLlm(reply(STUB_REPLY))
    mentions = [{"id": {"open_id": BOT_OPEN_ID}, "name": BOT_NAME}]
    server = _serve(ctx, recorder)
    try:
        status, _ = _send(
            server,
            _message_payload(
                "@机器人 帮我看下", chat_type="group", chat_id="oc_grp_2", mentions=mentions
            ),
        )
    finally:
        server.stop()
    assert status == 200
    assert recorder.sent == [("oc_grp_2", STUB_REPLY)]


def test_group_disabled_drops_even_when_mentioned(ctx: AppContext, recorder, caplog) -> None:
    os.environ["FEISHU_BOT_OPEN_ID"] = BOT_OPEN_ID
    os.environ["FEISHU_GROUP_POLICY"] = "disabled"
    ctx.llm = StubLlm(reply(STUB_REPLY))
    mentions = [{"id": {"open_id": BOT_OPEN_ID}, "name": BOT_NAME}]
    with caplog.at_level(logging.WARNING, logger="scholarscout.channels.feishu"):
        server = _serve(ctx, recorder)
        try:
            status, _ = _send(
                server,
                _message_payload(
                    "@机器人", chat_type="group", chat_id="oc_grp_3", mentions=mentions
                ),
            )
        finally:
            server.stop()
    assert status == 200
    assert recorder.sent == []
    assert any("group_disabled" in r.message for r in caplog.records)


def test_pairing_code_message_registers_openid(ctx: AppContext, recorder) -> None:
    """渠道层收到「把配对码私聊发给机器人」那条消息 → 登记 openid（不跑会话）。"""
    code = dispatch(ctx, "feishu.pairing.create", {})["code"]
    server = _serve(ctx, recorder)
    try:
        status, _ = _send(server, _message_payload(code))
    finally:
        server.stop()
    assert status == 200
    assert recorder.sent == [(CHAT_ID, feishu.PAIRING_CLAIMED_REPLY)]
    # 登记成功：待批里那枚码已绑上发送者 openid
    pending = dispatch(ctx, "feishu.pairing.list", {})["pending"]
    assert [row["user_id"] for row in pending if row["claimed"]] == [SENDER]


def test_invalid_pairing_code_message_gets_prompt(ctx: AppContext, recorder) -> None:
    """**反例**：非法（根本不是码）的消息 → 当普通未授权私聊处理，只回配对提示，绝不登记/跑会话。"""
    ctx.llm = StubLlm(reply(STUB_REPLY))
    server = _serve(ctx, recorder)
    try:
        status, _ = _send(server, _message_payload("你好呀，随便聊两句"))
    finally:
        server.stop()

    assert status == 200
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions") == 0
    assert recorder.sent == [(CHAT_ID, feishu.PAIRING_PROMPT)]
    assert dispatch(ctx, "feishu.pairing.list", {})["pending"] == []  # 没登记出任何待批


def test_expired_pairing_code_message_gets_prompt(ctx: AppContext, recorder) -> None:
    """**反例**：过期配对码 → 登记失败，按普通未授权处理（只回配对提示，不跑会话）。"""
    import time

    ctx.llm = StubLlm(reply(STUB_REPLY))
    # 造一枚「两小时前」生成的码（早过 TTL），再把临时放行关掉，隔离出「码消息」这一条路径
    old_now = time.time() - pairing.CODE_TTL_SECONDS - 3600
    code = pairing.create_code(ctx.db, now=old_now)
    assert code is not None
    pairing.set_temp_open(ctx.db, False)

    server = _serve(ctx, recorder)
    try:
        status, _ = _send(server, _message_payload(code))
    finally:
        server.stop()

    assert status == 200
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions") == 0
    assert recorder.sent == [(CHAT_ID, feishu.PAIRING_PROMPT)]


def test_temp_open_revoked_after_pairing_stops_unapproved(ctx: AppContext, recorder) -> None:
    """「取 id 的临时放行」在配对成功后**收回**：此后未授权私聊不再被放行（只回配对提示）。"""
    ctx.llm = StubLlm(reply(STUB_REPLY))
    code = dispatch(ctx, "feishu.pairing.create", {})["code"]
    assert dispatch(ctx, "feishu.access", {"open_id": SENDER})["temp_open"] is True
    dispatch(ctx, "feishu.pairing.claim", {"code": code, "open_id": SENDER})
    assert dispatch(ctx, "feishu.pairing.verify", {"code": code})["temp_open"] is False

    # 收回后：另一个未授权用户的私聊不再有临时放行 → 只回配对提示
    server = _serve(ctx, recorder)
    try:
        status, _ = _send(server, _message_payload("我是后来的人", sender="ou_late_user"))
    finally:
        server.stop()

    assert status == 200
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions") == 0
    assert recorder.sent == [(CHAT_ID, feishu.PAIRING_PROMPT)]


# ================================================================== 纯函数（契约向量）


def test_compute_signature_matches_manual_sha256() -> None:
    import hashlib

    body = b'{"a":1}'
    expected = hashlib.sha256(b"1700000000" + b"n1" + b"ek" + body).hexdigest()
    assert feishu.compute_signature("1700000000", "n1", "ek", body) == expected


def test_parse_message_event_extracts_text_and_mentions() -> None:
    msg = feishu.parse_message_event(_message_payload("取文本"))
    assert msg is not None
    assert (msg.chat_id, msg.text, msg.message_id) == (CHAT_ID, "取文本", "om_stub_msg_0001")
    assert msg.chat_type == "" and msg.mentions == ()

    payload: dict[str, Any] = _message_payload("x")
    payload["event"]["message"]["message_type"] = "image"
    assert feishu.parse_message_event(payload) is None
    assert feishu.parse_message_event({"header": {"event_type": "other"}}) is None

    grouped = feishu.parse_message_event(
        _message_payload(
            "@我",
            chat_type="group",
            mentions=[{"id": {"open_id": "ou_x"}, "name": "某人"}],
        )
    )
    assert grouped is not None
    assert grouped.chat_type == "group"
    assert grouped.mentions == (("ou_x", "某人"),)


def _msg(**kw: Any) -> feishu.FeishuMessage:
    base: dict[str, Any] = {
        "chat_id": "c",
        "text": "t",
        "message_id": "m",
        "sender_open_id": SENDER,
    }
    base.update(kw)
    return feishu.FeishuMessage(**base)


def test_authorize_group_policies() -> None:
    mention = {"mentions": (("ou_bot", ""),)}
    assert (
        feishu.authorize_message(
            _msg(chat_type="group", **mention),
            dm_policy="pairing",
            group_policy="disabled",
            approved=False,
            bot_open_id="ou_bot",
        ).reason
        == "group_disabled"
    )
    assert (
        feishu.authorize_message(
            _msg(chat_type="group"),
            dm_policy="pairing",
            group_policy="mention",
            approved=True,
            bot_open_id="ou_bot",
        ).action
        == "drop"
    )
    assert (
        feishu.authorize_message(
            _msg(chat_type="group", **mention),
            dm_policy="pairing",
            group_policy="mention",
            approved=False,
            bot_open_id="ou_bot",
        ).action
        == "run"
    )


def test_bot_mentioned_by_name_only_when_no_open_id() -> None:
    message = _msg(mentions=(("", BOT_NAME),))
    assert feishu.bot_mentioned(message, bot_name=BOT_NAME) is True
    # 配了 bot_open_id 时不再按名字兜底（避免同名误判）
    assert feishu.bot_mentioned(message, bot_open_id="ou_bot", bot_name=BOT_NAME) is False
    assert feishu.bot_mentioned(_msg(), bot_open_id="ou_bot") is False


def test_authorize_dm_policies() -> None:
    # all：一律放行
    assert (
        feishu.authorize_message(
            _msg(), dm_policy="all", group_policy="mention", approved=False
        ).action
        == "run"
    )
    # allowlist：名单内 run / 名单外 drop
    assert (
        feishu.authorize_message(
            _msg(), dm_policy="allowlist", group_policy="mention", approved=True
        ).action
        == "run"
    )
    assert (
        feishu.authorize_message(
            _msg(), dm_policy="allowlist", group_policy="mention", approved=False
        ).reason
        == "not_allowlisted"
    )
    # pairing：已批准 run / 临时放行 run / 都没有 → pair（绝不 run）
    assert (
        feishu.authorize_message(
            _msg(), dm_policy="pairing", group_policy="mention", approved=True
        ).action
        == "run"
    )
    assert (
        feishu.authorize_message(
            _msg(), dm_policy="pairing", group_policy="mention", approved=False, temp_open=True
        ).reason
        == "dm_temp_open"
    )
    decision = feishu.authorize_message(
        _msg(), dm_policy="pairing", group_policy="mention", approved=False
    )
    assert decision.action == "pair" and decision.reply == feishu.PAIRING_PROMPT
