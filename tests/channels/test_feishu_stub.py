"""飞书渠道**入站核心**的自测：**不联网、无凭据、不需要传输层**跑通整条链路。

`channels/feishu.py` 是传输无关的（只认"已经解好的事件报文"），所以这里直接把报文喂给
`feishu.FeishuMessageHandler.handle_event` —— 与真长连接收到事件后走的是**同一段代码**
（长连接那一层单独测：`tests/channels/test_feishu_ws_stub.py`）。

覆盖两批断言：
  A. 消息进会话路径（真落库）、同 chat 复用会话、出站发送器被调用（含真端点/真字段的端到端桩）；
  B. 运行时拦截（docs/17 §3.4，默认 `pairing`）：未授权私聊**不执行任何会话**、只回配对提示；
     `allowlist` 名单外**直接丢弃并记日志**；群聊遵守 `@提及` 策略；临时放行、报文 @ 判定、配对码登记。

渠道层不读库：白名单状态一律经 `feishu.access` RPC 取（`tests/rpc/test_feishu_methods.py` 钉住它）。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
import pytest

from scholarscout.channels import feishu
from scholarscout.fetch import feishu_send
from scholarscout.fetch.http import HttpClient
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state import pairing
from scholarscout.tools import register_tools

from ..conftest import StubLlm, redirect_home, reply

CHAT_ID = "oc_stub_chat_0001"
STUB_REPLY = "这是桩模型对飞书消息的回复"
SENDER = "ou_stub_user"
BOT_OPEN_ID = "ou_stub_bot"
BOT_NAME = "大浩的AI毕设"

#: 渠道层读到的环境变量（运行时拦截依据）；每个用例进出都清干净
_ENV_KEYS = (
    "FEISHU_DM_POLICY",
    "FEISHU_GROUP_POLICY",
    "FEISHU_BOT_OPEN_ID",
    "FEISHU_BOT_NAME",
    "FEISHU_CONNECTION_MODE",
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """`.env` 重定向到 tmp；清掉渠道层环境变量（拦截判定直接读 os.environ）。

    **入站即时回执在本文件里默认关掉**（`FEISHU_ACK_TEXT=""`）：回执会让每个"跑一轮"的用例多出
    一条出站消息，把 `recorder.sent == [(chat, 回答)]` 这类断言全部改写 —— 那是**另一个功能**的
    断言面。照本项目既有做法（新引入的额外出站同理）：产品默认值保持开（`DEFAULT_FEISHU_ACK_TEXT`），
    在**专用文件** `tests/channels/test_feishu_ack.py` 里显式打开来测；这里关掉，让每个用例只盯它自己那件事。
    """
    redirect_home(monkeypatch, tmp_path)
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FEISHU_ACK_TEXT", "")
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


def _message_payload(
    text: str,
    *,
    chat_id: str = CHAT_ID,
    chat_type: str = "",
    mentions: list[dict[str, Any]] | None = None,
    sender: str = SENDER,
) -> dict[str, Any]:
    """一份合造的 `im.message.receive_v1` 报文（形状照真 SDK 反序列化后的报文）。"""
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
        "header": {"event_id": "evt_stub_0001", "event_type": feishu.RECEIVE_EVENT},
        "event": {
            "sender": {"sender_id": {"open_id": sender}},
            "message": message,
        },
    }


def _handler(
    ctx: AppContext,
    recorder: feishu.RecordingSender,
    *,
    sender: feishu.Sender | None = None,
    dispatcher: feishu.Dispatcher = dispatch,
) -> feishu.FeishuMessageHandler:
    return feishu.FeishuMessageHandler(
        ctx,
        credentials=feishu.FeishuCredentials(app_id="cli_stub_app", app_secret="secret_stub"),
        sender=sender if sender is not None else recorder,
        dispatcher=dispatcher,
    )


def _deliver(
    handler: feishu.FeishuMessageHandler, payload: dict[str, Any]
) -> feishu.InterceptDecision | None:
    """把一份事件报文喂给入站核心（真长连接收到事件后走的就是这一句）。"""
    return handler.handle_event(payload)


# ================================================================== A. 入站 → 会话 → 出站


def test_stub_event_runs_turn_and_round_trips_reply(ctx: AppContext, recorder) -> None:
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _approve(ctx)  # 默认 pairing：授权后的私聊才跑会话
    decision = _deliver(_handler(ctx, recorder), _message_payload("你好，帮我看下知识追踪入门"))

    assert decision is not None and decision.action == "run"
    # 消息进了会话路径：真的落库（session_key=chat_id，用户消息在库）
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions WHERE session_key = ?", (CHAT_ID,)) == 1
    session_id = ctx.db.query_one("SELECT id FROM sessions WHERE session_key = ?", (CHAT_ID,))["id"]
    users = ctx.db.query(
        "SELECT content FROM messages WHERE session_id = ? AND role = 'user'", (session_id,)
    )
    assert [r["content"] for r in users] == ["你好，帮我看下知识追踪入门"]
    # 回复交给了发送器（真实部署注入飞书 REST 发送；这里记下来）
    assert recorder.sent == [(CHAT_ID, STUB_REPLY)]
    print(f"[stub] 会话={session_id} 发送器收到={recorder.sent}")


def test_same_chat_reuses_session(ctx: AppContext, recorder) -> None:
    ctx.llm = StubLlm(reply("第一轮"), reply("第二轮"))
    _approve(ctx)
    handler = _handler(ctx, recorder)
    for text in ("第一句", "第二句继续"):
        assert _deliver(handler, _message_payload(text)) is not None

    # 同一个 chat_id → 只有一条会话（session.ensure 幂等）
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions WHERE session_key = ?", (CHAT_ID,)) == 1
    assert recorder.sent == [(CHAT_ID, "第一轮"), (CHAT_ID, "第二轮")]


def test_new_session_pushes_session_updated_to_the_window(ctx: AppContext, recorder) -> None:
    """**用户报的 bug**：飞书里发一条，会话与消息都在库里，界面上却看不见。

    根因不在界面：`session.ensure` 这条入口**一个事件都不发**（在此之前只有"标题真被改写"
    才发 `session.updated`，见 `rpc/session.py::_title_before_turn`），而左栏的会话列表靠
    事件驱动刷新 —— 所以入站建出来的会话要等下一次手动动作才冒出来。

    这里用**假桥**接住 `ctx.emit`（桌面端窗口收到的通知就是这一串：`rpc/server.py` 的
    emitter → `webview.evaluate_js("window.scoutEvent(...)")`），并**当场查库对账**：
    事件里的 `session_id` 必须就是库里那条会话，且 user / assistant 两条消息都在。
    """
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _approve(ctx)
    bridge: list[tuple[str, dict[str, Any]]] = []
    ctx.emit = lambda name, payload: bridge.append((name, payload))

    _deliver(_handler(ctx, recorder), _message_payload("帮我找知识追踪的入门资料"))

    row = ctx.db.query_one("SELECT id FROM sessions WHERE session_key = ?", (CHAT_ID,))
    assert row is not None, "入站消息必须落库"
    session_id = str(row["id"])
    updates = [payload for name, payload in bridge if name == "session.updated"]
    assert updates == [{"session_id": session_id, "created": True}], (
        f"新建会话必须推一条 session.updated（带 created=True）把界面叫醒：{bridge}"
    )
    # 载荷形状：新建那条**只带** session_id + created（标题那条另有 title/source），
    # 前端据此区分"新建"与"改标题"，两者都会触发 loadSessions()。
    assert set(updates[0]) == {"session_id", "created"}
    # 对账：这条会话的 user + assistant 两条消息都在库里（界面该看到的正文就是它们）
    rows = ctx.db.query(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY idx", (session_id,)
    )
    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "帮我找知识追踪的入门资料"),
        ("assistant", STUB_REPLY),
    ]
    print(f"[bridge] {bridge} 库里消息={[(r['role'], r['content']) for r in rows]}")


def test_second_inbound_reuses_session_without_re_pushing_created(
    ctx: AppContext, recorder
) -> None:
    """**反例**：同一会话的第二条消息**不再**推 `created: True`（幂等；否则左栏被反复"新建"）。"""
    ctx.llm = StubLlm(reply("第一轮"), reply("第二轮"))
    _approve(ctx)
    bridge: list[tuple[str, dict[str, Any]]] = []
    ctx.emit = lambda name, payload: bridge.append((name, payload))
    handler = _handler(ctx, recorder)

    _deliver(handler, _message_payload("第一句"))
    created_events = [p for name, p in bridge if name == "session.updated"]
    _deliver(handler, _message_payload("第二句继续"))

    assert len(created_events) == 1, "第一条消息那一次才该推 session.updated"
    assert [p for name, p in bridge if name == "session.updated"] == created_events
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions WHERE session_key = ?", (CHAT_ID,)) == 1


def test_non_text_payload_is_ignored(ctx: AppContext, recorder) -> None:
    """非文本 / 非消息事件 → 返回 None（不当成错误，也不跑会话）。"""
    handler = _handler(ctx, recorder)
    payload = _message_payload("x")
    payload["event"]["message"]["message_type"] = "image"  # type: ignore[index]
    assert _deliver(handler, payload) is None
    assert _deliver(handler, {"header": {"event_type": "other"}}) is None
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions") == 0


# ================================================================== B. 运行时拦截


def test_default_pairing_unapproved_only_gets_prompt_no_session(
    ctx: AppContext, recorder, caplog
) -> None:
    """**反例**：默认 pairing 下未授权私聊 → 只回配对提示，绝不执行会话。"""
    ctx.llm = StubLlm(reply(STUB_REPLY))  # 备着；真跑了会消耗它
    decision = _deliver(_handler(ctx, recorder), _message_payload("喂，在吗"))

    assert decision is not None and decision.action == "pair"
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
    _deliver(_handler(ctx, recorder), _message_payload("我是白名单用户"))
    assert recorder.sent == [(CHAT_ID, STUB_REPLY)]


def test_temp_open_lets_unapproved_run(ctx: AppContext, recorder) -> None:
    """「临时放行」开着时，未授权也能先跑（配对码就是在这期间被发出去的）。"""
    ctx.llm = StubLlm(reply(STUB_REPLY))
    pairing.set_temp_open(ctx.db, True)
    _deliver(_handler(ctx, recorder), _message_payload("临时放行期间"))
    assert recorder.sent == [(CHAT_ID, STUB_REPLY)]


def test_allowlist_denies_unlisted_and_logs(ctx: AppContext, recorder, caplog) -> None:
    """**反例**：`allowlist` 下名单外 → 直接丢弃并记日志（不回、不跑会话）。"""
    os.environ["FEISHU_DM_POLICY"] = "allowlist"
    ctx.llm = StubLlm(reply(STUB_REPLY))
    with caplog.at_level(logging.WARNING, logger="scholarscout.channels.feishu"):
        decision = _deliver(_handler(ctx, recorder), _message_payload("我是名单外的"))

    assert decision is not None and decision.action == "drop"
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions") == 0
    assert recorder.sent == []
    assert any("消息被丢弃" in r.message and "not_allowlisted" in r.message for r in caplog.records)


def test_allowlist_allows_listed(ctx: AppContext, recorder) -> None:
    """**正例**：`allowlist` 下名单内 → 跑会话。"""
    os.environ["FEISHU_DM_POLICY"] = "allowlist"
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _approve(ctx)
    _deliver(_handler(ctx, recorder), _message_payload("我是名单内的"))
    assert recorder.sent == [(CHAT_ID, STUB_REPLY)]


def test_group_without_mention_is_dropped(ctx: AppContext, recorder, caplog) -> None:
    """**反例**：群聊没 @ 机器人 → 丢弃并记日志。"""
    os.environ["FEISHU_BOT_OPEN_ID"] = BOT_OPEN_ID
    ctx.llm = StubLlm(reply(STUB_REPLY))
    with caplog.at_level(logging.WARNING, logger="scholarscout.channels.feishu"):
        _deliver(
            _handler(ctx, recorder),
            _message_payload("大家好", chat_type="group", chat_id="oc_grp_1"),
        )
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions") == 0
    assert recorder.sent == []
    assert any("group_no_mention" in r.message for r in caplog.records)


def test_group_with_mention_runs(ctx: AppContext, recorder) -> None:
    """**正例**：群聊 @ 到机器人（按 open_id 认）→ 跑会话。"""
    os.environ["FEISHU_BOT_OPEN_ID"] = BOT_OPEN_ID
    ctx.llm = StubLlm(reply(STUB_REPLY))
    mentions = [{"id": {"open_id": BOT_OPEN_ID}, "name": BOT_NAME}]
    _deliver(
        _handler(ctx, recorder),
        _message_payload(
            "@机器人 帮我看下", chat_type="group", chat_id="oc_grp_2", mentions=mentions
        ),
    )
    assert recorder.sent == [("oc_grp_2", STUB_REPLY)]


def test_group_disabled_drops_even_when_mentioned(ctx: AppContext, recorder, caplog) -> None:
    os.environ["FEISHU_BOT_OPEN_ID"] = BOT_OPEN_ID
    os.environ["FEISHU_GROUP_POLICY"] = "disabled"
    ctx.llm = StubLlm(reply(STUB_REPLY))
    mentions = [{"id": {"open_id": BOT_OPEN_ID}, "name": BOT_NAME}]
    with caplog.at_level(logging.WARNING, logger="scholarscout.channels.feishu"):
        _deliver(
            _handler(ctx, recorder),
            _message_payload("@机器人", chat_type="group", chat_id="oc_grp_3", mentions=mentions),
        )
    assert recorder.sent == []
    assert any("group_disabled" in r.message for r in caplog.records)


def test_pairing_code_message_registers_openid(ctx: AppContext, recorder) -> None:
    """渠道层收到「把配对码私聊发给机器人」那条消息 → 登记 openid（不跑会话）。"""
    code = dispatch(ctx, "feishu.pairing.create", {})["code"]
    _deliver(_handler(ctx, recorder), _message_payload(code))
    assert recorder.sent == [(CHAT_ID, feishu.PAIRING_CLAIMED_REPLY)]
    # 登记成功：待批里那枚码已绑上发送者 openid
    pending = dispatch(ctx, "feishu.pairing.list", {})["pending"]
    assert [row["user_id"] for row in pending if row["claimed"]] == [SENDER]


def test_invalid_pairing_code_message_gets_prompt(ctx: AppContext, recorder) -> None:
    """**反例**：非法（根本不是码）的消息 → 当普通未授权私聊处理，只回配对提示，绝不登记/跑会话。"""
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _deliver(_handler(ctx, recorder), _message_payload("你好呀，随便聊两句"))

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

    _deliver(_handler(ctx, recorder), _message_payload(code))

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
    _deliver(_handler(ctx, recorder), _message_payload("我是后来的人", sender="ou_late_user"))

    assert ctx.db.scalar_int("SELECT count(*) FROM sessions") == 0
    assert recorder.sent == [(CHAT_ID, feishu.PAIRING_PROMPT)]


# ================================================================== 纯函数（契约向量）


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


def test_recording_sender_never_pretends_to_succeed(caplog) -> None:
    """没注入发送器时**不假装发成功**：只记录 + 告警（如实回 `recorded`）。"""
    sender = feishu.RecordingSender()
    with caplog.at_level(logging.WARNING, logger="scholarscout.channels.feishu"):
        result = sender("oc_x", "hi")
    assert result == {"code": 0, "recorded": True, "chat_id": "oc_x"}
    assert sender.sent == [("oc_x", "hi")]
    assert any("未注入发送器" in r.message for r in caplog.records)


# ================================================================== C. 出站真发送器接线（端到端桩，docs/17 §3.7）

#: 实测的真实回包形状
_TOKEN_OK = {"code": 0, "msg": "ok", "tenant_access_token": "t-" + "a" * 40, "expire": 7200}
_MSG_OK = {"code": 0, "msg": "success", "data": {"message_id": "om_e2e_sent_1"}}


def _sender_http(seen: list[httpx.Request]) -> HttpClient:
    """出网口打桩：换 token → 200；发消息 → 200（真实回包形状）。不真联网。"""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "tenant_access_token" in str(request.url):
            return httpx.Response(200, json=_TOKEN_OK)
        return httpx.Response(200, json=_MSG_OK)

    return HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))


def test_end_to_end_event_runs_turn_then_outbound_is_called(ctx: AppContext) -> None:
    """**端到端桩**：入站真事件 → 跑一轮（桩模型）→ **出站真发送器被调用**（不联网）。

    出站走的是 `fetch/feishu_send.build_sender` → `fetch/http.py` → MockTransport，
    所以断言的正是**真端点与真字段**（`POST /open-apis/im/v1/messages?receive_id_type=chat_id`）。
    """
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _approve(ctx)
    seen: list[httpx.Request] = []
    sender = feishu_send.build_sender(
        _sender_http(seen), enabled=True, app_id="cli_e2e", app_secret="sec_e2e"
    )
    _deliver(_handler(ctx, None, sender=sender), _message_payload("端到端：入站→跑一轮→出站发送"))

    # 入站：真落库
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions WHERE session_key = ?", (CHAT_ID,)) == 1
    # 出站被调：真打了取 token + 发消息两个端点
    token_calls = [r for r in seen if "tenant_access_token" in str(r.url)]
    send_calls = [r for r in seen if "im/v1/messages" in str(r.url)]
    assert len(token_calls) == 1, "应复用 fetch/feishu_onboard.fetch_tenant_token 换一次 token"
    assert len(send_calls) == 1, "出站发送器应被调用一次"
    req = send_calls[0]
    assert req.method == "POST"
    assert str(req.url).endswith("/open-apis/im/v1/messages?receive_id_type=chat_id")
    assert req.headers["authorization"] == "Bearer " + str(_TOKEN_OK["tenant_access_token"])
    body = json.loads(req.content.decode("utf-8"))
    assert body["receive_id"] == CHAT_ID
    assert body["msg_type"] == "text"
    assert json.loads(body["content"]) == {"text": STUB_REPLY}  # 桩模型的回答被推回去
    print(
        f"[e2e] 入站 → 会话落库 → 出站 POST {req.url} body={json.dumps(body, ensure_ascii=False)}"
    )


def test_end_to_end_outbound_failure_does_not_break_the_turn(ctx: AppContext) -> None:
    """**反例**：出站发送失败（网络异常）时，会话照常落库（发送失败不崩会话）。"""
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _approve(ctx)

    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in str(request.url):
            return httpx.Response(200, json=_TOKEN_OK)
        raise httpx.ConnectError("boom")

    sender = feishu_send.build_sender(
        HttpClient(respect_robots=False, transport=httpx.MockTransport(handler)),
        enabled=True,
        app_id="cli_e2e",
        app_secret="sec_e2e",
    )
    decision = _deliver(_handler(ctx, None, sender=sender), _message_payload("出站会失败的这条"))

    assert decision is not None and decision.action == "run"
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions WHERE session_key = ?", (CHAT_ID,)) == 1


def test_end_to_end_outbound_disabled_runs_turn_but_never_sends(ctx: AppContext) -> None:
    """**默认关**：出站发送器关着时，照常跑一轮、**但一次外网都不发**。"""
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _approve(ctx)
    seen: list[httpx.Request] = []
    sender = feishu_send.build_sender(_sender_http(seen), enabled=False)
    _deliver(_handler(ctx, None, sender=sender), _message_payload("关着的时候跑一轮"))

    assert ctx.db.scalar_int("SELECT count(*) FROM sessions WHERE session_key = ?", (CHAT_ID,)) == 1
    assert seen == []  # 关着：一次外网都没发
