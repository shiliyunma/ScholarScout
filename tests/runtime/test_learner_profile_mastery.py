"""① 的接线测试：画像里必须出现**类型化**掌握度与门槛游标。

期望值手算（与 learning/mastery.py 的口径一致）：
- 只评过 1 次（fixture 里 reviews=1）、当天评的 good → 1.0 × (1/3) = 0.3333；
- 未评过的卡 → 0.0；
- 因此这一组的卡全都在门槛之下，pending 里应当有三张，next 是第一张。
"""

from __future__ import annotations

import datetime

from scholarscout.runtime import learner_profile as lp
from scholarscout.state.db import Database

NOW = datetime.datetime(2026, 10, 7, 20, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
CARD_CONTENT = "## Q1 问题一？\n**A.** 答一。\n\n## Q2 问题二？\n**A.** 答二。\n"


def _cards(db: Database, *, note_id: str, tags_json: str = "[]") -> str:
    db.execute(
        "INSERT INTO notes (id, session_id, kind, title, content, tags_json, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (
            note_id,
            "ses_1",
            "card",
            "记忆类 抽背卡",
            CARD_CONTENT,
            tags_json,
            "2026-10-07T00:00:00+08:00",
        ),
    )
    return note_id


def _review(db: Database, note_id: str, index: int, *, rating: str = "good") -> None:
    db.execute(
        "INSERT INTO card_reviews (note_id, card_index, rating, reviews, reviewed_at, ease,"
        " interval_days, streak, lapses, due_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            note_id,
            index,
            rating,
            1,
            "2026-10-07T20:00:00+08:00",
            2.5,
            1.0,
            0,
            0,
            "2026-10-20T10:00:00+08:00",
        ),
    )


def test_profile_carries_typed_mastery(db: Database) -> None:
    note_id = _cards(db, note_id="nt_mem", tags_json='["memory", "rag"]')
    _review(db, note_id, 0)

    profile = lp.build_profile(db, now=NOW)
    group = profile["card_groups"][0]

    assert group["knowledge_type"] == "memory", "tags 里的 memory 应当被认出来"
    assert group["gate"] == 0.80, "记忆类的门槛应当是 0.80（不是一刀切）"
    # 只评过一次：1.0 × (1/3) = 0.3333
    assert group["level"] == 0.3333
    assert group["above_gate"] == 0, "0.3333 远低于 0.80，不该算达标"


def test_pending_lists_cards_below_the_gate_in_order(db: Database) -> None:
    """门槛即游标：pending 里是没过门槛的卡，next 取第一张。"""
    note_id = _cards(db, note_id="nt_mem", tags_json='["memory"]')
    _review(db, note_id, 0)

    group = lp.build_profile(db, now=NOW)["card_groups"][0]
    pending = group["pending"]
    assert [item["title"] for item in pending] == ["第 1 张", "第 2 张"]
    assert group["next"]["title"] == "第 1 张"
    assert group["next"]["type"] == "memory"


def test_untyped_cards_fall_back_to_concept(db: Database) -> None:
    """认不出来按概念类（门槛 0.70），类型不写死也要能用。"""
    _cards(db, note_id="nt_plain", tags_json="[]")
    group = lp.build_profile(db, now=NOW)["card_groups"][0]
    assert group["knowledge_type"] == "concept"
    assert group["gate"] == 0.70
