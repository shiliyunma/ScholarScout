"""群绑模块的**端到端**证明：一条群消息 → `run_turn` → 实际交给下一层的 `toolsets`。

已有测试分别钉住了三件事：迁移能升（`tests/state/`）、`channel.set_module/get_module` 能设能取
（`tests/rpc/test_channel_modules.py`）、**解析函数**能回退不崩。但它们都停在
`channel.module_for_chat` 的**返回值**上 —— 没有任何一条跑通"这条群消息最后到底用了哪个模块"。

`channels/feishu.py::run_turn` 的真实链路（照代码）：

    decide(message)                  → feishu.access（白名单/临时放行/群 @策略）
    session.ensure                   → chat_id → session_id（幂等）
    module_toolsets(message)         → channel.module_for_chat → rpc/channel.py::_resolve
                                       （库里的绑定 → 全局默认 → 兜底，只有这一份实现）
    chat.send(session_id, text, toolsets=…)

所以这里的断言接在**两个真实终点**上，都不是中间函数的返回值：

1. `chat.send` 的参数 —— 用 `SpyDispatcher` 包住真的 `dispatch`，记录**原样**递给下一层的
   `params`，再原样转发（真跑一轮，不桩 RPC）。断言的 `params["toolsets"]` 就是渠道层
   交给主进程的那一份；
2. 模型实际拿到的工具表 —— 桩模型的 `calls[0]["tools"]`。绑 `kb` 时线上只该有
   `kb__search`；回退到全局默认时 `kb__search` 必须不在里面。这一层能抓到"参数传对了、
   但下游又按全局默认挑工具"这种假生效。

全部桩掉网络与模型：无长连接、无 REST（发送器是 `RecordingSender`）、模型是 `StubLlm`。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from typing import Any

import pytest

from scholarscout.channels import feishu
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.tools import register_tools

from ..conftest import StubLlm, redirect_home, reply

#: 两个群：A 绑模块，B 用作"没绑"的对照
GROUP_A = "oc_group_bound_a"
GROUP_B = "oc_group_plain_b"
SENDER = "ou_stub_user"
BOT_OPEN_ID = "ou_stub_bot"
BOT_NAME = "大浩的AI毕设"
STUB_REPLY = "桩模型的回复"

#: 开了 `enable_kb` 之后**全局默认**是什么（`runtime/registry.enabled_toolsets` 会把 kb 补上）。
#: 写死成字面量：如果哪天默认变了，这里的差异断言会先失败，而不是悄悄变成"两份一样"的假通过。
DEFAULT_TOOLSETS_WITH_KB = ["research", "kb"]

_ENV_KEYS = (
    "FEISHU_DM_POLICY",
    "FEISHU_GROUP_POLICY",
    "FEISHU_BOT_OPEN_ID",
    "FEISHU_BOT_NAME",
    "FEISHU_CONNECTION_MODE",
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """`.env` 重定向到 tmp；渠道层的拦截判定直接读 os.environ，进出都清干净。

    入站回执（`FEISHU_ACK_TEXT`）在本文件里关掉：它会让每条消息多一次出站，把
    `recorder.sent` 的断言改写 —— 那是 `tests/channels/test_feishu_ack.py` 的事。
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
    """把 `kb.search` 一起注册进来（`enable_kb=True`）。

    产品里 `register_tools(enable_kb=...)` 与 `features.enable_kb` 是同一件事
    （`app.build_context` 显式传）。本文件要在**模型侧**对账"这一轮到底给了哪些工具"，
    所以两个开关一起打开；漏掉注册的话 `kb` 模块会被解析成空工具表，
    断言就变成"参数对了、下游什么都没给"的假通过。
    """
    register_tools(enable_kb=True)


@pytest.fixture
def recorder() -> feishu.RecordingSender:
    return feishu.RecordingSender()


class SpyDispatcher:
    """包一层**真** `dispatch`：记下每次「方法名 + 原样参数」，再转给真实现。

    不做任何改写 —— 所以记下来的 `params` 就是渠道层真正交给下一层的那一份，
    轮次也照常真跑（桩模型）。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, ctx: Any, method: str, params: Any) -> Any:
        recorded = dict(params) if isinstance(params, dict) else params
        self.calls.append((method, recorded))
        return dispatch(ctx, method, params)

    def params_of(self, method: str) -> list[dict[str, Any]]:
        return [dict(params) for name, params in self.calls if name == method]

    def order(self) -> list[str]:
        return [name for name, _ in self.calls]


def _with_kb(ctx: AppContext, *, on: bool) -> None:
    """原地翻 `features.enable_kb`（处理器与 `SessionRuntime` 都持有同一个 ctx 对象）。"""
    ctx.config = replace(ctx.config, features=replace(ctx.config.features, enable_kb=on))


def _bind(ctx: AppContext, chat_id: str, module: str) -> None:
    dispatch(ctx, "channel.set_module", {"channel": "feishu", "chat_id": chat_id, "module": module})


_MESSAGE_SEQ = [0]


def _group_payload(text: str, *, chat_id: str) -> dict[str, Any]:
    """一份合造的群消息报文：`chat_type=group` + 真的 @ 到机器人（走 mention 策略）。"""
    _MESSAGE_SEQ[0] += 1
    return {
        "schema": "2.0",
        "header": {"event_id": f"evt_module_{_MESSAGE_SEQ[0]}", "event_type": feishu.RECEIVE_EVENT},
        "event": {
            "sender": {"sender_id": {"open_id": SENDER}},
            "message": {
                "message_id": f"om_module_{_MESSAGE_SEQ[0]}",
                "chat_id": chat_id,
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "mentions": [{"id": {"open_id": BOT_OPEN_ID}, "name": BOT_NAME}],
            },
        },
    }


def _handler(
    ctx: AppContext, recorder: feishu.RecordingSender, *, dispatcher: Any
) -> feishu.FeishuMessageHandler:
    return feishu.FeishuMessageHandler(
        ctx,
        credentials=feishu.FeishuCredentials(app_id="cli_stub_app", app_secret="secret_stub"),
        sender=recorder,
        dispatcher=dispatcher,
        bot_open_id=BOT_OPEN_ID,
        bot_name=BOT_NAME,
    )


def _model_tool_names(ctx: AppContext, call: int = 0) -> list[str]:
    """桩模型第 `call` 次调用时**实际收到**的工具线上名（`kb.search` → `kb__search`）。"""
    assert ctx.llm is not None
    tools = ctx.llm.calls[call]["tools"]
    assert isinstance(tools, list)
    return [str(tool["function"]["name"]) for tool in tools]


def _send(handler: feishu.FeishuMessageHandler, text: str, *, chat_id: str) -> None:
    decision = handler.handle_event(_group_payload(text, chat_id=chat_id))
    assert decision is not None and decision.action == "run", (
        f"群消息没跑起来（decision={decision}）：这之后的断言都无从谈起"
    )


# ================================================================== ① 绑定群用绑定模块


def test_bound_group_passes_its_module_toolsets_to_chat_send(
    ctx: AppContext, recorder: feishu.RecordingSender
) -> None:
    """群 A 绑 `kb` → 这一轮的 `chat.send` 里 `toolsets` 就是 `["kb"]`，**不是**全局默认。

    `["kb"]` 与全局默认 `["research", "kb"]` 是不同的两份 —— 所以这一条同时排除了
    "绑定被忽略、恰好看起来一样"的可能。
    """
    _with_kb(ctx, on=True)
    ctx.llm = StubLlm(reply(STUB_REPLY))
    _bind(ctx, GROUP_A, "kb")
    spy = SpyDispatcher()

    _send(_handler(ctx, recorder, dispatcher=spy), "绑定群问一句", chat_id=GROUP_A)

    sends = spy.params_of("chat.send")
    assert len(sends) == 1, f"应当只有一次 chat.send，实际={sends}"
    # ← 真正传给下一层的参数
    assert sends[0]["toolsets"] == ["kb"]
    assert sends[0]["toolsets"] != DEFAULT_TOOLSETS_WITH_KB, "绑定群却走了全局默认"
    assert "research" not in sends[0]["toolsets"]
    assert sends[0]["session_id"] and sends[0]["text"] == "绑定群问一句"

    # 取模块这一步发生在 session.ensure 之后、chat.send 之前（顺序也是契约）
    order = spy.order()
    assert order.index("session.ensure") < order.index("channel.module_for_chat")
    assert order.index("channel.module_for_chat") < order.index("chat.send")
    assert spy.params_of("channel.module_for_chat") == [{"channel": "feishu", "chat_id": GROUP_A}]

    # 下游真按它挑工具：模型这一轮只看到 kb 的工具，一个 research 工具都没有
    assert _model_tool_names(ctx) == ["kb__search"]
    assert recorder.sent == [(GROUP_A, STUB_REPLY)]


# ================================================================== ② 未绑群走全局默认


def test_unbound_group_falls_back_to_the_global_default(
    ctx: AppContext, recorder: feishu.RecordingSender
) -> None:
    """同一个进程里：群 A 绑 `kb`、群 B 没绑 → B 的 `toolsets` 是全局默认，A 的仍是 `["kb"]`。"""
    _with_kb(ctx, on=True)
    ctx.llm = StubLlm(reply("A 的回复"), reply("B 的回复"))
    _bind(ctx, GROUP_A, "kb")
    spy = SpyDispatcher()
    handler = _handler(ctx, recorder, dispatcher=spy)

    _send(handler, "绑定群的这条", chat_id=GROUP_A)
    assert dispatch(ctx, "channel.get_module", {"channel": "feishu", "chat_id": GROUP_B}) == {
        "module": None
    }, "B 应当是没绑过的状态"
    _send(handler, "没绑的群的这条", chat_id=GROUP_B)

    assert [s["toolsets"] for s in spy.params_of("chat.send")] == [
        ["kb"],
        DEFAULT_TOOLSETS_WITH_KB,
    ], "B 没绑却拿到了 A 的模块（或没拿到全局默认）"
    # 模型侧对账：B 那一轮拿的是 research 工具（默认模块），不是只有 kb
    assert "kb__search" in _model_tool_names(ctx, call=1)
    assert any(name.startswith("research") for name in _model_tool_names(ctx, call=1))
    assert recorder.sent == [(GROUP_A, "A 的回复"), (GROUP_B, "B 的回复")]


# ================================================================== ③ 绑定失效 → 回退 + warning


def test_broken_binding_falls_back_with_warning_and_still_replies(
    ctx: AppContext, recorder: feishu.RecordingSender, caplog: pytest.LogCaptureFixture
) -> None:
    """绑定指向**已不存在的模块**（`enable_kb` 关掉后 `kb` 不在白名单）→ 回退默认、有 warning、不崩。

    这是用户手上真实有过的状态：先开着开关把群绑到 `kb`，之后又把开关关了。
    一条过期绑定不该让这个群从此收不到回复。
    """
    _with_kb(ctx, on=True)
    _bind(ctx, GROUP_A, "kb")
    _with_kb(ctx, on=False)  # kb 此刻不在可用模块里
    ctx.llm = StubLlm(reply(STUB_REPLY))
    spy = SpyDispatcher()

    with caplog.at_level(logging.WARNING):
        _send(_handler(ctx, recorder, dispatcher=spy), "绑定已失效的群里问一句", chat_id=GROUP_A)

    sends = spy.params_of("chat.send")
    assert sends[0]["toolsets"] == ["research"], "失效绑定必须回退到全局默认，而不是空/失效值"
    assert recorder.sent == [(GROUP_A, STUB_REPLY)], "绑定失效不能让这条群消息丢掉"
    # 回退真的到了模型层：按失效的 kb 挑工具的话这里会出现 kb__search
    assert "kb__search" not in _model_tool_names(ctx)

    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "绑定失效" in r.getMessage()
    ]
    assert warnings, (
        f"绑定失效必须留一条 warning 日志，实际记录={[r.getMessage() for r in caplog.records]}"
    )
    assert warnings[0].name == "scholarscout.rpc.channel"
    assert "kb" in warnings[0].getMessage()


# ================================================================== ④ 改绑生效


def test_rebinding_changes_the_next_message(
    ctx: AppContext, recorder: feishu.RecordingSender
) -> None:
    """同一个群改绑 `kb` → `research`：下一条消息的 `toolsets` 立刻换成新模块（旧绑定不残留）。"""
    _with_kb(ctx, on=True)
    ctx.llm = StubLlm(reply("kb 那一轮"), reply("research 那一轮"))
    _bind(ctx, GROUP_A, "kb")
    spy = SpyDispatcher()
    handler = _handler(ctx, recorder, dispatcher=spy)

    _send(handler, "改绑前", chat_id=GROUP_A)
    _bind(ctx, GROUP_A, "research")
    _send(handler, "改绑后", chat_id=GROUP_A)

    assert [s["toolsets"] for s in spy.params_of("chat.send")] == [["kb"], ["research"]], (
        "改绑后的那一条仍在用旧模块（或者两条都用默认）"
    )
    # 模型侧：第二条不再只给 kb 工具
    assert _model_tool_names(ctx, call=0) == ["kb__search"]
    assert "kb__search" in _model_tool_names(ctx, call=1)
    assert any(name.startswith("research") for name in _model_tool_names(ctx, call=1))


# ================================================================== ⑤ 连续两条不串


def test_two_consecutive_messages_both_use_the_binding(
    ctx: AppContext, recorder: feishu.RecordingSender
) -> None:
    """同一群连续两条：**两条都用绑定模块**，第二条不会掉回默认（解析每轮都重新做）。"""
    _with_kb(ctx, on=True)
    ctx.llm = StubLlm(reply("第一条"), reply("第二条"))
    _bind(ctx, GROUP_A, "kb")
    spy = SpyDispatcher()
    handler = _handler(ctx, recorder, dispatcher=spy)

    _send(handler, "第一条", chat_id=GROUP_A)
    _send(handler, "第二条", chat_id=GROUP_A)

    sends = spy.params_of("chat.send")
    assert len(sends) == 2
    assert [s["toolsets"] for s in sends] == [["kb"], ["kb"]], "第二条丢了绑定（掉回全局默认）"
    assert [s["text"] for s in sends] == ["第一条", "第二条"]
    # 两轮各自问一次模块（不是一轮问了之后缓存串到下一轮）
    assert len(spy.params_of("channel.module_for_chat")) == 2
    assert _model_tool_names(ctx, call=0) == ["kb__search"]
    assert _model_tool_names(ctx, call=1) == ["kb__search"]
    assert recorder.sent == [(GROUP_A, "第一条"), (GROUP_A, "第二条")]


# ================================================================== ⑥ 库里的脏值


def test_dirty_binding_value_in_db_falls_back_without_crashing(
    ctx: AppContext, recorder: feishu.RecordingSender, caplog: pytest.LogCaptureFixture
) -> None:
    """库里是绕过 `set_module` 写进去的脏值（模块根本不存在）→ 同样回退 + warning + 回复照发。"""
    _with_kb(ctx, on=True)
    ctx.db.execute(
        "INSERT INTO channel_modules (channel, chat_id, module, updated_at)"
        " VALUES ('feishu', ?, 'ghost_module', '2026-10-01T00:00:00.000000+08:00')",
        (GROUP_A,),
    )
    ctx.llm = StubLlm(reply(STUB_REPLY))
    spy = SpyDispatcher()

    with caplog.at_level(logging.WARNING):
        _send(_handler(ctx, recorder, dispatcher=spy), "脏绑定的群", chat_id=GROUP_A)

    assert spy.params_of("chat.send")[0]["toolsets"] == DEFAULT_TOOLSETS_WITH_KB
    assert recorder.sent == [(GROUP_A, STUB_REPLY)]
    assert any(
        r.levelno == logging.WARNING and "ghost_module" in r.getMessage() for r in caplog.records
    ), "脏绑定值必须出现在 warning 里（否则线上查不出这条绑定从哪来）"
