"""主题地图的状态读写（从 state/learning.py 按域拆出，逻辑一字未改）。

对外入口仍是 ``scholarscout.state.learning``（那里做再导出），本模块是其实现。
"""

from __future__ import annotations

import json
from typing import Any

from scholarscout.state.db import Database
from scholarscout.state.migrate import now_iso
from scholarscout.state.sessions import new_id

#: 允许的笔记类型（与 notes.kind 的 CHECK 约束一致）
NOTE_KINDS = ("summary", "note", "card")

#: 抽背评价档位（与 card_reviews.rating 的 CHECK 约束一致）
#: good=会了 / fuzzy=模糊 / bad=不会 —— 三档就够，再多用户自己也分不清
CARD_RATINGS = ("good", "fuzzy", "bad")


# --------------------------------------------------------------------- 主题地图


def save_topic_map(
    db: Database,
    *,
    topic: str,
    graph: dict[str, Any],
    session_id: str | None = None,
    map_id: str | None = None,
) -> str:
    new_id_value = map_id or new_id("tpm")
    db.execute(
        """
        INSERT INTO topic_maps (id, session_id, topic, graph_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (new_id_value, session_id, topic, json.dumps(graph, ensure_ascii=False), now_iso()),
    )
    return new_id_value


def replace_topic_map_graph(db: Database, *, topic_id: str, graph: dict[str, Any]) -> bool:
    """**原子替换**整份 `graph_json`：先序列化，再在一个事务里 `UPDATE` 一行。

    两道保险，缺一不可：

    1. **先序列化、后动库** —— 图里混进不可 JSON 化的东西（`set` / 自定义对象）时，
       异常发生在写之前的 `json.dumps` 里，旧图**一个字节都没被碰过**；
    2. **单条 UPDATE 走一个写事务** —— SQLite 不会出现"写了一半"的 `graph_json`；
       中途任何异常都 ROLLBACK，读到的仍是旧整图。

    为什么不是"先 INSERT 新行、再删旧行"：那会让 `topic_maps.id` 变掉，前端手上的
    `topic_id`、`route.topic_map_id` 外键一起失效。原地替换才叫"同一张图长大了"。

    返回 `False` 表示这一行不存在（不静默新建 —— 生长只作用于已有图）。
    """
    payload = json.dumps(graph, ensure_ascii=False)
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE topic_maps SET graph_json = ? WHERE id = ?", (payload, topic_id)
        )
        return bool(cursor.rowcount)


def get_topic_map(db: Database, topic_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM topic_maps WHERE id = ?", (topic_id,))
    if row is None:
        return None
    payload = dict(row)
    payload["graph"] = json.loads(str(payload.pop("graph_json")))
    return payload


def latest_topic_map(db: Database, topic: str) -> dict[str, Any] | None:
    row = db.query_one(
        """
        SELECT * FROM topic_maps WHERE topic = ?
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (topic,),
    )
    if row is None:
        return None
    payload = dict(row)
    payload["graph"] = json.loads(str(payload.pop("graph_json")))
    return payload


def list_topic_maps(db: Database, *, limit: int = 50) -> list[dict[str, Any]]:
    """列出历史主题图（**轻量**：不带 graph 正文，只给条目信息，供列表与点击加载）。

    为什么要这个：前端原先只能看"本会话刚生成的那一份"，进程一退、会话一换，
    库里攒下的产物就再也点不开了。列表只回摘要信息，正文仍走 `get_topic_map`。
    """
    rows = db.query(
        """
        SELECT id, session_id, topic, graph_json, created_at
        FROM topic_maps ORDER BY created_at DESC, rowid DESC LIMIT ?
        """,
        (int(limit),),
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            graph = json.loads(str(row["graph_json"]))
        except (TypeError, ValueError):
            graph = {}
        nodes = graph.get("nodes") if isinstance(graph, dict) else None
        items.append(
            {
                "topic_id": row["id"],
                "session_id": row["session_id"],
                "topic": row["topic"],
                "created_at": row["created_at"],
                "nodes": len(nodes) if isinstance(nodes, list) else 0,
                "mode": graph.get("mode") if isinstance(graph, dict) else None,
                "mode_reason": graph.get("mode_reason") if isinstance(graph, dict) else None,
            }
        )
    return items
