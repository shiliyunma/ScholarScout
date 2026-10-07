"""SM-2 间隔重复：把 Anki 那套算法缩到界面上的三档按钮。

## 出处与取舍

算法来自 SuperMemo 的 SM-2（Piotr Wozniak, 1987，<https://super-memory.com/english/ol/sm2.htm>），
`docs/14 §2` 里登记的就是这一条。FSRS 更新（Anki 当前默认），但它的参数要用大量复习记录拟合，
本机没有那种数据量，硬上等于编参数 —— 所以**用 SM-2，参数少、可解释、能逐条对上出处**。

## 三档怎么映射到 SM-2 的 0–5 分

界面只有三档，直接对应 SM-2 的**通过/不通过**分界（q<3 视为没记住）：

| 界面 | q | 含义 |
|---|---|---|
| `good` | 5 | 会了，顺利答出 |
| `fuzzy` | 3 | 模糊，勉强想起（**算通过**，但 EF 会降） |
| `bad` | 1 | 不会 |

## 两条口径

1. **EF 每次评都更新**（含没答对的），但下限 1.3 —— 这是原论文的写法：
   `EF' = EF + (0.1 - (5-q) * (0.08 + (5-q) * 0.02))`，然后 `EF := max(1.3, EF')`。
   扣分只影响"以后间隔涨得多慢"，不会让卡永远排不上。
2. **没答对 → 连续成功次数清零、间隔回到 1 天**（明天再来），并记一次 `lapses`。
   答对的话：第 1 次成功 1 天、第 2 次 6 天、之后 `round(上次间隔 × EF)`。

时间**一律靠传入的 `now`**，不在函数里读钟 —— 否则测试没法钉住口径。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

#: 三档 → SM-2 的 q 值
QUALITY: dict[str, int] = {"good": 5, "fuzzy": 3, "bad": 1}
#: EF 下限（原论文：1.3 以下会让复习过于频繁，反而难坚持）
MIN_EASE = 1.3
DEFAULT_EASE = 2.5
#: 连续第 1/2 次成功后的固定间隔（天）
FIRST_INTERVAL = 1.0
SECOND_INTERVAL = 6.0
#: 通过分界：q < PASS_QUALITY 视为没记住
PASS_QUALITY = 3


@dataclass(frozen=True)
class Schedule:
    """一张卡复习之后的排期。字段名与 DB 列一一对应，避免两处各自起名。"""

    ease: float
    interval_days: float
    streak: int
    lapses: int
    due_at: str


def next_ease(ease: float, quality: int) -> float:
    updated = ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    return max(MIN_EASE, round(updated, 4))


def review(
    *,
    rating: str,
    ease: float | None = None,
    interval_days: float | None = None,
    streak: int = 0,
    lapses: int = 0,
    now: datetime.datetime,
) -> Schedule:
    """按 SM-2 算出这张卡的下一次排期。"""
    if rating not in QUALITY:
        raise ValueError(f"非法评价：{rating!r}（允许：{', '.join(QUALITY)}）")
    quality = QUALITY[rating]
    current_ease = DEFAULT_EASE if ease is None else float(ease)
    updated_ease = next_ease(current_ease, quality)

    if quality < PASS_QUALITY:
        # 没记住：清零重来，明天再练；间隔不沿用（沿用会越攒越长，最后干脆想不起来）
        return Schedule(
            ease=updated_ease,
            interval_days=FIRST_INTERVAL,
            streak=0,
            lapses=lapses + 1,
            due_at=_add_days(now, FIRST_INTERVAL),
        )

    next_streak = streak + 1
    if next_streak == 1:
        interval = FIRST_INTERVAL
    elif next_streak == 2:
        interval = SECOND_INTERVAL
    else:
        previous = float(interval_days or FIRST_INTERVAL)
        interval = float(max(1, round(previous * updated_ease)))
    return Schedule(
        ease=updated_ease,
        interval_days=interval,
        streak=next_streak,
        lapses=lapses,
        due_at=_add_days(now, interval),
    )


def _add_days(now: datetime.datetime, days: float) -> str:
    moment = now + datetime.timedelta(days=float(days))
    return moment.replace(microsecond=0).isoformat()


def describe_interval(interval_days: float | None) -> str:
    """给界面用的一句话（"1 天后"/"6 天后"/"约 2 周后"）—— 别让用户自己换算天数。"""
    if interval_days is None:
        return ""
    days = float(interval_days)
    if days <= 1:
        return "1 天后"
    if days < 7:
        return f"{round(days)} 天后"
    # 60 天以内按周说：49 天讲"约 7 周"比"约 2 个月"更贴近用户的实际感受
    if days < 60:
        return f"约 {round(days / 7)} 周后"
    return f"约 {round(days / 30)} 个月后"


def is_due(due_at: str | None, *, now: datetime.datetime) -> bool:
    """到点了吗？

    `due_at` 为空 = 没有排期（老数据，或从没排过）→ **算到期**。
    宁可多练一次，也不要让一张没有排期的卡永远沉在库里。
    """
    if not due_at:
        return True
    try:
        moment = datetime.datetime.fromisoformat(str(due_at))
    except ValueError:
        return True
    if moment.tzinfo is None and now.tzinfo is not None:
        moment = moment.replace(tzinfo=now.tzinfo)
    return moment <= now
