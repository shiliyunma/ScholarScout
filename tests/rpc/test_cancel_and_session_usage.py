"""停止一轮对话，以及"这次会话一共花了多少"。

## 为什么要测这两件

**取消**：后端早就有 `chat.cancel`，但界面**一直没法调用** ——
它要 `turn_id`，而整轮结束前前端拿不到 turn_id（它随 `chat.done` 才回来）。
所以这不是"忘了加按钮"，是接口从设计上就不可用。改法是按 `session_id` 取消 +
后端记 `session_id -> turn_id`，这两个测试分别钉住"能取消"和"取消完不留残渣"。

**用量**：一轮 3–12 分钟、多次模型调用，用户只看得到"这一轮的 token"。
本会话累计必须能查、且**只在单价配置了的时候才折算金额**
（没配就报 0 会让人以为免费）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.runtime import registry
from scholarscout.runtime.registry import ToolSpec
from scholarscout.state import sessions, traces

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


def _register_echo(handler) -> ToolSpec:
    return registry.register(
        ToolSpec(
            name="research.echo",
            description="回显查询",
            parameters=ECHO_SCHEMA,
            handler=handler,
            toolset="research",
        )
    )


# ------------------------------------------------------------------ 按会话取消


def test_cancel_by_session_id_finds_the_running_turn(ctx: AppContext) -> None:
    """界面只知道 session_id，所以按会话必须能找到正在跑的那一轮。"""
    ctx.running["ses_1"] = "trn_running"
    result = dispatch(ctx, "chat.cancel", {"session_id": "ses_1"})
    assert result["ok"] is True
    assert result["turn_id"] == "trn_running"
    assert "trn_running" in ctx.cancelled
    # 生效时机要如实说明，别让人以为点了就立刻停
    assert "步间检查" in result["note"]


def test_cancel_without_running_turn_reports_failure(ctx: AppContext) -> None:
    """没在跑就不能假装成功 —— 静默回 ok 会让界面显示"已取消"而实际什么都没发生。"""
    result = dispatch(ctx, "chat.cancel", {"session_id": "ses_none"})
    assert result["ok"] is False
    assert result["reason"] == "no_running_turn"
    assert ctx.cancelled == set()


def test_cancel_by_turn_id_still_works(ctx: AppContext) -> None:
    """脚本/测试仍按 turn_id 取消（旧调用方式不能坏）。"""
    result = dispatch(ctx, "chat.cancel", {"turn_id": "trn_script"})
    assert result["ok"] is True
    assert "trn_script" in ctx.cancelled


def test_turn_id_wins_over_session_id(ctx: AppContext) -> None:
    """两个都给时以 turn_id 为准（显式指定不该被注册表覆盖）。"""
    ctx.running["ses_1"] = "trn_registry"
    result = dispatch(ctx, "chat.cancel", {"session_id": "ses_1", "turn_id": "trn_explicit"})
    assert result["turn_id"] == "trn_explicit"
    assert "trn_explicit" in ctx.cancelled
    assert "trn_registry" not in ctx.cancelled


# ------------------------------------------------- 取消真的会让这一轮停下来


def test_cancel_actually_stops_the_turn(ctx: AppContext) -> None:
    """端到端离线：工具执行中点了「停止」→ 这一轮以 `cancelled` 收尾。

    走的是真实链路（RPC → 注册表 → 主循环的步间检查），只把模型和工具换成桩。
    """
    session_id = _session(ctx)

    def handler(tool_ctx: object, query: str) -> dict[str, Any]:
        del tool_ctx, query
        # 用户在这一步点了「停止」：按会话取消（界面拿不到 turn_id）
        result = dispatch(ctx, "chat.cancel", {"session_id": session_id})
        assert result["ok"] is True
        return {"ok": True, "data": {}, "refs": {}}

    _register_echo(handler)
    ctx.llm = StubLlm(
        reply("", tool_calls=[tool_call("research.echo", json.dumps({"query": "x"}))])
    )
    result = dispatch(ctx, "chat.send", {"session_id": session_id, "text": "查一下"})
    assert result["stop_reason"] == "cancelled"
    # 只剩一次模型调用：取消在工具之后、下一次调用之前就生效了
    assert len(ctx.llm.calls) == 1


def test_running_registry_is_cleared_after_the_turn(ctx: AppContext) -> None:
    """跑完必须摘掉注册 —— 留着会让"下一次取消"打到一轮已经结束的轮次上。"""
    session_id = _session(ctx)
    ctx.llm = StubLlm(reply("好的"))
    dispatch(ctx, "chat.send", {"session_id": session_id, "text": "你好"})
    assert ctx.running == {}


def test_running_registry_is_cleared_even_when_the_turn_explodes(ctx: AppContext) -> None:
    """异常路径也要摘干净（finally 覆盖），否则取消会指向一轮早就死掉的轮次。"""
    session_id = _session(ctx)

    class BoomLlm:
        def chat(self, messages: list[dict[str, object]], *, tools=None) -> Any:
            del messages, tools
            raise RuntimeError("模型炸了")

    ctx.llm = BoomLlm()  # type: ignore[assignment]
    result = dispatch(ctx, "chat.send", {"session_id": session_id, "text": "你好"})
    assert result["stop_reason"] == "error"
    assert ctx.running == {}


# ------------------------------------------------------------------ 会话用量


def test_session_usage_sums_only_this_session(ctx: AppContext) -> None:
    """ "这次会话花了多少"必须只算这一次 —— 混进别的会话数字就不可核对。"""
    traces.record_usage(
        ctx.db,
        model="m",
        purpose="chat",
        session_id="ses_a",
        turn_id="trn_1",
        prompt_tokens=1000,
        completion_tokens=200,
    )
    traces.record_usage(
        ctx.db,
        model="m",
        purpose="chat",
        session_id="ses_a",
        turn_id="trn_2",
        prompt_tokens=500,
        completion_tokens=100,
    )
    traces.record_usage(
        ctx.db,
        model="m",
        purpose="summary",
        session_id="ses_a",
        turn_id="trn_2",
        prompt_tokens=300,
        completion_tokens=50,
    )
    traces.record_usage(
        ctx.db,
        model="m",
        purpose="chat",
        session_id="ses_b",
        turn_id="trn_9",
        prompt_tokens=9999,
        completion_tokens=9999,
    )
    usage = dispatch(ctx, "session.usage", {"session_id": "ses_a"})
    assert usage["calls"] == 3
    assert usage["tokens"] == {"prompt": 1800, "completion": 350, "cached": 0}
    # 服务端没报过缓存字段 → 命中率必须是 None（把"没报"算成 0% 会误导）
    assert usage["cache_hit_rate"] is None
    # 按用途拆开：摘要用的是另一档模型，混在一起看不清钱花在哪
    by_purpose = {row["purpose"]: row for row in usage["by_purpose"]}
    assert by_purpose["chat"]["calls"] == 2
    assert by_purpose["summary"]["calls"] == 1


def test_session_usage_does_not_invent_money(ctx: AppContext) -> None:
    """没配单价就回 None：报 0 会让用户以为这次会话免费。"""
    traces.record_usage(
        ctx.db,
        model="m",
        purpose="chat",
        session_id="ses_a",
        turn_id="trn_1",
        prompt_tokens=10,
        completion_tokens=5,
    )
    assert dispatch(ctx, "session.usage", {"session_id": "ses_a"})["est_cost"] is None


def test_session_usage_reports_cost_when_recorded(ctx: AppContext) -> None:
    """记了成本就报出来（不四舍五入成 0）。"""
    traces.record_usage(
        ctx.db,
        model="m",
        purpose="chat",
        session_id="ses_a",
        turn_id="trn_1",
        prompt_tokens=1000,
        completion_tokens=200,
        est_cost=0.0123,
    )
    assert dispatch(ctx, "session.usage", {"session_id": "ses_a"})["est_cost"] == pytest.approx(
        0.0123
    )


def test_session_usage_on_empty_session_is_zero_not_an_error(ctx: AppContext) -> None:
    """从没跑过的会话回全 0，别抛异常（界面每次切会话都会问一次）。"""
    session_id = _session(ctx)
    usage = dispatch(ctx, "session.usage", {"session_id": session_id})
    assert usage["calls"] == 0
    assert usage["tokens"] == {"prompt": 0, "completion": 0, "cached": 0}
    assert usage["cache_hit_rate"] is None
    assert usage["est_cost"] is None
