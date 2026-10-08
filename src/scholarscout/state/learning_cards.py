"""笔记、抽背卡与复习的状态读写（从 state/learning.py 按域拆出，逻辑一字未改）。

对外入口仍是 ``scholarscout.state.learning``（那里做再导出），本模块是其实现。
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
    # ③ 追加式证据日志：汇总行是为了快，日志是为了**可回放**（排期随时能重算对账）
    db.execute(
        "INSERT INTO card_review_log (note_id, card_index, rating, reviewed_at)"
        " VALUES (?, ?, ?, ?)",
        (note_id, card_index, rating, moment.replace(microsecond=0).isoformat()),
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


def reveal_card(db: Database, note_id: str, card_index: int) -> dict[str, Any]:
    """取某一张卡的答案（②）。抽背页点「看答案」时才调它 —— 答案不随题下发。

    越界/不存在的卡返回空 back，不抛错：界面据此显示"这张卡不见了"，而不是整页崩掉。
    """
    note = get_note(db, note_id)
    if note is None or str(note.get("kind") or "") != "card":
        return {"note_id": note_id, "card_index": int(card_index), "back": ""}
    cards = parse_cards(str(note.get("content") or ""))
    card = cards[card_index] if 0 <= int(card_index) < len(cards) else {}
    return {
        "note_id": note_id,
        "card_index": int(card_index),
        "note_title": str(note.get("title") or ""),
        "front": str(card.get("front") or ""),
        "back": str(card.get("back") or ""),
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
                    # ② 只发题目：**答案不随题下发**（原样下发等于把谜底交给考生）
                    "front": card.get("front", ""),
                    "card_id": f"{note_id}#{index}",
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


def replay_card(db: Database, note_id: str, card_index: int) -> dict[str, Any]:
    """从追加式日志**重放**这张卡的排期（③）。

    口径与 `review_card` 完全一致（同一个 srs.review 链），所以重算结果必须等于汇总行 ——
    `tests/state/test_card_review_replay.py` 就是钉这一条：**重算 == 现状**。
    汇总行是"为了快"，日志是"为了可审计"；两者不一致就说明某次写入漏了日志。

    `logged` 只数日志条数：迁移之前的老数据没有日志，logged < reviews 是如实暴露，不是错。
    """
    rows = db.query(
        "SELECT rating, reviewed_at FROM card_review_log"
        " WHERE note_id = ? AND card_index = ? ORDER BY id",
        (note_id, card_index),
    )
    ease: float | None = None
    interval_days: float | None = None
    streak, lapses = 0, 0
    due_at: str | None = None
    rating: str | None = None
    for row in rows:
        rating = str(row["rating"])
        schedule = srs.review(
            rating=rating,
            ease=ease,
            interval_days=interval_days,
            streak=streak,
            lapses=lapses,
            now=datetime.datetime.fromisoformat(str(row["reviewed_at"])),
        )
        ease = schedule.ease
        interval_days = schedule.interval_days
        streak = schedule.streak
        lapses = schedule.lapses
        due_at = schedule.due_at
    return {
        "note_id": note_id,
        "card_index": int(card_index),
        "logged": len(rows),
        "rating": rating,
        "ease": ease,
        "interval_days": interval_days,
        "streak": streak,
        "lapses": lapses,
        "due_at": due_at,
    }
