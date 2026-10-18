"""思考档位的**真值表**：界面五档 + 关 → 真正发出去的字段。

为什么值得单独一个文件：档位是这套设计里最容易"看起来改了、其实没发"的东西 ——
界面显示「最高」而请求体里还是 high，用户只会觉得"模型没变聪明"，没人会去抓包。
所以这里按**五档 + 关**逐档钉住 effort，并把服务端只声明三档时的降级口径也钉住
（就近向弱，不许悄悄升档）。

本服务的真实档位来自 `GET /models` 的 `effort.supported_levels`（low/high/max），
五档是**并档映射**（官方文档口径：minimal/low→low、medium/high→high、max→max）。
"""

from __future__ import annotations

import pytest

from scholarscout.providers.thinking import (
    DEFAULT_EFFORTS,
    DEFAULT_LEVEL,
    DISABLED_FIELDS,
    LEVEL_TO_EFFORT,
    LEVELS,
    UnknownLevelError,
    clamp_effort,
    mentions_reasoning_param,
    normalize_level,
    options,
    plan_for,
    strip_wire_fields,
)

#: 界面档位 → 本服务真实档位（五档 + 关）。**这张表就是需求**：改它等于改产品口径。
TRUTH_TABLE: dict[str, str | None] = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "max": "max",
    "none": None,
}


def test_truth_table_matches_the_interface_levels() -> None:
    """真值表的键必须与界面档位**完全一致**（少了就是下拉里有档发不出去，多了就是幽灵档）。"""
    assert tuple(TRUTH_TABLE) == LEVELS
    assert {level: LEVEL_TO_EFFORT.get(level) for level in LEVELS if level != "none"} == {
        level: effort for level, effort in TRUTH_TABLE.items() if effort is not None
    }
    assert DEFAULT_LEVEL in LEVELS


@pytest.mark.parametrize(("level", "effort"), sorted(TRUTH_TABLE.items()))
def test_every_level_maps_to_the_real_effort(level: str, effort: str | None) -> None:
    """五档 + 关，逐档断言真实 effort 与请求体字段。"""
    plan = plan_for(level, supported=DEFAULT_EFFORTS)
    assert plan.level == level
    assert plan.effort == effort
    assert plan.exact is True, f"{level} 在服务端声明的三档内，不该被判为降级"
    fields = plan.wire_fields()
    if effort is None:
        assert fields == DISABLED_FIELDS == {"thinking": {"type": "disabled"}}
        assert "reasoning_effort" not in fields, "关档发了 reasoning_effort"
    else:
        assert fields == {"thinking": {"type": "enabled"}, "reasoning_effort": effort}


def test_default_level_equals_the_server_default() -> None:
    """默认档必须映射到服务端默认的 high：否则"升级到显式参数"本身就会改变行为与成本。"""
    assert plan_for(DEFAULT_LEVEL).effort == "high"
    assert plan_for(None).level == DEFAULT_LEVEL, "没配置时落默认档"


def test_thinking_false_is_the_same_as_level_none() -> None:
    """老配置 `thinking: false` 与界面「关」等价（不需要迁移用户配置）。"""
    assert plan_for("max", thinking=False) == plan_for("none")


def test_unknown_level_raises_instead_of_silently_defaulting() -> None:
    """拼错的档位**必须报错**：静默回默认档 = 用户以为改了、其实没改。"""
    for bad in ("highest", "middle", "0", "medium "):
        if bad.strip() in LEVELS:
            continue
        with pytest.raises(UnknownLevelError):
            normalize_level(bad)
    assert normalize_level("  HIGH ") == "high"
    assert normalize_level("") == DEFAULT_LEVEL


def test_clamp_only_degrades_never_upgrades() -> None:
    """服务端只声明两档时：就近**向弱**压，绝不偷偷升档（升档＝无声涨成本）。"""
    plan = plan_for("minimal", supported=("high", "max"))
    assert plan.effort == "high", "没有更弱的可选时才取该集合里最弱的一档"
    assert plan.exact is False, "降级了却没标出来（界面/报告会以为按原档跑的）"

    plan = plan_for("max", supported=("low", "high"))
    assert (plan.effort, plan.exact) == ("high", False)

    assert clamp_effort("low", ("low", "high")) == "low"
    assert clamp_effort("max", ()) == "max", "服务端什么都没声明时按官方三档兜底，不做无意义降级"


def test_labels_carry_the_real_effort() -> None:
    """界面文案必须把真实档位写在括号里（"中（high）"）—— 不假装五档各不相同。"""
    assert plan_for("medium").label() == "中（high）"
    assert plan_for("minimal").label() == "最小（low）"
    assert plan_for("max").label() == "最高（max）"
    assert plan_for("none").label() == "关（不发思考参数）"


def test_options_cover_all_levels_and_mark_only_off_as_disabled() -> None:
    """下拉的六个选项：value 是界面档位、label 带真实档位、只有「关」是 disabled。"""
    items = options()
    assert [item["value"] for item in items] == list(LEVELS)
    assert len({item["label"] for item in items}) == len(LEVELS)
    assert [item["value"] for item in items if item["disabled"]] == ["none"]
    assert "（high）" in str(items[2]["label"])


def test_strip_wire_fields_removes_both_and_leaves_the_original_alone() -> None:
    """降级要摘的是**副本**：原请求体还得留给缓存键与重试之外的路径。"""
    payload = {"model": "m", "thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    stripped = strip_wire_fields(payload)
    assert stripped == {"model": "m"}
    assert payload["reasoning_effort"] == "high", "原地改了原请求体（缓存键会跟着变）"
    assert strip_wire_fields({"model": "m"}) is None, "本来就没有思考字段时应回 None（无从降级）"


def test_only_reasoning_related_errors_count_as_degradable() -> None:
    """400 正文判据宁可窄：把真正的参数错误也重试一遍，只是把一次失败变成两次。"""
    assert mentions_reasoning_param("Unsupported parameter: reasoning_effort")
    assert mentions_reasoning_param("invalid value for thinking.type")
    assert not mentions_reasoning_param("model_not_found")
    assert not mentions_reasoning_param("")
