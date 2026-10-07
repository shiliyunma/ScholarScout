"""`route.revise` 工具 + `route.revise` / `route.versions` 两个 RPC。

这一层的重点不是"能改"，而是**改不坏**：

- 补丁不合法（模型写错字段/周号/类型）→ **整条拒绝**，原路线与版本号都不许动；
- 改成功时：先归档旧版、再升版，`artifact_versions` 里能取回**改动前**的正文；
- 界面上点一次和模型调一次走**同一条实现**（RPC 直接复用工具函数），不许有第二套行为。
"""

from __future__ import annotations

import json

import pytest

from scholarscout.rpc.errors import RpcError
from scholarscout.rpc.methods import dispatch
from scholarscout.state import learning
from scholarscout.tools.route_revise import render_route, route_revise

from ..conftest import StubLlm, insert_source, reply, tool_ctx


def _plan() -> dict:
    return {
        "topic": "教育智能体入门",
        "level": "入门",
        "weekly_hours": 6,
        "weeks": [
            {
                "idx": 1,
                "goal": "建立全局印象",
                "checkpoint": "能画出关键环节",
                "items": [
                    {
                        "kind": "paper",
                        "title": "论文甲",
                        "source_id": "src_rev_a",
                        "est_minutes": 90,
                    }
                ],
            },
            {
                "idx": 2,
                "goal": "能改起来",
                "checkpoint": "改一处并验证",
                "items": [
                    {
                        "kind": "exercise",
                        "title": "跑通 demo",
                        "source_id": None,
                        "est_minutes": 120,
                    }
                ],
            },
        ],
        "stats": {"items": 2, "est_minutes": 210},
    }


@pytest.fixture
def route_id(ctx) -> str:  # type: ignore[no-untyped-def]
    insert_source(ctx.db, "src_rev_a", url="https://arxiv.org/abs/2501.00001")
    insert_source(ctx.db, "src_rev_b", url="https://arxiv.org/abs/2501.00002")
    return learning.save_route(
        ctx.db,
        topic="教育智能体入门",
        plan=_plan(),
        level="入门",
        weeks=2,
    )


def test_revise_applies_patch_bumps_version_and_archives_old(ctx, route_id) -> None:  # type: ignore[no-untyped-def]
    patch = {
        "summary": "把每周时长改成 8 小时，并给第 1 周补一条练习",
        "ops": [
            {"op": "set_plan_field", "field": "weekly_hours", "value": 8},
            {
                "op": "add_item",
                "week": 1,
                "item": {"kind": "exercise", "title": "写 200 字小结", "why": "先输出再深入"},
            },
        ],
    }
    ctx.llm = StubLlm(reply(json.dumps(patch, ensure_ascii=False)))

    result = route_revise(tool_ctx(ctx), route_id, "每周改 8 小时；第 1 周加一条练习")

    assert result["version"] == 2
    assert result["summary"].startswith("把每周时长")
    assert result["weeks"] == 2
    assert len(result["changes"]) == 2
    assert result["changes"][0]["before"] == 6
    assert result["changes"][1]["target"] == "第 1 周 / 新增第 2 条"

    row = learning.get_route(ctx.db, route_id)
    assert row is not None
    plan = row["plan"]  # get_route 已把 plan_json 解析成 plan
    assert plan["weekly_hours"] == 8
    assert len(plan["weeks"][0]["items"]) == 2
    assert int(row["version"]) == 2
    assert row["updated_at"], "改过之后要留下修改时间"

    # 旧版必须还在（这就是"改错了能退回去"）
    old = learning.get_version(ctx.db, kind="route", artifact_id=route_id, version=1)
    assert old is not None
    assert old["payload"]["weekly_hours"] == 6
    assert old["note"] == patch["summary"]
    assert len(old["payload"]["weeks"][0]["items"]) == 1


def test_revise_prompt_contains_route_and_instruction(ctx, route_id) -> None:  # type: ignore[no-untyped-def]
    """提示词里必须带着**当前路线原文**与用户要求，否则模型改的就不是这条路。"""
    ctx.llm = StubLlm(reply(json.dumps({"summary": "改了检查点", "ops": []}, ensure_ascii=False)))
    with pytest.raises(ValueError):
        # ops 为空 → 引擎拒绝（没有要改的就别调用这个工具）
        route_revise(tool_ctx(ctx), route_id, "第 2 周检查点改成：交一份可运行的 demo")
    sent = str(ctx.llm.calls[0]["messages"][0]["content"])
    assert "第 2 周" in sent and "能改起来" in sent
    assert "交一份可运行的 demo" in sent


def test_invalid_patch_leaves_route_untouched(ctx, route_id) -> None:  # type: ignore[no-untyped-def]
    """模型写错字段 → 整条拒绝，版本与正文都不许动（不写半截）。"""
    bad = {
        "summary": "试图改个不存在的字段",
        "ops": [{"op": "set_week_field", "week": 1, "field": "title", "value": "x"}],
    }
    ctx.llm = StubLlm(reply(json.dumps(bad, ensure_ascii=False)))

    with pytest.raises(ValueError, match="被拒绝"):
        route_revise(tool_ctx(ctx), route_id, "把第 1 周标题改一下")

    row = learning.get_route(ctx.db, route_id)
    assert row is not None and int(row["version"]) == 1
    assert row["plan"] == _plan()
    assert learning.list_versions(ctx.db, kind="route", artifact_id=route_id) == []


def test_malformed_patch_is_rejected_without_writing(ctx, route_id) -> None:  # type: ignore[no-untyped-def]
    ctx.llm = StubLlm(reply("我觉得应该把每周时长改成 8 小时（没给 JSON）"))
    with pytest.raises(ValueError, match="被拒绝"):
        route_revise(tool_ctx(ctx), route_id, "每周改成 8 小时")
    assert learning.list_versions(ctx.db, kind="route", artifact_id=route_id) == []


def test_unknown_route_is_rejected(ctx) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="路线不存在"):
        route_revise(tool_ctx(ctx), "rte_nope", "改点东西")


def test_revise_needs_an_instruction(ctx, route_id) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="instruction 不能为空"):
        route_revise(tool_ctx(ctx), route_id, "   ")


def test_render_route_is_compact_and_numbered() -> None:
    text = render_route(_plan())
    assert "每周时长：6 小时" in text
    assert "第 1 周" in text and "第 2 周" in text
    assert "1. [paper] 论文甲 | source_id=src_rev_a（90 分钟）" in text
    # 条目编号是**每周内部**从 1 起（与提示词里"第 3 周第 2 条"的说法一致）
    assert "1. [exercise] 跑通 demo | source_id=-（120 分钟）" in text


# ------------------------------------------------------------------ RPC


def test_rpc_revise_and_versions(ctx, route_id) -> None:  # type: ignore[no-untyped-def]
    patch = {
        "summary": "加一周",
        "ops": [{"op": "add_week", "week": {"goal": "收尾", "checkpoint": "交总结"}}],
    }
    ctx.llm = StubLlm(reply(json.dumps(patch, ensure_ascii=False)))

    result = dispatch(ctx, "route.revise", {"route_id": route_id, "instruction": "再加一周收尾"})
    assert result["version"] == 2

    versions = dispatch(ctx, "route.versions", {"route_id": route_id})
    assert [item["version"] for item in versions["items"]] == [1]
    assert versions["items"][0]["note"] == "加一周"

    with_payload = dispatch(ctx, "route.versions", {"route_id": route_id, "with_payload": True})
    assert with_payload["items"][0]["payload"]["topic"] == "教育智能体入门"


def test_rpc_revise_reports_engine_rejection_as_invalid(ctx, route_id) -> None:  # type: ignore[no-untyped-def]
    ctx.llm = StubLlm(reply(json.dumps({"ops": [{"op": "nope"}]}, ensure_ascii=False)))
    with pytest.raises(RpcError):
        dispatch(ctx, "route.revise", {"route_id": route_id, "instruction": "随便改"})
    assert learning.list_versions(ctx.db, kind="route", artifact_id=route_id) == []


def test_rpc_versions_needs_a_route_id(ctx) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RpcError):
        dispatch(ctx, "route.versions", {})
