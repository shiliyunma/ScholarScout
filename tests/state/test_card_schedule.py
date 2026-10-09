"""排期落库与"今天该练"队列（B6）。

口径：读真钟的地方一律把 `now` 注入进来，否则测试只能"跑到哪算哪"。
"""

from __future__ import annotations

import datetime

import pytest

from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state import learning
from scholarscout.state.db import Database
from scholarscout.state.migrate import apply_migrations

NOW = datetime.datetime(2026, 10, 7, 20, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))

CARD_CONTENT = """# 抽背

## Q1 GraphRAG 是什么？
**A.**
实体关系建图 + 社区摘要。

## Q2 社区摘要解决什么？
**A.**
全局性问题（"都在说什么"）靠社区摘要回答。
"""


def _card_note(db: Database, note_id: str = "nt_card") -> str:
    db.execute(
        "INSERT INTO notes (id, session_id, kind, title, content, created_at) VALUES (?,?,?,?,?,?)",
        (note_id, "ses_1", "card", "GraphRAG 抽背卡", CARD_CONTENT, "2026-10-07T00:00:00+08:00"),
    )
    return note_id


def test_review_persists_schedule(db: Database) -> None:
    note_id = _card_note(db)
    first = learning.review_card(db, note_id, 0, "good", now=NOW)
    assert first["interval_days"] == 1.0
    assert first["interval_label"] == "1 天后"
    assert first["due_at"] == "2026-10-08T20:00:00+08:00"
    row = db.query_one(
        "SELECT * FROM card_reviews WHERE note_id = ? AND card_index = 0", (note_id,)
    )
    assert row["ease"] == pytest.approx(2.6)
    assert row["streak"] == 1 and row["lapses"] == 0
    assert row["due_at"] == first["due_at"]

    # 第二次：读回上一次的 ease/streak，间隔变 6 天
    second = learning.review_card(db, note_id, 0, "good", now=NOW)
    assert second["interval_days"] == 6.0
    assert second["streak"] == 2
    assert (
        db.query_one("SELECT reviews FROM card_reviews WHERE note_id = ?", (note_id,))["reviews"]
        == 2
    )


def test_failure_records_a_lapse(db: Database) -> None:
    note_id = _card_note(db)
    learning.review_card(db, note_id, 0, "good", now=NOW)
    result = learning.review_card(db, note_id, 0, "bad", now=NOW)
    assert result["streak"] == 0 and result["lapses"] == 1
    assert result["due_at"] == "2026-10-08T20:00:00+08:00"


def test_due_queue_holds_new_and_overdue_only(db: Database) -> None:
    note_id = _card_note(db)
    # 两张卡：第 0 张答对（6 天后到期），第 1 张没练过（新卡）
    learning.review_card(db, note_id, 0, "good", now=NOW)
    learning.review_card(db, note_id, 0, "good", now=NOW)

    same_day = learning.due_cards(db, now=NOW)
    assert [item["card_index"] for item in same_day["items"]] == [1]
    assert same_day["items"][0]["is_new"] is True
    assert same_day["counts"] == {
        "total_cards": 2,
        "due": 1,
        "new": 1,
        "reviewed_cards": 1,
    }

    # 6 天后：第 0 张也到期了
    later = NOW + datetime.timedelta(days=6)
    both = learning.due_cards(db, now=later)
    assert sorted(item["card_index"] for item in both["items"]) == [0, 1]
    assert both["counts"]["due"] == 2 and both["counts"]["new"] == 1


def test_limit_only_truncates_items_not_counts(db: Database) -> None:
    """`limit` 只该影响"这次发几张给你练"，不该影响"到底有多少张到期"。

    真踩过：`counts.due` 用截断后的条数算，于是界面上的"今天该练 N 张"会随 limit 变。
    """
    note_id = _card_note(db)
    learning.review_card(db, note_id, 0, "good", now=NOW)  # 排到 6 天后
    learning.review_card(db, note_id, 1, "good", now=NOW)  # 同样排到 6 天后
    later = NOW + datetime.timedelta(days=7)  # 两张都到期
    limited = learning.due_cards(db, now=later, limit=1)
    assert len(limited["items"]) == 1
    assert limited["counts"]["due"] == 2
    zero = learning.due_cards(db, now=later, limit=0)
    assert zero["items"] == [] and zero["counts"]["due"] == 2


def test_card_stats_reports_due_and_next_due(db: Database) -> None:
    note_id = _card_note(db)
    learning.review_card(db, note_id, 0, "good", now=NOW)
    stats = learning.card_stats(db, now=NOW)
    assert stats["due"] == 1  # 卡片 1 是新卡
    assert stats["due_new"] == 1
    assert stats["total_cards"] == 2
    assert stats["next_due_at"] == "2026-10-08T20:00:00+08:00"


def test_review_rpc_returns_schedule(ctx: AppContext) -> None:
    note_id = _card_note(ctx.db)
    result = dispatch(ctx, "note.review", {"note_id": note_id, "card_index": 0, "rating": "good"})
    assert result["ok"] is True
    assert result["interval_days"] == 1.0
    assert result["interval_label"] == "1 天后"
    # 同一个响应里带上最新统计，前端不用再打一枪
    assert result["stats"]["due"] >= 1


def test_due_rpc(ctx: AppContext) -> None:
    note_id = _card_note(ctx.db)
    learning.review_card(ctx.db, note_id, 0, "good", now=datetime.datetime.now().astimezone())
    payload = dispatch(ctx, "note.due", {"limit": 10})
    assert payload["counts"]["total_cards"] == 2
    # ② 答案谜底分离：题目投影只有 front + card_id，**没有 back**（答案要单独调 note.reveal）
    assert all("front" in item and "card_id" in item for item in payload["items"])
    assert all("back" not in item for item in payload["items"])


def test_migration_adds_schedule_columns(db: Database) -> None:
    columns = {str(row["name"]) for row in db.query("PRAGMA table_info(card_reviews)")}
    assert {"ease", "interval_days", "streak", "lapses", "due_at"} <= columns
    assert apply_migrations(db) == []
