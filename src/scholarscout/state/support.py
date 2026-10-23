"""引用核查的落库与读侧（`support_checks` 表 + 待审对需要的两张原料表）。

一条判定 = 一个 (artifact, item)，同一对只保留最新一次判定（``ON CONFLICT`` 覆盖
verdict/reason/证据等级/时间），这样复核同一批产物**不重复计费**。
判定怎么产生（模型调用、回复解析）在 ``grading/support.py``，不属于状态层。

读侧那五个函数（`source_row` / `latest_source_summary` / `topic_map_row` / `route_row` /
`checks_for_artifact`）原本以 SQL 原文长在 ``grading/support.py`` 里，这一轮只是**位置**搬进
state：列清单、过滤条件、排序、`LIMIT` 一个字没动，解析（`json.loads`）仍留在 grading 层 ——
这里返回**原始行**，不替调用方猜形状。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from scholarscout.state.db import Database


def upsert_check(
    db: Database,
    *,
    check_id: str,
    artifact_kind: str,
    artifact_id: str,
    session_id: str | None,
    item_id: str,
    claim: str,
    source_ids: Sequence[str],
    evidence_level: str,
    verdict: str,
    reason: str,
    model: str,
    prompt_version: str,
    created_at: str,
) -> None:
    """写入/覆盖一条核查判定（``(artifact_id, item_id)`` 唯一）。"""
    db.execute(
        """
        INSERT INTO support_checks (id, artifact_kind, artifact_id, session_id, item_id, claim,
                                    source_ids_json, evidence_level, verdict, reason, model,
                                    prompt_version, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (artifact_id, item_id) DO UPDATE SET
            verdict = excluded.verdict, reason = excluded.reason,
            evidence_level = excluded.evidence_level, created_at = excluded.created_at
        """,
        (
            check_id,
            artifact_kind,
            artifact_id,
            session_id,
            item_id,
            claim,
            json.dumps(list(source_ids), ensure_ascii=False),
            evidence_level,
            verdict,
            reason,
            model,
            prompt_version,
            created_at,
        ),
    )


def list_checks(db: Database, session_id: str) -> list[dict[str, Any]]:
    """本会话的全部判定（看板证据表的原料），``source_ids`` 已解析成列表。

    坏 JSON 退化为空列表而不是抛：一条判定的 ids 写坏了不该让整张看板空着 ——
    那一行会显示"没有可解析的来源"，比整页 500 更接近事实。
    """
    rows = db.query(
        "SELECT artifact_kind, artifact_id, item_id, claim, source_ids_json, evidence_level,"
        " verdict, reason FROM support_checks WHERE session_id = ? ORDER BY artifact_kind, item_id",
        (session_id,),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["source_ids"] = [
                str(entry) for entry in json.loads(str(item["source_ids_json"]) or "[]")
            ]
        except json.JSONDecodeError:
            item["source_ids"] = []
        out.append(item)
    return out


# --------------------------------------------------- 待审对的原料（读侧）


def source_row(db: Database, source_id: str) -> dict[str, Any] | None:
    """一条来源的元数据（弱证据就靠这五列拼出来）；库里没有就返回 `None`。"""
    row = db.query_one(
        "SELECT id, title, url, year, source_type FROM sources WHERE id = ?", (source_id,)
    )
    return None if row is None else dict(row)


def latest_source_summary(db: Database, source_id: str) -> dict[str, Any] | None:
    """这条来源**最新一条有正文**的摘要（强证据）；一条都没有就是 `None`。

    `coalesce(summary_short,'') != ''` 是口径的一部分：空白摘要不能把证据撑成"有摘要"。
    """
    row = db.query_one(
        "SELECT title_zh, summary_short, points_json FROM summaries"
        " WHERE source_id = ? AND coalesce(summary_short,'') != ''"
        " ORDER BY created_at DESC LIMIT 1",
        (source_id,),
    )
    return None if row is None else dict(row)


def topic_map_row(db: Database, map_id: str) -> dict[str, Any] | None:
    """主题图的 `graph_json` 原文（**不解析**：形状由 grading 层负责）。"""
    row = db.query_one("SELECT id, graph_json FROM topic_maps WHERE id = ?", (map_id,))
    return None if row is None else dict(row)


def route_row(db: Database, route_id: str) -> dict[str, Any] | None:
    """学习路线的 `plan_json` 原文（同上：解析留在调用方）。"""
    row = db.query_one("SELECT id, plan_json FROM learning_routes WHERE id = ?", (route_id,))
    return None if row is None else dict(row)


def checks_for_artifact(db: Database, artifact_id: str) -> list[dict[str, Any]]:
    """一件产物的全部判定，按 `item_id` 排序（与 `list_checks` 的会话口径区分开）。"""
    rows = db.query(
        "SELECT * FROM support_checks WHERE artifact_id = ? ORDER BY item_id", (artifact_id,)
    )
    return [dict(row) for row in rows]
