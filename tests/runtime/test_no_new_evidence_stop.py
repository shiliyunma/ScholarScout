"""A7：连续几轮检索都拿不到新来源时，必须**明确叫停**并如实总结。

为什么要这条：原来只有"工具预算"这一个刹车，于是模型会一路换词重搜到把预算烧光
（真机：一个会话 16 次工具调用、其中 2 次是同一件事换词）。停的条件应该是"没有新信息"，
不是"调用次数用完" —— 前者省钱且诚实，后者只是硬顶。

这些用例同时钉住 A1/A2 的注入：台账必须出现在**本轮消息的最后**（前缀稳定才能吃缓存）。
"""

from __future__ import annotations

from pathlib import Path

from scholarscout.rpc.methods import AppContext
from scholarscout.runtime import loop, registry
from scholarscout.runtime import research_state as rs
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state import sessions

from ..conftest import StubLlm, reply, tool_call


class _NoNewSources:
    """已注册的假工具：每次调用都回**同一批** source_id（模拟"换了词但没搜到新东西"）。"""

    calls = 0

    @classmethod
    def handler(cls, ctx: ToolContext, query: str = "") -> dict:
        cls.calls += 1
        return {
            "ok": True,
            "data": {"items": [], "query": query, "provider_status": []},
            "refs": {"source_ids": []},
        }


def _session(ctx: AppContext) -> str:
    return sessions.create_session(ctx.db, model=ctx.config.chat.model)


def _register_fake(monkeypatch) -> None:
    from scholarscout.runtime.registry import ToolSpec

    spec = ToolSpec(
        name="research.search",
        description="假检索",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        handler=_NoNewSources.handler,
        toolset="research",
    )
    registry.register(spec, replace=True)


def test_three_empty_rounds_stops_and_asks_for_an_honest_summary(
    ctx: AppContext, monkeypatch, tmp_path: Path
) -> None:
    _register_fake(monkeypatch)
    _NoNewSources.calls = 0
    # 脚本：连续 3 次"换个说法再搜"，第 4 次给它收尾的机会（此时工具已被收走）
    ctx.llm = StubLlm(
        reply("", tool_calls=[tool_call("research.search", '{"query": "角度一"}')]),
        reply("", tool_calls=[tool_call("research.search", '{"query": "角度二"}', call_id="c2")]),
        reply("", tool_calls=[tool_call("research.search", '{"query": "角度三"}', call_id="c3")]),
        reply("只搜到同一批资料，没有新证据。"),
    )
    result = loop.run_turn(ctx.turn_context(), session_id=_session(ctx), text="调研 X")

    assert _NoNewSources.calls == 3
    assert result.stop_reason == "no_new_evidence"
    # 收尾那次调用**不该再给工具**（否则它还会接着搜）
    assert ctx.llm.calls[-1]["tools"] is None
    # 倒数第二次调用带上的是"叫停"的说明
    # 收尾那次调用的最后一条消息就是"叫停"说明（它排在最后一个工具结果之后）
    stop_note = ctx.llm.calls[-1]["messages"][-1]["content"]
    assert "停止检索" in str(stop_note)
    assert "没有新增来源" in str(stop_note)
    assert "没有新证据" in result.text


def test_new_sources_reset_the_counter(ctx: AppContext, monkeypatch) -> None:
    """有新来源就把计数清零 —— 不能因为"前几轮空过"就提前放弃一个还有产出的方向。

    脚本：第 1 次搜到 src_a（新）→ 第 2 次还是 src_a（无新增，计数 1）→
    第 3 次搜到 src_b（新增，计数清零）。三轮都该照常跑完，不许提前叫停。
    """

    class _Alternating:
        calls = 0

        @classmethod
        def handler(cls, ctx: ToolContext, query: str = "") -> dict:
            cls.calls += 1
            fresh = ["src_a"] if cls.calls <= 2 else ["src_b"]
            return {"ok": True, "data": {"items": []}, "refs": {"source_ids": fresh}}

    from scholarscout.runtime.registry import ToolSpec

    registry.register(
        ToolSpec(
            name="research.search",
            description="假检索",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            handler=_Alternating.handler,
            toolset="research",
        ),
        replace=True,
    )
    _Alternating.calls = 0
    ctx.llm = StubLlm(
        reply("", tool_calls=[tool_call("research.search", '{"query": "a"}')]),
        reply("", tool_calls=[tool_call("research.search", '{"query": "b"}', call_id="c2")]),
        reply("", tool_calls=[tool_call("research.search", '{"query": "c"}', call_id="c3")]),
        reply("收尾"),
    )
    session_id = _session(ctx)
    result = loop.run_turn(ctx.turn_context(), session_id=session_id, text="调研 Y")
    assert result.stop_reason == "completed"
    state = rs.load_state(ctx.db, session_id)
    assert state is not None and int(state.get("empty_rounds") or 0) == 0


def test_ledger_note_is_injected_at_the_end_of_the_messages(ctx: AppContext) -> None:
    """台账注入必须在**消息末尾**：放头部会每轮都改前缀，服务端缓存必不命中（A6）。"""
    ctx.llm = StubLlm(reply("好的"))
    loop.run_turn(ctx.turn_context(), session_id=_session(ctx), text="调研 LLM Wiki")

    sent = ctx.llm.calls[0]["messages"]
    assert "【本轮研究台账】" in str(sent[-1]["content"])
    assert sent[-1]["role"] == "system"
    # 前缀仍然是 [system, 用户消息]，与上一轮逐字节相同
    assert sent[1]["role"] == "user" and sent[1]["content"] == "调研 LLM Wiki"


def test_loop_records_the_query_and_next_turn_rejects_the_repeat(
    ctx: AppContext, monkeypatch
) -> None:
    """端到端：循环记下第 1 轮的查询 → 第 2 轮换词重搜被工具拒绝。

    这条钉的是"单一写者"这个修复：原来工具也写台账，结果循环在轮末用它自己那份
    （不含工具的更新）整表覆盖 → 第 1 轮的查询记录被抹掉 → 第 2 轮照样重复搜
    （真机两轮验证抓到过）。
    """
    from scholarscout.rpc.methods import AppContext as _AppContext  # noqa: F401
    from scholarscout.runtime.registry import ToolSpec

    registry.register(
        ToolSpec(
            name="research.search",
            description="假检索",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            handler=lambda ctx, query="": {
                "ok": True,
                "data": {"items": []},
                "refs": {"source_ids": ["src_a"]},
            },
            toolset="research",
        ),
        replace=True,
    )
    session_id = _session(ctx)
    ctx.llm = StubLlm(
        reply("", tool_calls=[tool_call("research.search", '{"query": "GraphRAG 的社区摘要"}')]),
        reply("第一轮结论"),
    )
    loop.run_turn(ctx.turn_context(), session_id=session_id, text="调研 GraphRAG")

    state = rs.load_state(ctx.db, session_id)
    assert state is not None
    assert state["used_queries"] == ["GraphRAG 的社区摘要"], "循环必须把这条查询记进台账"
    # 下轮换词重搜：判重闸门直接命中（判据与产品代码同一实现）
    assert rs.find_repeat(state, "GraphRAG 社区摘要 是什么") == "GraphRAG 的社区摘要"
