"""子 Agent P0：``runtime/subtasks.py::fetch_many`` 的行为钉死。

三条必须成立（任务卡与 docs/16 §3.3）：
1. **并发上限生效** —— 峰值并发不超过 ``runtime.max_parallel_fetches``（也不可能超过 wanted 数）
2. **单来源失败不影响其它** —— 逐条返回，混合成败算成功
3. **异常不挂死** —— 任一来源抛异常都被就地兜住，整次扇出照常返回、顺序不乱

另有一条契约：``ProviderFetch.as_status()`` 的键集必须与旧的顺序循环**逐字一致**
（既有 ``tests/tools/test_research_search.py`` 依赖它）。这里用离线假 provider，不打真网。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

import pytest

from scholarscout.config import RuntimeProfile
from scholarscout.fetch.http import FetchError
from scholarscout.providers.base import Query, SourceCard
from scholarscout.providers.llm import LlmError
from scholarscout.providers.settings import SearchSettings
from scholarscout.runtime import registry
from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.subtasks import (
    SUBTASK_SYSTEM_PROMPT,
    ProviderFetch,
    _total_budget,
    fetch_many,
    open_goals,
    run_subtask,
    run_subtasks,
)
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state import sessions

from ..conftest import StubLlm, reply, tool_call


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
    # 名字故意用 KNOWN 里没有的：``crossref`` 曾经是"没实现"的替身，现在已是真来源，
    # 它未装配时按契约报 ``unavailable``（known 但没装配），与 unknown 是两回事。
    outcomes = fetch_many(
        _ctx(),
        {},
        wanted=["arxiv", "no_such_source"],
        query="q",
        per_provider=1,
        missing={"arxiv": "missing_api_key: X"},
    )
    by = {o.provider: o for o in outcomes}
    assert by["arxiv"].ok is False and by["arxiv"].error == "missing_api_key: X"
    assert by["arxiv"].n == 0
    assert by["no_such_source"].error == "unknown_provider"


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


# ====================================================== 子 Agent P1：顺序子任务（ReWOO 式）
#
# 五件必须成立（任务卡与 docs/16 §3.3 P1）：
# 1. **隔离** —— 子任务用独立的 messages，**不污染主会话的 messages 表**
# 2. **预算封顶** —— 单个子任务不超过 max_tool_calls；整批不超过总额度池
# 3. **失败隔离** —— 一个子任务炸了不影响其它
# 4. **默认关** —— runtime.subtask_enabled=false 时不触发、不记账、不发事件
# 5. **独立提示词** —— 用 SUBTASK_SYSTEM_PROMPT（不进而 system_prompt_hash 的装配）

_ECHO_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    registry.clear_registry()
    yield
    registry.clear_registry()


def _register_echo(*, fail: bool = False) -> ToolSpec:
    def handler(ctx: object, query: str = "") -> dict[str, Any]:
        del ctx, query
        if fail:
            raise RuntimeError("工具炸了")
        return {
            "ok": True,
            "data": {"items": [{"source_id": "src_1", "title": "T"}]},
            "refs": {"source_ids": ["src_1"]},
        }

    return registry.register(
        ToolSpec(
            name="research.echo",
            description="回显查询",
            parameters=_ECHO_SCHEMA,
            handler=handler,
            toolset="research",
        )
    )


def _tool_ctx(
    config: Any,
    db: Any,
    llm: Any,
    *,
    session_id: str,
    turn_id: str = "trn_1",
    enabled: bool = True,
    budget: int = 12,
) -> ToolContext:
    runtime = replace(config.runtime, max_tool_calls_per_turn=budget, subtask_enabled=enabled)
    return ToolContext(
        db=db,
        config=replace(config, runtime=runtime),
        llm=llm,
        session_id=session_id,
        turn_id=turn_id,
    )


def _message_count(db: Any, session_id: str) -> int:
    row = db.query_one("SELECT count(*) AS n FROM messages WHERE session_id = ?", (session_id,))
    return int(row["n"])


# --------------------------------------------------------------- 1. 隔离


def test_run_subtask_isolates_the_main_session_messages(config, db) -> None:
    _register_echo()
    session_id = sessions.create_session(db, model="stub-model")
    before = _message_count(db, session_id)
    stub = StubLlm(
        reply("", tool_calls=[tool_call("research.echo", '{"query":"x"}')]),
        reply("结论：见 src_1"),
    )

    result = run_subtask(_tool_ctx(config, db, stub, session_id=session_id), "子问题一")

    # 主会话的 messages 表**一条都没多**（子任务全程用自建 messages）
    assert _message_count(db, session_id) == before
    # 子任务用的是自己的系统提示词 + 目标，而不是主提示词
    assert stub.calls[0]["messages"][0] == {"role": "system", "content": SUBTASK_SYSTEM_PROMPT}
    assert stub.calls[0]["messages"][1] == {"role": "user", "content": "子问题一"}
    assert SUBTASK_SYSTEM_PROMPT not in stub.calls[0]["messages"][0]["content"].replace(
        SUBTASK_SYSTEM_PROMPT, ""
    )
    assert result.text == "结论：见 src_1"
    assert result.tool_calls == ["research.echo"]
    assert result.refs == {"source_ids": ["src_1"]}
    assert result.stop_reason == "completed"
    # 工具轨迹落库、带 subtask: 前缀（与主循环的调用分得开）
    row = db.query_one("SELECT tool_name, ok FROM tool_traces")
    assert row["tool_name"] == "subtask:research.echo" and row["ok"] == 1


def test_run_subtask_without_goal_is_a_noop(config, db) -> None:
    session_id = sessions.create_session(db, model="stub-model")
    result = run_subtask(_tool_ctx(config, db, StubLlm(), session_id=session_id), "   ")
    assert result.stop_reason == "empty" and result.tool_calls == []


# --------------------------------------------------------------- 2. 预算封顶


def test_run_subtask_stops_at_the_tool_budget(config, db) -> None:
    _register_echo()
    session_id = sessions.create_session(db, model="stub-model")
    stub = StubLlm(
        reply("", tool_calls=[tool_call("research.echo", "{}", call_id="c1")]),
        reply("", tool_calls=[tool_call("research.echo", "{}", call_id="c2")]),
        reply("收口了"),
    )

    result = run_subtask(_tool_ctx(config, db, stub, session_id=session_id), "g", max_tool_calls=2)

    assert result.tool_calls == ["research.echo", "research.echo"]
    assert result.stop_reason == "completed"
    # 轮次 = 工具预算 + 1：最后一轮**不给工具**（否则用满预算会被误判 tool_limit）
    assert stub.calls[2]["tools"] is None


def test_total_budget_pool_is_capped_at_half_the_main_budget(config, db) -> None:
    session_id = sessions.create_session(db, model="stub-model")
    ctx = _tool_ctx(config, db, StubLlm(), session_id=session_id, budget=4)
    assert _total_budget(ctx, None) == 2  # 默认 = 主预算的 1/2
    assert _total_budget(ctx, 99) == 2  # 显式给大了也夹到 1/2 上限
    assert _total_budget(ctx, 1) == 1


def test_run_subtasks_pool_stops_the_rest_with_budget(config, db) -> None:
    _register_echo()
    session_id = sessions.create_session(db, model="stub-model")
    stub = StubLlm(
        reply("", tool_calls=[tool_call("research.echo", "{}", call_id="a")]),
        reply("A"),
        reply("", tool_calls=[tool_call("research.echo", "{}", call_id="b")]),
        reply("B"),
    )

    results = run_subtasks(
        _tool_ctx(config, db, stub, session_id=session_id),
        ["g1", "g2", "g3"],
        per_task_budget=2,
        total_budget=2,
    )

    assert [r.stop_reason for r in results] == ["completed", "completed", "budget"]
    assert sum(len(r.tool_calls) for r in results) <= 2  # 总额度池封顶
    assert results[2].tool_calls == []


# --------------------------------------------------------------- 3. 失败隔离


def test_subtask_tool_failure_is_reported_not_raised(config, db) -> None:
    _register_echo(fail=True)
    session_id = sessions.create_session(db, model="stub-model")
    stub = StubLlm(
        reply("", tool_calls=[tool_call("research.echo", "{}")]),
        reply("工具失败也收口"),
    )

    result = run_subtask(_tool_ctx(config, db, stub, session_id=session_id), "g", max_tool_calls=2)

    assert result.stop_reason == "completed"
    assert result.text == "工具失败也收口"
    row = db.query_one("SELECT ok FROM tool_traces")
    assert row["ok"] == 0


class _GoalLlm:
    """按目标决定行为的桩：目标是 "bad" 就抛错，其余回一句结论。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None) -> Any:
        self.calls.append({"messages": list(messages), "tools": tools})
        goal = messages[1]["content"]
        if goal == "bad":
            raise LlmError("server", "炸了")
        return reply(f"ok:{goal}")


def test_one_failing_subtask_does_not_block_the_others(config, db, caplog) -> None:
    """一个子任务炸了不影响其它；给用户看的文案不留内部标识，原始异常只进日志。"""
    caplog.set_level(logging.WARNING, logger="scholarscout.runtime.subtasks")
    session_id = sessions.create_session(db, model="stub-model")

    results = run_subtasks(
        _tool_ctx(config, db, _GoalLlm(), session_id=session_id), ["g1", "bad", "g2"]
    )

    # `LlmError` 走的是"模型服务这一侧"那条文案（`except LlmError` 比 `except Exception` 先命中）
    assert [r.text for r in results] == [
        "ok:g1",
        "子任务没有跑完：模型服务这一侧出错（原因已记进日志）。",
        "ok:g2",
    ]
    assert [r.stop_reason for r in results] == ["completed", "error", "completed"]
    # 双向验证（前一半）：`kind`（server）/ 异常类名 / 原始报文都不许出现在给用户看的 text 里
    failed_text = results[1].text
    assert "server" not in failed_text
    assert "LlmError" not in failed_text
    assert "炸了" not in failed_text
    # 双向验证（后一半）：原始异常类名与 kind **仍留在日志**里 —— 剥掉的只是"给用户那份"，
    # 不是诊断信息本身；日志里没有它就说明我们只是把错误静默吞了。
    assert "LlmError" in caplog.text
    assert "kind=server" in caplog.text
    assert "炸了" in caplog.text


# --------------------------------------------------------------- 4. 默认关


def test_disabled_switch_does_not_trigger(config, db) -> None:
    _register_echo()
    session_id = sessions.create_session(db, model="stub-model")
    events: list[tuple[str, dict[str, Any]]] = []

    results = run_subtasks(
        _tool_ctx(config, db, StubLlm(), session_id=session_id, enabled=False),
        ["g1", "g2"],
        emit=lambda kind, payload: events.append((kind, payload)),
    )

    assert results == []
    assert events == []
    assert db.query_one("SELECT count(*) AS n FROM usage_records")["n"] == 0


def test_subtask_enabled_defaults_to_false() -> None:
    assert RuntimeProfile().subtask_enabled is False


# --------------------------------------------------------------- 事件与记账


def test_subtask_events_come_in_start_done_pairs(config, db) -> None:
    _register_echo()
    session_id = sessions.create_session(db, model="stub-model")
    events: list[tuple[str, dict[str, Any]]] = []

    run_subtasks(
        _tool_ctx(config, db, StubLlm(reply("done")), session_id=session_id),
        ["g1", "g2"],
        per_task_budget=1,
        total_budget=2,
        emit=lambda kind, payload: events.append((kind, payload)),
    )

    assert [kind for kind, _ in events] == [
        "chat.subtask_start",
        "chat.subtask_done",
        "chat.subtask_start",
        "chat.subtask_done",
    ]
    # 载荷**逐键**钉住：多一个键要显式承认（docs/20 §3.1 给每条通知加了 session_id）
    assert set(events[0][1]) == {"session_id", "turn_id", "seq", "index", "goal"}
    assert set(events[1][1]) == {
        "session_id",
        "turn_id",
        "seq",
        "index",
        "stop_reason",
        "refs",
    }
    assert events[0][1]["turn_id"] == "trn_1"
    # 子任务事件必须能分派回会话（docs/20 §3.1）：界面靠它把子任务轨迹归位
    assert all(payload["session_id"] == session_id for _, payload in events)
    assert [events[i][1]["index"] for i in (0, 2)] == [0, 1]


def test_subtask_usage_is_recorded_with_its_own_purpose(config, db) -> None:
    _register_echo()
    session_id = sessions.create_session(db, model="stub-model")

    run_subtasks(_tool_ctx(config, db, StubLlm(reply("done")), session_id=session_id), ["g1"])

    rows = [dict(r) for r in db.query("SELECT purpose, session_id FROM usage_records")]
    assert rows and all(r["purpose"] == "subtask" for r in rows)
    assert all(r["session_id"] == session_id for r in rows)


# --------------------------------------------------------------- 子任务来源


def test_open_goals_extracts_only_open_questions() -> None:
    state = {
        "questions": [
            {"text": "已覆盖的", "status": "covered"},
            {"text": "缺口的", "status": "open"},
            {"text": "没回填状态的"},  # 不猜已覆盖 → 算 open
            {"text": "   "},
            "不是字典",
        ]
    }
    assert open_goals(state) == ["缺口的", "没回填状态的"]
    assert open_goals(None) == []
