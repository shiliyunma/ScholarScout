"""记忆抽取要看的「会话侧产物」：这次会话产出了哪些主题、产出了多少东西。

为什么单独一个模块：这两条查询跨 ``learning_routes`` / ``topic_maps`` / ``notes``
三张表，不属于任何单张表的状态模块；它们只服务于「这个会话值不值得抽一次记忆」
这一个判断，而那个判断（阈值、轮数）在 ``runtime/memory.py``。

入参是领域值（会话 id），返回主题列表 / 计数，不暴露 ``sqlite3.Connection``。
"""

from __future__ import annotations

from scholarscout.state.db import Database


def session_topics(db: Database, session_id: str) -> list[str]:
    """这条会话产出的主题（学习路线 ∪ 主题地图，去重交给 ``UNION``）。"""
    rows = db.query(
        "SELECT topic FROM learning_routes WHERE session_id = ?"
        " UNION SELECT topic FROM topic_maps WHERE session_id = ?",
        (session_id, session_id),
    )
    return [str(row["topic"]) for row in rows]


def count_session_artifacts(db: Database, session_id: str) -> int:
    """学习路线 + 主题地图 + 笔记 的总条数（>0 就算「这次会话有产出」）。"""
    return db.scalar_int(
        "SELECT (SELECT count(*) FROM learning_routes WHERE session_id = ?)"
        " + (SELECT count(*) FROM topic_maps WHERE session_id = ?)"
        " + (SELECT count(*) FROM notes WHERE session_id = ?) AS n",
        (session_id, session_id, session_id),
    )
