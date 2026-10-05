"""会话与消息的读写（仓储层）。表结构见 docs/02 §5。

约定：
- 写多行时用 ``db.transaction()``，避免半截状态
- ``messages.idx`` 由本模块分配（会话内从 0 递增），不依赖调用方
"""

from __future__ import annotations

import secrets
from typing import Any

from scholarscout.state.db import Database
from scholarscout.state.migrate import now_iso

_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # 去掉易混的 i l o 0 1
_TITLE_MAX = 32


def new_id(prefix: str) -> str:
    """前缀 + 8 位随机串，例如 ``ses_7fk2m9qa``。"""
    token = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return f"{prefix}_{token}"


def create_session(
    db: Database,
    *,
    title: str | None = None,
    session_key: str | None = None,
    source: str = "cli",
    model: str | None = None,
) -> str:
    session_id = new_id("ses")
    stamp = now_iso()
    db.execute(
        """
        INSERT INTO sessions (id, session_key, title, source, model, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, session_key, title, source, model, stamp, stamp),
    )
    return session_id


def get_session(db: Database, session_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
    return dict(row) if row is not None else None


def list_sessions(db: Database, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    rows = db.query(
        """
        SELECT s.id AS session_id, s.title, s.source, s.model,
               s.created_at, s.updated_at,
               (SELECT count(*) FROM messages m WHERE m.session_id = s.id AND m.active = 1)
                   AS message_count
        FROM sessions s
        ORDER BY s.updated_at DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )
    return [dict(r) for r in rows]


def touch_session(db: Database, session_id: str) -> None:
    db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now_iso(), session_id))


def append_message(
    db: Database,
    session_id: str,
    *,
    role: str,
    content: str,
    tool_calls_json: str | None = None,
    tool_call_id: str | None = None,
    name: str | None = None,
    token_in: int | None = None,
    token_out: int | None = None,
) -> str:
    """追加一条消息，返回 message_id。首个用户消息顺带把会话标题定下来。"""
    if role not in {"system", "user", "assistant", "tool"}:
        raise ValueError(f"非法 role：{role!r}")
    message_id = new_id("msg")
    stamp = now_iso()
    with db.transaction() as conn:
        next_idx = int(
            conn.execute(
                "SELECT coalesce(max(idx) + 1, 0) FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO messages (id, session_id, idx, role, content, tool_calls_json,
                                  tool_call_id, name, token_in, token_out, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                session_id,
                next_idx,
                role,
                content,
                tool_calls_json,
                tool_call_id,
                name,
                token_in,
                token_out,
                stamp,
            ),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (stamp, session_id))
        if role == "user":
            conn.execute(
                "UPDATE sessions SET title = ? WHERE id = ? AND (title IS NULL OR title = '')",
                (_title_from(content), session_id),
            )
    return message_id


def list_messages(
    db: Database,
    session_id: str,
    *,
    limit: int = 50,
    before_idx: int | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """按时间正序返回消息；``has_more`` 表示还有更早的。"""
    params: list[object] = [session_id]
    condition = "AND idx < ?"
    if before_idx is not None:
        params.append(before_idx)
    else:
        condition = ""
    params.append(limit + 1)
    rows = db.query(
        f"""
        SELECT id, idx, role, content, tool_calls_json, tool_call_id, name,
               token_in, token_out, created_at
        FROM messages
        WHERE session_id = ? AND active = 1 {condition}
        ORDER BY idx DESC
        LIMIT ?
        """,
        params,
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    return [dict(r) for r in reversed(page)], has_more


def count_messages(db: Database, session_id: str) -> int:
    return db.scalar_int(
        "SELECT count(*) FROM messages WHERE session_id = ? AND active = 1", (session_id,)
    )


def _title_from(content: str) -> str:
    text = " ".join(content.split())
    return text[:_TITLE_MAX] if text else "新会话"
