"""`SessionRuntime`：每会话一个工作单元 + 上限 3 的线程池（docs/20 §2/§7.2/§7.3/§7.5）。

这一层是**调度**，不是对话：`loop.run_turn` 的内部逻辑一字未改。所以这里测的全是调度语义 ——

1. `submit_turn` **立即返回** `turn_id`（异步的前提），进行中一轮的状态在 `status` 里看得到；
2. **一个会话同时只能有一轮**（`turn_in_progress`）：同会话两轮并行会踩坏 `messages.idx`
   与 `research_states`（会话内自增 / 一会话一行）；
3. **上限 3 且池满不排队**：第 4 个被拒，**没有第 4 个线程起来**；
4. **取消只影响一个会话**：每轮一个 `threading.Event`，另一个会话照常 `completed`；
5. **关窗立刻能退**：`shutdown()` 不 join，工作线程摘出解释器退出 join 名单 ——
   最后一条用**子进程实测退出耗时**证明（不是靠读注释）。

全桩测：不真调模型、不真出网。线程名前缀每个用例各不相同，这样"起了几个线程"是可数的
（默认前缀是全局共享的，别的用例的池线程会污染计数）。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from scholarscout.config import Config
from scholarscout.providers.llm import ChatReply
from scholarscout.rpc.methods import AppContext
from scholarscout.runtime import registry
from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.session_runtime import SessionRuntime
from scholarscout.state import sessions
from scholarscout.state.db import Database

from ..conftest import reply, tool_call

ECHO_TOOL = "research.echo"
ECHO_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    registry.clear_registry()
    yield
    registry.clear_registry()


# --------------------------------------------------------------------- 桩与工具


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for item in reversed(messages):
        if item.get("role") == "user":
            return str(item.get("content") or "")
    return ""


class _ScriptedLlm:
    """按**用户消息**分发行为的模型桩：一个 ctx、一个线程池、多个会话各自被认出来。

    这样才可能只用一个 `AppContext` 就同时跑三个会话（各自的行为不同），
    而"共享一个线程池"正是被测对象（每个会话一个 ctx 就测不到上限了）。
    """

    def __init__(self, behaviors: dict[str, Callable[[], ChatReply]]) -> None:
        self.behaviors = dict(behaviors)
        self.seen: list[str] = []
        self._lock = threading.Lock()

    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None) -> ChatReply:
        text = _last_user_text(messages)
        with self._lock:
            self.seen.append(text)
        behavior = self.behaviors.get(text)
        assert behavior is not None, f"没有为这轮准备脚本：{text!r}"
        return behavior()


def _register_echo() -> None:
    def handler(tool_ctx: Any, query: str) -> dict[str, Any]:
        del tool_ctx
        return {"ok": True, "data": {"query": query}, "refs": {}}

    registry.register(
        ToolSpec(
            name=ECHO_TOOL,
            description="回显查询",
            parameters=ECHO_SCHEMA,
            handler=handler,
            toolset="research",
        )
    )


def _new_session(ctx: AppContext) -> str:
    return sessions.create_session(ctx.db, model=ctx.config.chat.model)


def _submit(
    runtime: SessionRuntime, session_id: str, text: str, events: list[Any]
) -> dict[str, Any]:
    return runtime.submit_turn(
        session_id=session_id,
        text=text,
        toolsets=["research"],
        emit=lambda kind, payload: events.append((kind, payload)),
    )


def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 10.0, what: str = "条件"
) -> None:
    """等一个条件成立 —— 用轮询等信号，不用 sleep 赌时序（照 `test_serve_concurrency.py`）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"{timeout}s 内没等到：{what}")


def _worker_threads(prefix: str) -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name.startswith(prefix)]


# --------------------------------------------------------------------- 提交与观测


def test_submit_returns_a_turn_id_while_the_turn_still_runs(ctx: AppContext) -> None:
    """`submit_turn` 必须**立即**返回（异步的前提），进行中一轮在 `status` 里可观测。"""
    gate = threading.Event()
    entered = threading.Event()

    def blocked() -> ChatReply:
        entered.set()
        assert gate.wait(timeout=10), "被卡住的那一轮没被放开"
        return reply("A-正文-1")

    ctx.llm = _ScriptedLlm({"A 的问题": blocked})
    session_id = _new_session(ctx)
    events: list[Any] = []
    runtime = SessionRuntime(ctx, max_concurrent=3, thread_prefix="probe-submit")
    try:
        started = time.monotonic()
        accepted = _submit(runtime, session_id, "A 的问题", events)
        elapsed = time.monotonic() - started

        assert accepted["ok"] is True
        assert str(accepted["turn_id"]).startswith("trn_")
        assert elapsed < 0.5, f"submit_turn 必须立即返回（实测 {elapsed:.2f}s）"
        assert entered.wait(timeout=5), "工作线程没起来"

        status = runtime.status(session_id)
        assert [item["turn_id"] for item in status["running"]] == [accepted["turn_id"]]
        assert status["running"][0]["session_id"] == session_id
        assert status["capacity"] == {"max": 3, "used": 1}
        assert status["session"]["running"] is True

        gate.set()
        result = runtime.wait(accepted["turn_id"], timeout=10)
        assert result is not None
        assert result.turn_id == accepted["turn_id"], "预分配的 turn_id 必须就是这一轮的真号"
        assert result.text == "A-正文-1"
        assert result.stop_reason == "completed"
        assert runtime.status(session_id)["running"] == []
        assert runtime.status()["capacity"]["used"] == 0
        # 收尾事件带上权威正文（docs/20 §3.2）
        done = [payload for kind, payload in events if kind == "chat.done"]
        assert done[-1]["text"] == "A-正文-1"
        assert done[-1]["message_id"] == result.message_id
    finally:
        gate.set()
        runtime.shutdown()


def test_a_second_turn_on_the_same_session_is_refused(ctx: AppContext) -> None:
    """同一会话不许两轮并行 —— 被拒的那一轮**一个字都不落库**（被拒 = 没发生过）。"""
    gate = threading.Event()
    entered = threading.Event()

    def blocked() -> ChatReply:
        entered.set()
        assert gate.wait(timeout=10)
        return reply("A-正文-1")

    ctx.llm = _ScriptedLlm({"A 的问题": blocked})
    session_id = _new_session(ctx)
    runtime = SessionRuntime(ctx, max_concurrent=3, thread_prefix="probe-inflight")
    try:
        first = _submit(runtime, session_id, "A 的问题", [])
        assert entered.wait(timeout=5)
        second = _submit(runtime, session_id, "A 的第二问", [])

        assert second["ok"] is False
        assert second["reason"] == "turn_in_progress"
        assert second["turn_id"] == first["turn_id"], "要如实告诉调用方是哪一轮在跑"
        assert len(runtime.status()["running"]) == 1

        gate.set()
        runtime.wait(str(first["turn_id"]), timeout=10)
        rows = ctx.db.query("SELECT content FROM messages WHERE session_id = ?", (session_id,))
        assert all("第二问" not in str(row["content"]) for row in rows)
    finally:
        gate.set()
        runtime.shutdown()


def test_the_three_sessions_really_overlap_on_the_wall_clock(ctx: AppContext) -> None:
    """**并行证据（本任务的硬交付）**：三个会话在同一个 `Barrier(3)` 上汇合 → 墙钟上真重叠。

    断言分三层，缺一层就不能算"真并行"：
    1. **能汇合**：三个工作线程都进到模型调用里 `barrier.wait()` 并全部通过 —— 池子只有 2 个槽时
       第三个永远到不了屏障，10s 后 `BrokenBarrierError` 会把这一轮打成 `error`，用例当场红；
    2. **区间重叠**：三段时间戳区间 `[start, end]` 的**交集非空**（`max(starts) < min(ends)`），
       且重叠加持了实测时长 —— 只断言"都在跑"是区间套，断言不出重叠；
    3. 汇合后**仍被主线程按着**（`release`），于是"第 4 个被拒"是在三方都在跑时断言的，
       不是等它们跑完再补一句。
    """
    timeline: list[tuple[str, str, float]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(3)  # 三个会话都进到模型调用里才放行 → 三个确实同时在跑
    release = threading.Event()

    def gated(label: str) -> Callable[[], ChatReply]:
        def behavior() -> ChatReply:
            with lock:
                timeline.append((label, "start", time.perf_counter()))
            barrier.wait(timeout=15)
            with lock:
                # 计时一律用 `perf_counter`：Windows 上 `time.monotonic()` 的粒度约 15.6ms，
                # 三个只差几十微秒的采样会读到**同一个值**（第一版就是这么被它骗过去的：
                # 断言 max(start) < min(joined) 恒假）。
                timeline.append((label, "joined", time.perf_counter()))
            assert release.wait(timeout=20), f"{label} 没被主线程放行"
            with lock:
                timeline.append((label, "end", time.perf_counter()))
            return reply(f"{label}-正文-1")

        return behavior

    ctx.llm = _ScriptedLlm(
        {f"{label} 的问题": gated(label) for label in "ABC"}
        | {"D 的问题": lambda: reply("D-正文-1")}
    )
    ids = {label: _new_session(ctx) for label in "ABC"}
    fourth = _new_session(ctx)
    runtime = SessionRuntime(ctx, max_concurrent=3, thread_prefix="probe-cap")
    try:
        accepted = {label: _submit(runtime, ids[label], f"{label} 的问题", []) for label in "ABC"}
        # ① 三个会话全部走到屏障之后（这就是"同时"的硬证据，不是 sleep 赌的）
        _wait_until(
            lambda: sum(1 for _, phase, _ in timeline if phase == "joined") == 3,
            timeout=20,
            what="三个会话在 Barrier(3) 上汇合",
        )
        _wait_until(
            lambda: runtime.status()["capacity"]["used"] == 3,
            what="三个会话同时在跑（容量闸）",
        )

        # ② 墙钟重叠：屏障本身保证"三方同时在模型调用里"，再用时间戳把它量出来
        with lock:
            spans: dict[str, dict[str, float]] = {label: {} for label in "ABC"}
            for label, phase, at in timeline:
                spans[label][phase] = at
        starts = [spans[label]["start"] for label in "ABC"]
        joineds = [spans[label]["joined"] for label in "ABC"]
        assert max(starts) < min(joineds), (
            "三个会话没有同时在模型调用里（屏障没汇合）："
            + "、".join(f"{label}={spans[label]['start']:.4f}" for label in "ABC")
        )

        # ③ 池满：第 4 个**当场被拒**，且**没有第 4 个线程起来**（前缀是本用例独有的，可数）
        refused = _submit(runtime, fourth, "D 的问题", [])
        asserted_at = time.perf_counter()
        assert refused["ok"] is False
        assert refused["reason"] == "at_capacity"
        assert {item["session_id"] for item in refused["running"]} == set(ids.values())
        assert len(runtime.status()["running"]) == 3
        assert len(_worker_threads("probe-cap")) == 3
        print(
            "[并行证据] Barrier(3) 汇合跨度 "
            f"{min(joineds) - max(starts):.6f}s；"
            "三方同时在跑的可测窗口 "
            f"{asserted_at - max(starts):.3f}s；"
            + "；".join(
                f"{label} start={spans[label]['start'] - min(starts):+.4f}s "
                f"joined={spans[label]['joined'] - min(starts):+.4f}s"
                for label in "ABC"
            )
        )

        release.set()
        results = {
            label: runtime.wait(str(accepted[label]["turn_id"]), timeout=20) for label in "ABC"
        }
        for label in "ABC":
            assert results[label] is not None
            assert results[label].stop_reason == "completed"
            assert results[label].text == f"{label}-正文-1"
        # 完整区间（start→end）的交集也非空：**这一条才是"墙钟上真重叠"**（上面的屏障窗口只是它的下界）
        with lock:
            for label, phase, at in timeline:
                spans[label][phase] = at
        full_starts = [spans[label]["start"] for label in "ABC"]
        ends = [spans[label]["end"] for label in "ABC"]
        assert max(full_starts) < min(ends), "三个会话的墙钟区间没有交集：" + "、".join(
            f"{label}={spans[label]['start']:.4f}~{spans[label]['end']:.4f}" for label in "ABC"
        )
        print(
            f"[并行证据] 三会话墙钟区间交集 {min(ends) - max(full_starts):.3f}s（"
            + "、".join(
                f"{label} {spans[label]['start'] - min(full_starts):+.4f}~"
                f"{spans[label]['end'] - min(full_starts):+.4f}s"
                for label in "ABC"
            )
            + "）"
        )
        # 槽位腾出来之后，第 4 个会话能被正常接受（上限是"同时在跑"，不是"终身配额"）
        again = _submit(runtime, fourth, "D 的问题", [])
        assert again["ok"] is True
        re_result = runtime.wait(str(again["turn_id"]), timeout=10)
        assert re_result is not None
        # 不只是"被接受了"：这一轮要真的跑完（"接受但立刻以 error 收尾"是另一种坏）
        assert re_result.stop_reason == "completed"
        assert re_result.text == "D-正文-1"
    finally:
        release.set()
        with contextlib.suppress(Exception):
            barrier.abort()
        runtime.shutdown()


def test_cancel_touches_only_its_own_session(ctx: AppContext) -> None:
    """取消 A：A 以 `cancelled` 收尾，**B 照常 `completed`**，且没有全局留痕。

    **同步手法：零计时** —— 判据里没有任何"谁先谁后 / 等了多久"，只有事件与屏障；
    每个 `timeout` 都只是**死锁哨兵**（等不到就带着明确原因红，而不是"机器慢一点就红"）。

    - `a_entered` / `b_entered`：两边的第一次模型调用**已经进来**。它们进来后都停在
      `Barrier(2)` 上，主线程不抵达就**走不了** —— 于是"A 被取消时仍在模型调用里"、
      "A 收尾时 B 仍在模型调用里"都是**因果事实**，不是抢时间抢出来的。
    - A 收口靠它自己的 `done_event`（`runtime.wait`），不看时钟。
    - B "还没收尾"用**结果信箱里查不到它**来证：`_results` 只在 `_finish()` 里写
      （`runtime/session_runtime.py:551`），所以这条与调度快慢无关。
    - 断言强度不放宽：A 必须 `cancelled`、B 必须 `completed` 且正文完整、零 `chat.error`、
      A 的正文一个字都不许出现在 B 的事件里。

    旧写法为什么偶发红：它用两个**独立 Event** 约定"先后都入场过"，桩里还嵌了
    `assert release_x.wait(timeout=10)` —— 判据里混进了"机器快慢"这一维。
    """
    _register_echo()
    a_entered = threading.Event()
    b_entered = threading.Event()
    # A 的模型调用 ↔ 主线程（**取消之后**主线程才到第二方，A 才被放行）
    a_hold = threading.Barrier(2)
    # B 的模型调用 ↔ 主线程（**A 收口之后**主线程才到第二方）
    b_hold = threading.Barrier(2)
    calls = {"a": 0, "b": 0}

    def a_first() -> ChatReply:
        calls["a"] += 1
        if calls["a"] == 1:
            a_entered.set()
            a_hold.wait(timeout=30)  # 死锁哨兵：主线程发完取消才会到
        # 回一个工具调用：这样主循环会进第二轮的**步间检查**，取消才有机可乘
        return reply("A-前言", tool_calls=[tool_call(ECHO_TOOL, '{"query":"a"}', call_id="c_a")])

    def b_first() -> ChatReply:
        calls["b"] += 1
        if calls["b"] == 1:
            b_entered.set()
            b_hold.wait(timeout=30)
        return reply("B-正文-1")

    ctx.llm = _ScriptedLlm({"A 的问题": a_first, "B 的问题": b_first})
    sid_a, sid_b = _new_session(ctx), _new_session(ctx)
    events_a: list[Any] = []
    events_b: list[Any] = []
    runtime = SessionRuntime(ctx, max_concurrent=3, thread_prefix="probe-cancel")
    try:
        turn_a = str(_submit(runtime, sid_a, "A 的问题", events_a)["turn_id"])
        turn_b = str(_submit(runtime, sid_b, "B 的问题", events_b)["turn_id"])
        # 两边都进了模型调用，且都停在屏障上（走不了）
        assert a_entered.wait(timeout=30) and b_entered.wait(timeout=30)
        assert {item["session_id"] for item in runtime.status()["running"]} == {sid_a, sid_b}

        # 取消 A：此刻 A 一定还在模型调用里（它卡在 a_hold 上等主线程）
        cancelled = runtime.cancel(session_id=sid_a)
        assert cancelled["ok"] is True
        assert cancelled["turn_id"] == turn_a
        assert ctx.cancelled == set(), "取消走的是每轮自己的 Event，不该再往全局集合里写"
        # 取消只碰 A：B 那一轮还没收尾（结果信箱只在收尾时写）
        assert runtime.result(turn_b) is None, "取消 A 期间 B 不该已经收尾"

        a_hold.wait(timeout=30)  # 放行 A：模型返回 → 工具 → 第二轮开头看到取消 → cancelled
        a_result = runtime.wait(turn_a, timeout=30)
        assert a_result is not None
        assert a_result.stop_reason == "cancelled"

        # B 还在跑：卡在 b_hold 上、结果信箱里没有它、状态里仍挂着它
        status_b = runtime.status(sid_b)
        assert status_b["session"]["running"] is True
        assert [item["turn_id"] for item in status_b["running"]] == [turn_b]
        assert runtime.result(turn_b) is None, "A 收尾不该顺手把 B 也收掉"
        assert runtime.status()["capacity"]["used"] == 1, "A 走了，坑位只剩 B 一个"

        b_hold.wait(timeout=30)  # 放行 B
        b_result = runtime.wait(turn_b, timeout=30)
        assert b_result is not None
        assert b_result.stop_reason == "completed"
        assert b_result.text == "B-正文-1"
        assert [kind for kind, _ in events_b if kind == "chat.error"] == []

        # 不串会话：A 的正文一个字都没落进 B 的帧（docs/20 §7.1 的最小可证伪断言）
        blob_a = json.dumps(events_a, ensure_ascii=False)
        blob_b = json.dumps(events_b, ensure_ascii=False)
        assert "A-前言" in blob_a and "B-正文-1" not in blob_a
        assert "B-正文-1" in blob_b and "A-前言" not in blob_b
        assert all(payload.get("session_id") == sid_a for _, payload in events_a)
        assert all(payload.get("session_id") == sid_b for _, payload in events_b)

        # 已经结束了再取消 → 不假装成功
        after = runtime.cancel(session_id=sid_a)
        assert after["ok"] is False
        assert after["reason"] == "no_running_turn"
    finally:
        # 断掉两个屏障：失败路径上停在屏障里的工作线程必须被放开（BrokenBarrierError 会让
        # 那一轮以 error 收口 —— 在收尾阶段无所谓，不该让它一直挂着）
        a_hold.abort()
        b_hold.abort()
        runtime.shutdown()


def test_cancel_without_a_running_turn_is_not_ok(ctx: AppContext) -> None:
    runtime = SessionRuntime(ctx, max_concurrent=3, thread_prefix="probe-nocancel")
    try:
        outcome = runtime.cancel(session_id="ses_nope")
        assert outcome["ok"] is False
        assert outcome["reason"] == "no_running_turn"
        assert ctx.cancelled == set()
    finally:
        runtime.shutdown()


# --------------------------------------------------------------------- 结果信箱 / 用量


def test_result_mailbox_keeps_only_the_last_n(ctx: AppContext) -> None:
    """结果信箱是**有限**的：挤出去的就如实说取不到（不编一个像结果的东西）。"""
    ctx.llm = _ScriptedLlm({f"问题{i}": (lambda i=i: reply(f"答复{i}")) for i in range(3)})
    session_id = _new_session(ctx)
    runtime = SessionRuntime(ctx, max_concurrent=3, keep_results=1, thread_prefix="probe-mail")
    try:
        turns = []
        for i in range(3):
            accepted = _submit(runtime, session_id, f"问题{i}", [])
            turn_id = str(accepted["turn_id"])
            turns.append(turn_id)
            assert runtime.wait(turn_id, timeout=10) is not None

        assert runtime.result(turns[0]) is None, "只留 1 条：最老的那条必须已经出去"
        assert runtime.result(turns[-1]) is not None
        assert runtime.result("trn_nope") is None
    finally:
        runtime.shutdown()


def test_usage_is_counted_per_session(ctx: AppContext) -> None:
    """用量按会话分账（token / 工具调用次数）：A 的 token 不许算到 B 头上。"""
    ctx.llm = _ScriptedLlm(
        {"A 的问题": lambda: reply("A-正文-1"), "B 的问题": lambda: reply("B-正文-1")}
    )
    sid_a, sid_b = _new_session(ctx), _new_session(ctx)
    runtime = SessionRuntime(ctx, max_concurrent=3, thread_prefix="probe-usage")
    try:
        for text, session_id in (("A 的问题", sid_a), ("B 的问题", sid_b), ("B 的问题", sid_b)):
            accepted = _submit(runtime, session_id, text, [])
            assert runtime.wait(str(accepted["turn_id"]), timeout=10) is not None

        usage_a = runtime.status(sid_a)["session"]["usage"]
        usage_b = runtime.status(sid_b)["session"]["usage"]
        assert usage_a["turns"] == 1
        assert usage_b["turns"] == 2
        assert usage_a["tokens"] == {"prompt": 11, "completion": 7}  # conftest.reply 的固定用量
        assert usage_b["tokens"] == {"prompt": 22, "completion": 14}
        assert usage_a["last_stop_reason"] == usage_b["last_stop_reason"] == "completed"
        assert (
            runtime.status(sid_a)["session"]["usage"]["last_turn_id"] != (usage_b["last_turn_id"])
        )
    finally:
        runtime.shutdown()


def test_reap_idle_releases_idle_units_only(ctx: AppContext) -> None:
    """回收只碰空闲单元；**结果信箱不受影响**（库/信箱才是真源，热缓存可丢）。"""
    gate = threading.Event()
    entered = threading.Event()

    def blocked() -> ChatReply:
        entered.set()
        assert gate.wait(timeout=10)
        return reply("A-正文-1")

    # 先跑一个**不卡**的会话把它的单元做成"空闲"，再用一个卡住的会话证明"在跑的不能被回收"。
    # （早先两次都用了卡住的脚本，于是"先等 B 跑完"这一步永远等不到 —— 是用例自己写错了。）
    ctx.llm = _ScriptedLlm({"A 的问题": blocked, "B 的快速问题": lambda: reply("B-正文-1")})
    sid_a, sid_b = _new_session(ctx), _new_session(ctx)
    runtime = SessionRuntime(ctx, max_concurrent=3, thread_prefix="probe-reap")
    try:
        done = _submit(runtime, sid_b, "B 的快速问题", [])
        turn_b = str(done["turn_id"])
        assert runtime.wait(turn_b, timeout=10) is not None

        running = _submit(runtime, sid_a, "A 的问题", [])
        assert entered.wait(timeout=5)

        assert runtime.reap_idle(idle_s=3600) == 0, "刚活动过的单元不该被回收"
        assert runtime.reap_idle(idle_s=0) == 1, "只该回收到空闲的那一个（在跑的不能动）"
        assert [item["session_id"] for item in runtime.status()["running"]] == [sid_a]
        assert runtime.result(turn_b) is not None, "结果信箱不属于单元，不该被回收"

        gate.set()
        assert runtime.wait(str(running["turn_id"]), timeout=10) is not None
    finally:
        gate.set()
        runtime.shutdown()


# --------------------------------------------------------------------- 关闭


def test_shutdown_does_not_wait_and_stops_accepting(ctx: AppContext) -> None:
    """关窗：`shutdown()` **不 join**、置取消标志、不再接受新轮次，线程摘出退出 join 名单。

    `try/finally` 里**必须**放开那一轮并等它收尾：工作线程是异步的，若断言在它跑完前把用例结束掉，
    夹具会关库，而它还在 `append_message` —— 那是**进程级 access violation**（本机实测），
    会把整个测试套件打崩、看不到任何失败清单。
    """
    gate = threading.Event()
    entered = threading.Event()

    def blocked() -> ChatReply:
        entered.set()
        assert gate.wait(timeout=10)
        return reply("A-正文-1")

    ctx.llm = _ScriptedLlm({"A 的问题": blocked})
    session_id = _new_session(ctx)
    runtime = SessionRuntime(ctx, max_concurrent=3, thread_prefix="probe-stop")
    turn_id = ""
    try:
        accepted = _submit(runtime, session_id, "A 的问题", [])
        turn_id = str(accepted["turn_id"])
        assert entered.wait(timeout=5)

        started = time.monotonic()
        runtime.shutdown()
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"shutdown() 不许等一轮跑完（实测 {elapsed:.2f}s）"
        assert runtime.closed is True

        refused = _submit(runtime, session_id, "A 的问题", [])
        assert refused["ok"] is False
        assert refused["reason"] == "shutdown"

        workers = _worker_threads("probe-stop")
        assert workers, "工作线程仍在跑（shutdown 不 join）—— 它不该已经消失"

        # 两张退出名单都读一遍：**读到了就断言，读不到才 skip**。早先这里按
        # `isinstance(queue_registry, dict)` 判，而标准库里它是 `WeakKeyDictionary`（**不是
        # dict 的子类**）→ 恒 skip，于是「到底摘出去了吗」从来没被验过：假绿比红危险。
        from concurrent.futures import thread as futures_thread

        queue_registry = getattr(futures_thread, "_threads_queues", None)
        shutdown_locks = getattr(threading, "_shutdown_locks", None)
        if not callable(getattr(queue_registry, "pop", None)) or shutdown_locks is None:
            pytest.skip("标准库结构变了：并发退出名单不可观测")
        for thread in workers:
            assert thread not in queue_registry, "工作线程还在并发库的退出 join 名单里"
        for thread in workers:
            assert getattr(thread, "_tstate_lock", None) not in shutdown_locks

        # 取消标志已置：放开被卡住的那一轮，它仍会正常收尾（收尾本身不受 shutdown 影响）
        gate.set()
        result = runtime.wait(turn_id, timeout=10)
        assert result is not None and result.text == "A-正文-1"
    finally:
        gate.set()
        if turn_id:  # 断言失败 / skip 时也要等它跑完再让夹具关库（否则整个进程崩）
            runtime.wait(turn_id, timeout=15)
        runtime.shutdown()


#: 子进程驱动：一轮被模型调用卡住（模拟"这一轮要跑几分钟"），跑完 shutdown 就返回。
#: **这是唯一能真正证明"关窗进程不挂住"的办法** —— 进程内断言不出解释器退出的行为。
_DRIVER = '''\
import sys
import threading

sys.path.insert(0, "src")

from pathlib import Path

from scholarscout.config import Config, Features, Generation, LlmProfile, RuntimeProfile
from scholarscout.runtime.session_runtime import SessionRuntime
from scholarscout.state import sessions
from scholarscout.state.db import Database
from scholarscout.state.migrate import apply_migrations

root = Path(sys.argv[1]).resolve()
db = Database(root / "driver.db")
apply_migrations(db)
config = Config(
    chat=LlmProfile(model="stub-model", base_url="http://localhost:9/v1", temperature=0.0),
    summary=LlmProfile(model="stub-model", base_url="http://localhost:9/v1", temperature=0.0),
    runtime=RuntimeProfile(max_tool_calls_per_turn=1, stream=False),
    features=Features(),
    generation=Generation(title_by_model=False),
    data_dir=root / "data",
    db_path=root / "driver.db",
    raw={},
)


class Blocked:
    """永远等不到（60s 远超测试的等待上限）：这一轮"跑不完"。"""

    def chat(self, messages, *, tools=None):
        del messages, tools
        threading.Event().wait(60)
        raise AssertionError("unreachable")


class Host:
    def __init__(self):
        self.db = db
        self.config = config
        self.http = None

    def require_llm(self):
        return Blocked()


runtime = SessionRuntime(Host(), max_concurrent=3, thread_prefix="driver-turn")
session_id = sessions.create_session(db, model="stub-model")
accepted = runtime.submit_turn(
    session_id=session_id,
    text="这一轮会卡住",
    toolsets=["research"],
    emit=None,
)
print("accepted:", accepted["ok"])
runtime.shutdown()
# 关库前必须确认没有轮次在跑：从**另一个线程** close 一个正被 `append_message` 使用的 SQLite
# 连接是**进程级 access violation**（本机实测 0xC0000005，faulthandler 里能看到 worker 停在
# sessions.append_message 而主线程在 db.close）。产品侧同一条口径写在 `AppContext.close()`，
# 这里照抄 —— 于是"关库的时机"成了一条可执行的约定，而不是注释里的愿望。
if runtime.busy:
    print("busy: 按 AppContext.close() 的口径不关库（进程退出交给 OS 回收）")
else:
    db.close()
print("driver done")
'''


def test_the_process_exits_without_waiting_for_a_blocked_turn(tmp_path: Path) -> None:
    """子进程实测：一轮卡在模型调用里时，关窗后进程必须**立刻**能退（否则用户看到"关不掉"）。

    没有"摘出退出 join 名单"这一步时，实测进程会多活 20s（卡多久就多久）；
    这里给 25s 硬超时 + 15s 断言：既证明结论，也不让失败变成"卡住整个测试套件"。
    """
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")
    root = Path(__file__).resolve().parents[2]

    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, str(driver), str(tmp_path)],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
        )
    except subprocess.TimeoutExpired:  # pragma: no cover - 只有在回归时才会走到
        pytest.fail("子进程 25s 内没退出：工作线程又回到解释器退出的 join 名单里了")
    elapsed = time.monotonic() - started

    assert proc.returncode == 0, proc.stderr
    assert "driver done" in proc.stdout
    assert elapsed < 15.0, f"关窗后进程等了 {elapsed:.1f}s —— 工作线程还在退出 join 名单里"


def test_closing_the_context_does_not_pull_the_db_out_from_under_a_running_turn(
    ctx: AppContext,
) -> None:
    """关窗顺序：**还有轮次在跑时不关库**；跑完再关。

    为什么这条必须有测试：`shutdown()` 不 join，于是关窗那一刻工作线程可能正处在一轮中间。
    从主线程 `db.close()` 掉一个它正在用的连接 = 跨线程 use-after-close = **进程级
    access violation**（本机实测，子进程驱动用例 `_DRIVER` 抓到的那次崩溃就是这个）。
    断言的是"库还能用"（`sqlite3` 关掉后会抛 `ProgrammingError`），不是"代码里写了 if"。
    """
    gate = threading.Event()
    entered = threading.Event()

    def blocked() -> ChatReply:
        entered.set()
        assert gate.wait(timeout=10)
        return reply("A-正文-1")

    ctx.llm = _ScriptedLlm({"A 的问题": blocked})
    session_id = _new_session(ctx)
    runtime = ctx.session_runtime()
    turn_id = ""
    try:
        accepted = runtime.submit_turn(
            session_id=session_id, text="A 的问题", toolsets=["research"], emit=None
        )
        turn_id = str(accepted["turn_id"])
        assert entered.wait(timeout=5)

        ctx.close()  # 一轮在跑 → 只停会话池，**不关**库
        assert runtime.closed is True
        assert ctx.db.query_one("SELECT 1 AS ok")[0] == 1, "库被关掉了：跨线程 use-after-close"

        gate.set()
        assert runtime.wait(turn_id, timeout=10) is not None

        ctx.close()  # 没有在跑的轮次了 → 这时才真的关
        with pytest.raises(sqlite3.ProgrammingError):
            ctx.db.query_one("SELECT 1")
    finally:
        gate.set()
        if turn_id:
            runtime.wait(turn_id, timeout=15)


def test_a_minimal_host_is_enough(config: Config, db: Database) -> None:
    """`runtime/` 只要求宿主提供 4 个成员（Protocol）—— 于是分层规则不破（runtime 不 import rpc）。

    这条钉的是"依赖方向"：`runtime/session_runtime.py` 里没有任何 `scholarscout.rpc` 的 import
    （连 TYPE_CHECKING 里都没有，`tests/test_layering.py` 用 AST 扫得到），
    所以一个只有 db/config/http/require_llm 的普通对象就能当宿主。
    """

    class MinimalHost:
        def __init__(self) -> None:
            self.db = db
            self.config = config
            self.http = None

        def require_llm(self) -> Any:
            raise AssertionError("这个用例不该调模型")

    runtime = SessionRuntime(MinimalHost(), max_concurrent=2, thread_prefix="probe-host")
    try:
        assert runtime.max_concurrent == 2
        assert runtime.status()["capacity"] == {"max": 2, "used": 0}
    finally:
        runtime.shutdown()
