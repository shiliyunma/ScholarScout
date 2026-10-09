"""子 Agent P0：``runtime/subtasks.py::fetch_many`` 的行为钉死。

三条必须成立（任务卡与 docs/16 §3.3）：
1. **并发上限生效** —— 峰值并发不超过 ``runtime.max_parallel_fetches``（也不可能超过 wanted 数）
2. **单来源失败不影响其它** —— 逐条返回，混合成败算成功
3. **异常不挂死** —— 任一来源抛异常都被就地兜住，整次扇出照常返回、顺序不乱

另有一条契约：``ProviderFetch.as_status()`` 的键集必须与旧的顺序循环**逐字一致**
（既有 ``tests/tools/test_research_search.py`` 依赖它）。这里用离线假 provider，不打真网。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

from scholarscout.config import RuntimeProfile
from scholarscout.fetch.http import FetchError
from scholarscout.providers.base import Query, SourceCard
from scholarscout.providers.settings import SearchSettings
from scholarscout.runtime.subtasks import ProviderFetch, fetch_many


def _card(url: str) -> SourceCard:
    return SourceCard(url=url, title=f"标题 {url}", source_type="web")


def _ctx(max_parallel: int = 3) -> SimpleNamespace:
    """最小化的 ToolContext 桩：fetch_many 只读 ``config.runtime`` 与 ``config.search_cfg``。"""
    return SimpleNamespace(
        config=SimpleNamespace(
            runtime=RuntimeProfile(max_parallel_fetches=max_parallel),
            search_cfg=SearchSettings(),
        )
    )


class _Tracker:
    """记录峰值并发（线程安全）；用来证明并发真的发生、且没有超限。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def __enter__(self) -> None:
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def __exit__(self, *_exc: object) -> None:
        with self._lock:
            self.active -= 1


@dataclass
class _FakeProvider:
    """假来源：可选延迟、可选抛出的异常、可选返回的卡片。"""

    min_interval_s: float = 1.0
    cards: tuple[SourceCard, ...] = ()
    error: Exception | None = None
    delay: float = 0.0
    tracker: _Tracker | None = None
    seen_queries: list[Query] | None = None

    def search(self, query: Query) -> list[SourceCard]:
        if self.seen_queries is not None:
            self.seen_queries.append(query)
        if self.tracker is not None:
            with self.tracker:
                time.sleep(self.delay)
        elif self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return list(self.cards)


# ------------------------------------------------------------------ 1. 并发上限


def test_concurrency_limit_is_respected() -> None:
    tracker = _Tracker()
    names = [f"p{i}" for i in range(6)]
    providers = {name: _FakeProvider(delay=0.05, tracker=tracker) for name in names}

    outcomes = fetch_many(_ctx(max_parallel=2), providers, wanted=names, query="q", per_provider=5)

    assert [o.provider for o in outcomes] == names  # 顺序保持
    assert all(o.ok for o in outcomes)
    assert tracker.peak == 2, f"并发上限应为 2，实测峰值 {tracker.peak}"


def test_concurrency_one_is_serial() -> None:
    tracker = _Tracker()
    names = ["a", "b", "c"]
    providers = {name: _FakeProvider(delay=0.01, tracker=tracker) for name in names}

    fetch_many(
        _ctx(max_parallel=3), providers, wanted=names, query="q", per_provider=1, concurrency=1
    )

    assert tracker.peak == 1, "concurrency=1 时必须串行"


def test_explicit_concurrency_overrides_config() -> None:
    tracker = _Tracker()
    names = ["a", "b", "c", "d"]
    providers = {name: _FakeProvider(delay=0.05, tracker=tracker) for name in names}

    fetch_many(
        _ctx(max_parallel=4), providers, wanted=names, query="q", per_provider=1, concurrency=2
    )

    assert tracker.peak == 2, "concurrency 参数应覆盖 config 里的并发上限"


def test_default_concurrency_reads_config_key() -> None:
    """不传 concurrency 时，上限来自 runtime.max_parallel_fetches（默认 3）。"""
    tracker = _Tracker()
    names = [f"p{i}" for i in range(5)]
    providers = {name: _FakeProvider(delay=0.05, tracker=tracker) for name in names}

    fetch_many(_ctx(max_parallel=3), providers, wanted=names, query="q", per_provider=1)

    assert tracker.peak == 3, f"应从 config 读到上限 3，实测 {tracker.peak}"


# ------------------------------------------------------------- 2. 失败隔离


def test_one_provider_failure_does_not_block_the_others() -> None:
    good = _FakeProvider(cards=(_card("https://a/1"), _card("https://a/2")))
    bad = _FakeProvider(error=FetchError("server", "arXiv 挂了"))
    providers = {"good": good, "bad": bad}

    outcomes = fetch_many(_ctx(), providers, wanted=["good", "bad"], query="q", per_provider=8)

    by = {o.provider: o for o in outcomes}
    assert by["good"].ok is True
    assert by["good"].n == 2
    assert [c.url for c in by["good"].cards] == ["https://a/1", "https://a/2"]
    assert by["bad"].ok is False
    assert by["bad"].error is not None and by["bad"].error.startswith("server:")
    # 顺序稳定：wanted 怎么给，结果就怎么回
    assert [o.provider for o in outcomes] == ["good", "bad"]


def test_all_providers_failing_still_returns_one_row_each() -> None:
    providers = {
        "x": _FakeProvider(error=FetchError("timeout", "超时")),
        "y": _FakeProvider(error=RuntimeError("炸了")),
    }
    outcomes = fetch_many(_ctx(), providers, wanted=["x", "y"], query="q", per_provider=1)

    assert len(outcomes) == 2
    assert all(not o.ok for o in outcomes)
    assert outcomes[0].error is not None and outcomes[0].error.startswith("timeout:")
    assert outcomes[1].error is not None and outcomes[1].error.startswith("RuntimeError:")


# ------------------------------------------------------------- 3. 异常不挂死


def test_unexpected_exception_is_isolated_and_never_hangs() -> None:
    """一个来源抛任意异常，其它来源照常返回；整次调用必须能结束（不挂死）。"""
    names = ["slow", "boom", "ok"]
    providers = {
        "slow": _FakeProvider(delay=0.05, cards=(_card("https://s/1"),)),
        "boom": _FakeProvider(error=ValueError("意料之外")),
        "ok": _FakeProvider(cards=(_card("https://o/1"),)),
    }

    started = time.monotonic()
    outcomes = fetch_many(_ctx(), providers, wanted=names, query="q", per_provider=1)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, "扇出必须能在合理时间内结束，不能挂死"
    by = {o.provider: o for o in outcomes}
    assert by["boom"].ok is False
    assert by["boom"].error is not None and "ValueError" in by["boom"].error
    assert by["ok"].ok is True and by["slow"].ok is True


def test_empty_wanted_returns_empty() -> None:
    assert fetch_many(_ctx(), {}, wanted=[], query="q", per_provider=1) == []


# ------------------------------------------------------- 未装配来源 / 状态键集


def test_missing_and_unknown_providers_are_reported_without_network() -> None:
    outcomes = fetch_many(
        _ctx(),
        {},
        wanted=["arxiv", "crossref"],
        query="q",
        per_provider=1,
        missing={"arxiv": "missing_api_key: X"},
    )
    by = {o.provider: o for o in outcomes}
    assert by["arxiv"].ok is False and by["arxiv"].error == "missing_api_key: X"
    assert by["arxiv"].n == 0
    assert by["crossref"].error == "unknown_provider"


def test_as_status_keys_match_the_old_contract() -> None:
    """成功/失败的键集必须与旧的顺序循环逐字一致（既有契约测试依赖）。"""
    ok = ProviderFetch("arxiv", ok=True, n=2, interval_s=3.0, latency_ms=120)
    assert ok.as_status() == {"provider": "arxiv", "ok": True, "n": 2, "interval_s": 3.0}
    bad = ProviderFetch("arxiv", ok=False, n=0, error="server: 挂了")
    assert bad.as_status() == {"provider": "arxiv", "ok": False, "n": 0, "error": "server: 挂了"}


def test_query_and_interval_are_passed_per_provider() -> None:
    """每个来源都收到同一个 Query；成功行的 interval_s 与 latency_ms 都记下来了。"""
    seen: list[Query] = []
    providers = {"p": _FakeProvider(cards=(_card("https://p/1"),), seen_queries=seen)}

    outcomes = fetch_many(_ctx(), providers, wanted=["p"], query="教育智能体", per_provider=7)

    assert len(seen) == 1
    assert seen[0].text == "教育智能体"
    assert seen[0].limit == 7
    assert outcomes[0].interval_s == 1.0  # 表里没有 p → fallback 到 provider.min_interval_s
    assert outcomes[0].latency_ms is not None and outcomes[0].latency_ms >= 0
