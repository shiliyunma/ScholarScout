"""作答留痕（大浩选定：每题每次作答都留痕、可回看）。"""

from __future__ import annotations

from scholarscout.state import learning


def _note(db):
    return learning.save_note(
        db, kind="card", content="## Q1. [填空] 一" + chr(10) + "答案：x", title="组"
    )


def test_attempt_records_response_and_correctness(db):
    note_id = _note(db)
    learning.review_card(db, note_id, 1, "good", response="x", correct=True)
    learning.review_card(db, note_id, 1, "bad", response="y", correct=False)
    rows = learning.card_attempts(db, note_id, 1)
    assert len(rows) == 2  # 每次作答都留一行，不是只留最后一次
    assert rows[0]["response"] == "x" and rows[0]["correct"] == 1
    assert rows[1]["response"] == "y" and rows[1]["correct"] == 0
    assert rows[0]["rating"] == "good" and rows[1]["rating"] == "bad"


def test_old_rows_have_no_response_instead_of_fake_ones(db):
    """迁移 0015 之前的老记录只有档位 —— 如实给 None，不假装那时也记了作答。"""
    note_id = _note(db)
    learning.review_card(db, note_id, 1, "good")
    row = learning.card_attempts(db, note_id, 1)[0]
    assert row["rating"] == "good"
    assert row["response"] is None and row["correct"] is None
