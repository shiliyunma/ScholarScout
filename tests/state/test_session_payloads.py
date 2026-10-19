"""`state/sessions.py` 里两条"会话级状态"的落库：研究台账原文 + 系统提示词哈希。

钉的是**行数**与**不被顺手改掉的列**：
- 台账一会话一行（第二次保存是覆盖，不是再插一行）；
- 提示词哈希只动那一列（标题/时间等不跟着变）。
"""

from __future__ import annotations

from scholarscout.state import sessions
from scholarscout.state.db import Database


def test_research_payload_is_none_before_any_save(db: Database) -> None:
    session_id = sessions.create_session(db, model="stub-model", title="会话")
    assert sessions.research_payload(db, session_id) is None


def test_save_research_payload_overwrites_one_row(db: Database) -> None:
    session_id = sessions.create_session(db, model="stub-model", title="会话")
    sessions.save_research_payload(
        db, session_id, payload_json='{"goal": "A"}', updated_at="2026-10-01T00:00:00+08:00"
    )
    sessions.save_research_payload(
        db, session_id, payload_json='{"goal": "B"}', updated_at="2026-10-02T00:00:00+08:00"
    )
    assert sessions.research_payload(db, session_id) == '{"goal": "B"}'
    count = db.query_one(
        "SELECT count(*) AS n FROM research_states WHERE session_id = ?", (session_id,)
    )
    assert count is not None and count["n"] == 1, "同一会话只能有一行台账"
    row = db.query_one("SELECT updated_at FROM research_states WHERE session_id = ?", (session_id,))
    assert row is not None and row["updated_at"] == "2026-10-02T00:00:00+08:00"


def test_set_system_prompt_hash_only_touches_that_column(db: Database) -> None:
    session_id = sessions.create_session(db, model="stub-model", title="原标题")
    sessions.set_system_prompt_hash(db, session_id, "abc123")
    row = db.query_one("SELECT system_prompt_hash, title FROM sessions WHERE id = ?", (session_id,))
    assert row is not None
    assert row["system_prompt_hash"] == "abc123"
    assert row["title"] == "原标题"
