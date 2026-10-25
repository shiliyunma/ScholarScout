"""对话循环契约：工具执行、轨迹、上限、取消、事件序列。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scholarscout.config import Features
from scholarscout.providers.llm import LlmError
from scholarscout.rpc.methods import AppContext
from scholarscout.runtime import loop, registry
from scholarscout.runtime.registry import ToolSpec
from scholarscout.state import sessions
from scholarscout.tools import register_tools
from scholarscout.tools import research_search as search_tool

from ..conftest import StubLlm, redirect_home, reply, tool_call

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
    assert stub.calls[0]["tools"][0]["function"]["name"] == "research__echo", (
        "发给模型的必须是线上名（点号会被 API 拒掉）；而落库与对外结果仍是规范名 research.echo"
    )
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


def test_exhausted_tool_budget_still_lets_the_model_answer(ctx: AppContext) -> None:
    """用满工具预算 ≠ 超限：最后一轮不给工具，模型仍要把结果讲完。"""
    _register_echo()
    session_id = _session(ctx)
    stub = StubLlm(
        reply("", tool_calls=[tool_call("research.echo", '{"query":"1"}', call_id="c1")]),
        reply("", tool_calls=[tool_call("research.echo", '{"query":"2"}', call_id="c2")]),
        reply("", tool_calls=[tool_call("research.echo", '{"query":"3"}', call_id="c3")]),
        reply("三次都查完了，结论是……"),  # 预算用尽后的那一轮（此时 tools=None）
    )
    ctx.llm = stub
    result = loop.run_turn(_context(ctx), session_id=session_id, text="循环")

    assert result.stop_reason == "completed"
    assert result.tool_calls == ["research.echo"] * 3
    assert result.text == "三次都查完了，结论是……"
    assert ctx.db.scalar_int("SELECT count(*) FROM tool_traces") == 3
    assert stub.calls[-1]["tools"] is None, "预算用尽后必须收走工具，逼模型收口"


def test_tool_limit_when_model_tries_to_exceed_budget(ctx: AppContext) -> None:
    _register_echo()
    session_id = _session(ctx)
    ctx.llm = StubLlm(
        reply("", tool_calls=[tool_call("research.echo", '{"query":"1"}', call_id="c1")]),
        reply("", tool_calls=[tool_call("research.echo", '{"query":"2"}', call_id="c2")]),
        reply("", tool_calls=[tool_call("research.echo", '{"query":"3"}', call_id="c3")]),
        reply("", tool_calls=[tool_call("research.echo", '{"query":"4"}', call_id="c4")]),
    )
    result = loop.run_turn(_context(ctx), session_id=session_id, text="循环")
    assert result.stop_reason == "tool_limit"
    assert ctx.db.scalar_int("SELECT count(*) FROM tool_traces") == 3, "第 4 次调用不该被执行"


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


def test_product_default_arm_declares_all_thirteen_and_reads_a_local_file(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """产品默认（`files` 开、渐进披露**关**）真跑一轮：「搜一下 + 读一份本地文件」端到端可用。

    这条钉的是**产品默认臂的真实形状**：2026-10-25 决议是 `enable_files=True` /
    `enable_tool_disclosure=False`（A/B 实测披露开着把整轮调用数 19 → 25、冗余 7 → 15，
    包含率无收益），所以默认臂**没有闸**，13 个工具（11 旧集 + `file.*`）全部写进 `tools=`。
    断言逐名列出而不是只数字数：数量对了但少的是 `file.read` 这类，一样是坏的产品。

    只把检索扇出换成空表（不出网；`research.search` 回 `ok:false`，但**不是** `tool_not_loaded`）。
    """
    redirect_home(monkeypatch, tmp_path)
    note = tmp_path / "data" / "notes" / "demo.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# 本地笔记\n教育智能体的检索增强先看 3 篇综述。\n", encoding="utf-8")
    register_tools()  # 默认参数 = 产品默认（13 个）
    monkeypatch.setattr(search_tool, "fetch_many", lambda *args, **kwargs: [])

    prod = replace(ctx, config=replace(ctx.config, features=Features()))
    session_id = _session(prod)
    stub = StubLlm(
        reply("", tool_calls=[tool_call("research.search", '{"query":"教育智能体"}')]),
        reply(
            "",
            tool_calls=[
                tool_call("file.read", '{"path":"data/notes/demo.md"}', call_id="call_2"),
            ],
        ),
        reply("检索没拿到来源，但本地笔记里写着：先看 3 篇综述。"),
    )
    prod.llm = stub

    result = loop.run_turn(
        _context(prod),
        session_id=session_id,
        text="搜一下教育智能体，再读一下 data/notes/demo.md",
    )

    assert prod.config.features.enable_files is True
    assert prod.config.features.enable_tool_disclosure is False
    assert result.stop_reason == "completed"
    assert result.tool_calls == ["research.search", "file.read"]
    # 默认臂没有闸：13 个声明全在（不是披露档的 7 个）
    declared = [decl["function"]["name"] for decl in stub.calls[0]["tools"]]
    assert declared == [
        "file__list",
        "file__read",
        "note__save",
        "py__exec",
        "research__delegate",
        "research__search",
        "route__plan",
        "route__revise",
        "skill__list",
        "skill__load",
        "source__fetch",
        "source__grade",
        "topic__build_map",
    ]
    # 一条 `tool_not_loaded` 都不许出现：默认臂里这两个工具本来就常驻
    fed_back = [msg for msg in stub.calls[-1]["messages"] if msg["role"] == "tool"]
    assert len(fed_back) == 2
    assert all("tool_not_loaded" not in str(msg["content"]) for msg in fed_back)
    assert "3 篇综述" in str(fed_back[-1]["content"]), "file.read 必须真的读到正文（不是空壳回包）"
    system = stub.calls[0]["messages"][0]
    assert system["role"] == "system"
    # 默认档是**逐工具点名**那版（FILES_RULE 在、披露档文本不在）
    assert "什么时候用只读文件工具" in str(system["content"])
    assert "工具是先查后用的" not in str(system["content"])


def test_disclosure_arm_declares_seven_and_is_not_deadlocked(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """披露臂（显式开 `enable_tool_disclosure`）真跑一轮：只发 7 个声明，且**核心集够用**。

    披露默认关不等于机制可以烂掉 —— 它是留给「长尾工具很多」那天的开关。这条盯着两件事：
    ① 声明从 13 收到 7（5 核心 + 2 元工具）；
    ② 核心集必须覆盖「先检索、再读本地文件」这两步，否则用户第一句需求就撞 `tool_not_loaded`
       （真坑：核心集曾经没有 `file.list` / `file.read`）。
    """
    redirect_home(monkeypatch, tmp_path)
    note = tmp_path / "data" / "notes" / "demo.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# 本地笔记\n教育智能体的检索增强先看 3 篇综述。\n", encoding="utf-8")
    register_tools(enable_files=True, enable_tool_disclosure=True)  # 披露臂：15 个注册
    monkeypatch.setattr(search_tool, "fetch_many", lambda *args, **kwargs: [])

    arm = replace(
        ctx,
        config=replace(
            ctx.config, features=Features(enable_files=True, enable_tool_disclosure=True)
        ),
    )
    session_id = _session(arm)
    stub = StubLlm(
        reply("", tool_calls=[tool_call("research.search", '{"query":"教育智能体"}')]),
        reply(
            "",
            tool_calls=[
                tool_call("file.read", '{"path":"data/notes/demo.md"}', call_id="call_2"),
            ],
        ),
        reply("检索没拿到来源，但本地笔记里写着：先看 3 篇综述。"),
    )
    arm.llm = stub

    result = loop.run_turn(
        _context(arm),
        session_id=session_id,
        text="搜一下教育智能体，再读一下 data/notes/demo.md",
    )

    assert result.stop_reason == "completed"
    assert result.tool_calls == ["research.search", "file.read"]
    declared = [decl["function"]["name"] for decl in stub.calls[0]["tools"]]
    assert declared == [
        "file__list",
        "file__read",
        "note__save",
        "research__search",
        "source__fetch",
        "tool__describe",
        "tool__search",
    ]
    fed_back = [msg for msg in stub.calls[-1]["messages"] if msg["role"] == "tool"]
    assert len(fed_back) == 2
    assert all("tool_not_loaded" not in str(msg["content"]) for msg in fed_back)
    assert "3 篇综述" in str(fed_back[-1]["content"]), "核心集里的 file.read 必须真能读到正文"
    system = stub.calls[0]["messages"][0]
    assert "工具是先查后用的" in str(system["content"]), "披露臂必须走披露档提示词"
