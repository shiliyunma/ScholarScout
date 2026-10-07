"""学习者画像（跨会话）的口径测试。

钉三件事：掌握度分档规则、**未练的卡不进分母**、以及"没有原料时不注入空话"。
"""

from __future__ import annotations

import datetime

from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.runtime import learner_profile as lp
from scholarscout.state.db import Database

NOW = datetime.datetime(2026, 10, 7, 20, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
CARD_CONTENT = "## Q1 问题一？\n**A.** 答一。\n\n## Q2 问题二？\n**A.** 答二。\n\n## Q3 问题三？\n**A.** 答三。\n"


def _cards(db: Database, note_id: str = "nt_a", title: str = "图算法 抽背卡") -> str:
    db.execute(
        "INSERT INTO notes (id, session_id, kind, title, content, created_at) VALUES (?,?,?,?,?,?)",
        (note_id, "ses_1", "card", title, CARD_CONTENT, "2026-10-07T00:00:00+08:00"),
    )
    return note_id


def _review(
    db: Database,
    note_id: str,
    index: int,
    *,
    streak: int,
    lapses: int,
    interval: float,
    rating: str = "good",
    at: str = "2026-10-07T10:00:00+08:00",
) -> None:
    db.execute(
        "INSERT INTO card_reviews (note_id, card_index, rating, reviews, reviewed_at, ease,"
        " interval_days, streak, lapses, due_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (note_id, index, rating, 1, at, 2.5, interval, streak, lapses, "2026-10-20T10:00:00+08:00"),
    )


def test_mastery_buckets_are_deterministic(db: Database) -> None:
    note_id = _cards(db)
    _review(db, note_id, 0, streak=3, lapses=0, interval=17.0)  # 熟：连着答对
    _review(db, note_id, 1, streak=1, lapses=0, interval=1.0)  # 复习中
    _review(db, note_id, 2, streak=5, lapses=2, interval=40.0)  # 薄弱：答错过就优先算薄弱
    profile = lp.build_profile(db, now=NOW)
    group = profile["card_groups"][0]
    assert group["cards"] == 3
    assert (group["mastered"], group["reviewing"], group["weak"]) == (1, 1, 1)
    assert group["mastery_rate"] == 1 / 3


def test_unseen_cards_stay_out_of_the_denominator(db: Database) -> None:
    """未练的卡不进分母 —— 把没练过的算成不会，画像必然难看，用户会以为系统在打击他。"""
    note_id = _cards(db)
    _review(db, note_id, 0, streak=2, lapses=0, interval=6.0)
    profile = lp.build_profile(db, now=NOW)
    group = profile["card_groups"][0]
    assert group["cards"] == 3 and group["reviewed"] == 1
    assert group["mastery_rate"] == 1.0
    assert profile["overview"]["cards"] == 3 and profile["overview"]["reviewed"] == 1


def test_long_interval_alone_counts_as_mastered(db: Database) -> None:
    """间隔已排到 6 天以上也算熟（连着答对过才会排那么远）。"""
    note_id = _cards(db)
    _review(db, note_id, 0, streak=1, lapses=0, interval=6.0)
    assert lp.build_profile(db, now=NOW)["card_groups"][0]["mastered"] == 1


def test_weakest_group_is_named(db: Database) -> None:
    ok = _cards(db, "nt_ok", "已经练熟的组")
    bad = _cards(db, "nt_bad", "还很薄的组")
    _review(db, ok, 0, streak=4, lapses=0, interval=30.0)
    _review(db, bad, 1, streak=0, lapses=3, interval=1.0, rating="bad")
    profile = lp.build_profile(db, now=NOW)
    assert profile["weak_groups"] == ["还很薄的组"]
    note = lp.render_profile_note(profile)
    assert "还很薄的组" in note and "别只给结论" in note


def test_topics_come_from_artifacts_only(db: Database) -> None:
    db.execute(
        "INSERT INTO learning_routes (id, session_id, topic, level, weeks, plan_json, version,"
        " created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("rt_1", "ses_1", "GraphRAG", "入门", 4, "{}", 1, "2026-10-07T00:00:00+08:00"),
    )
    db.execute(
        "INSERT INTO topic_maps (id, session_id, topic, graph_json, created_at) VALUES (?,?,?,?,?)",
        ("tm_1", "ses_1", "教育智能体", "{}", "2026-10-07T00:00:00+08:00"),
    )
    profile = lp.build_profile(db, now=NOW)
    assert sorted(profile["covered_topics"]) == ["GraphRAG", "教育智能体"]
    assert {item["topic"] for item in profile["topics"]} == {"GraphRAG", "教育智能体"}


def test_activity_window_is_bounded(db: Database) -> None:
    note_id = _cards(db)
    _review(db, note_id, 0, streak=1, lapses=0, interval=1.0, at=NOW.isoformat())
    _review(db, note_id, 1, streak=1, lapses=0, interval=1.0, at="2026-01-01T10:00:00+08:00")
    profile = lp.build_profile(db, now=NOW)
    assert profile["overview"]["reviews_recent"] == 1
    assert profile["overview"]["active_days_recent"] == 1


def test_empty_library_renders_nothing(db: Database) -> None:
    """没有原料就**不注入**（不然每轮白白多一段空话）。"""
    profile = lp.build_profile(db, now=NOW)
    assert lp.render_profile_note(profile) == ""


def test_profile_rpc(ctx: AppContext) -> None:
    note_id = _cards(ctx.db)
    _review(ctx.db, note_id, 0, streak=2, lapses=0, interval=6.0)
    payload = dispatch(ctx, "learner.profile", {})
    assert payload["overview"]["reviewed"] == 1
    assert payload["card_groups"][0]["mastery_rate"] == 1.0


def test_legacy_review_without_schedule_is_not_called_unseen(db: Database) -> None:
    """B6 之前的复习行只有评价、没有排期字段 —— 不能算成"未练"。

    用户明明练过，画像说他没练过就是造谣（这条是拿真库跑画像时发现的：
    4 张卡里有 1 张练过，却显示"熟 0 / 复习中 0 / 薄弱 0"）。
    """
    note_id = _cards(db)
    db.execute(
        "INSERT INTO card_reviews (note_id, card_index, rating, reviews, reviewed_at)"
        " VALUES (?,?,?,?,?)",
        (note_id, 0, "good", 1, "2026-10-07T10:00:00+08:00"),
    )
    db.execute(
        "INSERT INTO card_reviews (note_id, card_index, rating, reviews, reviewed_at)"
        " VALUES (?,?,?,?,?)",
        (note_id, 1, "bad", 1, "2026-10-07T10:00:00+08:00"),
    )
    profile = lp.build_profile(db, now=NOW)
    group = profile["card_groups"][0]
    assert group["reviewed"] == 2
    assert group["reviewing"] == 1  # 评过 good、但还没有排期信息
    assert group["weak"] == 1  # 评过 bad
    assert group["mastery_rate"] == 0.0  # 一张都没到"熟"，这个 0 是真值
