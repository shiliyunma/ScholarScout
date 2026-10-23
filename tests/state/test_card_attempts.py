"""作答留痕（大浩选定：每题每次作答都留痕、可回看）。"""

from __future__ import annotations

import datetime

from scholarscout.state import learning, learning_cards

T1 = datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
T2 = T1 + datetime.timedelta(days=1)


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


# `review_log_rows`：`runtime/practice.review_history` 的查询（原本写在那层，现下沉到 state）。


def test_review_log_rows_empty_is_empty_list(db):
    assert learning_cards.review_log_rows(db, "nt_nope", 0) == []


def test_review_log_rows_returns_two_columns_in_append_order(db):
    """只回两列（档位 + 时间），且按**追加顺序**（`id`）而不是 `reviewed_at` 排序。"""
    note_id = _note(db)
    learning.review_card(db, note_id, 0, "good", now=T2)  # 时间故意倒着写：
    learning.review_card(db, note_id, 0, "bad", now=T1)  # 后写的反而更早
    rows = learning_cards.review_log_rows(db, note_id, 0)
    assert list(rows[0]) == ["rating", "reviewed_at"]
    assert [row["rating"] for row in rows] == ["good", "bad"]
    assert [row["reviewed_at"] for row in rows] == [T2.isoformat(), T1.isoformat()]


def test_review_log_rows_filters_by_card_index(db):
    """同一篇笔记里别的卡的日志不许串进来。"""
    note_id = _note(db)
    learning.review_card(db, note_id, 0, "good", now=T1)
    learning.review_card(db, note_id, 1, "fuzzy", now=T1)
    assert [row["rating"] for row in learning_cards.review_log_rows(db, note_id, 1)] == ["fuzzy"]
