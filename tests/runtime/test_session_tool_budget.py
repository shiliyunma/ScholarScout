"""会话级工具调用上限（`runtime.max_tool_calls_per_session`，docs/20 §5.1 / 评审 R-07）。

**全桩**：不真调模型、不真出网（模型是脚本化的假客户端）。守五条：

1. **默认不限**：没配这个键时守卫为 `None`（不查库、不判断），轮次行为与引入前逐字一致；
2. **超限当场停手**：配 `2` → 第 3 次工具调用**不执行**，`stop_reason="session_tool_limit"`，
   而且**已生成的正文不丢**（无工具收口那一次照常落库）；
3. **新轮次被拒**：同一个会话再 `submit_turn` → `reason="session_tool_limit_exceeded"` + 明确文案，
   一个字都不落库；
4. **`chat.status` 可见**：`session_tool_budget` 与 `global_budget` 并列；未配置 `enabled=False`、
   配了且未超/已超各回各的真实值；
5. **重启后计数仍在**：换一个**新的 `Database` 连接 + 新的 `SessionRuntime`**（内存里零计数）
   仍然数得到这个会话的历史用量 —— 因为计数来自 `tool_traces`，不是内存计数器。

第 5 条是这条能力与"本轮预算"（`TurnBudget`）最本质的区别：它必须**跨轮、跨进程**成立，
否则"一个下午烧穿额度"的问题一点没解决。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from scholarscout.config import RuntimeProfile
from scholarscout.rpc.registry import AppContext
from scholarscout.runtime import budget as daily_budget
from scholarscout.runtime import registry
from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.session_runtime import SessionRuntime
from scholarscout.state import sessions, traces
from scholarscout.state.db import Database

from ..conftest import StubLlm, reply, tool_call

ECHO_TOOL = "research.echo"
ECHO_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    registry.clear_registry()
    yield
    registry.clear_registry()


def _register_echo(calls: list[str] | None = None) -> None:
    """注册一个回显工具；`calls` 非空时把每次真实执行记进去（"到底执行了几次"的直接证据）。"""

    def handler(tool_ctx: Any, query: str) -> dict[str, Any]:
        del tool_ctx
        if calls is not None:
            calls.append(query)
        return {"ok": True, "data": {"query": query}, "refs": {}}

    registry.register(
        ToolSpec(
            name=ECHO_TOOL,
            description="回显查询",
            parameters=ECHO_SCHEMA,
            handler=handler,
            toolset="research",
        )
    )


def _with_runtime(ctx: AppContext, **kwargs: Any) -> None:
    """把测试配置的 `runtime` 段换成指定值（`Config` 是 frozen dataclass，只能 replace）。"""
    ctx.config = replace(ctx.config, runtime=replace(ctx.config.runtime, **kwargs))


def _new_session(ctx: AppContext, title: str = "会话 A") -> str:
    session_id = sessions.create_session(ctx.db, model=ctx.config.chat.model)
    sessions.rename_session(ctx.db, session_id, title)
    return session_id


def _always_tool_then_summary(summary: str, *, per_round: int = 1) -> Any:
    """只要还给了工具就再调一次，tools=None 时给出总结正文（并记录每次调用的 tools）。"""

    class _Scripted:
        def __init__(self) -> None:
            self.tools_seen: list[bool] = []
            self._seq = 0

        def chat(self, messages: list[dict[str, Any]], *, tools: Any = None) -> Any:
            del messages
            self.tools_seen.append(tools is not None)
            if tools is None:
                return reply(summary)
            self._seq += 1
            return reply(
                "",
                tool_calls=[
                    tool_call(
                        ECHO_TOOL,
                        '{"query":"x"}',
                        call_id=f"call_{self._seq}_{index}",
                    )
                    for index in range(per_round)
                ],
            )

    return _Scripted()


def _seed_traces(db: Database, session_id: str, count: int) -> None:
    """直接往 `tool_traces` 里落 `count` 行（模拟"这个会话已经调过这么多次"）。"""
    for index in range(count):
        traces.record_trace(
            db,
            session_id=session_id,
            turn_id="trn_seed",
            step=index,
            tool_name=ECHO_TOOL,
            ok=True,
            args={"query": f"q{index}"},
            result={"ok": True},
        )


# ------------------------------------------------------------------ 1. 默认不限


def test_no_limit_configured_keeps_todays_behaviour(ctx: AppContext) -> None:
    """**默认 None = 不限**：守卫不介入，连开两轮、每轮用满 3 次工具都不被拦。"""
    _register_echo()
    llm = _always_tool_then_summary("普通总结", per_round=3)
    ctx.llm = llm
    session_id = _new_session(ctx)
    runtime = SessionRuntime(ctx, max_concurrent=2, thread_prefix="probe-sessionlimit-off")
    try:
        assert RuntimeProfile().max_tool_calls_per_session is None
        assert daily_budget.session_guard_from_runtime(ctx.config.runtime) is None
        for text in ("第一问", "第二问"):
            accepted = runtime.submit_turn(
                session_id=session_id, text=text, toolsets=["research"], emit=None
            )
            assert accepted["ok"] is True, accepted
            result = runtime.wait(str(accepted["turn_id"]), timeout=10)
            assert result is not None
            assert result.stop_reason == "completed"

        # 跨轮累计 6 次真实执行；没有上限就一个都不拦
        assert traces.count_tool_calls(ctx.db, session_id=session_id) == 6
        assert runtime.status(session_id)["session_tool_budget"]["enabled"] is False
    finally:
        runtime.shutdown()


# ------------------------------------------------------------------ 2. 超限当场收口


def test_the_third_tool_call_is_blocked_and_the_text_survives(ctx: AppContext) -> None:
    """配 `2` → 第 3 次工具调用**不执行**；收口仍给正文（`stop_reason="session_tool_limit"`）。

    这一条故意让**同一批**里多带一个调用（`[call_2, call_3]`）：第 3 次撞上限时它就在批内，
    所以顺带钉住"未执行的那条也要有一条如实的 tool 回复"（否则 assistant 的 tool_calls
    会有 id 没有结果，报文残缺）。
    """
    _with_runtime(ctx, max_tool_calls_per_session=2)
    executed: list[str] = []
    _register_echo(executed)
    llm = StubLlm(
        reply("", tool_calls=[tool_call(ECHO_TOOL, '{"query":"a"}', call_id="call_1")]),
        reply(
            "",
            tool_calls=[
                tool_call(ECHO_TOOL, '{"query":"b"}', call_id="call_2"),
                tool_call(ECHO_TOOL, '{"query":"c"}', call_id="call_3"),
            ],
        ),
        reply("已到会话上限，用已有资料的总结"),
    )
    ctx.llm = llm
    session_id = _new_session(ctx)
    runtime = SessionRuntime(ctx, max_concurrent=2, thread_prefix="probe-sessionlimit-stop")
    try:
        accepted = runtime.submit_turn(
            session_id=session_id, text="A 的问题", toolsets=["research"], emit=None
        )
        assert accepted["ok"] is True, accepted
        result = runtime.wait(str(accepted["turn_id"]), timeout=10)

        assert result is not None
        assert result.stop_reason == daily_budget.SESSION_STOP_REASON == "session_tool_limit"
        # 真的只执行了 2 次（第 3 次被拦在"执行之前"）
        assert executed == ["a", "b"]
        assert traces.count_tool_calls(ctx.db, session_id=session_id) == 2
        # 收口那一次**没有工具**，且正文没丢
        assert llm.calls[-1]["tools"] is None
        assert result.text == "已到会话上限，用已有资料的总结"
        # 被拦下的那次调用有一条如实的 tool 回复（不是假装成功、也不是留空）
        summary_messages = llm.calls[-1]["messages"]
        unanswered = {"call_1", "call_2", "call_3"} - {
            str(item.get("tool_call_id"))
            for item in summary_messages
            if isinstance(item, dict) and item.get("role") == "tool"
        }
        assert unanswered == set(), "每个 tool_call id 都必须有对应结果（含被拦下的 call_3）"
        # 被拦下的那条还必须**如实**说「未执行」（不是假装成功、也不是留空）
        blocked = [
            item
            for item in summary_messages
            if isinstance(item, dict) and item.get("tool_call_id") == "call_3"
        ]
        assert len(blocked) == 1
        assert "未执行" in str(blocked[0]["content"])
        rows = ctx.db.query("SELECT content FROM messages WHERE session_id = ?", (session_id,))
        assert any("已到会话上限，用已有资料的总结" in str(row["content"]) for row in rows)
    finally:
        runtime.shutdown()


# ------------------------------------------------------------------ 3. 新轮次被拒


def test_a_new_turn_is_refused_once_the_session_budget_is_spent(ctx: AppContext) -> None:
    """用满后新轮次**直接拒绝**：理由明确、带上快照、一个字都不落库。"""
    _with_runtime(ctx, max_tool_calls_per_session=2)
    _register_echo()
    ctx.llm = _always_tool_then_summary("不该被用到")
    session_id = _new_session(ctx)
    _seed_traces(ctx.db, session_id, 2)
    runtime = SessionRuntime(ctx, max_concurrent=2, thread_prefix="probe-sessionlimit-refuse")
    try:
        before = ctx.db.query("SELECT id FROM messages WHERE session_id = ?", (session_id,))
        rejected = runtime.submit_turn(
            session_id=session_id, text="再问一句", toolsets=["research"], emit=None
        )

        assert rejected["ok"] is False
        assert rejected["reason"] == daily_budget.SESSION_REJECT_REASON
        assert rejected["reason"] == "session_tool_limit_exceeded"
        assert rejected["session_tool_budget"]["used"] == 2
        assert rejected["session_tool_budget"]["limit"] == 2
        assert rejected["session_tool_budget"]["exceeded"] is True
        # 文案必须说清"怎么办"（新开会话 / 调高键），不能只回一句"不行"
        assert "上限" in rejected["note"]
        assert "新开一个会话" in rejected["note"]
        assert rejected["running"] == []
        after = ctx.db.query("SELECT id FROM messages WHERE session_id = ?", (session_id,))
        assert after == before, "被拒绝的轮次不该留下任何消息"
    finally:
        runtime.shutdown()


# ------------------------------------------------------------------ 4. chat.status 可见


def test_chat_status_exposes_the_session_tool_budget(ctx: AppContext) -> None:
    """`session_tool_budget` 与 `global_budget` 并列：未配置 `enabled=False`；未超/已超各回真实值。"""
    _register_echo()
    session_id = _new_session(ctx)
    runtime = SessionRuntime(ctx, max_concurrent=2, thread_prefix="probe-sessionlimit-status")
    try:
        # ① 未配置 → enabled=False，各项 None（不假报 0）
        off = runtime.status(session_id)["session_tool_budget"]
        assert off["enabled"] is False
        assert off["used"] is None
        assert off["limit"] is None
        assert off["exceeded"] is False
        assert "global_budget" in runtime.status(session_id)

        # ② 配了但没超
        _seed_traces(ctx.db, session_id, 1)
        _with_runtime(ctx, max_tool_calls_per_session=3)
        under = runtime.status(session_id)["session_tool_budget"]
        assert under["enabled"] is True
        assert (under["used"], under["limit"], under["remaining"]) == (1, 3, 2)
        assert under["exceeded"] is False
        assert under["reason"] is None

        # ③ 已超（>= 上限即超：第 3 次之前就会被拦，所以恰好等于上限也算"不能再调"）
        _seed_traces(ctx.db, session_id, 2)
        over = runtime.status(session_id)["session_tool_budget"]
        assert (over["used"], over["remaining"]) == (3, 0)
        assert over["exceeded"] is True
        assert over["reason"] == daily_budget.SESSION_STOP_REASON
    finally:
        runtime.shutdown()


# ------------------------------------------------------------------ 5. 重启后仍然准


def test_the_count_survives_a_restart_because_it_is_read_from_the_ledger(
    ctx: AppContext, tmp_path: Any
) -> None:
    """换一个**新连接 + 新运行时**（内存里零计数）仍然数得到历史调用 —— 计数在库里。"""
    _with_runtime(ctx, max_tool_calls_per_session=5)
    _register_echo()
    session_id = _new_session(ctx)
    # 跑一轮，真的执行 2 次工具（不是手工插行）
    ctx.llm = StubLlm(
        reply("", tool_calls=[tool_call(ECHO_TOOL, '{"query":"a"}', call_id="call_a")]),
        reply("", tool_calls=[tool_call(ECHO_TOOL, '{"query":"b"}', call_id="call_b")]),
        reply("第一轮的总结"),
    )
    first = SessionRuntime(ctx, max_concurrent=2, thread_prefix="probe-sessionlimit-before")
    try:
        accepted = first.submit_turn(
            session_id=session_id, text="先跑一轮", toolsets=["research"], emit=None
        )
        assert accepted["ok"] is True, accepted
        result = first.wait(str(accepted["turn_id"]), timeout=10)
        assert result is not None and result.stop_reason == "completed"
        assert traces.count_tool_calls(ctx.db, session_id=session_id) == 2
    finally:
        first.shutdown()

    # ---- "重启"：另开一条连接（同一个库文件）+ 全新的运行时（`_units` 是空的）
    db_path = str(tmp_path / "state.db")
    restarted = Database(db_path)
    app = AppContext(config=ctx.config, db=restarted)
    second = SessionRuntime(app, max_concurrent=2, thread_prefix="probe-sessionlimit-after")
    try:
        # 新进程里既没有 SessionUnit 的热缓存，也没有任何内存计数器 —— 数字照样是 2
        status = second.status(session_id)["session_tool_budget"]
        assert (status["enabled"], status["used"], status["limit"]) == (True, 2, 5)

        # 把上限压到已用数：新进程也必须当场拦住新轮次
        app.config = replace(
            app.config, runtime=replace(app.config.runtime, max_tool_calls_per_session=2)
        )
        rejected = second.submit_turn(
            session_id=session_id, text="重启后再问", toolsets=["research"], emit=None
        )
        assert rejected["ok"] is False
        assert rejected["reason"] == daily_budget.SESSION_REJECT_REASON
        assert rejected["session_tool_budget"]["used"] == 2
    finally:
        second.shutdown()
        restarted.close()
