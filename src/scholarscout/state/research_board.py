"""研究看板（`research.board` / `research.overview`）的**读侧仓储**。

为什么单独一个模块：看板一次要把 ``sources`` / ``source_mentions`` / ``documents`` / ``summaries`` /
``topic_maps`` / ``learning_routes`` / ``notes`` 七八张表的形状拼起来，任何单张表的状态模块都装不下；
而这些查询全是「只读 + 固定的列与排序」，正是最该被钉在 ``state/`` 里的东西 ——
排序或过滤写错时，界面上只表现为"某几行不见了"，没人会怀疑是 SQL。

三条口径（与 ``runtime/research_view.py`` 的模块 docstring 一致，SQL 原文从那里照搬）：

1. **只读**：没有一条 INSERT/UPDATE；
2. **列与排序是约定**：会话来源按年份已知优先、再倒序、再按 id；产物按 ``created_at`` 倒序；
3. **"有没有正文/摘要"在这里算成布尔值**：调用方（看板）拿到的就是领域值，不用再理解
   ``count(*) AS docs`` 这种计数列。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from scholarscout.state.db import Database


def session_sources(db: Database, session_id: str) -> list[dict[str, Any]]:
    """本会话带出来的来源 + 每个来源的"有没有正文/有没有摘要"。"""
    rows = db.query(
        """
        SELECT s.id AS source_id, s.title, s.url, s.domain, s.source_type, s.year, s.tier,
               (SELECT count(*) FROM documents d WHERE d.source_id = s.id) AS docs,
               (SELECT count(*) FROM summaries sm WHERE sm.source_id = s.id) AS sums
        FROM sources s JOIN source_mentions m ON m.source_id = s.id
        WHERE m.session_id = ?
        GROUP BY s.id
        ORDER BY (s.year IS NULL), s.year DESC, s.id
        """,
        (session_id,),
    )
    out = []
    for row in rows:
        item = dict(row)
        item["has_text"] = bool(item.pop("docs"))
        item["has_summary"] = bool(item.pop("sums"))
        out.append(item)
    return out


def sources_by_ids(db: Database, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """按 id 取来源（**跨全会话**，不限本会话），键是 ``str(source_id)``。

    为什么不限本会话：引用核查记的是"这条断言引用了哪些来源"，那些来源可能是在
    生成产物的那一轮带出来的。只在会话内找会让一多半证据行显示"没有可解析的来源"。
    """
    wanted = [str(item) for item in ids if item]
    if not wanted:
        return {}
    placeholders = ",".join("?" for _ in wanted)
    rows = db.query(
        f"""
        SELECT s.id AS source_id, s.title, s.url, s.domain, s.source_type, s.year, s.tier,
               (SELECT count(*) FROM summaries sm WHERE sm.source_id = s.id) AS sums
        FROM sources s WHERE s.id IN ({placeholders})
        """,
        tuple(wanted),
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        item["has_summary"] = bool(item.pop("sums"))
        out[str(item["source_id"])] = item
    return out


def session_artifacts(db: Database, session_id: str) -> dict[str, list[dict[str, Any]]]:
    """本会话的产物清单（主题图 / 学习路线 / 笔记），只给列表需要的列。"""
    return {
        "maps": [
            dict(row)
            for row in db.query(
                "SELECT id, topic, created_at FROM topic_maps WHERE session_id = ?"
                " ORDER BY created_at DESC",
                (session_id,),
            )
        ],
        "routes": [
            dict(row)
            for row in db.query(
                "SELECT id, topic, level, weeks, version, created_at FROM learning_routes"
                " WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            )
        ],
        "notes": [
            dict(row)
            for row in db.query(
                "SELECT id, kind, title, created_at FROM notes WHERE session_id = ?"
                " ORDER BY created_at DESC",
                (session_id,),
            )
        ],
    }


def artifact_counts(db: Database, session_id: str) -> dict[str, int]:
    """本会话的产物条数（跨会话汇总用）。``cards`` 是 ``notes`` 里 ``kind='card'`` 那一档，
    因此 ``notes`` 的口径是"非卡片笔记"，两者不重叠。"""
    return {
        "maps": db.scalar_int(
            "SELECT count(*) FROM topic_maps WHERE session_id = ?", (session_id,)
        ),
        "routes": db.scalar_int(
            "SELECT count(*) FROM learning_routes WHERE session_id = ?", (session_id,)
        ),
        "notes": db.scalar_int(
            "SELECT count(*) FROM notes WHERE session_id = ? AND kind != 'card'", (session_id,)
        ),
        "cards": db.scalar_int(
            "SELECT count(*) FROM notes WHERE session_id = ? AND kind = 'card'", (session_id,)
        ),
    }


def recent_sessions(db: Database, *, limit: int = 30) -> list[dict[str, Any]]:
    """最近更新的会话（跨会话汇总的入口）。

    排序**只按** ``updated_at`` 倒序：置顶（``pinned``）是"我的会话列表"的排序需求，
    看板要的是"最近在做什么"，两者混在一起会让看板头几行永远是被置顶的旧会话。
    """
    rows = db.query(
        """
        SELECT s.id AS session_id, s.title, s.updated_at
        FROM sessions s
        ORDER BY s.updated_at DESC
        LIMIT ?
        """,
        (int(limit),),
    )
    return [dict(row) for row in rows]
