"""学习产物的读写：主题地图、学习路线、笔记与抽背卡。

三张表在 0001 里就建好了（docs/02 §7），这里只做仓储方法。
"""

from __future__ import annotations

import datetime
import json
import re
from typing import Any

from scholarscout.learning import srs
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


def parse_cards(content: str) -> list[dict[str, str]]:
    """把抽背卡的 Markdown 正文拆成 `[{front, back}]`。

    为什么在后端拆：格式（`## Q…` / `A.`）是**我们自己的约定**，
    前端各写一份正则早晚会漂移；这里是唯一实现，前端只消费结果。

    **口径（写清楚，因为它有取舍）**：
    - 每个 `## …` 段是问题（去掉 `Q1.` / `Q1、` 这类编号）；
    - **问题段之后的第一段非空正文就是答案**，`A.` / `**A.**` / `A：` 前缀只是顺手去掉 ——
      模型经常直接写一段话而不带 `A.`，只认前缀会把好卡丢掉；
    - 正文**只含标点/空白**（没有字母、数字或汉字）时丢弃该段 —— 那种"答案"没有信息；
    - 因此像"（模型忘了写答案）"这类占位文字**会被保留成一张卡**：抽背时一眼能看出它无效，
      比默默少一张卡好（少卡会让人以为系统漏了）。
    """
    cards: list[dict[str, str]] = []
    front: str | None = None
    answer: list[str] = []

    def flush() -> None:
        nonlocal front, answer
        if front and answer:
            cards.append({"front": front, "back": "\n".join(answer).strip()})
        front, answer = None, []

    for raw in (content or "").splitlines():
        line = raw.strip()
        if line.startswith("## "):
            flush()
            text = line[3:].strip()
            # 去掉 "Q1." / "Q1、" 这类编号前缀
            text = re.sub(r"^Q\s*\d+\s*[.、:：)]?\s*", "", text, flags=re.IGNORECASE)
            front = text or None
            continue
        if front is None:
            continue  # 还没进入卡片区（例如 "# 抽背卡" 标题）
        if answer or line:
            answer.append(raw.rstrip())
    flush()
    return _clean_cards(cards)


def _clean_cards(cards: list[dict[str, str]]) -> list[dict[str, str]]:
    """去掉 `A.` 前缀与残留星号，并丢掉"没有信息量"的答案段。"""
    cleaned: list[dict[str, str]] = []
    for card in cards:
        back = card["back"].strip()
        back = re.sub(r"^\*{0,2}A\*{0,2}\s*[.、:：)\]]?\s*", "", back)
        back = re.sub(r"^[*\s]+", "", back).strip()
        if not _has_content(back):
            continue
        cleaned.append({"front": card["front"], "back": back})
    return cleaned


def _has_content(text: str) -> bool:
    """答案里至少得有一个字母、数字或汉字 —— 只有标点的"答案"没有信息。"""
    return any(char.isalnum() for char in text)


def get_note(db: Database, note_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM notes WHERE id = ?", (note_id,))
    if row is None:
        return None
    payload = dict(row)
    payload["refs"] = json.loads(str(payload.pop("refs_json") or "{}"))
    payload["tags"] = json.loads(str(payload.pop("tags_json") or "[]"))
    if str(payload.get("kind")) == "card":
        # 卡片笔记额外给一份拆好的卡 + 各自的复习评价（界面据此画抽背模式）
        reviews = card_review_map(db, note_id)
        payload["cards"] = [
            {**card, "index": index, "rating": reviews.get(index)}
            for index, card in enumerate(parse_cards(str(payload.get("content") or "")))
        ]
    return payload


def review_card(
    db: Database,
    note_id: str,
    card_index: int,
    rating: str,
    *,
    now: datetime.datetime | None = None,
) -> dict[str, Any]:
    """记一次抽背评价，并按 SM-2 排下一次复习（同一张卡重复评＝更新）。

    `now` 可注入：测试要钉住"下次是什么时候"，读真钟就钉不住。
    """
    if rating not in CARD_RATINGS:
        raise ValueError(f"非法评价：{rating!r}（允许：{', '.join(CARD_RATINGS)}）")
    if card_index < 0:
        raise ValueError("card_index 不能为负")
    moment = now or datetime.datetime.now().astimezone()
    previous = db.query_one(
        "SELECT ease, interval_days, streak, lapses FROM card_reviews"
        " WHERE note_id = ? AND card_index = ?",
        (note_id, card_index),
    )
    schedule = srs.review(
        rating=rating,
        ease=(previous["ease"] if previous is not None else None),
        interval_days=(previous["interval_days"] if previous is not None else None),
        streak=(int(previous["streak"] or 0) if previous is not None else 0),
        lapses=(int(previous["lapses"] or 0) if previous is not None else 0),
        now=moment,
    )
    db.execute(
        """
        INSERT INTO card_reviews (note_id, card_index, rating, reviews, reviewed_at,
                                  ease, interval_days, streak, lapses, due_at)
        VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (note_id, card_index) DO UPDATE SET
            rating = excluded.rating,
            reviews = card_reviews.reviews + 1,
            reviewed_at = excluded.reviewed_at,
            ease = excluded.ease,
            interval_days = excluded.interval_days,
            streak = excluded.streak,
            lapses = excluded.lapses,
            due_at = excluded.due_at
        """,
        (
            note_id,
            card_index,
            rating,
            moment.replace(microsecond=0).isoformat(),
            schedule.ease,
            schedule.interval_days,
            schedule.streak,
            schedule.lapses,
            schedule.due_at,
        ),
    )
    return {
        "note_id": note_id,
        "card_index": card_index,
        "rating": rating,
        "ease": schedule.ease,
        "interval_days": schedule.interval_days,
        "streak": schedule.streak,
        "lapses": schedule.lapses,
        "due_at": schedule.due_at,
        "interval_label": srs.describe_interval(schedule.interval_days),
    }


def card_review_map(db: Database, note_id: str) -> dict[int, str]:
    """这张卡片笔记里每张卡的最近评价：`{card_index: rating}`。"""
    rows = db.query("SELECT card_index, rating FROM card_reviews WHERE note_id = ?", (note_id,))
    return {int(row["card_index"]): str(row["rating"]) for row in rows}


def due_cards(
    db: Database,
    *,
    now: datetime.datetime | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    """今天该练哪些卡：新卡（从没排过）+ 到期卡（`due_at <= now`）。

    **扫全库解析卡片正文**在这里是可接受的：卡片是本地 Markdown、几十到几百张的量级，
    正则解析比多存一张"卡片明细表"更不容易不一致（正文是唯一真相，明细表会腐化）。
    真要长大到几万张再谈索引，现在不做。
    """
    moment = now or datetime.datetime.now().astimezone()
    rows = db.query(
        "SELECT id, title, content, created_at FROM notes WHERE kind = 'card'"
        " ORDER BY created_at DESC"
    )
    schedule_rows = {
        (str(item["note_id"]), int(item["card_index"])): item
        for item in db.query("SELECT note_id, card_index, due_at FROM card_reviews")
    }
    items: list[dict[str, Any]] = []
    total = 0
    fresh = 0
    due_total = 0
    for row in rows:
        note_id = str(row["id"])
        for index, card in enumerate(parse_cards(str(row["content"] or ""))):
            total += 1
            key = (note_id, index)
            existing = schedule_rows.get(key)
            if existing is None:
                fresh += 1
            if existing is not None and not srs.is_due(
                None if existing["due_at"] is None else str(existing["due_at"]), now=moment
            ):
                continue
            due_total += 1
            if len(items) >= max(0, limit):
                # 计数与返回条数是两件事：`limit` 只该影响"这次发多少张给你练"，
                # 不该影响"到底有多少张到期"。混在一起会让界面上的数字随 limit 变（真踩过）。
                continue
            items.append(
                {
                    "note_id": note_id,
                    "note_title": str(row["title"] or "抽背卡"),
                    "card_index": index,
                    "front": card.get("front", ""),
                    "back": card.get("back", ""),
                    "is_new": existing is None,
                }
            )
    return {
        "items": items,
        "counts": {
            "total_cards": total,
            "due": due_total,
            "new": fresh,
            "reviewed_cards": len(schedule_rows),
        },
        "now": moment.replace(microsecond=0).isoformat(),
    }


def card_stats(db: Database) -> dict[str, Any]:
    """抽背总体情况（界面顶部与笔记页显示）：练过几张、各档多少、最近一次什么时候。"""
    row = db.query_one(
        "SELECT count(*) AS reviewed, coalesce(sum(reviews), 0) AS attempts,"
        " max(reviewed_at) AS last_at FROM card_reviews"
    )
    counts = {rating: 0 for rating in CARD_RATINGS}
    for item in db.query("SELECT rating, count(*) AS n FROM card_reviews GROUP BY rating"):
        if str(item["rating"]) in counts:
            counts[str(item["rating"])] = int(item["n"])
    reviewed = int(row["reviewed"]) if row is not None else 0
    good = counts.get("good", 0)
    timeline = due_cards(db, limit=0)["counts"]
    upcoming = db.query_one(
        "SELECT min(due_at) AS next_due FROM card_reviews WHERE due_at IS NOT NULL AND due_at > ?",
        (datetime.datetime.now().astimezone().replace(microsecond=0).isoformat(),),
    )
    return {
        "reviewed": reviewed,
        "attempts": int(row["attempts"]) if row is not None else 0,
        "by_rating": counts,
        # 记住率分母只算"评过的卡"（没评过的不该被算成不会）
        "recall_rate": (good / reviewed) if reviewed else None,
        "last_at": row["last_at"] if row is not None else None,
        # B6：今天该练多少、其中新卡多少（界面顶部那行"今天该练 N 张"就取这里）
        "due": timeline["due"],
        "due_new": timeline["new"],
        "total_cards": timeline["total_cards"],
        "next_due_at": upcoming["next_due"] if upcoming is not None else None,
    }


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
