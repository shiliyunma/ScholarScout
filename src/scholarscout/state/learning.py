"""学习产物的读写：主题地图、学习路线、笔记与抽背卡。

三张表在 0001 里就建好了（docs/02 §7），这里只做仓储方法。
"""

from __future__ import annotations

import json
from typing import Any

from scholarscout.state.db import Database
from scholarscout.state.migrate import now_iso
from scholarscout.state.sessions import new_id

#: 允许的笔记类型（与 notes.kind 的 CHECK 约束一致）
NOTE_KINDS = ("summary", "note", "card")


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


# --------------------------------------------------------------------- 学习路线


def save_route(
    db: Database,
    *,
    topic: str,
    plan: dict[str, Any],
    level: str | None = None,
    weeks: int | None = None,
    topic_map_id: str | None = None,
    session_id: str | None = None,
    route_id: str | None = None,
) -> str:
    new_id_value = route_id or new_id("rte")
    db.execute(
        """
        INSERT INTO learning_routes (id, session_id, topic_map_id, topic, level, weeks, plan_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id_value,
            session_id,
            topic_map_id,
            topic,
            level,
            weeks,
            json.dumps(plan, ensure_ascii=False),
            now_iso(),
        ),
    )
    return new_id_value


def get_route(db: Database, route_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM learning_routes WHERE id = ?", (route_id,))
    if row is None:
        return None
    payload = dict(row)
    payload["plan"] = json.loads(str(payload.pop("plan_json")))
    return payload


def latest_route(db: Database, topic: str) -> dict[str, Any] | None:
    row = db.query_one(
        """
        SELECT * FROM learning_routes WHERE topic = ?
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (topic,),
    )
    if row is None:
        return None
    payload = dict(row)
    payload["plan"] = json.loads(str(payload.pop("plan_json")))
    return payload


# --------------------------------------------------------------------- 笔记与卡片


def save_note(
    db: Database,
    *,
    kind: str,
    content: str,
    title: str | None = None,
    refs: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    path: str | None = None,
    session_id: str | None = None,
    note_id: str | None = None,
) -> str:
    if kind not in NOTE_KINDS:
        raise ValueError(f"非法笔记类型：{kind!r}（允许：{NOTE_KINDS}）")
    new_id_value = note_id or new_id("not")
    db.execute(
        """
        INSERT INTO notes (id, session_id, kind, title, content, refs_json, tags_json, path, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id_value,
            session_id,
            kind,
            title,
            content,
            json.dumps(refs or {}, ensure_ascii=False),
            json.dumps(tags or [], ensure_ascii=False),
            path,
            now_iso(),
        ),
    )
    return new_id_value


def get_note(db: Database, note_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM notes WHERE id = ?", (note_id,))
    if row is None:
        return None
    payload = dict(row)
    payload["refs"] = json.loads(str(payload.pop("refs_json") or "{}"))
    payload["tags"] = json.loads(str(payload.pop("tags_json") or "[]"))
    return payload


def list_notes(db: Database, *, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    if kind:
        rows = db.query(
            "SELECT * FROM notes WHERE kind = ? ORDER BY created_at DESC LIMIT ?", (kind, limit)
        )
    else:
        rows = db.query("SELECT * FROM notes ORDER BY created_at DESC LIMIT ?", (limit,))
    return [dict(row) for row in rows]


def notes_for_source(db: Database, source_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """反查：某条资料被哪些笔记/卡片引用过。

    LIKE 只作**粗筛**（带引号，避免 ``src_aa`` 命中 ``src_aaa``），真正的判定在 Python 里逐字段比对
    ——子串匹配会把 ``src_aaa_doc`` 误当成 ``src_aaa``（踩过）。
    """
    rows = db.query(
        "SELECT * FROM notes WHERE refs_json LIKE ? ORDER BY created_at DESC LIMIT ?",
        (f'%"{source_id}"%', limit),
    )
    hits: list[dict[str, Any]] = []
    for row in rows:
        refs = json.loads(str(row["refs_json"] or "{}"))
        if not isinstance(refs, dict):
            continue
        for key in ("source_ids", "doc_ids", "summary_ids"):
            values = refs.get(key) or []
            if isinstance(values, list) and source_id in values:
                hits.append(dict(row))
                break
    return hits
