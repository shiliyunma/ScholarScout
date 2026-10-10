"""``research.delegate`` 契约：模型可见的委派入口（子 Agent P1 **工具化**）。

六件必须成立（任务卡 + ``docs/01 §5.18`` + ``docs/16 §3.3``）：

1. **schema 卡死上限** —— ``goals`` 是字符串数组、1..3 条、``additionalProperties=false``；
2. **默认关** —— ``runtime.subtask_enabled=false`` 时**一次模型都不调**，直接 ``ok=false``；
3. **额度不足就拒绝且不消耗** —— 主账剩余为 0 时 ``ok=false``，账目一个都不动；
4. **同一本账** —— 子任务实际消耗的工具调用数**回写到主循环的 ``TurnBudget``**；
   相邻两次委派的总消耗永不超主预算（这是把 P1 做成工具后最核心的不变量）；
5. **失败隔离** —— 单个子任务失败只影响它自己，其余照跑（整体仍 ``ok=true``）；
6. **内部异常不外抛** —— ``run_subtasks`` 之外的意外收敛成 ``ok=false`` + 原因，主循环照常。

全程离线（桩模型 + 桩工具），不碰真网。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from scholarscout.providers.llm import ChatReply, LlmError
from scholarscout.runtime import registry
from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.subtasks import MAX_DELEGATE_GOALS, delegate_budget
from scholarscout.runtime.toolctx import ToolContext, TurnBudget
from scholarscout.state import sessions
from scholarscout.tools.delegate import PARAMETERS, SPEC, research_delegate

from ..conftest import StubLlm, reply, tool_call

_ECHO_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    """委派测试要一个**可控的**注册表：只放桩工具，别把真工具（会出网）引进来。"""
    registry.clear_registry()
    yield
    registry.clear_registry()


def _register_echo() -> ToolSpec:
    def handler(ctx: object, query: str = "") -> dict[str, Any]:
        del ctx, query
        return {
            "ok": True,
            "data": {"items": [{"source_id": "src_1", "title": "T"}]},
            "refs": {"source_ids": ["src_1"]},
        }

    return registry.register(
        ToolSpec(
            name="research.echo",
            description="回显查询",
            parameters=_ECHO_SCHEMA,
            handler=handler,
            toolset="research",
        )
    )


class _ScriptLlm:
    """按**子问题**给脚本的桩模型：每条脚本一项，用尽即报错（缺桩要显式暴露）。

    脚本项可以是 ``ChatReply``，也可以是 ``Exception`` 实例（抛出来 —— 用来造
    「子任务失败」）。``chat`` 每次都记调用，供「一次都没调」这类断言用。
    """

    def __init__(self, scripts: dict[str, list[Any]]) -> None:
        self._scripts = {goal: list(items) for goal, items in scripts.items()}
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None) -> ChatReply:
        self.calls.append({"messages": list(messages), "tools": tools})
        goal = str(messages[1]["content"])
        script = self._scripts.get(goal)
        if not script:
            raise AssertionError(f"未为子问题 {goal!r} 准备足够的桩回复")
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _tool_ctx(
    config: Any,
    db: Any,
    llm: Any,
    *,
    session_id: str,
    turn_id: str = "trn_1",
    enabled: bool = True,
    budget: TurnBudget | None = None,
    max_calls: int = 8,
) -> ToolContext:
    # max_tool_calls_per_turn 决定 P1 上限（= 它的 1/2）；conftest 默认 3 太小（上限只有 1），
    # 这里显式放大，让"同一本账"的多次委派有足够空间被观测到。
    runtime = replace(config.runtime, max_tool_calls_per_turn=max_calls, subtask_enabled=enabled)
    return ToolContext(
        db=db,
        config=replace(config, runtime=runtime),
        llm=llm,
        session_id=session_id,
        turn_id=turn_id,
        budget=budget,
    )


def _one_tool_call_script(final: str, *, call_id: str) -> list[Any]:
    """一个子任务「调一次工具再收口」的标准脚本：消耗恰好 1 次工具调用。"""
    return [
        reply("", tool_calls=[tool_call("research.echo", "{}", call_id=call_id)]),
        reply(final),
    ]


# ================================================================ 1. schema


def test_spec_shape() -> None:
    assert SPEC.name == "research.delegate"
    assert SPEC.toolset == "research"
    # 点号必须换成线上名（OpenAI 兼容 API 只收 ^[a-zA-Z0-9_-]+$）
    assert registry.wire_name(SPEC.name) == "research__delegate"


def test_schema_pins_goals_to_one_through_three() -> None:
    assert PARAMETERS["additionalProperties"] is False
    assert set(PARAMETERS["properties"]) == {"goals"}
    assert PARAMETERS["required"] == ["goals"]
    goals = PARAMETERS["properties"]["goals"]
    assert goals["type"] == "array"
    assert goals["items"] == {"type": "string"}
    assert goals["minItems"] == 1
    assert goals["maxItems"] == MAX_DELEGATE_GOALS == 3


# ================================================================ 2. 默认关


def test_disabled_switch_returns_ok_false_without_calling_the_model(config, db) -> None:
    session_id = sessions.create_session(db, model="stub-model")
    stub = StubLlm()  # 一旦被调用就 AssertionError —— 正好证明「一次都没调」
    ctx = _tool_ctx(config, db, stub, session_id=session_id, enabled=False)

    result = research_delegate(ctx, ["g1"])

    assert result["ok"] is False
    assert "subtask_enabled" in result["error"]
    assert stub.calls == [], "开关关着时不该发起任何模型调用"
    assert result["data"]["items"] == []
    assert db.scalar_int("SELECT count(*) FROM usage_records") == 0


# ================================================================ 3. 参数非法


@pytest.mark.parametrize(
    ("goals", "fragment"),
    [
        ("不是数组", "必须是字符串数组"),
        ([], "不能为空"),
        (["   ", ""], "不能为空"),
        (["a", "b", "c", "d"], "最多 3 个"),
    ],
)
def test_invalid_goals_are_rejected(config, db, goals, fragment) -> None:
    session_id = sessions.create_session(db, model="stub-model")
    stub = StubLlm()
    ctx = _tool_ctx(config, db, stub, session_id=session_id)

    result = research_delegate(ctx, goals)

    assert result["ok"] is False
    assert "参数非法" in result["error"] and fragment in result["error"]
    assert stub.calls == []


def test_too_many_goals_reports_how_many_were_sent(config, db) -> None:
    session_id = sessions.create_session(db, model="stub-model")
    ctx = _tool_ctx(config, db, StubLlm(), session_id=session_id)
    result = research_delegate(ctx, ["a", "b", "c", "d"])
    assert result["data"]["counts"]["requested"] == 4


# =========================================================== 4. 额度不足（不消耗）


def test_exhausted_budget_is_refused_and_spends_nothing(config, db) -> None:
    session_id = sessions.create_session(db, model="stub-model")
    ledger = TurnBudget(total=2, used=2)  # 主账已见底
    stub = StubLlm()
    ctx = _tool_ctx(config, db, stub, session_id=session_id, budget=ledger)

    result = research_delegate(ctx, ["g1"])

    assert result["ok"] is False
    assert "额度不足" in result["error"]
    assert ledger.used == 2, "被拒绝的委派不该动主账"
    assert stub.calls == []
    assert result["data"]["consumed_tool_calls"] == 0


# =========================================================== 5. 同一本账（核心）


def test_delegation_charges_the_same_ledger_across_adjacent_calls(config, db) -> None:
    """子任务消耗**从主预算里扣**：相邻两次委派的总消耗 ≤ 主预算，且账目对得上。

    这是把 P1 做成工具后最关键的不变量 —— 委派不该另起一个额度池。主预算 4（P1 上限 = 4），
    每次委派各吃掉 2，跑两次刚好用尽；第三次因剩余为 0 被拒。
    """
    _register_echo()
    session_id = sessions.create_session(db, model="stub-model")
    ledger = TurnBudget(total=4)  # P1 上限 = max_tool_calls_per_turn // 2 = 4
    stub = _ScriptLlm(
        {
            "g1": _one_tool_call_script("结论 g1", call_id="c1"),
            "g2": _one_tool_call_script("结论 g2", call_id="c2"),
            "g3": _one_tool_call_script("结论 g3", call_id="c3"),
            "g4": _one_tool_call_script("结论 g4", call_id="c4"),
        }
    )
    ctx = _tool_ctx(config, db, stub, session_id=session_id, budget=ledger)

    first = research_delegate(ctx, ["g1", "g2"])
    assert first["ok"] is True
    assert first["data"]["consumed_tool_calls"] == 2
    assert ledger.used == 2, "跑完必须把消耗回写到**同一本账**"
    assert ledger.remaining == 2

    # 相邻的第二次：可委派额度按主账剩余收缩（证明它读的是同一本账，不是另起一个池）
    assert delegate_budget(ctx, ledger) == min(4, ledger.remaining) == 2

    second = research_delegate(ctx, ["g3", "g4"])
    assert second["ok"] is True
    assert second["data"]["consumed_tool_calls"] == 2
    assert ledger.used == 4
    assert ledger.remaining == 0

    # 总消耗永不超主预算（账目恒满足 0 <= used <= total）
    assert ledger.used <= ledger.total
    assert first["data"]["consumed_tool_calls"] + second["data"]["consumed_tool_calls"] <= 4

    # 第三次：剩余 0 → 直接拒绝，且不消耗（拒绝发生在动账之前）
    third = research_delegate(ctx, ["g5"])
    assert third["ok"] is False
    assert "额度不足" in third["error"]
    assert ledger.used == 4


def test_consumed_count_matches_the_ledger_delta(config, db) -> None:
    """每次委派报出的 ``consumed_tool_calls`` 必须等于主账实际扣掉的数。"""
    _register_echo()
    session_id = sessions.create_session(db, model="stub-model")
    ledger = TurnBudget(total=8)
    stub = _ScriptLlm({"g1": _one_tool_call_script("结论 g1", call_id="c1")})
    ctx = _tool_ctx(config, db, stub, session_id=session_id, budget=ledger)

    before = ledger.used
    result = research_delegate(ctx, ["g1"])

    assert result["data"]["consumed_tool_calls"] == ledger.used - before == 1


def test_delegation_without_a_ledger_falls_back_to_half_the_main_budget(config, db) -> None:
    """脚本直调（没有主循环、没注入账）时退化为 P1 上限，行为与工具化之前一致。"""
    _register_echo()
    session_id = sessions.create_session(db, model="stub-model")
    stub = _ScriptLlm({"g1": _one_tool_call_script("结论 g1", call_id="c1")})
    ctx = _tool_ctx(config, db, stub, session_id=session_id, budget=None)

    result = research_delegate(ctx, ["g1"])

    assert result["ok"] is True
    assert result["data"]["consumed_tool_calls"] == 1


# =========================================================== 6. 失败隔离


def test_one_failing_subtask_does_not_block_the_others(config, db) -> None:
    _register_echo()
    session_id = sessions.create_session(db, model="stub-model")
    ledger = TurnBudget(total=8)
    stub = _ScriptLlm(
        {
            "g1": _one_tool_call_script("结论 g1", call_id="c1"),
            "bad": [LlmError("server", "炸了")],
            "g2": [reply("结论 g2")],
        }
    )
    ctx = _tool_ctx(config, db, stub, session_id=session_id, budget=ledger)

    result = research_delegate(ctx, ["g1", "bad", "g2"])

    assert result["ok"] is True
    items = {item["goal"]: item for item in result["data"]["items"]}
    assert items["g1"]["text"] == "结论 g1" and items["g1"]["stop_reason"] == "completed"
    assert items["bad"]["stop_reason"] == "error"
    assert items["g2"]["text"] == "结论 g2" and items["g2"]["stop_reason"] == "completed"
    # 失败的那个没调工具 → 不该占额度
    assert items["bad"]["tool_calls"] == []
    assert result["data"]["consumed_tool_calls"] == 1


def test_all_subtasks_empty_returns_ok_false(config, db) -> None:
    """全部子任务都没产出结论（空文本）→ 如实回失败，不假装成功。"""
    session_id = sessions.create_session(db, model="stub-model")
    ledger = TurnBudget(total=8)
    stub = _ScriptLlm({"g1": [reply("")], "g2": [reply("  ")]})
    ctx = _tool_ctx(config, db, stub, session_id=session_id, budget=ledger)

    result = research_delegate(ctx, ["g1", "g2"])

    assert result["ok"] is False
    assert "全部子任务都没产出结论" in result["error"]
    assert result["data"]["counts"]["ran"] == 0
    assert ledger.used == 0


# =========================================================== 7. 内部异常不外抛


def test_internal_exception_is_returned_not_raised(config, db, monkeypatch) -> None:
    """run_subtasks 之外的意外（如记账层出问题）必须收敛成 ok=false，绝不抛回主循环。"""
    _register_echo()
    session_id = sessions.create_session(db, model="stub-model")
    ledger = TurnBudget(total=8)
    ctx = _tool_ctx(config, db, StubLlm(), session_id=session_id, budget=ledger)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("记账层炸了")

    monkeypatch.setattr("scholarscout.tools.delegate.run_subtasks", boom)

    result = research_delegate(ctx, ["g1"])

    assert result["ok"] is False
    assert "内部异常" in result["error"] and "RuntimeError" in result["error"]
    assert ledger.used == 0, "没跑成就不该扣账"


# =========================================================== 8. refs 归并


def test_refs_from_all_subtasks_are_merged_and_deduped(config, db) -> None:
    _register_echo()
    session_id = sessions.create_session(db, model="stub-model")
    ledger = TurnBudget(total=8)
    stub = _ScriptLlm(
        {
            "g1": _one_tool_call_script("结论 g1", call_id="c1"),
            "g2": _one_tool_call_script("结论 g2", call_id="c2"),
        }
    )
    ctx = _tool_ctx(config, db, stub, session_id=session_id, budget=ledger)

    result = research_delegate(ctx, ["g1", "g2"])

    # 两个子任务都报 src_1 → 归并去重后只剩一份（任何进入回答的 id 都要能追溯）
    assert result["refs"]["source_ids"] == ["src_1"]
