"""画像原料的 state 读函数：`state/learner_profile.py` 七个函数各一条最小用例。

这些 SQL 原本长在 `runtime/learner_profile.py` 里，钉的是**口径**（取哪些列、按什么过滤/排序/
截多少条）—— 写错列会让画像的返回形状悄悄变形，界面上看不出来（画像只被拼成两三行中文）。
"""

from __future__ import annotations

from scholarscout.state import learner_profile as lp
from scholarscout.state.db import Database

from ..conftest import insert_source

T0 = "2026-10-07T00:00:00+08:00"


def _note(
    db: Database,
    note_id: str,
    *,
    kind: str = "card",
    title: str = "图算法 抽背卡",
    content: str = "## Q1 问题一？\n**A.** 答一。\n",
    tags_json: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO notes (id, session_id, kind, title, content, tags_json, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (note_id, "ses_1", kind, title, content, tags_json, T0),
    )


def _review(db: Database, note_id: str, index: int, *, reviewed_at: str = T0) -> None:
    db.execute(
        "INSERT INTO card_reviews (note_id, card_index, rating, reviews, reviewed_at, ease,"
        " interval_days, streak, lapses, due_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (note_id, index, "good", 1, reviewed_at, 2.5, 6.0, 2, 0, "2026-10-20T00:00:00+08:00"),
    )


def _route(db: Database, route_id: str, *, topic: str, weeks: int, created_at: str) -> None:
    db.execute(
        "INSERT INTO learning_routes (id, session_id, topic, weeks, plan_json, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (route_id, "ses_1", topic, weeks, "{}", created_at),
    )


def _map(db: Database, topic_id: str, *, topic: str, created_at: str) -> None:
    db.execute(
        "INSERT INTO topic_maps (id, session_id, topic, graph_json, created_at) VALUES (?,?,?,?,?)",
        (topic_id, "ses_1", topic, "{}", created_at),
    )


def test_card_group_notes_only_card_kind_and_its_four_columns(db: Database) -> None:
    """只要 `kind='card'` 的笔记：普通笔记混进来会让画像把非卡组当卡组算。"""
    _note(db, "nt_card", kind="card", tags_json='["concept"]')
    _note(db, "nt_note", kind="note", title="普通笔记")
    rows = lp.card_group_notes(db)
    assert [row["id"] for row in rows] == ["nt_card"]
    assert set(rows[0]) == {"id", "title", "content", "tags_json"}, "列清单变了会改掉画像的输入形状"
    assert rows[0]["tags_json"] == '["concept"]'


def test_review_schedule_returns_every_row_with_all_schedule_columns(db: Database) -> None:
    """排期按 (note_id, card_index) 全量给：少一列就跑不出 streak/lapses 分档。"""
    _note(db, "nt_card")
    _review(db, "nt_card", 0, reviewed_at="2026-10-07T10:00:00+08:00")
    _review(db, "nt_card", 1, reviewed_at="2026-10-06T10:00:00+08:00")
    rows = lp.review_schedule(db)
    assert {(row["note_id"], row["card_index"]) for row in rows} == {("nt_card", 0), ("nt_card", 1)}
    assert set(rows[0]) == {
        "note_id",
        "card_index",
        "rating",
        "reviews",
        "streak",
        "lapses",
        "interval_days",
        "reviewed_at",
        "due_at",
    }


def test_route_facts_carry_topic_weeks_and_created_at(db: Database) -> None:
    _route(db, "rt_1", topic="图算法", weeks=4, created_at="2026-10-01T00:00:00+08:00")
    _route(db, "rt_2", topic="图算法", weeks=6, created_at="2026-10-05T00:00:00+08:00")
    rows = lp.route_facts(db)
    assert sorted(row["weeks"] for row in rows) == [4, 6], (
        "同一主题多条路线要都留着（才算得出「几条」）"
    )
    assert set(rows[0]) == {"topic", "weeks", "created_at"}


def test_map_facts_carry_topic_and_created_at(db: Database) -> None:
    _map(db, "tm_1", topic="图算法", created_at="2026-10-02T00:00:00+08:00")
    rows = lp.map_facts(db)
    assert [row["topic"] for row in rows] == ["图算法"]
    assert set(rows[0]) == {"topic", "created_at"}


def test_note_counts_by_kind_groups_all_three_kinds(db: Database) -> None:
    _note(db, "nt_card_a", kind="card")
    _note(db, "nt_card_b", kind="card")
    _note(db, "nt_note", kind="note")
    _note(db, "nt_sum", kind="summary")
    assert lp.note_counts_by_kind(db) == {"card": 2, "note": 1, "summary": 1}


def test_review_activity_counts_attempts_and_distinct_days_since_bound(db: Database) -> None:
    """`since` 之后才计：窗口外的那条既不算次数也不算「学习天数」。"""
    _note(db, "nt_card")
    _review(db, "nt_card", 0, reviewed_at="2026-10-07T09:00:00+08:00")
    _review(db, "nt_card", 1, reviewed_at="2026-10-07T21:00:00+08:00")  # 同一天
    _review(db, "nt_card", 2, reviewed_at="2026-10-03T09:00:00+08:00")  # 窗口外
    activity = lp.review_activity(db, "2026-10-05T00:00:00+08:00")
    assert activity == {"reviews": 2, "days": 1}


def test_source_type_counts_are_top_n_by_count(db: Database) -> None:
    """按类型计数、多的在前、默认只给前 4 —— 画像那行只说「你信哪类资料」。"""
    counts = {"arxiv": 5, "web": 4, "official_blog": 3, "paper": 2, "other": 1}
    serial = 0
    for source_type, n in counts.items():
        for _ in range(n):
            serial += 1
            source_id = f"src_{serial:04d}"
            insert_source(db, source_id, url=f"https://example.com/{serial}")
            db.execute("UPDATE sources SET source_type = ? WHERE id = ?", (source_type, source_id))
    rows = lp.source_type_counts(db)
    assert [(row["source_type"], int(row["n"])) for row in rows] == [
        ("arxiv", 5),
        ("web", 4),
        ("official_blog", 3),
        ("paper", 2),
    ], "第 5 名（other，1 条）不该出现在前 4 里"
    assert set(rows[0]) == {"source_type", "n"}
    assert [row["source_type"] for row in lp.source_type_counts(db, limit=1)] == ["arxiv"]
