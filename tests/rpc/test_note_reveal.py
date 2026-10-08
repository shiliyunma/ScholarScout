"""② 答案谜底分离的契约：`note.due` **只发题目**，答案要单独调 `note.reveal` 取。

为什么值得钉：如果答案随题一起下发，"看答案"就只是前端的一个显示开关 ——
打开开发者工具就能看到谜底，抽背失去意义。这条测试断言的是**投影里根本没有答案**，
不是"界面没显示"。
"""

from __future__ import annotations

import json

from scholarscout.rpc.methods import AppContext, dispatch

CARD_CONTENT = "## Q1 问题一？\n**A.** 答一。\n\n## Q2 问题二？\n**A.** 答二。\n"


def _card_note(ctx: AppContext, note_id: str = "nt_reveal") -> str:
    ctx.db.execute(
        "INSERT INTO notes (id, session_id, kind, title, content, created_at) VALUES (?,?,?,?,?,?)",
        (note_id, "ses_1", "card", "答案分离 抽背卡", CARD_CONTENT, "2026-10-07T00:00:00+08:00"),
    )
    return note_id


def test_due_projection_never_carries_the_answer(ctx: AppContext) -> None:
    _card_note(ctx)
    payload = dispatch(ctx, "note.due", {"limit": 10})

    assert payload["items"], "应当有卡可练"
    for item in payload["items"]:
        assert "back" not in item, "题目投影里不该有 back 字段"
    dumped = json.dumps(payload, ensure_ascii=False)
    assert "答一" not in dumped and "答二" not in dumped, "投影里出现了答案原文"


def test_reveal_returns_the_answer_on_demand(ctx: AppContext) -> None:
    note_id = _card_note(ctx)
    payload = dispatch(ctx, "note.reveal", {"note_id": note_id, "card_index": 1})
    assert payload["back"] == "答二。"
    assert payload["front"] == "问题二？"


def test_reveal_out_of_range_is_empty_not_an_error(ctx: AppContext) -> None:
    """越界不该抛错：界面据此显示"这张卡不见了"，而不是整页崩掉。"""
    note_id = _card_note(ctx)
    payload = dispatch(ctx, "note.reveal", {"note_id": note_id, "card_index": 99})
    assert payload["back"] == ""


def test_due_items_carry_a_stable_card_id(ctx: AppContext) -> None:
    """界面靠 card_id 做答案缓存与复习上报，它必须稳定（note_id#index）。"""
    _card_note(ctx, note_id="nt_ids")
    items = dispatch(ctx, "note.due", {"limit": 10})["items"]
    assert [item["card_id"] for item in items] == ["nt_ids#0", "nt_ids#1"]
