"""类型化掌握度 +「门槛即游标」—— 纯函数、零模型调用。

借鉴 DeepTutor 的核心做法：掌握度不该是「模型说会了没有」，而应由**证据**按确定性规则算出来。
这里钉三件事：

1. 掌握度 ∈ [0,1] = 近因加权正确率 × 置信度封顶（复习次数少就不敢给高分）× 遗忘惩罚；
2. 门槛**按知识点类型**给（记忆类 0.80 / 概念类 0.70 / 技能类 0.65），不是一刀切；
3. 推进 = 扫第一个没到门槛的知识点（**门槛即游标**），不用 stage 计数器 ——
   已经会了的可以跳级压缩，不必按部就班。

口径只在这里定义：任何调用方都不许自己拍一个阈值或权重，要调就改这一张表。
好处是可复算：同样的证据必然算出同一个数，界面显示、画像汇总、排期都用同一套。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

#: 知识点类型。约定写在笔记的 tags 里（notes.tags_json），认不出来按概念类。
KNOWLEDGE_TYPES = ("memory", "concept", "procedure")
DEFAULT_TYPE = "concept"

#: 各类型的门槛：记忆类要求最稳（要背下来），技能类允许带着手册用
GATE_THRESHOLD: dict[str, float] = {"memory": 0.80, "concept": 0.70, "procedure": 0.65}

#: 近因半衰期（天）：两周前的表现只算一半权重
HALF_LIFE_DAYS = 14.0

#: 复习到这么多次才算「有把握」；不到就按比例打折
CONFIDENT_REVIEWS = 3.0

#: 评价 → 质量分。good「会了」/ fuzzy「模糊」/ bad「不会」，与 card_reviews 的 CHECK 一致
RATING_QUALITY: dict[str, float] = {"good": 1.0, "fuzzy": 0.55, "bad": 0.0}

#: 每遗忘一次（评了 bad）的惩罚系数：lapses 次惩罚 = 1/(1+0.25×lapses)
LAPSE_PENALTY_STEP = 0.25


def normalize_type(raw: object) -> str:
    """把任意字符串收敛到三类之一；认不出来按概念类（宁可宽，不要把人卡死）。"""
    text = str(raw or "").strip().lower()
    return text if text in KNOWLEDGE_TYPES else DEFAULT_TYPE


def type_from_tags(tags: Iterable[object] | None) -> str:
    """从笔记 tags 里认知识点类型：出现 memory / concept / procedure 即生效，否则概念类。"""
    for tag in tags or ():
        text = str(tag).strip().lower()
        if text in KNOWLEDGE_TYPES:
            return text
    return DEFAULT_TYPE


def quality(rating: object) -> float:
    """评价 → 质量分。认不出来的评价按 0 算 —— 不猜。"""
    return RATING_QUALITY.get(str(rating or "").strip().lower(), 0.0)


def recency_weight(age_days: float) -> float:
    """近因权重：越久以前的表现越不值钱（半衰期两周）。"""
    return 0.5 ** (max(0.0, float(age_days)) / HALF_LIFE_DAYS)


def mastery_level(
    *,
    rating: object,
    reviews: int | None,
    age_days: float,
    lapses: int | None = 0,
) -> float:
    """一张卡的掌握度 ∈ [0,1]。

    三个乘子：正确率（含近因）、置信度（复习次数）、遗忘惩罚。
    乘起来而不是加起来，是为了让"只练过一次就说掌握"这种事不可能发生。
    """
    base = quality(rating) * recency_weight(age_days)
    confidence = min(1.0, max(0.0, float(reviews or 0)) / CONFIDENT_REVIEWS)
    penalty = 1.0 / (1.0 + LAPSE_PENALTY_STEP * max(0.0, float(lapses or 0)))
    return round(min(1.0, base * confidence * penalty), 4)


def gate_threshold(knowledge_type: object) -> float:
    return GATE_THRESHOLD[normalize_type(knowledge_type)]


def is_mastered(level: float, knowledge_type: object) -> bool:
    """过没过门槛。注意是 >=：正好等于门槛算过。"""
    return float(level) >= gate_threshold(knowledge_type)


def next_objective(items: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """门槛即游标：按给定顺序扫，返回**第一个没达门槛**的知识点；全达标返回 None。

    items 每项需含：``id``、``level``（掌握度）、可选 ``type`` 与 ``title``。
    为什么不用 stage 计数器：计数器一旦落后于实际掌握情况，用户就会被按着重复学
    已经会了的东西；用"扫第一个未达标"天然支持跳级。
    """
    for item in items:
        if not is_mastered(float(item.get("level") or 0.0), item.get("type")):
            return dict(item)
    return None


def summarize(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """按类型汇总掌握情况（个人中心 / 研究者都能用同一份口径）。"""
    groups: dict[str, dict[str, Any]] = {}
    for item in rows:
        kind = normalize_type(item.get("type"))
        bucket = groups.setdefault(
            kind,
            {"type": kind, "threshold": gate_threshold(kind), "total": 0, "mastered": 0},
        )
        bucket["total"] += 1
        if is_mastered(float(item.get("level") or 0.0), kind):
            bucket["mastered"] += 1
    for bucket in groups.values():
        total = bucket["total"] or 1
        bucket["mastery_rate"] = round(bucket["mastered"] / total, 3)
    return {"groups": sorted(groups.values(), key=lambda x: x["type"])}
