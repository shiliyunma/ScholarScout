"""入站刷新**真实 open_id**（修 `99992361 open_id cross app`）的桩测：离线、无凭据、不装 SDK。

真 bug 的形状（本机真打接口查到）：`feishu_paired_users` 里那条手工抄来的
`ou_...` 属于**别的应用**，飞书 open_id 按应用隔离 → 私聊发送永远回
`HTTP 400 {"code":99992361,"msg":"open_id cross app"}`，配置全对也发不出去。

修法：**入站事件里的 `sender.sender_id.open_id` 一定属于本应用**，所以放行之后顺手把它
落/刷新到白名单。本文件钉住四条边界：

1. **白名单内 → 落库/刷新**：表里那一行的 `user_id` 就是事件里的 open_id，`source="inbound"`；
2. **白名单外 → 零写入**：`feishu_paired_users` 一行都不多（否则等于没有拦截）；
3. **写库失败不影响回复**：RPC 抛异常只记日志，会话照常跑、回答照常发；
4. **渠道层仍然不读库**：这件事只经 RPC（`feishu.pairing.remember`），分层门禁仍然成立。
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from scholarscout.channels import feishu
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state import pairing
from scholarscout.tools import register_tools

from ..conftest import StubLlm, redirect_home, reply

CHAT_ID = "oc_identity_chat_0001"
SENDER = "ou_inbound_real_id"
STUB_REPLY = "这是桩模型对飞书消息的回复"

_ENV_KEYS = (
    "FEISHU_DM_POLICY",
    "FEISHU_GROUP_POLICY",
    "FEISHU_BOT_OPEN_ID",
    "FEISHU_BOT_NAME",
    "FEISHU_CONNECTION_MODE",
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """`.env` 重定向到 tmp；清掉渠道层环境变量、关掉即时回执（回执是另一个功能的断言面）。"""
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


def _payload(text: str = "帮我看看教育智能体", *, sender: str = SENDER) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {"event_id": "evt_identity_0001", "event_type": feishu.RECEIVE_EVENT},
        "event": {
            "sender": {"sender_id": {"open_id": sender}},
            "message": {
                "message_id": "om_identity_msg_0001",
                "chat_id": CHAT_ID,
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


def _handler(
    ctx: AppContext,
    recorder: feishu.RecordingSender,
    *,
    dispatcher: feishu.Dispatcher = dispatch,
) -> feishu.FeishuMessageHandler:
    return feishu.FeishuMessageHandler(
        ctx,
        credentials=feishu.FeishuCredentials(app_id="cli_identity", app_secret="sec_identity"),
        sender=recorder,
        dispatcher=dispatcher,
        ack_text="",
    )


def _rows(ctx: AppContext) -> list[dict[str, Any]]:
    return pairing.approved_users(ctx.db)


# ============================================================ ① 白名单内：落库 / 刷新


def test_whitelisted_sender_lands_its_real_open_id_with_inbound_source(
    ctx: AppContext, recorder: feishu.RecordingSender
) -> None:
    """白名单内的发送者：表里出现这一行，`user_id` = 事件里的 open_id，`source="inbound"`。"""
    pairing.approve_user(ctx.db, SENDER, user_name="大浩", source="manual")
    ctx.llm = StubLlm(reply(STUB_REPLY))

    decision = _handler(ctx, recorder).handle_event(_payload())

    assert decision.action == "run"
    rows = _rows(ctx)
    assert [row["user_id"] for row in rows] == [SENDER]
    assert rows[0]["source"] == "inbound"  # 手工/配对写进来的旧 source 被刷新
    assert rows[0]["user_name"] == "大浩"  # 事件里没有昵称：**不许把已有昵称抹掉**
    # 会话真的跑了（刷新身份不能把这一轮吃掉）
    assert recorder.sent == [(CHAT_ID, STUB_REPLY)]


def test_inbound_refresh_updates_the_stored_row_in_place(ctx: AppContext) -> None:
    """同一个 open_id 再来一条：**原地刷新**（不新增行、不换 id）。"""
    pairing.approve_user(ctx.db, SENDER, user_name="旧昵称", source="pairing")

    assert pairing.remember_inbound(ctx.db, SENDER) is True

    rows = _rows(ctx)
    assert len(rows) == 1
    assert rows[0]["user_id"] == SENDER and rows[0]["source"] == "inbound"
    assert rows[0]["user_name"] == "旧昵称"


# ============================================================ ② 白名单外：零写入


def test_non_whitelisted_sender_writes_nothing(
    ctx: AppContext, recorder: feishu.RecordingSender
) -> None:
    """非白名单：**一行都不写**（否则谁发一条消息都能把自己写进白名单）。"""
    stranger = "ou_not_whitelisted"

    decision = _handler(ctx, recorder).handle_event(_payload(sender=stranger))

    assert decision.action == "pair"  # 默认 pairing 策略：只回配对提示
    assert _rows(ctx) == []
    assert (
        ctx.db.scalar_int("SELECT count(*) FROM feishu_paired_users WHERE user_id = ?", (stranger,))
        == 0
    )


def test_state_layer_refuses_to_remember_unknown_identity(db) -> None:
    """写库这一层也复核一次：不在白名单 → 回 `False`，表里没有行（纵深防御）。"""
    assert pairing.remember_inbound(db, "ou_never_approved", user_name="路人") is False
    assert pairing.approved_users(db) == []


# ============================================================ ③ 写库失败：回复照常


def test_db_failure_does_not_break_the_reply(
    ctx: AppContext, recorder: feishu.RecordingSender
) -> None:
    """RPC 抛异常只记日志：会话照常跑、回答照常发。"""
    pairing.approve_user(ctx.db, SENDER, user_name="大浩", source="manual")
    ctx.llm = StubLlm(reply(STUB_REPLY))

    def flaky(ctx_: Any, method: str, params: dict[str, Any]) -> Any:
        if method == feishu.IDENTITY_REMEMBER_METHOD:
            raise RuntimeError("database is locked")
        return dispatch(ctx_, method, params)

    decision = _handler(ctx, recorder, dispatcher=flaky).handle_event(_payload())

    assert decision.action == "run"
    assert recorder.sent == [(CHAT_ID, STUB_REPLY)]  # 回复照样发出去了
    # 刷新失败 = 那一行的 source 保持原样（没有被写坏）
    assert _rows(ctx)[0]["source"] == "manual"


# ============================================================ ④ RPC 契约本身


def test_rpc_remember_reports_refreshed_and_not_allowlisted(ctx: AppContext) -> None:
    """RPC 层：白名单内 → `updated:true`；名单外 → `updated:false`（**不是错误，不抛**）。"""
    pairing.approve_user(ctx.db, SENDER, user_name="大浩")

    assert dispatch(ctx, "feishu.pairing.remember", {"open_id": SENDER}) == {
        "ok": True,
        "updated": True,
        "reason": "refreshed",
    }
    assert dispatch(ctx, "feishu.pairing.remember", {"open_id": "ou_stranger"}) == {
        "ok": True,
        "updated": False,
        "reason": "not_allowlisted",
    }
    assert [row["user_id"] for row in _rows(ctx)] == [SENDER]
