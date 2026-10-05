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

#: 标题里要剥掉的**祈使/客套前缀**（用户不会希望标题是"帮我…""请…"）
_TITLE_PREFIXES = (
    "帮我",
    "帮忙",
    "请你",
    "请",
    "麻烦",
    "我想",
    "我要",
    "我需要",
    "我想让你",
    "能不能",
    "可以",
    "帮忙看看",
    "给我",
    "我要你",
    "你来",
    "开始",
)
#: 这些是"占位标题"，不算用户起的名字 —— 见到就允许自动改名
#: （前端曾硬编码 title="新调研"，导致自动命名永远不触发；占位符集合让老会话也能被救回来）
PLACEHOLDER_TITLES = frozenset({"新调研", "新会话", "未命名", "未命名会话", "新的话题"})


def auto_title(content: str, *, max_chars: int = _TITLE_MAX) -> str:
    """从首条用户消息压出一个标题。

    做的是"总结"而不是原样截断：剥掉祈使/客套前缀（帮我/请/我想…）与首尾标点，
    再把空白压平。所以「帮我调研一下知识追踪方向该从哪里入门」→「调研一下知识追踪方向该从哪里入门…」。
    纯本地、零 token（标题每次新会话都要生成，不该花一次模型调用）。
    """
    text = " ".join(str(content or "").split())
    text = text.strip(" 　\t\r\n，。！？、：；,.!?:;…—-·")
    changed = True
    while changed and text:
        changed = False
        for prefix in sorted(_TITLE_PREFIXES, key=len, reverse=True):
            if text.startswith(prefix):
                text = text[len(prefix) :].lstrip(" 　，,。.、：:！!？?")
                changed = True
                break
    if not text:
        return "新会话"
    return text[:max_chars] if len(text) > max_chars else text


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
            retitle_if_unnamed(db, session_id, content)
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
    """兼容旧调用点：等价于 `auto_title`。"""
    return auto_title(content)


def retitle_if_unnamed(db: Database, session_id: str, content: str) -> str | None:
    """会话还没有**真标题**时，用这条消息给它起名；返回新标题（没改则 None）。

    "没有真标题" = NULL / 空白 / 占位符（`PLACEHOLDER_TITLES`）——
    占位符也算未命名，否则前端一旦传了 `title: "新调研"`，自动命名就永远轮不到
    （这是真发生过的：所有会话都叫"新调研"）。
    用户手动改过名的会话**不会被覆盖**。
    """
    title = auto_title(content)
    placeholders = ",".join("?" for _ in PLACEHOLDER_TITLES)
    params: tuple[Any, ...] = (title, session_id, *sorted(PLACEHOLDER_TITLES))
    cursor = db.execute(
        f"""
        UPDATE sessions SET title = ?, updated_at = ?
        WHERE id = ?
          AND (title IS NULL OR trim(title) = '' OR title IN ({placeholders}))
        """,
        (title, now_iso(), *params[1:]),
    )
    return title if cursor.rowcount else None


def rename_session(db: Database, session_id: str, title: str) -> str | None:
    """用户手动改名（改过一次就不再自动命名）。返回存进去的标题；会话不存在返回 None。"""
    cleaned = " ".join(str(title or "").split())[:_TITLE_MAX]
    if not cleaned:
        raise ValueError("标题不能为空")
    cursor = db.execute(
        "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
        (cleaned, now_iso(), session_id),
    )
    return cleaned if cursor.rowcount else None
