"""对话循环契约：工具执行、轨迹、上限、取消、事件序列。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from scholarscout.providers.llm import LlmError
from scholarscout.rpc.methods import AppContext
from scholarscout.runtime import loop, registry
from scholarscout.runtime.registry import ToolSpec
from scholarscout.state import sessions

from ..conftest import StubLlm, reply, tool_call

ECHO_SCHEMA = {
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


def _register_echo(handler=None) -> ToolSpec:
    def default_handler(ctx: object, query: str) -> dict[str, Any]:
        del ctx
        return {"ok": True, "data": {"query": query}, "refs": {"source_ids": ["src_1"]}}

    return registry.register(
        ToolSpec(
            name="research.echo",
            description="回显查询",
            parameters=ECHO_SCHEMA,
            handler=handler or default_handler,
            toolset="research",
        )
    )


def _context(ctx: AppContext) -> loop.TurnContext:
    return ctx.turn_context()


def _session(ctx: AppContext) -> str:
    return sessions.create_session(ctx.db, model=ctx.config.chat.model)


def test_plain_turn_completes(ctx: AppContext) -> None:
    session_id = _session(ctx)
    ctx.llm = StubLlm(reply("你好"))
    result = loop.run_turn(_context(ctx), session_id=session_id, text="嗨")
    assert result.stop_reason == "completed"
    assert result.text == "你好"
    assert result.tool_calls == []


def test_registered_tool_executes_and_is_traced(ctx: AppContext) -> None:
    _register_echo()
    session_id = _session(ctx)
    stub = StubLlm(
        reply("", tool_calls=[tool_call("research.echo", '{"query":"教育智能体"}')]),
        reply("根据资料，这个方向从三篇综述入手"),
    )
    ctx.llm = stub

    events: list[tuple[str, dict[str, Any]]] = []
    result = loop.run_turn(
        _context(ctx),
        session_id=session_id,
        text="帮我找入门资料",
        emit=lambda k, p: events.append((k, p)),
    )

    assert result.stop_reason == "completed"
    assert result.tool_calls == ["research.echo"]
    assert result.text == "根据资料，这个方向从三篇综述入手"
    # 工具声明确实传给了模型
    assert stub.calls[0]["tools"][0]["function"]["name"] == "research.echo"
    # 轨迹落库且成功
    row = ctx.db.query_one("SELECT * FROM tool_traces")
    assert row is not None and row["ok"] == 1 and row["tool_name"] == "research.echo"
    # 工具结果作为 tool 消息喂回模型
    tool_messages = stub.calls[1]["messages"]
    assert tool_messages[-1]["role"] == "tool"
    assert "教育智能体" in str(tool_messages[-1]["content"])
    # 事件序列包含工具调用与结果
    kinds = [k for k, _ in events]
    assert kinds.count("chat.tool_call") == 1
    assert kinds.count("chat.tool_result") == 1
    assert kinds[-1] == "chat.done"


def test_tool_exception_is_reported_not_raised(ctx: AppContext) -> None:
    def boom(ctx_arg: object, query: str) -> dict[str, Any]:
        del ctx_arg, query
        raise RuntimeError("抓取失败")

    _register_echo(boom)
    session_id = _session(ctx)
    ctx.llm = StubLlm(
        reply("", tool_calls=[tool_call("research.echo", '{"query":"x"}')]),
        reply("工具坏了，我先按已有知识回答"),
    )
    result = loop.run_turn(_context(ctx), session_id=session_id, text="找资料")

    assert result.stop_reason == "completed"
    row = ctx.db.query_one("SELECT * FROM tool_traces")
    assert row is not None and row["ok"] == 0
    assert "抓取失败" in str(row["error"])


def test_invalid_tool_arguments_json_is_handled(ctx: AppContext) -> None:
    _register_echo()
    session_id = _session(ctx)
    ctx.llm = StubLlm(
        reply("", tool_calls=[tool_call("research.echo", "{不是 JSON")]),
        reply("参数不对，我换个说法"),
    )
    result = loop.run_turn(_context(ctx), session_id=session_id, text="找资料")
    assert result.stop_reason == "completed"
    row = ctx.db.query_one("SELECT * FROM tool_traces")
    assert row is not None and row["ok"] == 0
    assert "JSON" in str(row["error"])


def test_tool_limit_stops_the_loop(ctx: AppContext) -> None:
    _register_echo()
    session_id = _session(ctx)
    # 每轮都要求调用工具，配置上限是 3（见 conftest 的 runtime profile）
    ctx.llm = StubLlm(
        reply("", tool_calls=[tool_call("research.echo", '{"query":"1"}', call_id="c1")]),
        reply("", tool_calls=[tool_call("research.echo", '{"query":"2"}', call_id="c2")]),
        reply("", tool_calls=[tool_call("research.echo", '{"query":"3"}', call_id="c3")]),
    )
    result = loop.run_turn(_context(ctx), session_id=session_id, text="循环")
    assert result.stop_reason == "tool_limit"
    assert ctx.db.scalar_int("SELECT count(*) FROM tool_traces") == 3


def test_llm_error_sets_error_stop_reason(ctx: AppContext) -> None:
    class Boom:
        def chat(self, messages, *, tools=None):
            raise LlmError("timeout", "超时了")

    session_id = _session(ctx)
    ctx.llm = Boom()
    result = loop.run_turn(_context(ctx), session_id=session_id, text="在吗")
    assert result.stop_reason == "error"
    assert "timeout" in str(result.error)
    # 即便失败也要落助手消息，保证会话可回放
    assert ctx.db.scalar_int("SELECT count(*) FROM messages WHERE role = 'assistant'") == 1


def test_cancelled_turn_short_circuits(ctx: AppContext, monkeypatch) -> None:
    monkeypatch.setattr(loop.TurnContext, "is_cancelled", lambda self, turn_id: True)
    session_id = _session(ctx)
    ctx.llm = StubLlm()  # 不应被调用
    result = loop.run_turn(_context(ctx), session_id=session_id, text="取消我")
    assert result.stop_reason == "cancelled"


def test_second_turn_sees_history(ctx: AppContext) -> None:
    session_id = _session(ctx)
    stub = StubLlm(reply("第一轮回答"), reply("第二轮回答"))
    ctx.llm = stub
    loop.run_turn(_context(ctx), session_id=session_id, text="第一轮问题")
    loop.run_turn(_context(ctx), session_id=session_id, text="第二轮问题")

    messages = stub.calls[1]["messages"]
    contents = [str(m.get("content")) for m in messages]
    assert "第一轮问题" in contents
    assert "第一轮回答" in contents
    assert "第二轮问题" in contents
    assert messages[0]["role"] == "system"


def test_kb_tools_not_passed_when_disabled(ctx: AppContext) -> None:
    registry.register(
        ToolSpec(
            name="kb.search",
            description="d",
            parameters=ECHO_SCHEMA,
            handler=lambda **_: {},
            toolset="kb",
        )
    )
    session_id = _session(ctx)
    stub = StubLlm(reply("好"))
    ctx.llm = stub
    loop.run_turn(_context(ctx), session_id=session_id, text="查知识库", toolsets=["kb"])
    assert stub.calls[0]["tools"] is None, "enable_kb=False 时不应把 kb 工具暴露给模型"
