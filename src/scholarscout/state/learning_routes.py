"""学习路线与版本历史的状态读写（从 state/learning.py 按域拆出，逻辑一字未改）。

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
        INSERT INTO learning_routes (id, session_id, topic_map_id, topic, level, weeks, plan_json, created_at, version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            1,
        ),
    )
    return new_id_value


def archive_version(
    db: Database,
    *,
    kind: str,
    artifact_id: str,
    version: int,
    payload: dict[str, Any],
    note: str | None = None,
) -> None:
    """把某一版产物正文存进 `artifact_versions`（可回退的账本）。

    `INSERT OR REPLACE`：同一版重复归档不报错（重试安全），但还是那一版，不会伪造出新版号。
    """
    if kind not in ("route", "map"):
        raise ValueError(f"未知产物类型：{kind}")
    db.execute(
        """
        INSERT OR REPLACE INTO artifact_versions
            (kind, artifact_id, version, payload_json, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (kind, artifact_id, int(version), json.dumps(payload, ensure_ascii=False), note, now_iso()),
    )


def list_versions(db: Database, *, kind: str, artifact_id: str) -> list[dict[str, Any]]:
    """按版本号**从新到旧**列出归档版本（只给元信息，正文按需取）。"""
    rows = db.query(
        """
        SELECT version, note, created_at FROM artifact_versions
        WHERE kind = ? AND artifact_id = ?
        ORDER BY version DESC
        """,
        (kind, artifact_id),
    )
    return [dict(row) for row in rows]


def get_version(
    db: Database, *, kind: str, artifact_id: str, version: int
) -> dict[str, Any] | None:
    """取某个归档版本的正文（用于"退回上一版"或人工核对）。"""
    row = db.query_one(
        """
        SELECT version, payload_json, note, created_at FROM artifact_versions
        WHERE kind = ? AND artifact_id = ? AND version = ?
        """,
        (kind, artifact_id, int(version)),
    )
    if row is None:
        return None
    payload = dict(row)
    payload["payload"] = json.loads(payload.pop("payload_json"))
    return payload


def revise_route(
    db: Database,
    route_id: str,
    plan: dict[str, Any],
    *,
    note: str | None = None,
) -> int:
    """把新计划写回原路线（原地升级），**先把旧版归档**，返回新版本号。

    为什么必须先归档再改写：路线是花钱生成出来的，改错了要能退回去 ——
    没有旧版就只剩"重新生成一遍"，那正是这次要避免的贵操作。
    """
    row = db.query_one("SELECT * FROM learning_routes WHERE id = ?", (route_id,))
    if row is None:
        raise ValueError(f"路线不存在：{route_id}")
    current = int(row["version"] or 1)
    archive_version(
        db,
        kind="route",
        artifact_id=route_id,
        version=current,
        payload=json.loads(row["plan_json"]),
        note=note,
    )
    new_version = current + 1
    db.execute(
        """
        UPDATE learning_routes
        SET plan_json = ?, weeks = ?, version = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            json.dumps(plan, ensure_ascii=False),
            len(plan.get("weeks") or []),
            new_version,
            now_iso(),
            route_id,
        ),
    )
    return new_version


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


def list_routes(db: Database, *, limit: int = 50) -> list[dict[str, Any]]:
    """列出历史学习路线（轻量版，正文仍走 `get_route`）。"""
    rows = db.query(
        """
        SELECT id, session_id, topic_map_id, topic, level, weeks, plan_json, created_at
        FROM learning_routes ORDER BY created_at DESC, rowid DESC LIMIT ?
        """,
        (int(limit),),
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            plan = json.loads(str(row["plan_json"]))
        except (TypeError, ValueError):
            plan = {}
        week_items = plan.get("weeks") if isinstance(plan, dict) else None
        entries = 0
        if isinstance(week_items, list):
            for week in week_items:
                if isinstance(week, dict) and isinstance(week.get("items"), list):
                    entries += len(week["items"])
        items.append(
            {
                "route_id": row["id"],
                "session_id": row["session_id"],
                "topic_map_id": row["topic_map_id"],
                "topic": row["topic"],
                "level": row["level"],
                "weeks": row["weeks"],
                "created_at": row["created_at"],
                "entries": entries,
                "mode": plan.get("mode") if isinstance(plan, dict) else None,
                "mode_reason": plan.get("mode_reason") if isinstance(plan, dict) else None,
            }
        )
    return items
