"""用量记账：**每次模型调用后立即记**，而不是整轮结束才记一次。

为什么改（真机发现的缺口）：原来 `traces.record_usage` 只在 `run_turn` 尾部调一次，
所以**被「停止」或中断的轮次会把已经花掉的 token 全丢掉** —— 实测杀掉实例后，
该会话的用量一条不剩，而账单是真花了的。对代价敏感的用户来说，
"会话累计"少算比"看不到"更糟：它看起来像个可信的数字。

配套口径：
- 一次模型调用 = 一条 `usage_records`（purpose="chat"）；标题总结记 purpose="title"
  （用户据此判断"关掉标题开关能省多少"）；
- 不许双重记账：整轮尾部不再记总账（用"调用次数 == 行数"钉住）。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

from scholarscout.rpc.errors import RpcError
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.runtime import registry
from scholarscout.runtime.registry import ToolSpec
from scholarscout.state import sessions

from ..conftest import StubLlm, reply, tool_call

ECHO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    registry.clear_registry()
    yield
    registry.clear_registry()


def _session(ctx: AppContext) -> str:
    return sessions.create_session(ctx.db, model=ctx.config.chat.model)


def _register_echo() -> None:
    registry.register(
        ToolSpec(
            name="research.echo",
            description="回显查询",
            parameters=ECHO_SCHEMA,
            handler=lambda ctx, query: {"ok": True, "data": {"query": query}, "refs": {}},
            toolset="research",
        )
    )


def _usage_rows(ctx: AppContext, session_id: str) -> list[dict[str, Any]]:
    rows = ctx.db.query(
        "SELECT purpose, prompt_tokens, completion_tokens FROM usage_records"
        " WHERE session_id = ? ORDER BY rowid",
        (session_id,),
    )
    return [dict(row) for row in rows]


def test_each_model_call_records_one_row(ctx: AppContext) -> None:
    """两次模型调用 → 两条账；而且总量与桩回复声明的一致（没有双重记账）。"""
    session_id = _session(ctx)
    _register_echo()
    ctx.emit = None
    ctx.llm = StubLlm(
        reply("", tool_calls=[tool_call("research.echo", '{"query":"x"}')]),
        reply("好了"),
    )
    dispatch(ctx, "chat.send", {"session_id": session_id, "text": "查一下"})
    rows = _usage_rows(ctx, session_id)
    assert len(rows) == len(ctx.llm.calls) == 2
    assert [row["purpose"] for row in rows] == ["chat", "chat"]
    assert sum(row["prompt_tokens"] for row in rows) == 22  # 桩回复每次 11
    assert sum(row["completion_tokens"] for row in rows) == 14


def test_cancelled_turn_still_records_what_it_spent(ctx: AppContext) -> None:
    """**这就是那个缺口**：一轮跑到一半被停止，已经花掉的 token 必须留在账上。"""
    session_id = _session(ctx)

    def handler(_ctx: Any, query: str) -> dict[str, Any]:
        dispatch(ctx, "chat.cancel", {"session_id": session_id})  # 用户在工具这一步点了停止
        return {"ok": True, "data": {"query": query}, "refs": {}}

    registry.register(
        ToolSpec(
            name="research.echo",
            description="回显查询",
            parameters=ECHO_SCHEMA,
            handler=handler,
            toolset="research",
        )
    )
    ctx.emit = None
    ctx.llm = StubLlm(reply("", tool_calls=[tool_call("research.echo", '{"query":"x"}')]))
    result = dispatch(ctx, "chat.send", {"session_id": session_id, "text": "查一下"})
    assert result["stop_reason"] == "cancelled"
    rows = _usage_rows(ctx, session_id)
    assert len(rows) == 1, "取消的轮次也必须留下这一笔账"
    assert rows[0]["prompt_tokens"] == 11


def test_failed_turn_still_records_what_it_spent(ctx: AppContext) -> None:
    """模型第二次调用报错 → 第一次调用花掉的钱不能丢。"""
    session_id = _session(ctx)
    _register_echo()
    ctx.emit = None

    class FlakyLlm(StubLlm):
        def chat(self, messages: list[dict[str, object]], *, tools: Any = None) -> Any:
            if len(self.calls) >= 1:
                self.calls.append({"messages": list(messages), "tools": tools})
                raise RuntimeError("模型炸了")
            return super().chat(messages, tools=tools)

    ctx.llm = FlakyLlm(reply("", tool_calls=[tool_call("research.echo", '{"query":"x"}')]))
    result = dispatch(ctx, "chat.send", {"session_id": session_id, "text": "查一下"})
    assert result["stop_reason"] == "error"
    assert len(_usage_rows(ctx, session_id)) == 1


def test_title_call_is_recorded_separately(ctx: AppContext) -> None:
    """标题那次调用的钱单独记（purpose="title"）—— 用户要能算出关掉它能省多少。"""
    session_id = _session(ctx)
    ctx.config = replace(ctx.config, generation=replace(ctx.config.generation, title_by_model=True))
    ctx.emit = None
    ctx.llm = StubLlm(reply("标题党"), reply("答复"))
    dispatch(ctx, "chat.send", {"session_id": session_id, "text": "帮我看看"})
    rows = _usage_rows(ctx, session_id)
    assert [row["purpose"] for row in rows] == ["title", "chat"]


def test_title_switch_off_saves_that_row(ctx: AppContext) -> None:
    """关掉标题开关 → 账上只剩这一轮的调用（省钱是可验证的）。"""
    session_id = _session(ctx)
    ctx.emit = None
    ctx.llm = StubLlm(reply("答复"))
    dispatch(ctx, "chat.send", {"session_id": session_id, "text": "帮我看看"})
    assert [row["purpose"] for row in _usage_rows(ctx, session_id)] == ["chat"]


def test_session_usage_reports_the_cancelled_spend(ctx: AppContext) -> None:
    """会话累计要包含被取消那一轮的钱（否则界面上的数字是错的）。"""
    session_id = _session(ctx)

    def handler(_ctx: Any, query: str) -> dict[str, Any]:
        dispatch(ctx, "chat.cancel", {"session_id": session_id})
        return {"ok": True, "data": {"query": query}, "refs": {}}

    registry.register(
        ToolSpec(
            name="research.echo",
            description="回显查询",
            parameters=ECHO_SCHEMA,
            handler=handler,
            toolset="research",
        )
    )
    ctx.emit = None
    ctx.llm = StubLlm(reply("", tool_calls=[tool_call("research.echo", '{"query":"x"}')]))
    dispatch(ctx, "chat.send", {"session_id": session_id, "text": "查一下"})
    usage = dispatch(ctx, "session.usage", {"session_id": session_id})
    assert usage["calls"] == 1
    assert usage["tokens"]["prompt"] == 11


def test_unknown_session_still_raises(ctx: AppContext) -> None:
    """跑不存在的会话仍然要报错（记账改动不该放宽入参校验）。"""
    with pytest.raises(RpcError):
        dispatch(ctx, "chat.send", {"session_id": "ses_nope", "text": "x"})
