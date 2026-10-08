"""③ 的契约：排期必须能从追加式日志**重放**出来，且与汇总行一致。

这条测试的价值：它把"排期是怎么来的"从"只存在于代码里的过程"变成**可对账的账本** ——
以后任何一次算错，都能从日志重算出来比对；汇总行与日志不一致就说明有写入漏了日志。
"""

from __future__ import annotations

import datetime

from scholarscout.state import learning
from scholarscout.state.db import Database

T0 = datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))


def _card_note(db: Database, note_id: str = "nt_replay") -> str:
    db.execute(
        "INSERT INTO notes (id, session_id, kind, title, content, created_at) VALUES (?,?,?,?,?,?)",
        (
            note_id,
            "ses_1",
            "card",
            "回放 抽背卡",
            "## Q1 问题一？\n**A.** 答一。\n",
            "2026-10-01T00:00:00+08:00",
        ),
    )
    return note_id


def test_replay_matches_the_summary_row(db: Database) -> None:
    note_id = _card_note(db)
    # 三次评价：会了 → 模糊 → 会了（每次间隔一天，时间可注入，所以结果完全确定）
    for offset, rating in enumerate(("good", "fuzzy", "good")):
        learning.review_card(db, note_id, 0, rating, now=T0 + datetime.timedelta(days=offset))

    summary = db.query_one(
        "SELECT rating, reviews, ease, interval_days, streak, lapses, due_at"
        " FROM card_reviews WHERE note_id = ? AND card_index = 0",
        (note_id,),
    )
    replayed = learning.replay_card(db, note_id, 0)

    assert replayed["logged"] == 3, "三次评价应当留下三条日志"
    assert replayed["rating"] == str(summary["rating"]), "最近评价要对得上"
    assert replayed["ease"] == float(summary["ease"])
    assert replayed["interval_days"] == float(summary["interval_days"])
    assert replayed["streak"] == int(summary["streak"])
    assert replayed["lapses"] == int(summary["lapses"])
    assert replayed["due_at"] == str(summary["due_at"])


def test_replay_reports_when_history_is_missing(db: Database) -> None:
    """迁移之前的老数据没有日志 —— replay 如实报 logged=0，不假装完整。"""
    note_id = _card_note(db, note_id="nt_old")
    db.execute(
        "INSERT INTO card_reviews (note_id, card_index, rating, reviews, reviewed_at, ease,"
        " interval_days, streak, lapses, due_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            note_id,
            0,
            "good",
            5,
            "2026-09-01T09:00:00+08:00",
            2.5,
            6.0,
            2,
            0,
            "2026-09-07T09:00:00+08:00",
        ),
    )
    replayed = learning.replay_card(db, note_id, 0)
    assert replayed["logged"] == 0, "没有日志就是没有日志"
    assert replayed["ease"] is None, "重放不出东西时给空值，不要编一个看着像的数"


def test_log_is_append_only(db: Database) -> None:
    """同一张卡评三次 → 三条日志（不是更新一行）。这是"可回放"的前提。"""
    note_id = _card_note(db, note_id="nt_append")
    for offset, rating in enumerate(("bad", "fuzzy", "good")):
        learning.review_card(db, note_id, 0, rating, now=T0 + datetime.timedelta(days=offset))
    rows = db.query("SELECT rating FROM card_review_log WHERE note_id = ? ORDER BY id", (note_id,))
    assert [str(row["rating"]) for row in rows] == ["bad", "fuzzy", "good"]
