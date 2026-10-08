"""掌握度口径的测试：期望值**手算**，不是从实现里抄。

钉四件事：
- 近因加权（半衰期两周）、置信度封顶（复习次数少不敢给高分）、遗忘惩罚（评过 bad 要打折）；
- 门槛按类型不同，且是 `>=`（正好等于门槛算过）；
- 「门槛即游标」= 扫第一个未达标的，**会了的能跳级**；
- 认不出来的输入不猜：类型退化为概念类，评价认不出按 0 分。
"""

from __future__ import annotations

import pytest

from scholarscout.learning import mastery


def test_fresh_good_review_is_fully_confident() -> None:
    """今天评了会了、且已练满 3 次 → 1.0×1.0×1.0 = 1.0。"""
    assert mastery.mastery_level(rating="good", reviews=3, age_days=0) == 1.0


def test_single_review_is_discounted_by_confidence() -> None:
    """只练过一次不能算掌握：1.0 × (1/3) = 0.3333。"""
    assert mastery.mastery_level(rating="good", reviews=1, age_days=0) == 0.3333


def test_recency_halves_after_half_life() -> None:
    """两周前的"模糊"：0.55 × 0.5 = 0.275（再乘置信度 1.0）。"""
    assert mastery.mastery_level(rating="fuzzy", reviews=3, age_days=14) == 0.275


def test_bad_review_clears_mastery() -> None:
    assert mastery.mastery_level(rating="bad", reviews=9, age_days=0, lapses=0) == 0.0


def test_lapses_discount_proportionally() -> None:
    """忘过 4 次：惩罚 1/(1+0.25×4) = 0.5 → 1.0 × 1.0 × 0.5 = 0.5。"""
    assert mastery.mastery_level(rating="good", reviews=3, age_days=0, lapses=4) == 0.5


def test_unknown_rating_scores_zero() -> None:
    """认不出来的评价按 0 分 —— 不猜（拼错 rating 不该被当成"会了"）。"""
    assert mastery.quality("excellent") == 0.0
    assert mastery.mastery_level(rating="excellent", reviews=9, age_days=0) == 0.0


def test_type_defaults_to_concept_when_unknown() -> None:
    assert mastery.normalize_type("") == "concept"
    assert mastery.normalize_type("MEMORY") == "memory"
    assert mastery.type_from_tags(["rag", "Memory", "x"]) == "memory"
    assert mastery.type_from_tags(["rag", "x"]) == "concept"
    assert mastery.type_from_tags(None) == "concept"


@pytest.mark.parametrize(
    ("kind", "threshold"),
    [("memory", 0.80), ("concept", 0.70), ("procedure", 0.65)],
)
def test_thresholds_are_type_specific(kind: str, threshold: float) -> None:
    assert mastery.gate_threshold(kind) == threshold
    assert mastery.is_mastered(threshold, kind) is True  # >= 门槛算过
    assert mastery.is_mastered(threshold - 0.001, kind) is False


def test_threshold_is_a_cursor_that_allows_skipping() -> None:
    """门槛即游标：第一个没达标的才是"下一个该学"的，前面会了的直接跳过。"""
    items = [
        {"id": "a", "type": "memory", "level": 0.9, "title": "A"},
        {"id": "b", "type": "memory", "level": 0.5, "title": "B"},
        {"id": "c", "type": "memory", "level": 0.0, "title": "C"},
    ]
    assert mastery.next_objective(items)["id"] == "b"
    # A 达标、B 也达标 → 游标直接跳到 C（跳级，不用按部就班）
    items[1]["level"] = 0.85
    assert mastery.next_objective(items)["id"] == "c"
    # 全部达标 → 没有下一个目标
    items[2]["level"] = 0.95
    assert mastery.next_objective(items) is None


def test_summarize_counts_by_type_with_its_own_threshold() -> None:
    rows = [
        {"type": "memory", "level": 0.75},  # 记忆门槛 0.80 → 没过
        {"type": "memory", "level": 0.85},  # 过
        {"type": "concept", "level": 0.75},  # 概念门槛 0.70 → 过
    ]
    summary = {group["type"]: group for group in mastery.summarize(rows)["groups"]}
    assert summary["memory"]["total"] == 2 and summary["memory"]["mastered"] == 1
    assert summary["memory"]["mastery_rate"] == 0.5
    assert summary["concept"]["mastered"] == 1
    assert summary["memory"]["threshold"] == 0.80
