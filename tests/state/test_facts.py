"""`state/facts.py`：记忆行的增删改。

只钉三件容易被改写坏的事：
- 插入的列与值一一对应（``target`` 决定账本，写错就串账）；
- ``update_fact(kind=None)`` **不动** ``kind``（改正文不等于改类别）；
- ``delete_fact`` 只删指定那条。
"""

from __future__ import annotations

from scholarscout.state import facts
from scholarscout.state.db import Database


def _row(db: Database, fact_id: str) -> dict[str, object]:
    row = db.query_one("SELECT * FROM user_facts WHERE id = ?", (fact_id,))
    assert row is not None
    return dict(row)


def _insert(db: Database, fact_id: str = "mem_001") -> str:
    facts.insert_fact(
        db,
        fact_id=fact_id,
        text="在写本科毕设",
        kind="fact",
        target="user",
        source_session="sess_001",
        created_at="2026-10-01T00:00:00+08:00",
    )
    return fact_id


def test_insert_fact_keeps_every_column(db: Database) -> None:
    _insert(db)
    row = _row(db, "mem_001")
    assert row["text"] == "在写本科毕设"
    assert row["kind"] == "fact"
    assert row["target"] == "user"
    assert row["source_session"] == "sess_001"
    assert row["created_at"] == "2026-10-01T00:00:00+08:00"
    assert row["updated_at"] is None


def test_update_without_kind_leaves_kind_untouched(db: Database) -> None:
    _insert(db)
    facts.update_fact(
        db, "mem_001", text="在写 AI+教育毕设", kind=None, updated_at="2026-10-02T00:00:00+08:00"
    )
    row = _row(db, "mem_001")
    assert row["text"] == "在写 AI+教育毕设"
    assert row["kind"] == "fact", "只改正文时不能顺手把类别也改掉"
    assert row["updated_at"] == "2026-10-02T00:00:00+08:00"


def test_update_with_kind_writes_both(db: Database) -> None:
    _insert(db)
    facts.update_fact(
        db, "mem_001", text="偏好中文", kind="preference", updated_at="2026-10-03T00:00:00+08:00"
    )
    row = _row(db, "mem_001")
    assert row["kind"] == "preference"
    assert row["text"] == "偏好中文"


def test_delete_fact_removes_only_that_row(db: Database) -> None:
    _insert(db, "mem_001")
    _insert(db, "mem_002")
    facts.delete_fact(db, "mem_001")
    assert db.query_one("SELECT id FROM user_facts WHERE id = ?", ("mem_001",)) is None
    assert db.query_one("SELECT id FROM user_facts WHERE id = ?", ("mem_002",)) is not None
