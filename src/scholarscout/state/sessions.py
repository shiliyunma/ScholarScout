"""会话与消息的读写（仓储层）。表结构见 docs/02 §5。

约定：
- 写多行时用 ``db.transaction()``，避免半截状态
- ``messages.idx`` 由本模块分配（会话内从 0 递增），不依赖调用方
"""

from __future__ import annotations

import json
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


def get_session_by_key(db: Database, session_key: str) -> dict[str, Any] | None:
    """按 `session_key` 找会话（渠道层用外部 chat_id 当 key，好让同一会话连续）。"""
    row = db.query_one("SELECT * FROM sessions WHERE session_key = ?", (session_key,))
    return dict(row) if row is not None else None


def ensure_session(
    db: Database,
    *,
    session_key: str,
    title: str | None = None,
    source: str = "cli",
    model: str | None = None,
) -> tuple[str, bool]:
    """按 `session_key` **幂等**拿到会话：存在就返回它，不存在才建。返回 `(session_id, created)`。

    为什么需要它（而不是直接用 `create_session`）：`sessions.session_key` 是 `UNIQUE`，
    渠道层每条消息都用同一个外部 chat_id 当 key，直接 `create_session` 第二条就会撞唯一约束。
    幂等入口把"换/建"合并成一次判断，调用方不必自己读库（渠道层也不许读库）。
    """
    existing = get_session_by_key(db, session_key)
    if existing is not None:
        return str(existing["id"]), False
    return (
        create_session(db, title=title, session_key=session_key, source=source, model=model),
        True,
    )


def session_meta(db: Database, session_id: str) -> dict[str, Any]:
    """读会话的 `meta_json`（缺省/损坏都当空字典）。"""
    row = db.query_one("SELECT meta_json FROM sessions WHERE id = ?", (session_id,))
    if row is None or not row["meta_json"]:
        return {}
    try:
        parsed = json.loads(row["meta_json"])
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def set_session_meta(db: Database, session_id: str, values: dict[str, Any]) -> None:
    """按键合并写入 `meta_json`（不动其它键）。"""
    meta = session_meta(db, session_id)
    meta.update(values)
    db.execute(
        "UPDATE sessions SET meta_json = ? WHERE id = ?",
        (json.dumps(meta, ensure_ascii=False), session_id),
    )


def research_payload(db: Database, session_id: str) -> str | None:
    """读会话的研究台账原文（``research_states.payload_json``）；没有就 None。

    只回**字符串**，反序列化与"坏了当没有"的判断留给运行时（那是领域口径）。
    """
    row = db.query_one(
        "SELECT payload_json FROM research_states WHERE session_id = ?", (session_id,)
    )
    return None if row is None else str(row["payload_json"])


def save_research_payload(
    db: Database, session_id: str, *, payload_json: str, updated_at: str
) -> None:
    """写研究台账（一会话一行，存在就覆盖 payload 与时间）。"""
    db.execute(
        """
        INSERT INTO research_states (session_id, payload_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET payload_json = excluded.payload_json,
                                              updated_at = excluded.updated_at
        """,
        (session_id, payload_json, updated_at),
    )


def set_system_prompt_hash(db: Database, session_id: str, value: str) -> None:
    """记下这次回答用的系统提示词哈希（改了提示词，历史就不可比）。"""
    db.execute("UPDATE sessions SET system_prompt_hash = ? WHERE id = ?", (value, session_id))


def list_sessions(db: Database, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    rows = db.query(
        """
        SELECT s.id AS session_id, s.title, s.source, s.model,
               s.created_at, s.updated_at, COALESCE(s.pinned, 0) AS pinned,
               (SELECT count(*) FROM messages m WHERE m.session_id = s.id AND m.active = 1)
                   AS message_count
        FROM sessions s
        ORDER BY COALESCE(s.pinned, 0) DESC, s.updated_at DESC
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


def user_message_texts(db: Database, session_id: str) -> list[str]:
    """这条会话里**用户说过的话**（按 ``created_at`` 正序）。

    记忆抽取只要用户自己的表述：助手的长回答又长又贵，塞进去只会稀释信号。
    与 :func:`list_messages` 的两点区别：按 ``created_at`` 排、**不过滤 ``active``**
    —— 记忆的判据是"用户说过几轮"，不看界面当前还显示几条。
    """
    rows = db.query(
        "SELECT role, content FROM messages WHERE session_id = ? AND role = 'user'"
        " ORDER BY created_at",
        (session_id,),
    )
    return [str(row["content"]) for row in rows]


def count_user_messages(db: Database, session_id: str) -> int:
    """用户发言条数（记忆 nudge 的触发基准；同样**不过滤 ``active``**）。"""
    return db.scalar_int(
        "SELECT count(*) AS n FROM messages WHERE session_id = ? AND role = 'user'",
        (session_id,),
    )


def _title_from(content: str) -> str:
    """兼容旧调用点：等价于 `auto_title`。"""
    return auto_title(content)


def is_unnamed(title: str | None) -> bool:
    """标题是否算"未命名"（NULL / 空白 / 占位符都算）。

    与 `set_title_if_unnamed` 的 WHERE 条件必须一致：不一致会出现
    "判定说没名字、写回时又不给写"这种自相矛盾。
    """
    text = (title or "").strip()
    return not text or text in PLACEHOLDER_TITLES


def set_title_if_unnamed(db: Database, session_id: str, title: str) -> str | None:
    """会话还没有**真标题**时，把它设成给定的 `title`；返回新标题（没改则 None）。

    与 `retitle_if_unnamed` 的分工：这里接受**调用方算好的**标题（例如模型总结出来的），
    那里是用消息内容本地截断。两者共用同一套"什么算未命名"的判定。
    """
    text = (title or "").strip()
    if not text:
        return None
    placeholders = ",".join("?" for _ in PLACEHOLDER_TITLES)
    cursor = db.execute(
        f"""
        UPDATE sessions SET title = ?, updated_at = ?
        WHERE id = ?
          AND (title IS NULL OR trim(title) = '' OR title IN ({placeholders}))
        """,
        (text, now_iso(), session_id, *sorted(PLACEHOLDER_TITLES)),
    )
    return text if cursor.rowcount else None


def retitle_if_unnamed(db: Database, session_id: str, content: str) -> str | None:
    """会话还没有**真标题**时，用这条消息给它起名；返回新标题（没改则 None）。

    "没有真标题" = NULL / 空白 / 占位符（`PLACEHOLDER_TITLES`）——
    占位符也算未命名，否则前端一旦传了 `title: "新调研"`，自动命名就永远轮不到
    （这是真发生过的：所有会话都叫"新调研"）。
    用户手动改过名的会话**不会被覆盖**。
    """
    return set_title_if_unnamed(db, session_id, auto_title(content))


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


def set_pinned(db: Database, session_id: str, pinned: bool) -> bool:
    """置顶/取消置顶。

    **故意不碰 updated_at**：置顶只是换个位置，不该把它顶成"最近更新过"，
    否则取消置顶之后它会莫名其妙地跑到列表最前面。
    """
    cursor = db.execute(
        "UPDATE sessions SET pinned = ? WHERE id = ?",
        (1 if pinned else 0, session_id),
    )
    return bool(cursor.rowcount)


#: 删会话时会一起清掉的**会话内**表（外键级联的也算在内，便于报数）
_SESSION_TABLES = ("messages", "tool_traces", "source_mentions")
#: 会话里产出的学习产物（要不要跟着删由 keep_artifacts 决定）
_ARTIFACT_TABLES = ("topic_maps", "learning_routes", "notes")


def delete_session(
    db: Database, session_id: str, *, keep_artifacts: bool = False
) -> dict[str, int] | None:
    """删掉一次会话，返回各表删掉的行数；会话不存在返回 None。

    边界（都是踩过的/想过的坑）：

    - `messages` / `tool_traces` 有外键级联（`db.py` 开了 `PRAGMA foreign_keys=ON`），
      但 `source_mentions` **没有**外键 → 必须显式删，否则留下指不到会话的引用行；
    - 产物（`topic_maps` / `learning_routes` / `notes`）默认一起删（用户点"删除会话"的
      预期是这次会话连同产出一起消失），`keep_artifacts=True` 可只删对话；
    - **`summaries` 与 `usage_records` 一律保留**：前者属于资料库（不属于某次对话），
      后者是花费账本 —— 账本不该因为删对话被改写。
    """
    if db.query_one("SELECT id FROM sessions WHERE id = ?", (session_id,)) is None:
        return None

    victims = list(_SESSION_TABLES) + ([] if keep_artifacts else list(_ARTIFACT_TABLES))
    counts: dict[str, int] = {}
    with db.transaction() as conn:
        for table in victims:
            cursor = conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
            counts[table] = int(cursor.rowcount or 0)
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return counts
