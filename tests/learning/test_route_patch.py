"""增量补丁引擎（`learning/route_patch.py`）—— 全是"错了会静默改坏数据"的地方，所以测得细。

重点四组：
1. **只改点到的地方**：没被点到的周/条目必须逐字节不变（这是增量相对重生成的全部价值）；
2. **越界/写错就报错**：周号、条目号、字段名、类型 —— 一律拒绝，不做"尽力而为"的忽略；
3. **出处不许放宽**：资料类条目必须挂真实的 `source_id`，且不能把已有出处改成空；
4. **应用完的不变量**：周号 1..N 连续、统计（条目数/总分钟）跟着变。
"""

from __future__ import annotations

import copy
import json

import pytest

from scholarscout.learning.route_patch import (
    _OPS,
    OP_NAMES,
    PatchError,
    apply_ops,
    parse_patch,
)

#: 补丁引擎对外宣称支持的 op —— 就是原来那串 if/elif 的分支集。
EXPECTED_OPS = (
    "set_plan_field",
    "set_week_field",
    "set_item_field",
    "add_item",
    "remove_item",
    "add_week",
    "remove_week",
)


def _plan() -> dict:
    return {
        "topic": "教育智能体",
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
                        "source_id": "src_a",
                        "why": "先读综述",
                        "est_minutes": 90,
                    },
                    {
                        "kind": "exercise",
                        "title": "跑通 demo",
                        "source_id": None,
                        "est_minutes": 120,
                    },
                ],
            },
            {
                "idx": 2,
                "goal": "能改起来",
                "checkpoint": "改一处并验证",
                "items": [
                    {"kind": "code", "title": "仓库乙", "source_id": "src_b", "est_minutes": 60},
                ],
            },
        ],
        "stats": {"items": 3, "est_minutes": 270},
    }


# ---------------------------------------------------------------- 正常路径


def test_set_plan_field_changes_only_that_field() -> None:
    plan = _plan()
    before = copy.deepcopy(plan)
    new_plan, changes = apply_ops(
        plan, [{"op": "set_plan_field", "field": "weekly_hours", "value": 8}]
    )
    assert new_plan["weekly_hours"] == 8
    assert changes == [{"target": "整条路线 / weekly_hours", "before": 6, "after": 8}]
    # 原对象没被改动（纯函数），其余字段逐字节不变
    assert plan == before
    untouched = {k: v for k, v in new_plan.items() if k not in ("weekly_hours", "stats")}
    assert untouched == {k: v for k, v in before.items() if k not in ("weekly_hours", "stats")}


def test_set_week_field_changes_only_that_week() -> None:
    plan = _plan()
    new_plan, changes = apply_ops(
        plan, [{"op": "set_week_field", "week": 2, "field": "checkpoint", "value": "能写一页复盘"}]
    )
    assert new_plan["weeks"][1]["checkpoint"] == "能写一页复盘"
    assert new_plan["weeks"][0] == plan["weeks"][0]
    assert changes == [
        {"target": "第 2 周 / checkpoint", "before": "改一处并验证", "after": "能写一页复盘"}
    ]


def test_set_item_field_keeps_other_items_identical() -> None:
    plan = _plan()
    new_plan, changes = apply_ops(
        plan,
        [{"op": "set_item_field", "week": 1, "item": 1, "field": "est_minutes", "value": 45}],
    )
    assert new_plan["weeks"][0]["items"][0]["est_minutes"] == 45
    assert new_plan["weeks"][0]["items"][1] == plan["weeks"][0]["items"][1]
    assert new_plan["weeks"][1] == plan["weeks"][1]
    assert changes[0]["target"] == "第 1 周 第 1 条 / est_minutes"
    assert changes[0]["before"] == 90


def test_add_and_remove_item() -> None:
    plan = _plan()
    new_plan, changes = apply_ops(
        plan,
        [
            {
                "op": "add_item",
                "week": 2,
                "item": {"kind": "exercise", "title": "写一页复盘", "why": "闭环比读更多重要"},
            },
            {"op": "remove_item", "week": 1, "item": 2},
        ],
    )
    week2_titles = [item["title"] for item in new_plan["weeks"][1]["items"]]
    assert week2_titles == ["仓库乙", "写一页复盘"]
    assert len(new_plan["weeks"][0]["items"]) == 1
    assert changes[0]["target"] == "第 2 周 / 新增第 2 条"
    assert changes[1]["before"] == "跑通 demo"


def test_add_and_remove_week_keeps_numbering_contiguous() -> None:
    plan = _plan()
    new_plan, _ = apply_ops(
        plan,
        [
            {"op": "remove_week", "week": 1},
            {"op": "add_week", "week": {"goal": "收尾", "checkpoint": "交一份总结"}},
        ],
    )
    assert [entry["idx"] for entry in new_plan["weeks"]] == [1, 2]
    assert new_plan["weeks"][0]["goal"] == "能改起来"  # 原来的第 2 周变成第 1 周


def test_stats_follow_the_edits() -> None:
    """统计不跟着改就是假数据。"""
    plan = _plan()
    new_plan, _ = apply_ops(plan, [{"op": "remove_item", "week": 1, "item": 2}])
    assert new_plan["stats"]["items"] == 2
    assert new_plan["stats"]["est_minutes"] == 150


# ---------------------------------------------------------------- 拒绝路径


@pytest.mark.parametrize(
    "op, message",
    [
        ({"op": "rewrite_everything"}, "不认识的操作名"),
        ({"op": "set_plan_field", "field": "topic", "value": "x"}, "计划级只允许改"),
        ({"op": "set_week_field", "week": 1, "field": "items", "value": "x"}, "周级只允许改"),
        ({"op": "set_week_field", "week": 9, "field": "goal", "value": "x"}, "不存在"),
        ({"op": "set_item_field", "week": 1, "item": 9, "field": "why", "value": "x"}, "取不到"),
        (
            {
                "op": "set_item_field",
                "week": 1,
                "item": 1,
                "field": "source_id",
                "value": "src_zzz",
            },
            "不在当前候选",
        ),
        (
            {
                "op": "set_item_field",
                "week": 1,
                "item": 1,
                "field": "est_minutes",
                "value": "一小时",
            },
            "必须是整数",
        ),
        ({"op": "set_plan_field", "field": "weekly_hours", "value": 99}, "必须在 1~40"),
        (
            {"op": "add_item", "week": 1, "item": {"kind": "podcast", "title": "x"}},
            "条目类型必须是",
        ),
        (
            {"op": "add_item", "week": 1, "item": {"kind": "paper", "title": "x"}},
            "必须挂一个真实的 source_id",
        ),
    ],
)
def test_bad_ops_are_rejected(op: dict, message: str) -> None:
    with pytest.raises(PatchError, match=message):
        apply_ops(_plan(), [op], allowed_source_ids={"src_a", "src_b"})


def test_unknown_op_error_text_is_exactly_the_same() -> None:
    """改映射表最容易改坏的一处：未知 op 的**错误类型与全文**必须一字不差。

    这条钉的是"行为等价" —— 报错文案是给用户看的，也是 `tools/route_revise` 判失败的依据。
    """
    with pytest.raises(PatchError) as excinfo:
        apply_ops(_plan(), [{"op": "rewrite_everything"}])
    assert type(excinfo.value) is PatchError
    assert str(excinfo.value) == (
        f"第 1 处（rewrite_everything）：不认识的操作名（可用：{' / '.join(EXPECTED_OPS)}）"
    )
    # 位置前缀也跟着位置走，不是写死的"第 1 处"
    with pytest.raises(PatchError) as second:
        apply_ops(
            _plan(),
            [
                {"op": "set_plan_field", "field": "level", "value": "进阶"},
                {"op": "rewrite_everything"},
            ],
        )
    assert str(second.value).startswith("第 2 处（rewrite_everything）：不认识的操作名")


def test_non_string_op_names_are_unknown_ops_not_crashes() -> None:
    """op 名写成列表/字典时原本就走"不认识的操作名"，不能变成不可哈希的 TypeError。"""
    for name in (None, 42, ["add_week"], {"op": "add_week"}):
        with pytest.raises(PatchError, match="不认识的操作名"):
            apply_ops(_plan(), [{"op": name}])


def test_op_table_keys_match_the_supported_ops() -> None:
    """映射表的键集＝原来 if/elif 的 op 集，顺序也一致（报错文案按这个顺序拼）。

    加新 op 却忘了登记，会在这一条上直接断——而不是上线后被动变成"不认识的操作名"。
    """
    assert tuple(_OPS) == EXPECTED_OPS
    assert OP_NAMES == EXPECTED_OPS
    for name, handler in _OPS.items():
        assert callable(handler)
        assert handler.__name__ == f"_op_{name}"  # 函数名要能看出它管哪个 op


def test_removing_the_last_week_is_rejected() -> None:
    plan = _plan()
    plan["weeks"] = plan["weeks"][:1]
    with pytest.raises(PatchError, match="至少要有一周"):
        apply_ops(plan, [{"op": "remove_week", "week": 1}])


def test_cannot_blank_out_a_resources_source_id() -> None:
    """资料类条目把出处改成空＝偷偷绕过"必须有出处"的规矩。"""
    with pytest.raises(PatchError, match="不能把 source_id 改成空"):
        apply_ops(
            _plan(),
            [{"op": "set_item_field", "week": 1, "item": 1, "field": "source_id", "value": None}],
            allowed_source_ids={"src_a", "src_b"},
        )


def test_empty_or_oversized_op_list_is_rejected() -> None:
    with pytest.raises(PatchError, match="非空数组"):
        apply_ops(_plan(), [])
    with pytest.raises(PatchError, match="最多改"):
        apply_ops(_plan(), [{"op": "set_plan_field", "field": "level", "value": "入门"}] * 51)


def test_no_partial_application_on_failure() -> None:
    """一处不合法＝整条拒绝，绝不留下"改了一半"的路线。"""
    plan = _plan()
    snapshot = json.dumps(plan, ensure_ascii=False, sort_keys=True)
    with pytest.raises(PatchError):
        apply_ops(
            plan,
            [
                {"op": "set_plan_field", "field": "weekly_hours", "value": 8},
                {"op": "set_week_field", "week": 9, "field": "goal", "value": "x"},
            ],
        )
    assert json.dumps(plan, ensure_ascii=False, sort_keys=True) == snapshot


# ---------------------------------------------------------------- 解析


def test_parse_patch_accepts_fences_and_prose() -> None:
    assert parse_patch('```json\n{"ops": []}\n```')["ops"] == []
    assert parse_patch('先说明一下：{"ops": []} 就这样')["ops"] == []
    assert parse_patch('{"summary": "改了时长", "ops": []}')["summary"] == "改了时长"


@pytest.mark.parametrize("content", ["", "模型什么都没说", "{不是 json}", "[1,2,3]"])
def test_parse_patch_rejects_junk(content: str) -> None:
    with pytest.raises(PatchError):
        parse_patch(content)
