"""学习者画像（跨会话）：把库里已有的学习痕迹汇总成"这个人学到哪了"。

## 为什么值得单独一层

库里早就有原料，只是没有汇总层：抽背排期（`card_reviews` 的 ease/streak/lapses）本身就是
**掌握度**，产物（路线/地图/笔记）说明他学过什么，来源表说明他信哪类资料。
把这些汇总成一段几十 token 的画像注入每轮，产物才能"按你的水平"生成，
而不是对新手和老手说同一套话。

## 三条口径（决定这张画像可不可信）

1. **纯本地汇总，零模型调用**：掌握度是按确定性规则算的，不让模型"感觉"用户水平。
   （唯一可选的模型调用是生成一句"画像描述"，必须用户点按钮才发生，且缓存。）
2. **未练过的卡不进任何分母**：`掌握度 = 熟卡 / 有记录的卡`。把没练过的算成不会，
   画像必然很难看，用户会以为系统在打击他（与 `card_stats.recall_rate` 同口径）。
3. **卡片不挂主题，就用卡片组标题**：卡片笔记没有 topic 字段，硬凑主题会编；
   所以主题维度只取产物（路线/地图）的 topic，卡片单独按"卡片组"列，并标明两种维度。

掌握度的四档（确定性，写在注释里便于复核）：

| 档 | 条件 |
|---|---|
| 熟 | `streak >= 2` 或 `interval_days >= 6`（连着答对过，或已经排到一周后） |
| 复习中 | `streak == 1` |
| 薄弱 | `lapses >= 1`（答错过，无论现在排到多久） |
| 未练 | 没有复习记录 |
"""

from __future__ import annotations

import datetime
import json
from typing import Any

from scholarscout.learning import mastery
from scholarscout.state import learner_profile as profile_state
from scholarscout.state.db import Database
from scholarscout.state.learning import parse_cards

#: 薄弱/活跃统计的时间窗（天）
ACTIVITY_WINDOW_DAYS = 14
#: 熟练阈值：连续答对 2 次，或间隔已排到 6 天及以上
MASTERED_STREAK = 2
MASTERED_INTERVAL = 6.0


def _bucket(rating: str | None, streak: int, lapses: int, interval: float | None) -> str:
    """掌握度分档（确定性；口径写在模块顶部，便于复核）。

    **老数据要单独兜一下**：B6 之前的复习记录只有评价、没有排期字段（streak/lapses 都是 0、
    interval 为空）。那种行不能算"未练" —— 用户明明练过，画像说他没练过就是造谣。
    这种行按评价落档：`bad` → 薄弱，其余 → 复习中。
    """
    if lapses >= 1 or rating == "bad":
        return "薄弱"
    if streak >= MASTERED_STREAK or float(interval or 0) >= MASTERED_INTERVAL:
        return "熟"
    if streak >= 1:
        return "复习中"
    if rating:
        return "复习中"  # 有评价无排期：至少不是"未练"
    return "未练"


def _tags_of(raw: object) -> list[str]:
    """笔记 tags_json → 字符串列表。解析不了就当没有标签（不猜）。"""
    try:
        parsed = json.loads(str(raw or "[]"))
    except (ValueError, TypeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _card_rows(db: Database, *, now: datetime.datetime | None = None) -> list[dict[str, Any]]:
    """每张卡一行：卡片组（笔记标题）+ 它的排期状态。"""
    moment = now or datetime.datetime.now().astimezone()
    notes = profile_state.card_group_notes(db)
    reviews = {
        (str(row["note_id"]), int(row["card_index"])): row
        for row in profile_state.review_schedule(db)
    }
    rows: list[dict[str, Any]] = []
    for note in notes:
        note_id = str(note["id"])
        title = str(note["title"] or "未命名卡片组")
        knowledge_type = mastery.type_from_tags(_tags_of(note["tags_json"]))
        for index, _card in enumerate(parse_cards(str(note["content"] or ""))):
            review = reviews.get((note_id, index))
            rows.append(
                {
                    "note_id": note_id,
                    "group": title,
                    "card_index": index,
                    "reviewed": review is not None,
                    "rating": str(review["rating"]) if review else None,
                    "streak": int(review["streak"] or 0) if review else 0,
                    "lapses": int(review["lapses"] or 0) if review else 0,
                    "interval_days": (
                        float(review["interval_days"])
                        if review and review["interval_days"]
                        else None
                    ),
                    "reviewed_at": str(review["reviewed_at"]) if review else None,
                    # ① 类型化掌握度：由证据按确定性规则算（见 learning/mastery.py）
                    "knowledge_type": knowledge_type,
                    "level": (
                        mastery.mastery_level(
                            rating=str(review["rating"]) if review else None,
                            reviews=int(review["reviews"] or 0) if review else 0,
                            age_days=_age_days(review, moment),
                            lapses=int(review["lapses"] or 0) if review else 0,
                        )
                        if review
                        else 0.0
                    ),
                    "bucket": (
                        _bucket(
                            str(review["rating"]) if review else None,
                            int(review["streak"] or 0) if review else 0,
                            int(review["lapses"] or 0) if review else 0,
                            float(review["interval_days"])
                            if review and review["interval_days"]
                            else None,
                        )
                        if review
                        else "未练"
                    ),
                }
            )
    return rows


def _age_days(review: dict[str, Any] | None, now: datetime.datetime) -> float:
    """距上次复习多少天。时间戳解析不了当 0 天 —— 宁可不打折，也不凭空说人忘了。"""
    if not review or not review.get("reviewed_at"):
        return 0.0
    try:
        moment = datetime.datetime.fromisoformat(str(review["reviewed_at"]))
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.UTC)
    return max(0.0, (now - moment).total_seconds() / 86400.0)


def _mastery_of(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """一组卡片的掌握度。**未练的不进分母**。"""
    reviewed = [row for row in rows if row["reviewed"]]
    counts: dict[str, int] = {"熟": 0, "复习中": 0, "薄弱": 0}
    for row in reviewed:
        counts[row["bucket"]] = counts.get(row["bucket"], 0) + 1
    return {
        "cards": len(rows),
        "reviewed": len(reviewed),
        "mastered": counts["熟"],
        "reviewing": counts["复习中"],
        "weak": counts["薄弱"],
        "mastery_rate": (counts["熟"] / len(reviewed)) if reviewed else None,
        # ---- ① 类型化掌握度（mastery.mastery_level 的口径，纯函数、可复算）----
        "knowledge_type": (reviewed[0]["knowledge_type"] if reviewed else None)
        or (rows[0]["knowledge_type"] if rows else None)
        or "concept",
        "level": (
            round(sum(float(row["level"]) for row in reviewed) / len(reviewed), 4)
            if reviewed
            else 0.0
        ),
        "above_gate": sum(
            1 for row in reviewed if mastery.is_mastered(float(row["level"]), row["knowledge_type"])
        ),
        "gate": mastery.gate_threshold(
            (reviewed[0]["knowledge_type"] if reviewed else None) or "concept"
        ),
    }


def build_profile(db: Database, *, now: datetime.datetime | None = None) -> dict[str, Any]:
    """汇总出画像（只读、零模型调用）。"""
    moment = now or datetime.datetime.now().astimezone()
    since = (
        (moment - datetime.timedelta(days=ACTIVITY_WINDOW_DAYS)).replace(microsecond=0).isoformat()
    )

    cards = _card_rows(db, now=moment)
    overview = _mastery_of(cards)

    routes = profile_state.route_facts(db)
    maps = profile_state.map_facts(db)
    note_counts = profile_state.note_counts_by_kind(db)

    # 主题维度只取产物的 topic —— 卡片没有 topic 字段，硬凑会编
    topics: dict[str, dict[str, Any]] = {}
    for row in routes:
        item = topics.setdefault(
            str(row["topic"] or "未命名"),
            {"topic": str(row["topic"] or "未命名"), "routes": 0, "maps": 0, "weeks": 0},
        )
        item["routes"] += 1
        item["weeks"] = max(item["weeks"], int(row["weeks"] or 0))
    for row in maps:
        item = topics.setdefault(
            str(row["topic"] or "未命名"),
            {"topic": str(row["topic"] or "未命名"), "routes": 0, "maps": 0, "weeks": 0},
        )
        item["maps"] += 1

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in cards:
        groups.setdefault(row["group"], []).append(row)
    card_groups = [
        {
            "group": name,
            **_mastery_of(rows),
            "last_at": max((r["reviewed_at"] or "" for r in rows), default=""),
            # 门槛即游标（①）：这一组里**还没过门槛**的卡，按卡片顺序 ——
            # next_objective 扫第一个，就是要提醒用户先练的那张。
            "pending": [
                {
                    "id": f"{row['note_id']}#{row['card_index']}",
                    "title": f"第 {row['card_index'] + 1} 张",
                    "type": row["knowledge_type"],
                    "level": row["level"],
                }
                for row in rows
                if not mastery.is_mastered(float(row["level"]), row["knowledge_type"])
            ],
        }
        for name, rows in groups.items()
    ]
    # 门槛即游标（①）：先算每个组的"下一个该学"，谁没过门槛谁排前面
    for item in card_groups:
        item["next"] = mastery.next_objective(item.get("pending", []))
    card_groups.sort(
        key=lambda item: (
            0 if item.get("next") else 1,
            -(item["weak"]),
            -(item["cards"] or 0),
        )
    )

    activity = profile_state.review_activity(db, since)
    sources = profile_state.source_type_counts(db)

    weak_topics = [item["group"] for item in card_groups if item["weak"] > 0][:3]
    covered = [str(item["topic"]) for item in topics.values()]
    return {
        "generated_at": moment.replace(microsecond=0).isoformat(),
        "overview": {
            **overview,
            "topics": len(topics),
            "routes": len(routes),
            "maps": len(maps),
            "notes": note_counts,
            "reviews_recent": int(activity["reviews"]) if activity else 0,
            "active_days_recent": int(activity["days"]) if activity else 0,
            "window_days": ACTIVITY_WINDOW_DAYS,
        },
        "topics": sorted(topics.values(), key=lambda item: item["topic"]),
        "card_groups": card_groups,
        "covered_topics": covered,
        "weak_groups": weak_topics,
        "source_types": sources,
    }


def render_profile_note(profile: dict[str, Any]) -> str:
    """画像注入用的两三行（省 token，只放"能改变行为"的信息）。"""
    overview = profile.get("overview") or {}
    if not (overview.get("cards") or overview.get("topics")):
        return ""
    lines = ["【你正在帮的这个人】（跨会话画像，供调整深度与措辞）"]
    bits = []
    if overview.get("topics"):
        bits.append(
            f"做过 {overview['topics']} 个主题（路线 {overview.get('routes', 0)} 条、地图 {overview.get('maps', 0)} 份）"
        )
    if overview.get("reviewed"):
        mastery = overview.get("mastery_rate")
        bits.append(
            f"抽背练过 {overview['reviewed']} 张、熟 {overview.get('mastered', 0)} 张"
            + (f"（{round(float(mastery) * 100)}%）" if mastery is not None else "")
        )
    if overview.get("active_days_recent"):
        bits.append(
            f"近 {overview.get('window_days', 14)} 天有 {overview['active_days_recent']} 天在学"
        )
    if bits:
        lines.append("；".join(bits) + "。")
    weak = profile.get("weak_groups") or []
    if weak:
        lines.append("最薄的几块（讲这些时多给例子、别只给结论）：" + "、".join(weak[:3]) + "。")
    covered = profile.get("covered_topics") or []
    if covered:
        lines.append("已经做过主题：" + "、".join(covered[:6]) + "（不要从零讲起）。")
    return "\n".join(lines)
