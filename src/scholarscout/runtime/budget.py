"""每日用量封顶（docs/20 §5.1 / §5.2 的「全局用量封顶」）：**默认无上限**。

口径三条（写死在这里，别处不许另算）：

1. **滚动 24 小时**，不是自然日 —— 自然日会在午夜突然"回血"，用户看到的"今天还能用多少"
   会在 23:59 → 00:01 之间跳变；滚动窗口没有这个悬崖。
   取数唯一入口是 ``usage_records``（每次模型调用都立即记账，见 ``runtime/loop.py``），
   用 ``julianday(created_at)`` 比较 —— 表里的 ``created_at`` 是带时区偏移的 ISO-8601，
   字符串直接比会在跨时区偏移时出错。
2. **默认 ``None`` = 不限制**：``from_runtime()`` 直接回 ``None``，于是调用方**不查库、不判断**，
   行为与引入本模块之前逐字一致（这是可断言的回归点，不是口头承诺）。
3. **单价未配就不算钱**：``est_cost`` 为空的行不进金额分母，``cost.used`` 回 ``None``；
   此时金额上限**不生效**（``reason`` 只会是 token），绝不按估算单价折出一个好看的数字。

超限**不新造事件名**：一轮的收口体现在 ``chat.done`` 的 ``stop_reason="budget"``，
当前用量走既有的 ``chat.status``（``global_budget`` 键）。

两个检查点（``docs/20 §5.2``，缺一处就漏）：

- ``SessionRuntime.submit_turn()`` —— 已超则**拒绝这一轮**（``reason="daily_budget_exceeded"``）；
- ``runtime/loop.run_turn`` 里**每一次模型调用之前** —— 正在跑的那一轮当场收口。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from scholarscout.state.db import Database

#: 统计窗口（小时）。改成自然日要同时改文档与界面口径，所以只在这里写一次。
WINDOW_HOURS = 24.0

#: 收口理由（进 ``stop_reason``，与 ``docs/01 §4`` 的枚举一致）
STOP_REASON = "budget"
#: ``submit_turn`` 的拒绝理由（RPC 层据此给明确提示）
REJECT_REASON = "daily_budget_exceeded"


def _positive_int(value: object) -> int | None:
    """``None`` / 非数字 / ``<= 0`` 一律当"不限制"。

    为什么 ``0`` 也算不限制：YAML 里写 ``daily_token_budget: 0`` 的直觉是"先关掉"，
    把它解释成"上限 0，一律拒绝"会让用户以为程序坏了。``bool`` 显式排除（YAML 的
    ``true`` 会被 ``int()`` 读成 1，那是个荒唐的上限）。
    """
    if isinstance(value, bool) or value is None:
        return None
    number: int
    if isinstance(value, (int, float)):
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if number > 0 else None


def _positive_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    number: float
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if number > 0 else None


def window_start(hours: float = WINDOW_HOURS, *, now: datetime | None = None) -> str:
    """滚动窗口的起点（带本机时区偏移的 ISO-8601，与 ``created_at`` 同一格式）。"""
    moment = now or datetime.now(UTC).astimezone()
    return (moment - timedelta(hours=hours)).isoformat(timespec="microseconds")


def usage_since(db: Database, since: str) -> dict[str, Any]:
    """``usage_records`` 里 ``since`` 之后的用量（token / 调用次数 / 金额）。

    金额只汇总 ``est_cost`` **非空**的行，并同时给出 ``priced_calls`` ——
    "没有价格"与"花了 0 元"必须能分开看。
    """
    row = db.query_one(
        "SELECT coalesce(sum(prompt_tokens),0) AS prompt_tokens,"
        " coalesce(sum(completion_tokens),0) AS completion_tokens,"
        " count(*) AS calls,"
        " sum(est_cost) AS est_cost,"
        " count(est_cost) AS priced_calls"
        " FROM usage_records WHERE julianday(created_at) >= julianday(?)",
        (since,),
    )
    data = dict(row) if row is not None else {}
    prompt = int(data.get("prompt_tokens") or 0)
    completion = int(data.get("completion_tokens") or 0)
    priced = int(data.get("priced_calls") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "tokens": prompt + completion,
        "calls": int(data.get("calls") or 0),
        # 一行都没配单价 → None（不是 0.0）：0.0 会被读成"免费"
        "est_cost": (
            float(data["est_cost"]) if priced > 0 and data.get("est_cost") is not None else None
        ),
        "priced_calls": priced,
    }


@dataclass(frozen=True)
class BudgetGuard:
    """一处上限的"已启用"形态。**只在上限为正数时才被创建**（``from_runtime`` 负责）。"""

    token_limit: int | None = None
    cost_limit: float | None = None
    window_hours: float = WINDOW_HOURS

    def snapshot(self, db: Database, *, now: datetime | None = None) -> dict[str, Any]:
        """当前用量 + 是否超限（给 ``chat.status`` 与拒绝载荷用，形状稳定）。"""
        since = window_start(self.window_hours, now=now)
        used = usage_since(db, since)
        tokens_used = int(used["tokens"])
        cost_used = used["est_cost"]

        token_remaining: int | None = None
        if self.token_limit is not None:
            token_remaining = max(0, self.token_limit - tokens_used)

        cost_remaining: float | None = None
        if self.cost_limit is not None and cost_used is not None:
            cost_remaining = max(0.0, self.cost_limit - float(cost_used))

        reason: str | None = None
        if self.token_limit is not None and tokens_used >= self.token_limit:
            reason = "daily_token_budget"
        elif (
            self.cost_limit is not None
            and cost_used is not None
            and float(cost_used) >= self.cost_limit
        ):
            reason = "daily_cost_budget"

        return {
            "enabled": True,
            "window_hours": self.window_hours,
            "since": since,
            "checked_at": (now or datetime.now(UTC).astimezone()).isoformat(
                timespec="microseconds"
            ),
            "tokens": {
                "used": tokens_used,
                "limit": self.token_limit,
                "remaining": token_remaining,
                "prompt": int(used["prompt_tokens"]),
                "completion": int(used["completion_tokens"]),
            },
            "cost": {
                "used": float(cost_used) if cost_used is not None else None,
                "limit": self.cost_limit,
                "remaining": cost_remaining,
                # 有单价的行数：0 表示"金额上限此刻无法判定"（不是"没花钱"）
                "priced_calls": int(used["priced_calls"]),
            },
            "calls": int(used["calls"]),
            "exceeded": reason is not None,
            "reason": reason,
        }

    def exceeded(self, db: Database, *, now: datetime | None = None) -> dict[str, Any] | None:
        """超限就回快照，否则回 ``None``（调用点的写法：``snap = guard.exceeded(db)``）。"""
        state = self.snapshot(db, now=now)
        return state if state["exceeded"] else None


def from_runtime(runtime_cfg: object) -> BudgetGuard | None:
    """从 ``config.runtime`` 造一个守卫；**两个上限都没配时回 ``None``**（= 完全不介入）。

    用 ``getattr`` 而不是直接取属性：老的 ``RuntimeProfile`` 实例（测试里手搓的）没有这两个字段，
    那正是"没配上限"，读成缺省即可，不该炸。
    """
    token_limit = _positive_int(getattr(runtime_cfg, "daily_token_budget", None))
    cost_limit = _positive_float(getattr(runtime_cfg, "daily_cost_budget", None))
    if token_limit is None and cost_limit is None:
        return None
    return BudgetGuard(token_limit=token_limit, cost_limit=cost_limit)


def snapshot(runtime_cfg: object, db: Database, *, now: datetime | None = None) -> dict[str, Any]:
    """给 ``chat.status`` 用的只读快照（未配置上限时 ``enabled=False``，各项为 ``None``）。"""
    guard = from_runtime(runtime_cfg)
    if guard is None:
        since = window_start(WINDOW_HOURS, now=now)
        return {
            "enabled": False,
            "window_hours": WINDOW_HOURS,
            "since": since,
            "checked_at": (now or datetime.now(UTC).astimezone()).isoformat(
                timespec="microseconds"
            ),
            "tokens": {"used": None, "limit": None, "remaining": None},
            "cost": {"used": None, "limit": None, "remaining": None, "priced_calls": 0},
            "exceeded": False,
            "reason": None,
        }
    return guard.snapshot(db, now=now)
