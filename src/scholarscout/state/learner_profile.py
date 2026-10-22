"""学习者画像要读的「跨表原料」：笔记 / 复习排期 / 路线 / 地图 / 来源。

为什么单独一个模块：画像一次要横跨 ``notes`` / ``card_reviews`` / ``learning_routes`` /
``topic_maps`` / ``sources`` 五张表，任何单张表的状态模块都装不下；而「取哪些列、按什么排序、
截多少条」是 SQL 细节，不该留在 ``runtime/learner_profile.py`` —— 那里只做分档、汇总与文案。

入参是领域值（时间下界、条数），返回行或计数，不暴露 ``sqlite3.Connection``。
分档规则（熟/复习中/薄弱/未练）与「未练的卡不进分母」都在 runtime 侧，这里只取原料。
"""

from __future__ import annotations

from typing import Any

from scholarscout.state.db import Database


def card_group_notes(db: Database) -> list[dict[str, Any]]:
    """卡片组原料：``kind='card'`` 的笔记（卡片明细在正文里，库里只存正文）。

    不按标题/时间排序 —— 画像的分组顺序由 runtime 侧按掌握度排，这里给的是「有哪些组」。
    """
    return [
        dict(row)
        for row in db.query("SELECT id, title, content, tags_json FROM notes WHERE kind = 'card'")
    ]


def review_schedule(db: Database) -> list[dict[str, Any]]:
    """全部复习排期（每张卡一行）：分档要的 rating/reviews/streak/lapses/间隔/时间都在这一行。"""
    return [
        dict(row)
        for row in db.query(
            "SELECT note_id, card_index, rating, reviews, streak, lapses, interval_days,"
            " reviewed_at, due_at FROM card_reviews"
        )
    ]


def route_facts(db: Database) -> list[dict[str, Any]]:
    """学习路线的轻量事实（topic / 周数 / 建立时间）：主题维度只认产物的 topic。"""
    return [dict(row) for row in db.query("SELECT topic, weeks, created_at FROM learning_routes")]


def map_facts(db: Database) -> list[dict[str, Any]]:
    """主题地图的轻量事实（topic / 建立时间）：与 ``route_facts`` 一起构成「做过哪些主题」。"""
    return [dict(row) for row in db.query("SELECT topic, created_at FROM topic_maps")]


def note_counts_by_kind(db: Database) -> dict[str, int]:
    """按 ``kind`` 数笔记（画像里那行「笔记 N 条」）：返回 ``{kind: 条数}``，没记录的 kind 不出现。"""
    return {
        str(row["kind"]): int(row["n"])
        for row in db.query("SELECT kind, count(*) AS n FROM notes GROUP BY kind")
    }


def review_activity(db: Database, since: str) -> dict[str, int]:
    """``since`` 之后的复习次数与「有复习的天数」（按 ``reviewed_at`` 的日期段去重）。

    ``since`` 是 ISO 时间串（由调用方按窗口算好）—— 窗口多长是领域判断，不在状态层。
    """
    row = db.query_one(
        "SELECT count(*) AS reviews, count(DISTINCT substr(reviewed_at, 1, 10)) AS days"
        " FROM card_reviews WHERE reviewed_at >= ?",
        (since,),
    )
    return {
        "reviews": int(row["reviews"]) if row else 0,
        "days": int(row["days"]) if row else 0,
    }


def source_type_counts(db: Database, *, limit: int = 4) -> list[dict[str, Any]]:
    """来源类型分布，**多的在前**（画像只说「你信哪类资料」的前几名）。"""
    return [
        dict(row)
        for row in db.query(
            "SELECT source_type, count(*) AS n FROM sources GROUP BY source_type"
            " ORDER BY n DESC LIMIT ?",
            (int(limit),),
        )
    ]
