"""`state/sessions.py::message_rows` 的口径测试（压缩下沉的那一条读查询）。

钉的是三件压缩依赖、且**容易在改写时被顺手"优化"掉**的事：
- 列只有 `id` / `role` / `content`（回包形状 = 压缩要算的东西）；
- 按 `idx` 正序，不看 `created_at`（`covered` 是"第几条"，基准只能是 `idx`）；
- **不过滤 `active`**（被界面折叠的消息仍在序号里，滤掉会让 `covered` 错位）。
角色白名单与空正文过滤是 `runtime/compaction.py` 的口径，这里只管把行读对。
"""

from __future__ import annotations

from scholarscout.state import sessions
from scholarscout.state.db import Database


def _insert(
    db: Database,
    session_id: str,
    *,
    message_id: str,
    idx: int,
    role: str = "user",
    content: str = "内容",
    active: int = 1,
    created_at: str = "2026-10-23T00:00:00+08:00",
) -> None:
    db.execute(
        "INSERT INTO messages (id, session_id, idx, role, content, active, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (message_id, session_id, idx, role, content, active, created_at),
    )


def test_empty_session_returns_empty_list(db: Database) -> None:
    session_id = sessions.create_session(db, model="stub", title="空会话")
    assert sessions.message_rows(db, session_id) == []
    assert sessions.message_rows(db, "ses_不存在") == []


def test_rows_carry_exactly_three_columns(db: Database) -> None:
    session_id = sessions.create_session(db, model="stub", title="会话")
    _insert(db, session_id, message_id="m1", idx=0, role="user", content="第一句")

    rows = sessions.message_rows(db, session_id)
    assert len(rows) == 1
    assert list(rows[0]) == ["id", "role", "content"], "列清单就是压缩要算的三个字段"
    assert rows[0] == {"id": "m1", "role": "user", "content": "第一句"}


def test_rows_follow_idx_not_created_at(db: Database) -> None:
    """`idx` 与 `created_at` 顺序故意相反：回的必须是 `idx` 序。"""
    session_id = sessions.create_session(db, model="stub", title="会话")
    _insert(db, session_id, message_id="m0", idx=0, created_at="2026-10-23T09:00:00+08:00")
    _insert(db, session_id, message_id="m1", idx=1, created_at="2026-10-23T08:00:00+08:00")
    _insert(db, session_id, message_id="m2", idx=2, created_at="2026-10-23T07:00:00+08:00")

    assert [row["id"] for row in sessions.message_rows(db, session_id)] == ["m0", "m1", "m2"]


def test_inactive_and_tool_rows_are_not_filtered_here(db: Database) -> None:
    """`active=0` 与 `tool` 行都要回：过滤口径在 compaction，挪到 SQL 里会让计数错位。"""
    session_id = sessions.create_session(db, model="stub", title="会话")
    _insert(db, session_id, message_id="m0", idx=0, role="user", content="问")
    _insert(db, session_id, message_id="m1", idx=1, role="tool", content="{}")
    _insert(db, session_id, message_id="m2", idx=2, role="assistant", content="答", active=0)

    rows = sessions.message_rows(db, session_id)
    assert [row["id"] for row in rows] == ["m0", "m1", "m2"]
    assert [row["role"] for row in rows] == ["user", "tool", "assistant"]


def test_only_the_named_session_is_returned(db: Database) -> None:
    mine = sessions.create_session(db, model="stub", title="我")
    other = sessions.create_session(db, model="stub", title="别人")
    _insert(db, mine, message_id="m0", idx=0, content="我的")
    _insert(db, other, message_id="m9", idx=0, content="别人的")

    assert [row["content"] for row in sessions.message_rows(db, mine)] == ["我的"]
