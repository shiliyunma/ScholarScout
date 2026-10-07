"""SM-2 的数值口径（B6）。

**这些期望值是按原论文公式手算出来的**，不是"跑一遍看输出再抄回来" —— 抄回来的测试
什么都钉不住。三档映射：good=5 / fuzzy=3 / bad=1；EF 下限 1.3；q<3 视为没记住。
出处：SuperMemo SM-2，<https://super-memory.com/english/ol/sm2.htm>（见 docs/14 §2）。
"""

from __future__ import annotations

import datetime

import pytest

from scholarscout.learning import srs

NOW = datetime.datetime(2026, 10, 7, 20, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))


def _good(state: dict) -> dict:
    result = srs.review(rating="good", now=NOW, **state)
    return {
        "ease": result.ease,
        "interval_days": result.interval_days,
        "streak": result.streak,
        "lapses": result.lapses,
    }


def test_consecutive_good_follows_sm2_intervals() -> None:
    """连续答对：1 天 → 6 天 → round(6×EF) → round(上次×EF)，EF 每次 +0.1。"""
    state: dict = {}
    expected = [
        (2.6, 1.0, 1),
        (2.7, 6.0, 2),
        (2.8, 17.0, 3),  # round(6 * 2.8) = round(16.8) = 17
        (2.9, 49.0, 4),  # round(17 * 2.9) = round(49.3) = 49
    ]
    for ease, interval, streak in expected:
        state = _good(state)
        assert state == {"ease": ease, "interval_days": interval, "streak": streak, "lapses": 0}


def test_fuzzy_passes_but_lowers_ease() -> None:
    """模糊算通过（间隔继续涨），但 EF 每次降 0.14 —— 慢慢筛出"总也想不干脆"的卡。"""
    state = _good(_good({}))
    assert state["interval_days"] == 6.0 and state["ease"] == 2.7
    result = srs.review(rating="fuzzy", now=NOW, **state)
    assert result.ease == pytest.approx(2.56)  # 2.7 - 0.14
    assert result.interval_days == 15.0  # round(6 * 2.56) = round(15.36)
    assert result.streak == 3


def test_failure_resets_streak_and_comes_back_tomorrow() -> None:
    state = {**_good(_good({})), "lapses": 0}
    result = srs.review(rating="bad", now=NOW, **state)
    assert result.streak == 0
    assert result.interval_days == 1.0
    assert result.lapses == 1
    assert result.ease == pytest.approx(2.16)  # 2.7 - 0.54（q=1）
    assert result.due_at.startswith("2026-10-08")


def test_ease_never_drops_below_floor() -> None:
    """EF 下限 1.3：连错也不会让卡变成"每天都练"（那谁也坚持不下来）。"""
    ease = 2.5
    for _ in range(10):
        ease = srs.next_ease(ease, srs.QUALITY["bad"])
    assert ease == srs.MIN_EASE


def test_quality_mapping_is_the_documented_one() -> None:
    assert srs.QUALITY == {"good": 5, "fuzzy": 3, "bad": 1}
    assert srs.PASS_QUALITY == 3  # fuzzy 恰好落在"通过"上，这是有意的


def test_bad_rating_rejected() -> None:
    with pytest.raises(ValueError):
        srs.review(rating="meh", now=NOW)


def test_describe_interval_is_human_readable() -> None:
    assert srs.describe_interval(1.0) == "1 天后"
    assert srs.describe_interval(6.0) == "6 天后"
    assert srs.describe_interval(8.0) == "约 1 周后"
    assert srs.describe_interval(49.0) == "约 7 周后"
    assert srs.describe_interval(100.0) == "约 3 个月后"
    assert srs.describe_interval(None) == ""


def test_is_due_treats_missing_schedule_as_due() -> None:
    """没有排期 = 到期。

    宁可多练一次，也不要让一张没有排期的卡永远沉在库里（老数据、导入的卡都会走到这里）。
    """
    assert srs.is_due(None, now=NOW) is True
    assert srs.is_due("不是时间", now=NOW) is True
    assert srs.is_due("2026-10-07T19:59:00+08:00", now=NOW) is True
    assert srs.is_due("2026-10-08T20:00:00+08:00", now=NOW) is False


def test_naive_due_time_is_compared_in_local_zone() -> None:
    """库里存的时间可能没有时区（老数据）—— 按本地时区比，不按 UTC 比。"""
    assert srs.is_due("2026-10-07T19:00:00", now=NOW) is True
    assert srs.is_due("2026-10-08T09:00:00", now=NOW) is False
