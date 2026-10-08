"""卡组计数与单组抽背口径（大浩：卡组列表点一组练那一组，组上显示到期张数）。"""

from __future__ import annotations

from scholarscout.state import learning


def _note(db, content, title="组"):
    return learning.save_note(db, kind="card", content=content, title=title)


def test_counts_treat_unseen_cards_as_due(db):
    """新建卡组必须显示"到期"，否则用户以为坏了 —— 新卡也算该练。"""
    note_id = _note(
        db,
        "## Q1. [简答] 一"
        + chr(10)
        + "答一"
        + chr(10)
        + chr(10)
        + "## Q2. [简答] 二"
        + chr(10)
        + "答二",
    )
    counts = learning.note_card_counts(db)
    assert counts[note_id]["total"] == 2
    assert counts[note_id]["due"] == 2


def test_reviewed_and_not_due_leaves_the_group_alone(db):
    note_id = _note(db, "## Q1. [填空] 一" + chr(10) + "答案：x")
    learning.review_card(db, note_id, 1, "good")
    counts = learning.note_card_counts(db)
    assert counts[note_id]["total"] == 1
    assert counts[note_id]["due"] == 0  # 刚评过"会了"，不该再催


def test_list_notes_carries_counts_for_card_kind(db):
    note_id = _note(db, "## Q1. [简答] 一" + chr(10) + "答一")
    items = learning.list_notes(db, kind="card")
    row = [item for item in items if item["id"] == note_id][0]
    assert row["card_total"] == 1 and row["card_due"] == 1


def test_due_cards_can_be_limited_to_one_group(db):
    first = _note(db, "## Q1. [简答] 甲" + chr(10) + "答甲", title="甲组")
    _note(db, "## Q1. [简答] 乙" + chr(10) + "答乙", title="乙组")
    payload = learning.due_cards(db, note_id=first)
    assert payload["items"], "这一组应该有卡"
    assert {item["note_id"] for item in payload["items"]} == {first}
