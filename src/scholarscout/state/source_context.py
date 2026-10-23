"""候选来源的三条查询（``tools/source_context.py`` 的三级取法：按 id / 按主题判权 / 最近入库）。

这三处 SQL 原本长在 ``tools/source_context.py`` 里（白名单 ``tools/source_context.py: 3``），
这一轮只是**位置**搬进 state：列清单、过滤条件、`GROUP BY`、`ORDER BY`、`LIMIT` 一个字没动。

两条口径容易写歪，放在这里是为了能用空库/多行直接试：

- 判权那一路是「来源 JOIN 它自己的判权行」，``max(g.created_at) AS graded_at`` 既是别名也是
  排序键，`GROUP BY s.id` 保证同一来源多条判权只出一行 —— 写成 `g.*` 会让一个来源出现多次；
- 最近入库那一路按 ``last_seen_at`` 倒序，不是 ``first_seen_at``（"最近还在被检索到"才算新）。

state 只回**原始行**：给模型看的标题/摘要（中文优先、摘要优先）由 tools 层的 ``attach_text``
挑，这里不替调用方猜形状。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from scholarscout.state.db import Database


def source_by_id(db: Database, source_id: str) -> dict[str, Any] | None:
    """按 id 取一条来源（不存在回 ``None``）。"""
    row = db.query_one("SELECT * FROM sources WHERE id = ?", (source_id,))
    return dict(row) if row is not None else None


def graded_sources_for_topic(
    db: Database, *, topic: str, verdicts: Sequence[str], limit: int
) -> list[dict[str, Any]]:
    """该主题下判权落在 ``verdicts`` 里的来源，按最近一次判权倒序取 ``limit`` 条。"""
    rows = db.query(
        """
        SELECT s.*, max(g.created_at) AS graded_at
        FROM sources s JOIN source_grades g ON g.source_id = s.id
        WHERE g.topic = ? AND g.verdict IN (?, ?)
        GROUP BY s.id
        ORDER BY graded_at DESC
        LIMIT ?
        """,
        (topic, *verdicts, limit),
    )
    return [dict(row) for row in rows]


def recent_sources(db: Database, *, limit: int) -> list[dict[str, Any]]:
    """最近入库的来源（按 ``last_seen_at`` 倒序）。"""
    rows = db.query("SELECT * FROM sources ORDER BY last_seen_at DESC LIMIT ?", (limit,))
    return [dict(row) for row in rows]
