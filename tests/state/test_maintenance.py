"""`state/maintenance.py`：库整理三条（页数统计 / VACUUM / PRAGMA optimize）。

为什么单独钉：这三条原来写在 `settings_io` 里（"设置面板"顺手管数据库），
搬进 state 之后最容易坏的是**语义**——VACUUM 到底有没有回收页、页数口径是不是逻辑大小。
"调通了"不等于"真的回收了"，所以这里比页数，不比返回值。
"""

from __future__ import annotations

from scholarscout.state import maintenance, sessions
from scholarscout.state.db import Database


def _fill(db: Database, *, count: int = 60) -> None:
    """塞够多的行，让"删完不 VACUUM 仍占页"这件事可观测。"""
    for index in range(count):
        session_id = sessions.create_session(db, model="stub-model", title=f"占位 {index}")
        sessions.append_message(db, session_id, role="user", content="x" * 800)


def test_database_pages_is_the_logical_size(db: Database) -> None:
    """页数口径 = `page_count × page_size`（WAL 下文件字节数会骗人，只信这个）。"""
    db.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, blob TEXT)")
    for _ in range(50):
        db.execute("INSERT INTO probe (blob) VALUES (?)", ("y" * 500,))

    expected = db.scalar_int("PRAGMA page_count") * db.scalar_int("PRAGMA page_size")
    assert maintenance.database_pages(db) == expected
    assert maintenance.database_pages(db) > 0


def test_vacuum_reclaims_pages_after_deletes(db: Database) -> None:
    """删行**不会**自己减页；`vacuum` 之后必须真的回收（否则"整理"就是个安慰按钮）。"""
    _fill(db)
    pages_full = maintenance.database_pages(db)

    db.execute("DELETE FROM messages")
    db.execute("DELETE FROM sessions")
    assert maintenance.database_pages(db) >= pages_full - 1, "删行不该立刻减页（SQLite 只标空闲）"

    maintenance.vacuum(db)
    assert maintenance.database_pages(db) < pages_full, "VACUUM 之后页数必须下降"


def test_optimize_is_safe_to_run_repeatedly(db: Database) -> None:
    """`PRAGMA optimize` 是轻量统计刷新：连跑两次不许报错，也不许动数据。"""
    _fill(db, count=3)
    maintenance.optimize(db)
    maintenance.optimize(db)
    assert db.scalar_int("SELECT COUNT(*) FROM sessions") == 3
