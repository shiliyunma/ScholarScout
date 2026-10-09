"""``fetch_many`` —— 并行 provider 扇出（子 Agent P0，见 ``docs/16 §3.3``）。

**这不是 agent，是一个函数。** 它把 ``tools/research_search.py`` 里"逐个来源顺序抓"的循环
换成线程池并发：N 条来源同时出网，攒齐后逐条返回。

四条铁律（违反任何一条都不算达成）：

1. **只并行网络抓取**：线程池里跑的**只有** ``provider.search``（出网）。
   绝不在此并发调模型 —— 摘要、判权、生成类工具仍归主循环，避免费用尖峰。
2. **单条失败不阻塞其它**：每个来源的异常就地兜住，逐条返回 :class:`ProviderFetch`；
   混合成败算成功（与 ``research.search`` 既有语义一致，见 ``docs/05 §3.3``）。
3. **不绕过限速**：每个来源的最小间隔仍按原口径取（``SearchSettings.interval_for``：
   配置表优先、provider 兜底），实际串行化在 ``fetch/http.py`` 的**出网层按 host** 生效 ——
   并行不会让任一 host 被超频（每个 provider 各自一个 host，互不干扰）。
4. **不新增运行期依赖**：只用标准库 ``concurrent.futures``（依赖政策见 ``docs/03 §5``）。

并发上限读 ``runtime.max_parallel_fetches``（默认 3）；``concurrency`` 参数可临时覆盖。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from scholarscout.fetch.http import FetchError
from scholarscout.providers.base import Query, SourceCard
from scholarscout.providers.settings import KNOWN
from scholarscout.runtime.toolctx import ToolContext

#: 线程名前缀：真机排查（卡住/超时）时 ``threading.enumerate()`` 一眼能认出是谁
_THREAD_PREFIX = "scout-fetch"


@dataclass(frozen=True)
class ProviderFetch:
    """一条来源的一次抓取结果（成功与失败都在这里，便于逐条观测）。

    ``cards`` 只在成功时非空；``latency_ms`` 供新指标使用，
    **不进** :meth:`as_status`（``provider_status`` 的键集必须与改造前逐字一致）。
    """

    provider: str
    ok: bool
    n: int
    error: str | None = None
    interval_s: float | None = None
    latency_ms: int | None = None
    cards: tuple[SourceCard, ...] = ()

    def as_status(self) -> dict[str, Any]:
        """回给模型的 ``provider_status`` 条目。

        成功：``{provider, ok, n, interval_s}``；失败：``{provider, ok, n, error}``。
        键集与顺序和改造前的顺序循环**完全一致**（有契约测试钉住）。
        """
        if self.ok:
            return {
                "provider": self.provider,
                "ok": True,
                "n": self.n,
                "interval_s": self.interval_s,
            }
        return {"provider": self.provider, "ok": False, "n": self.n, "error": self.error}


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _resolve_concurrency(ctx: ToolContext, override: int | None) -> int:
    """并发上限：显式 ``concurrency`` 优先，否则读 ``runtime.max_parallel_fetches``。

    下限夹到 1（0 会让线程池起不来；负数更没意义）。
    """
    raw = ctx.config.runtime.max_parallel_fetches if override is None else override
    return max(1, int(raw))


def _unavailable(name: str, reasons: Mapping[str, str]) -> ProviderFetch:
    """来源没装配：区分"已知但没配"（带原因）与"名字根本不认识"。"""
    if name in KNOWN:
        return ProviderFetch(name, ok=False, n=0, error=reasons.get(name, "unavailable"))
    return ProviderFetch(name, ok=False, n=0, error="unknown_provider")


def fetch_many(
    ctx: ToolContext,
    providers: Mapping[str, Any],
    *,
    wanted: Sequence[str],
    query: str,
    per_provider: int,
    year_from: int | None = None,
    sort: str = "relevance",
    missing: Mapping[str, str] | None = None,
    concurrency: int | None = None,
) -> list[ProviderFetch]:
    """并行对 ``wanted`` 里的每个来源跑一次检索，按**原顺序**逐条返回结果。

    - ``providers``：``available_providers(ctx)`` 的产物（name → provider 实例）
    - ``wanted``：``resolve_wanted(...)`` 解析后的来源名（保序、去重）
    - ``missing``：``unavailable_providers(ctx)`` 的产物（没装配的来源 → 原因）
    - ``concurrency``：并发上限；``None`` 时读 ``runtime.max_parallel_fetches``

    返回的列表长度恒等于 ``len(wanted)``、顺序一致：调用方据此重建 ``provider_status``
    与汇总卡片。**任何单条异常都被就地兜住**，绝不会让整次扇出挂死或吞掉其它来源。
    """
    if not wanted:
        return []

    reasons = dict(missing or {})
    limit = _resolve_concurrency(ctx, concurrency)
    query_obj = Query(text=query, limit=per_provider, year_from=year_from, sort=sort)

    def run(name: str) -> ProviderFetch:
        provider = providers.get(name)
        if provider is None:
            return _unavailable(name, reasons)
        # 限速口径与顺序版一致：配置表优先、provider fallback（真实串行化在出网层）
        interval = ctx.config.search_cfg.interval_for(name, provider.min_interval_s)
        started = time.monotonic()
        try:
            found = provider.search(query_obj)
        except FetchError as exc:
            return ProviderFetch(
                name,
                ok=False,
                n=0,
                error=f"{exc.kind}: {exc}",
                interval_s=interval,
                latency_ms=_elapsed_ms(started),
            )
        except Exception as exc:  # 单来源异常必须就地兜住，不许外溢到线程池
            return ProviderFetch(
                name,
                ok=False,
                n=0,
                error=f"{type(exc).__name__}: {exc}",
                interval_s=interval,
                latency_ms=_elapsed_ms(started),
            )
        return ProviderFetch(
            name,
            ok=True,
            n=len(found),
            interval_s=interval,
            latency_ms=_elapsed_ms(started),
            cards=tuple(found),
        )

    workers = min(limit, len(wanted))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=_THREAD_PREFIX) as pool:
        futures = [pool.submit(run, name) for name in wanted]
        outcomes: list[ProviderFetch] = []
        for name, future in zip(wanted, futures, strict=True):
            try:
                outcomes.append(future.result())
            except Exception as exc:  # 兜底：worker 已兜住，防线程池层面的意外
                outcomes.append(
                    ProviderFetch(name, ok=False, n=0, error=f"{type(exc).__name__}: {exc}")
                )
    return outcomes
