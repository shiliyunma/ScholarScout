"""候选来源的六条查询（`tools/source_context.py` 的三级取法 + `tools/source_grade.py` 的两条
+ `runtime/practice.py` 的一条）。

前五处原本长在工具层（`tools/source_context.py` 3 处、`tools/source_grade.py` 3 处，其中按 id
那一条两处原文相同，共用 `source_by_id`）；第六条原本长在 `runtime/practice.py` 的
`gather_sources`（抽背卡求助时喂给模型的「本会话已入库资料 + 最新摘要」），本轮一并搬进 state。
每一次都只是**位置**搬进来：列清单、过滤条件、`GROUP BY`、`ORDER BY`、`LIMIT` 一个字没动。业务边界仍留在工具层：URL 规范化由调用方先算好再传
`url_canonical`，`max(1, min(limit, 100))` 的夹取也不在这里做。

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


def source_by_url_canonical(db: Database, url_canonical: str) -> dict[str, Any] | None:
    """按**规范化后**的 URL 取一条来源（不存在回 ``None``）。

    规范化（`providers.base.canonical_url`）由调用方先做：state 只认库里存的那一列，
    不在这里猜「用户这次给的 URL 该不该去掉 utm 参数」。
    """
    row = db.query_one("SELECT * FROM sources WHERE url_canonical = ?", (url_canonical,))
    return dict(row) if row is not None else None


def ungated_sources(db: Database, *, limit: int) -> list[dict[str, Any]]:
    """还没判过权的来源（``tier IS NULL``），按 ``last_seen_at`` 倒序取 ``limit`` 条。

    ``limit`` 的上下夹取（``max(1, min(limit, 100))``）留在工具层：那是「顺手判多少条」的
    业务口径，不是这条查询的形状。
    """
    rows = db.query(
        """
        SELECT * FROM sources WHERE tier IS NULL
        ORDER BY last_seen_at DESC LIMIT ?
        """,
        (limit,),
    )
    return [dict(row) for row in rows]


def session_sources_with_summary(
    db: Database, *, session_id: str, limit: int
) -> list[dict[str, Any]]:
    """本会话带出来的来源 + 每条来源**最新一条**摘要文本（抽背卡求助时喂给模型）。

    原文从 `runtime/practice.py::gather_sources` 照搬，列清单、关联子查询、`ORDER BY`
    的「tier 未判的排最后」（`s.tier IS NULL` 在 SQLite 里是 0/1）、`LIMIT` 一个字没动。

    **业务边界留在 practice 层**：`session_id` 为空时直接回空列表（不查库）、
    `MAX_SOURCES=5` 的默认值、摘要换行压平与 `MAX_SUMMARY_CHARS=220` 截断、标题 `[:80]` 截断
    与「没标题就用 URL」的兜底，全在 `gather_sources`；这里只回原始行。
    """
    rows = db.query(
        "SELECT s.id AS source_id, s.title, s.url,"
        " (SELECT summary_short FROM summaries WHERE source_id = s.id"
        "  ORDER BY created_at DESC LIMIT 1) AS summary_short"
        " FROM source_mentions m JOIN sources s ON s.id = m.source_id"
        " WHERE m.session_id = ? ORDER BY s.tier IS NULL, s.tier LIMIT ?",
        (session_id, limit),
    )
    return [dict(row) for row in rows]
