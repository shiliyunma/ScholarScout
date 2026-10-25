"""工具渐进披露（`tool.search` / `tool.describe` + 直调闸）的对外行为。

为什么这一层要单独测：披露的全部价值在**两件事同时成立** ——

1. **省**：发给模型的工具声明只留核心 + 元工具，提示词里**不出现任何未加载的工具名**
   （出现名字＝模型会去调一个它没有参数定义的工具，那就是 ``tool_not_loaded`` 的来源）；
2. **拦**：模型凭旧记忆硬调一个没 describe 过的工具时，必须回 ``tool_not_loaded``
   并教它先 describe，**不能静默执行**（静默执行比报错更糟：它会以为自己"确认过"参数）。

少测任何一条，这个机制都可能"看起来省了 token、实际上模型调用全废"。

默认路径（``features.enable_tool_disclosure = False``）必须**逐字节不变** —— 黄金哈希
从 `tests/runtime/test_prompt_arms.py` import 过来，不在这里抄第二个字面量：抄了就会分成两份，
一份改了另一份不会红。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest

from scholarscout.config import Config
from scholarscout.providers.llm import ToolCall
from scholarscout.runtime import disclosure, registry
from scholarscout.runtime.prompts import build_system_prompt, system_prompt_hash
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.runtime.turn_context import TurnContext
from scholarscout.runtime.turn_steps import execute_tool
from scholarscout.state.db import Database
from scholarscout.tools import register_tools
from scholarscout.tools.tool_catalog import tool_describe, tool_search

from ..conftest import StubLlm, insert_session
from .test_prompt_arms import GOLDEN_ON_DEMAND_HASH

#: 最该被"藏起来"的几个：它们恰好是 E4 里最容易与别的工具互相干扰的那几个
MOST_LIKELY_TO_LEAK = ("route.plan", "source.grade", "py.exec")


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    """每个用例一份干净的注册表与披露状态。

    两者都是**进程内全局**：注册表决定"有什么工具"，披露状态决定"这个会话加载了什么"。
    不清理的话，前一条用例 describe 过的工具会漏到下一条 —— 跨会话隔离那条用例就永远绿。
    """
    disclosure.reset()
    registry.clear_registry()
    yield
    disclosure.reset()
    registry.clear_registry()


def _enabled(config: Config, **flags: bool) -> Config:
    """打开某个特性开关（``Features`` 与 ``Config`` 都是 frozen dataclass，用 ``replace``）。"""
    return replace(config, features=replace(config.features, **flags))


def _non_core_names() -> list[str]:
    """当前注册集里**非核心、非元工具**的名字（= 披露档必须藏起来的那些）。"""
    return [
        spec.name
        for spec in registry.get_tools(("research",))
        if not disclosure.is_core(spec.name) and not disclosure.is_meta(spec.name)
    ]


def _tool_ctx(db: Database, config: Config, session_id: str) -> ToolContext:
    """工具层上下文（与 `loop.run_turn` 注入的形状一致，只少 http/llm）。"""
    return ToolContext(db=db, config=config, session_id=session_id)


def _turn_ctx(db: Database, config: Config) -> TurnContext:
    """编排层上下文：``execute_tool`` 走的是这一层（闸就在那里）。"""
    return TurnContext(db=db, config=config, llm=StubLlm())


def _call(name: str, arguments: str = "{}") -> ToolCall:
    return ToolCall(id="call_1", name=name, arguments=arguments)


def _run(db: Database, config: Config, session_id: str, call: ToolCall) -> tuple[str, bool]:
    """跑一次 ``execute_tool``：``(喂回模型的文本, 是否成功)``。"""
    message, _refs, _canonical, ok = execute_tool(
        _turn_ctx(db, config), session_id, f"trn_{session_id}", 1, call, None
    )
    return message, ok


# --------------------------------------------------------------------------- #
# 1. 默认关：提示词逐字节不变（黄金哈希）
# --------------------------------------------------------------------------- #


def test_default_prompt_is_byte_identical_to_the_golden_arm() -> None:
    """不传 ``disclosure``（产品默认）与显式 ``disclosure=False`` 必须**逐字节相同**，且哈希是黄金值。

    这一条是整个改动的"不许动默认臂"承诺：披露是**实验臂**，一开就换提示词；
    默认臂的文本、拼装顺序一个字都不能跟着变，否则历史实验数字全部不可比。
    """
    default = build_system_prompt()
    explicit_off = build_system_prompt(disclosure=False)

    assert default == explicit_off
    assert system_prompt_hash(default) == GOLDEN_ON_DEMAND_HASH
    # 默认臂里**必须**写着那些工具（默认 11 个全常驻，那是它的前提）
    for name in MOST_LIKELY_TO_LEAK:
        assert name in default


# --------------------------------------------------------------------------- #
# 2. 披露档提示词：只有核心 + 元工具的名字
# --------------------------------------------------------------------------- #


def test_disclosure_prompt_never_names_a_non_core_tool() -> None:
    """披露档提示词里**一个非核心工具名都不许出现**（它们不在 payload 里）。

    写法是"从注册表取全集做减法"，不是手抄几个名字：工具改名 / 新增工具时这条会自动覆盖到，
    手抄的清单则会静默过期（过期之后提示词又悄悄开始点名了，没人会发现）。
    """
    register_tools(enable_tool_disclosure=True)
    prompt = build_system_prompt(disclosure=True)

    hidden = _non_core_names()
    assert hidden, "注册集里总该有非核心工具，否则这条用例什么都没测"
    leaked = [name for name in hidden if name in prompt]
    assert leaked == []
    for name in MOST_LIKELY_TO_LEAK:
        assert name not in prompt
    # 核心与元工具反过来**必须**在（它们是模型唯一能直接看见的东西）
    for name in (*disclosure.CORE_TOOLS, *disclosure.META_TOOLS):
        assert name in prompt
    # 披露档与默认档必须是两份不同的提示词，否则上面那些断言只是"默认臂恰好没写名字"
    assert prompt != build_system_prompt()


# --------------------------------------------------------------------------- #
# 3. tool.search：只列当前会话允许的工具，且只给名字 + 一句话
# --------------------------------------------------------------------------- #


def test_tool_search_lists_only_allowed_tools(db: Database, config: Config) -> None:
    """目录范围 = 本会话允许的工具集（过了特性开关），且**不含元工具自己**。"""
    register_tools(enable_tool_disclosure=True)  # files / kb 都关着
    ctx = _tool_ctx(db, config, "ses_catalog")

    result = tool_search(ctx, limit=50)

    assert result["ok"] is True, result
    data = result["data"]
    listed = {item["name"] for item in data["tools"]}
    allowed = {
        spec.name
        for spec in registry.get_tools(
            registry.enabled_toolsets(None, enable_kb=config.features.enable_kb, enable_files=False)
        )
    }
    assert listed == allowed - set(disclosure.META_TOOLS)
    assert set(disclosure.META_TOOLS).isdisjoint(listed)
    # 门控：关掉的工具集里的工具一个都不能出现
    assert "kb.search" not in listed
    assert "file.read" not in listed
    # 目录项只有名字与一句话：给了 parameters 就等于每轮又把全量 schema 付了一遍
    for item in data["tools"]:
        assert set(item) == {"name", "purpose", "hit"}
    assert data["loaded"] == []


# --------------------------------------------------------------------------- #
# 4–5. 未 describe 直调 → tool_not_loaded；describe 后可调
# --------------------------------------------------------------------------- #


def test_calling_without_describe_returns_tool_not_loaded(db: Database, config: Config) -> None:
    """没 describe 过的工具被直接调用 → ``tool_not_loaded`` + 先 describe 的下一步。

    "不执行"是重点：闸必须在**调用工具之前**拦下，且留一条 trace（否则事后查不出
    模型绕过声明硬调过什么）。
    """
    register_tools(enable_tool_disclosure=True)
    insert_session(db, "ses_gate")
    config_on = _enabled(config, enable_tool_disclosure=True)

    message, ok = _run(db, config_on, "ses_gate", _call("route.plan"))

    assert ok is False
    assert message.startswith("tool_not_loaded")
    assert "route.plan" in message
    assert "tool.describe" in message  # 教它怎么自救，而不是只说"不行"
    row = db.query_one(
        "SELECT error FROM tool_traces WHERE session_id = ? AND tool_name = ?",
        ("ses_gate", "route.plan"),
    )
    assert row is not None and row["error"] == "tool_not_loaded"


def test_describe_then_the_tool_is_callable_in_the_same_session(
    db: Database, config: Config
) -> None:
    """describe 之后：声明里有它、可用集里有它、直调闸放行（本会话内）。"""
    register_tools(enable_tool_disclosure=True)
    insert_session(db, "ses_after")
    config_on = _enabled(config, enable_tool_disclosure=True)
    ctx = _tool_ctx(db, config_on, "ses_after")
    disclosure.remember_universe(
        "ses_after", [spec.name for spec in registry.get_tools(("research",))]
    )

    described = tool_describe(ctx, names=["skill.list"])

    assert described["ok"] is True, described
    assert "parameters" in described["data"]["tools"][0]
    assert disclosure.is_available("skill.list", session_id="ses_after") is True
    declared = {
        spec.name
        for spec in disclosure.declared_specs(
            registry.get_tools(("research",)), session_id="ses_after"
        )
    }
    assert "skill.list" in declared
    assert "route.plan" not in declared  # 没 describe 的仍然不在声明里

    message, ok = _run(db, config_on, "ses_after", _call("skill.list", '{"query": "zzz-no-hit"}'))
    assert ok is True, message
    assert "tool_not_loaded" not in message


# --------------------------------------------------------------------------- #
# 6. 跨会话不继承
# --------------------------------------------------------------------------- #


def test_loaded_tools_are_not_inherited_by_a_new_session(db: Database, config: Config) -> None:
    """A 会话 describe 过的工具，B 会话**拿不到**（新会话必须重新 describe）。

    为什么不落库（模块顶注给了理由）：加载集是"这一轮模型见过哪些 schema"，
    跨进程带过去只会制造"加载集与真实 payload 不一致"的新漂移面。
    """
    register_tools(enable_tool_disclosure=True)
    insert_session(db, "ses_a")
    insert_session(db, "ses_b")
    config_on = _enabled(config, enable_tool_disclosure=True)

    assert tool_describe(_tool_ctx(db, config_on, "ses_a"), names=["route.plan"])["ok"] is True
    assert disclosure.is_available("route.plan", session_id="ses_a") is True
    assert disclosure.is_available("route.plan", session_id="ses_b") is False

    message, ok = _run(db, config_on, "ses_b", _call("route.plan"))
    assert ok is False
    assert message.startswith("tool_not_loaded")
    # B 会话的声明里也不该有它
    declared = {
        spec.name
        for spec in disclosure.declared_specs(registry.get_tools(("research",)), session_id="ses_b")
    }
    assert "route.plan" not in declared


# --------------------------------------------------------------------------- #
# 7. describe 的 3 个上限
# --------------------------------------------------------------------------- #


def test_describe_refuses_more_than_three_names(db: Database, config: Config) -> None:
    """一次最多 3 个：超了整批**一个都不加载**（原子），否则"上限"只是文案。"""
    register_tools(enable_tool_disclosure=True)
    ctx = _tool_ctx(db, config, "ses_limit")

    too_many = tool_describe(ctx, names=["route.plan", "route.revise", "skill.list", "py.exec"])

    assert too_many["ok"] is False
    assert too_many["data"]["error_kind"] == "too_many_names"
    assert too_many["data"]["limit"] == disclosure.MAX_DESCRIBE == 3
    assert disclosure.loaded_names("ses_limit") == frozenset()  # 原子：一个都没进可用集

    just_enough = tool_describe(ctx, names=["route.plan", "route.revise", "skill.list"])
    assert just_enough["ok"] is True, just_enough
    assert disclosure.loaded_names("ses_limit") == frozenset(
        {"route.plan", "route.revise", "skill.list"}
    )


# --------------------------------------------------------------------------- #
# 8–9. 工具集关掉时 describe 也拿不到
# --------------------------------------------------------------------------- #


def test_describe_cannot_reach_a_tool_from_a_disabled_gated_toolset(
    db: Database, config: Config
) -> None:
    """``features.enable_kb`` 关着 → ``kb.search`` 根本没注册，describe 只能如实说拿不到。"""
    register_tools(enable_tool_disclosure=True)  # enable_kb 默认 False
    ctx = _tool_ctx(db, config, "ses_gated")

    result = tool_describe(ctx, names=["kb.search"])

    assert result["ok"] is False
    data = result["data"]
    assert data["error_kind"] == "not_loadable"
    assert data["rejected"] == ["kb.search"]
    assert "不要凭印象编" in result["error"]  # 出路是回 search 找真名字
    assert disclosure.loaded_names("ses_gated") == frozenset()


def test_describe_refuses_a_tool_registered_but_outside_the_session(
    db: Database, config: Config
) -> None:
    """工具注册了、但**本会话的工具集里没有它** → 如实说"这个能力当前没启用"。

    这条钉的是 `_reject_reason` 的第三种出路：不是"不存在"、也不是"换个词搜"，
    而是"关着，你得如实告诉用户" —— 模型照着"不存在"的文案去 search 会一直搜不到。
    """
    register_tools(enable_tool_disclosure=True, enable_kb=True)
    config_on = replace(config, features=replace(config.features, enable_kb=True))
    ctx = _tool_ctx(db, config_on, "ses_universe")
    # 会话只要了 research（前端传的 toolsets）：kb 不在里面
    disclosure.remember_universe(
        "ses_universe",
        [spec.name for spec in registry.get_tools(("research",))],
    )

    result = tool_describe(ctx, names=["kb.search"])

    assert result["ok"] is False
    assert result["data"]["error_kind"] == "not_loadable"
    assert "没开对应的特性开关" in result["error"]
    assert disclosure.loaded_names("ses_universe") == frozenset()


# --------------------------------------------------------------------------- #
# 10. 默认关时闸不生效（默认臂不许被静默弄坏）
# --------------------------------------------------------------------------- #


def test_gate_is_inactive_when_the_feature_is_off(db: Database, config: Config) -> None:
    """``enable_tool_disclosure = False`` 时同一个直调**照常执行** —— 默认臂与改前逐字一致。

    没有这条，闸一旦写错成"无条件生效"，默认产品里 8 个工具会全部变成
    ``tool_not_loaded``，而所有涉及工具的既有用例仍可能绿（它们多半走 mock）。
    """
    register_tools()  # 默认：11 个工具全常驻
    insert_session(db, "ses_default")
    assert config.features.enable_tool_disclosure is False

    message, ok = _run(db, config, "ses_default", _call("skill.list", '{"query": "zzz-no-hit"}'))

    assert ok is True, message
    assert "tool_not_loaded" not in message
