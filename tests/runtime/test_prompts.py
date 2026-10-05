"""系统提示词契约。

背景（真机跑 E3 抓出来的行为缺陷）：模型把 10-12 次工具预算全花在反复检索上，
一次都没走到 topic.build_map / route.plan，六个任务一件学习产物都没产出。
所以"流程顺序"和"预算纪律"必须是提示词里的硬内容，而不是靠模型自觉。
"""

from __future__ import annotations

from scholarscout.runtime.prompts import build_system_prompt, system_prompt_hash


def test_prompt_states_the_pipeline_in_order() -> None:
    prompt = build_system_prompt(toolsets=["research"])
    for tool in (
        "research.search",
        "source.fetch",
        "source.grade",
        "topic.build_map",
        "route.plan",
        "note.save",
    ):
        assert tool in prompt, f"流程里必须点名 {tool}"
    # 顺序：检索在取原文之前，产物在最后
    assert (
        prompt.index("research.search") < prompt.index("source.fetch") < prompt.index("route.plan")
    )


def test_prompt_requires_both_artifacts_by_default() -> None:
    """默认"主题图 + 路线"两件都出。

    真机 E3 第三轮：6 道题里有两道只出了路线、没出主题图（题面只说"帮我找资料+排路线"，
    按旧提示词的"要方向全景才画图"确实不用画）。但产物数一顿一顿地变，度量就没法用，
    而且地图是路线的依据 —— 所以约定：方向型提问默认两件都出，只出一件不算收工。
    """
    prompt = build_system_prompt(toolsets=["research"], auto_artifacts=True)
    assert "别只出一件就收工" in prompt
    assert "先 topic.build_map" in prompt
    # 地图要排在路线之前（先看清方向再排顺序）
    assert prompt.index("topic.build_map") < prompt.index("route.plan")


def test_prompt_carries_budget_discipline() -> None:
    """从默认说 10 次工具调用 → 默认档**不**硬性要求产物，但预算纪律仍然要在。"""
    on_demand = build_system_prompt(toolsets=["research"])
    assert "最多 2-3 次" in on_demand
    assert "等用户点单再花" in on_demand

    auto = build_system_prompt(toolsets=["research"], auto_artifacts=True)
    assert "10 次工具调用" in auto
    assert "用户要的是路线和地图" in auto


def test_prompt_forbids_inventing_facts() -> None:
    prompt = build_system_prompt(toolsets=["research"])
    assert "资料里没写" in prompt
    assert "绝不用领域常识补全" in prompt


def test_clarify_rule_is_optional() -> None:
    with_rule = build_system_prompt(allow_clarify=True)
    without_rule = build_system_prompt(allow_clarify=False)
    assert "澄清规则" in with_rule
    assert "澄清规则" not in without_rule


def test_prompt_names_active_toolsets() -> None:
    assert "当前可用工具集：research" in build_system_prompt(toolsets=["research"])
    assert "research, kb" in build_system_prompt(toolsets=["research", "kb"])


def test_hash_is_stable_and_short() -> None:
    prompt = build_system_prompt()
    digest = system_prompt_hash(prompt)
    assert len(digest) == 12
    assert digest == system_prompt_hash(prompt)
    assert digest != system_prompt_hash(prompt + "改动")
