"""``route.plan`` 契约：解析、白名单与去重校验、骨架兜底、落库。"""

from __future__ import annotations

import pytest

from scholarscout.rpc.methods import AppContext
from scholarscout.state.ingest import upsert_source
from scholarscout.state.learning import get_route
from scholarscout.tools import register_tools
from scholarscout.tools.route_plan import (
    PARAMETERS,
    SPEC,
    apply_guards,
    build_skeleton,
    parse_plan,
    route_plan,
)

from ..conftest import StubLlm, reply, tool_ctx

ROW_A = {
    "url": "https://arxiv.org/abs/2501.00001",
    "url_canonical": "https://arxiv.org/abs/2501.00001",
    "domain": "arxiv.org",
    "title": "教育智能体的检索增强方法",
    "source_type": "arxiv",
    "year": 2025,
}
ROW_B = {
    "url": "https://docs.example.org/guide",
    "url_canonical": "https://docs.example.org/guide",
    "domain": "docs.example.org",
    "title": "教育智能体工程指南",
    "source_type": "docs",
    "year": 2024,
}

MODEL_OUTPUT = (
    "week: 1 | 能跑通检索 | 能解释每一步在做什么\n"
    "item: doc | 官方指南 | {b} | 先把环境跑起来 | 60\n"
    "item: paper | 检索增强方法 | {a} | 理解方法动机 | 90\n"
    "item: video | 补充视频 | - | 清单里没有对应资料，属补充项 | 30\n"
    "week: 2 | 读懂方法 | 能复述架构\n"
    "item: paper | 检索增强方法 | {a} | 重复读 | 45\n"
)


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_tools()


def _seed(ctx: AppContext) -> tuple[str, str]:
    a, _ = upsert_source(ctx.db, ROW_A)
    b, _ = upsert_source(ctx.db, ROW_B)
    return a, b


def test_spec_shape() -> None:
    assert SPEC.name == "route.plan"
    assert PARAMETERS["additionalProperties"] is False
    assert set(PARAMETERS["properties"]) == {
        "topic",
        "level",
        "weeks",
        "weekly_hours",
        "source_ids",
        "use_model",
    }


def test_parse_plan_reads_weeks_and_items() -> None:
    parsed = parse_plan(
        "week: 1 | 目标一 | 检查点一\n"
        "item: doc | 文档 | src_a | 先跑起来 | 60\n"
        "item: 怪类型 | 视频 | - | 补充 | 不是数字\n"
        "week: 2 | 目标二 | 检查点二\n"
    )
    assert [week["idx"] for week in parsed["weeks"]] == [1, 2]
    first = parsed["weeks"][0]
    assert first["goal"] == "目标一"
    assert first["items"][0]["source_id"] == "src_a"
    assert first["items"][1]["kind"] == "doc"
    assert first["items"][1]["source_id"] is None
    assert first["items"][1]["est_minutes"] == 45
    assert parsed["weeks"][1]["items"] == []


def test_parse_plan_clamps_minutes() -> None:
    parsed = parse_plan(
        "week: 1 | g | c\nitem: doc | 短 | src_a | w | 3\nitem: doc | 长 | src_b | w | 9999\n"
    )
    minutes = [item["est_minutes"] for item in parsed["weeks"][0]["items"]]
    assert minutes == [10, 600]


def test_apply_guards_whitelists_dedupes_and_truncates() -> None:
    raw = parse_plan(MODEL_OUTPUT.format(a="src_a", b="src_b"))
    plan = apply_guards(raw, allowed_ids={"src_a", "src_b"}, weeks=5)

    assert plan["stats"]["weeks"] == 2
    assert plan["stats"]["items"] == 3
    assert plan["stats"]["ungrounded_items"] == 1
    assert plan["stats"]["dropped_duplicates"] == 1
    assert plan["stats"]["sources_used"] == 2
    assert plan["weeks"][1]["items"] == []  # 第二条与第一条同源，被去重

    truncated = apply_guards(raw, allowed_ids={"src_a", "src_b"}, weeks=1)
    assert truncated["stats"]["weeks"] == 1


def test_apply_guards_nulls_unknown_source() -> None:
    raw = parse_plan("week: 1 | g | c\nitem: paper | 某论文 | src_不存在 | 因为它相关 | 60\n")
    plan = apply_guards(raw, allowed_ids={"src_a"}, weeks=4)
    assert plan["weeks"][0]["items"][0]["source_id"] is None
    assert plan["stats"]["ungrounded_items"] == 1


def test_exercise_items_do_not_count_as_ungrounded() -> None:
    """行动类条目（练习/产出）不声称出处 → 不计入 ungrounded，单独计数。

    这是口径修正：旧的实现把"安排了练习"也算成"没有出处"，既误报，又会反向逼模型
    给练习硬挂一个来源（那才是真幻觉）。真机 e3-04 上 11/18 条"无出处"里 7 条是练习。
    """
    raw = parse_plan(
        "week: 1 | 读一篇再动手 | 能跑通一次训练\n"
        "item: paper | 有出处的综述 | src_a | 先读 | 60\n"
        "item: doc | 清单里没有的教程 | - | 自己去找 | 45\n"
        "item: exercise | 自己跑通一次训练 | - | 动手 | 90\n"
        "item: exercise | 写一份复现报告 | - | 产出 | 60\n"
    )
    plan = apply_guards(raw, allowed_ids={"src_a"}, weeks=4)
    stats = plan["stats"]
    assert stats["ungrounded_items"] == 1, "只有那条无出处的资料类条目该计入"
    assert stats["ungrounded_titles"] == ["清单里没有的教程"]
    assert stats["exercise_items"] == 2
    assert stats["resource_items"] == 2
    assert stats["grounded_resource_items"] == 1
    assert stats["items"] == 4


def test_exercise_with_real_source_still_counts_as_grounded_resource() -> None:
    """练习若真挂了来源（照着某篇做），那条来源也要算进 sources_used，但不进资料类分母。"""
    raw = parse_plan(
        "week: 1 | g | c\n"
        "item: exercise | 按官方教程跑通 | src_a | 照着做 | 60\n"
        "item: paper | 综述 | src_b | 读 | 60\n"
    )
    plan = apply_guards(raw, allowed_ids={"src_a", "src_b"}, weeks=4)
    assert plan["stats"]["ungrounded_items"] == 0
    assert plan["stats"]["exercise_items"] == 1
    assert plan["stats"]["resource_items"] == 1
    assert plan["stats"]["sources_used"] == 2


def test_build_skeleton_orders_by_tier_and_covers_sources() -> None:
    rows = [
        {"id": "src_low", "tier": 4, "year": 2026, "title": "低质", "source_type": "web"},
        {"id": "src_top", "tier": 1, "year": 2025, "title": "一手", "source_type": "arxiv"},
        {"id": "src_mid", "tier": 2, "year": 2024, "title": "索引", "source_type": "openalex"},
    ]
    plan = build_skeleton(rows, weeks=3)
    assert plan["stats"]["weeks"] == 3
    assert plan["stats"]["sources_used"] == 3
    assert plan["stats"]["ungrounded_items"] == 0
    first_week_items = plan["weeks"][0]["items"]
    assert first_week_items[0]["source_id"] == "src_top"
    assert first_week_items[0]["kind"] == "paper"
    assert all(item["why"] for week in plan["weeks"] for item in week["items"])


def test_route_plan_with_model_saves_and_returns(ctx: AppContext) -> None:
    a, b = _seed(ctx)
    ctx.llm = StubLlm(reply(MODEL_OUTPUT.format(a=a, b=b)))

    result = route_plan(
        tool_ctx(ctx), "教育智能体", level="入门", weeks=2, weekly_hours=8, source_ids=[a, b]
    )
    data = result["data"]
    assert data["mode"] == "model"
    assert data["stats"]["weeks"] == 2
    assert result["refs"]["route_ids"] == [data["route_id"]]
    assert result["refs"]["source_ids"] == sorted([a, b])

    saved = get_route(ctx.db, data["route_id"])
    assert saved is not None
    assert saved["plan"]["mode"] == "model"
    assert saved["plan"]["weekly_hours"] == 8
    assert saved["weeks"] == 2


def test_route_plan_without_model_uses_skeleton(ctx: AppContext) -> None:
    a, b = _seed(ctx)
    ctx.llm = None
    result = route_plan(tool_ctx(ctx), "教育智能体", weeks=2, source_ids=[a, b])
    assert result["data"]["mode"] == "skeleton"
    assert result["data"]["stats"]["sources_used"] == 2


def test_route_plan_with_garbage_output_uses_skeleton(ctx: AppContext) -> None:
    a, b = _seed(ctx)
    ctx.llm = StubLlm(reply("我不太清楚"))
    result = route_plan(tool_ctx(ctx), "教育智能体", source_ids=[a, b])
    assert result["data"]["mode"] == "skeleton"


def test_route_plan_clamps_weeks_and_hours(ctx: AppContext) -> None:
    a, _ = _seed(ctx)
    ctx.llm = None
    result = route_plan(tool_ctx(ctx), "教育智能体", weeks=99, weekly_hours=0, source_ids=[a])
    saved = get_route(ctx.db, result["data"]["route_id"])
    assert saved is not None and saved["weeks"] == 24
    assert saved["plan"]["weekly_hours"] == 1


def test_route_plan_rejects_empty_topic(ctx: AppContext) -> None:
    with pytest.raises(ValueError, match="topic 不能为空"):
        route_plan(tool_ctx(ctx), "  ")
