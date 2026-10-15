"""飞书**入站即时回执**（docs/17 §3.11）的回归测试：**离线、无凭据、不装 SDK**。

一轮要跑 3–12 分钟，用户不该等这么久才知道"收到了" —— 所以白名单放行之后、跑轮次之前，
先回一条固定文案。这个文件把它的五条硬约定逐条钉住（都在文件头的黑体里），外加两条传参路径：

1. **顺序**：回执在 `session.ensure` / `chat.send` **之前**发出（不是"跑完了顺手补一条"）；
2. **失败不拖垮轮次**：发送器抛异常只记日志，会话照常落库、回答照常发；
3. **开关关 = 不发也不报错**：`runtime.feishu_ack_text: ""` 时一条都不发，且不记告警（那是用户的选择）；
4. **未授权不发**：配对/丢弃路径上的人**收不到回执**（回执不是"收到消息"的凭据，是"开始处理"的凭据）；
5. **同 message_id 重推只发一次**：飞书重推同一事件不会让用户看到双份"已接受消息"。

传参路径（配置 → 环境变量 → 子进程 → 渠道核心）有两条断言：`supervisor.config_env` 的注入，
以及 `FeishuWsClient` 把 `ack_text` 转交给它自己建的 `FeishuMessageHandler`。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import pytest

from scholarscout.channels import feishu, feishu_ws, supervisor
from scholarscout.config import DEFAULT_FEISHU_ACK_TEXT
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state import pairing
from scholarscout.tools import register_tools

from ..conftest import StubLlm, redirect_home, reply

CHAT_ID = "oc_ack_chat_0001"
SENDER = "ou_ack_user"
ACK = "已接受消息，正在处理中"
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
    """`.env` 重定向到 tmp；清掉渠道层环境变量，免得本机 `.env` 把判定带偏。

    `FEISHU_ACK_TEXT` **故意不删也不设** —— 本文件要测"缺省 = 开"，逐个用例自己设它。
    """
    redirect_home(monkeypatch, tmp_path)
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv(feishu.ACK_TEXT_ENV, raising=False)
    yield
    for key in _ENV_KEYS:
        os.environ.pop(key, None)


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_tools()


def _approve(ctx: AppContext, open_id: str = SENDER) -> None:
    pairing.approve_user(ctx.db, open_id, user_name="Stub")


def _payload(text: str, *, message_id: str = "om_ack_msg_0001") -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {"event_id": "evt_ack_0001", "event_type": feishu.RECEIVE_EVENT},
        "event": {
            "sender": {"sender_id": {"open_id": SENDER}},
            "message": {
                "message_id": message_id,
                "chat_id": CHAT_ID,
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


def _handler(
    ctx: AppContext,
    recorder: feishu.RecordingSender | feishu.Sender,
    *,
    ack_text: str | None = ACK,
    dispatcher: feishu.Dispatcher = dispatch,
) -> feishu.FeishuMessageHandler:
    """建一个入站核心；默认**显式注入**回执文案（被测对象是回执，不是环境变量）。"""
    return feishu.FeishuMessageHandler(
        ctx,
        credentials=feishu.FeishuCredentials(app_id="cli_ack", app_secret="sec_ack"),
        sender=recorder,
        dispatcher=dispatcher,
        ack_text=ack_text,
    )


def _ack_count(recorder: feishu.RecordingSender) -> int:
    return sum(1 for _, text in recorder.sent if text == ACK)


def _message_rows(ctx: AppContext) -> int:
    """会话 + 消息的总行数（**证据要能逐条对上**，不是"感觉写进去了"）。"""
    return int(
        ctx.db.scalar_int("SELECT count(*) FROM sessions WHERE session_key = ?", (CHAT_ID,)) or 0
    ) + int(ctx.db.scalar_int("SELECT count(*) FROM messages") or 0)


# ================================================================== ① 回执在跑轮次之前


def test_ack_is_sent_before_the_turn_starts(ctx: AppContext) -> None:
    """**顺序**：先回执 → 再 `session.ensure` → 再 `chat.send`（三条的**相对次序**，不是时间戳）。

    为什么不用时间戳：Windows 上 `time.monotonic()` 粒度 ~15.6ms，同一批动作会拿到**完全相同**的值，
    那种断言是空断言。这里改成把两边的动作记进同一条序列，比下标。
    """
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _approve(ctx)
    events: list[str] = []
    recorder = feishu.RecordingSender()

    def sender(chat_id: str, text: str) -> dict[str, Any]:
        events.append(f"send:{text}")
        recorder.sent.append((chat_id, text))
        return {"ok": True, "message_id": "om_sent_ack"}

    def dispatcher(ctx_: AppContext, name: str, params: Any = None) -> Any:
        if name in ("session.ensure", "chat.send"):
            events.append(name)
        return dispatch(ctx_, name, params)

    _handler(ctx, sender, dispatcher=dispatcher).handle_event(
        _payload("你好，帮我看下知识追踪入门")
    )

    assert events[0] == f"send:{ACK}", f"回执必须是这一条链路上的第一个动作，实际序列={events}"
    assert events.index("session.ensure") > 0, "回执不许排在落库之后"
    assert events.index("chat.send") > events.index("session.ensure")
    # 回答照常发出（回执不是替代品，只是"先应一声"）
    assert events[-1] == f"send:{STUB_REPLY}"
    print(f"[ack] 顺序={events}")


# ================================================================== ② 失败不拖垮轮次


def test_ack_failure_does_not_break_the_turn(ctx: AppContext) -> None:
    """**反例**：回执发送器抛异常 → 会话照常落库、回答照常发（回执绝不能成为链路的单点）。"""
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _approve(ctx)
    sent: list[tuple[str, str]] = []

    def sender(chat_id: str, text: str) -> dict[str, Any]:
        if text == ACK:
            raise RuntimeError("回执这一发炸了（网络/限流）")
        sent.append((chat_id, text))
        return {"ok": True, "message_id": "om_reply"}

    decision = _handler(ctx, sender).handle_event(_payload("这条的回执会炸"))

    assert decision is not None and decision.action == "run"
    # 库里三条：1 条会话 + 2 条消息（用户那条 + 助手那条）
    assert _message_rows(ctx) == 3, "回执失败不许影响落库"
    assert sent == [(CHAT_ID, STUB_REPLY)], "回答必须照常发出"
    print(f"[ack] 回执炸了但轮次跑完：会话+消息行数={_message_rows(ctx)} 已发={sent}")


def test_ack_failure_returns_false_and_logs(ctx: AppContext, caplog) -> None:
    """发送器**回 `ok=false`**（不是抛异常）也当没发：只记告警，不往外抛。"""
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _approve(ctx)
    handler = _handler(ctx, lambda chat_id, text: {"ok": False, "error": "rate_limited"})
    with caplog.at_level(logging.WARNING, logger="scholarscout.channels.feishu"):
        decision = handler.handle_event(_payload("回执被限流"))

    assert decision is not None and decision.action == "run"
    assert _message_rows(ctx) == 3
    assert any("回执未发出" in r.message and "rate_limited" in r.message for r in caplog.records)


# ================================================================== ③ 开关关 = 不发也不报错


def test_empty_ack_text_sends_nothing_and_warns_nothing(ctx: AppContext, caplog) -> None:
    """**开关关**（`runtime.feishu_ack_text: ""`）：一条都不发，且**不记告警** —— 那是用户的选择。"""
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _approve(ctx)
    recorder = feishu.RecordingSender()
    with caplog.at_level(logging.WARNING, logger="scholarscout.channels.feishu"):
        decision = _handler(ctx, recorder, ack_text="").handle_event(_payload("关着的时候"))

    assert decision is not None and decision.action == "run"
    assert recorder.sent == [(CHAT_ID, STUB_REPLY)], "关掉回执时只该有回答那一条"
    assert not [r for r in caplog.records if "回执" in r.message], "关掉不是异常，不许记回执告警"


def test_ack_text_defaults_to_ship_on_and_env_off_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺省（不传参、环境变量也没有）= **产品默认开着**；环境变量给空串 = 用户显式关掉，不许被兜回默认。"""
    assert feishu.ack_text_from_env() == DEFAULT_FEISHU_ACK_TEXT
    assert DEFAULT_FEISHU_ACK_TEXT == ACK
    monkeypatch.setenv(feishu.ACK_TEXT_ENV, "")
    assert feishu.ack_text_from_env() == "", "空串是合法取值，用 `or` 兜默认会把它吃掉"
    monkeypatch.setenv(feishu.ACK_TEXT_ENV, "  别急，正在处理  ")
    assert feishu.ack_text_from_env() == "别急，正在处理"


# ================================================================== ④ 未授权不发回执


def test_unapproved_sender_never_gets_an_ack(ctx: AppContext) -> None:
    """**反例**：默认 pairing 下未授权私聊 → 只回配对提示，**收不到回执**（也不会跑会话）。"""
    ctx.llm = StubLlm(reply(STUB_REPLY))
    recorder = feishu.RecordingSender()
    decision = _handler(ctx, recorder).handle_event(_payload("喂，在吗"))

    assert decision is not None and decision.action == "pair"
    assert recorder.sent == [(CHAT_ID, feishu.PAIRING_PROMPT)]
    assert _ack_count(recorder) == 0
    assert ctx.db.scalar_int("SELECT count(*) FROM sessions") == 0


def test_dropped_sender_never_gets_an_ack(ctx: AppContext) -> None:
    """**反例**：`allowlist` 名单外被丢弃 → 一条都不回，更不会有回执。"""
    os.environ["FEISHU_DM_POLICY"] = "allowlist"
    ctx.llm = StubLlm(reply(STUB_REPLY))
    recorder = feishu.RecordingSender()
    decision = _handler(ctx, recorder).handle_event(_payload("我是名单外的"))

    assert decision is not None and decision.action == "drop"
    assert recorder.sent == []


# ================================================================== ⑤ 同 message_id 重推只发一次


def test_same_message_id_redelivered_acks_only_once(ctx: AppContext) -> None:
    """飞书重推同一事件（长连接重连/平台重试）：回执**只发一次**，回答按轮次照常出。"""
    ctx.llm = StubLlm(reply("第一轮"), reply("第二轮"))
    _approve(ctx)
    recorder = feishu.RecordingSender()
    handler = _handler(ctx, recorder)

    payload = _payload("同一条消息会被推两次")
    handler.handle_event(payload)
    handler.handle_event(payload)

    assert _ack_count(recorder) == 1, f"重推不许双份回执：{recorder.sent}"
    assert [text for _, text in recorder.sent] == [ACK, "第一轮", "第二轮"]


def test_missing_message_id_is_never_treated_as_a_repeat(ctx: AppContext) -> None:
    """拿不到 `message_id` 时**不判重**：宁可多回一条，也绝不能把后面所有消息静默吞掉。"""
    dedupe = feishu.RecentMessageIds(maxsize=2)
    assert dedupe.mark("om_1") is True
    assert dedupe.mark("om_1") is False
    assert dedupe.mark("") is True and dedupe.mark("") is True  # 空 key 永远"头一回见"
    assert dedupe.mark("om_2") is True and dedupe.mark("om_3") is True
    assert dedupe.mark("om_1") is True, "窗口（2）滚掉之后旧的 id 要能被重新放行"


# ================================================================== 传参路径（配置 → 子进程 → 渠道核心）


def test_config_env_injects_the_ack_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """父进程起渠道子进程时，把 `runtime.feishu_ack_text` 翻译成 `FEISHU_ACK_TEXT` 带下去。

    渠道子进程**没有配置对象**（只当 RPC 客户端、不开库），所以这是它拿不到配置时的唯一通道；
    **空串也要注入**（= 关掉回执），不许被 `or` 兜成默认。
    """
    from types import SimpleNamespace

    ctx = SimpleNamespace(config=SimpleNamespace(runtime=SimpleNamespace(feishu_ack_text="")))
    env = supervisor.config_env(ctx)
    assert env[feishu.ACK_TEXT_ENV] == "", "空串必须原样带下去（否则用户关不掉）"
    # 既有的两条纪律没被破坏：强制 UTF-8 + 继承进程环境
    assert env["PYTHONUTF8"] == "1" and env["PYTHONIOENCODING"] == "utf-8"
    assert "PATH" in env

    ctx.config.runtime.feishu_ack_text = "处理中，请稍候"
    assert supervisor.config_env(ctx)[feishu.ACK_TEXT_ENV] == "处理中，请稍候"
    # 拿不到配置（老调用点 / 桩 ctx）：**一个键都不注入**，不许编一个值
    assert feishu.ACK_TEXT_ENV not in supervisor.config_env(SimpleNamespace())


def test_ws_client_forwards_ack_text_to_the_handler(ctx: AppContext) -> None:
    """长连接适配层把它自己建的入站核心接上回执文案（不传 = 仍由环境变量决定）。"""
    ctx_holder: feishu.ChannelContext = ctx  # type: ignore[assignment]
    client = feishu_ws.FeishuWsClient(ctx_holder, ack_text="收到，马上看")
    handler = client.handler
    assert handler is not None and isinstance(handler, feishu.FeishuMessageHandler)
    assert handler.ack_text == "收到，马上看"

    defaulted = feishu_ws.FeishuWsClient(ctx_holder)
    assert defaulted.handler is not None
    assert defaulted.handler.ack_text == DEFAULT_FEISHU_ACK_TEXT  # type: ignore[union-attr]
