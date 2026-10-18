"""事件信封必须带 `session_id` —— 两个会话交错发事件时**不串会话**（docs/20 §3.1、docs/01 §4）。

为什么必须有这个文件：多会话并行的真机症状是"切到 B 后 A 的回复写进 B 的气泡"，
根因是事件不带 `session_id`，界面无从判断一条 `chat.delta` 属于谁（docs/20 §1.1 B/E）。
只断言"字段在不在"不够 —— 必须断言 **A 的正文一个字都没落进 B 的帧**；
否则"字段写了但值写错"照样绿，而界面照样串会话。

两个会话刻意各持一条连接（同一个库文件、两个 `Database`）：事件分派与存储并发是两件事，
存储写的"先红后绿"由 `tests/state/test_db_concurrent_writes.py` 单独钉 —— 若这里共用一条连接，
写锁落地前这个用例会因为**别的原因**红，就分不清是谁坏了。

**两道屏障，而不是一道（修过一次偶发红）**：只在第一轮模型调用前汇合时，"两轮真并行"只能靠
时间线前两项的**记序**来判 —— 而屏障放行后，最后到达的那个线程是被 `notify_all` 直接唤醒的，
另一个必须再抢一次条件锁。于是只要对端被延迟哪怕 1ms（GC 停顿、OS 调度、整目录跑时堆更大），
**同一会话**就可能连着记下"第一轮 + 收口轮"，用例偶发红（实测 timeline 变成
`['B第一轮', 'B收口轮', 'A第一轮', 'A收口轮']`，而两轮的 6 条事件都在）。那不是产品串行：
两轮确实同时在模型调用处。**现在收口轮的模型调用前也过屏障**，任一方进收口轮都得等另一方
第一轮结束 → 时间线前两项必然是两条不同的"第一轮"，判据不依赖任何计时。

全部用桩模型，不真调模型、不出网。
"""

from __future__ import annotations

import dataclasses
import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from scholarscout.config import Config
from scholarscout.providers.llm import ChatReply
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.rpc.server import NOTIFICATION_METHODS
from scholarscout.runtime import registry
from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.subtasks import run_subtask
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state import learning, sessions
from scholarscout.state.db import Database
from scholarscout.state.migrate import apply_migrations

from ..conftest import reply, tool_call

ECHO_TOOL = "research.echo"
ECHO_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}

#: 一轮带工具的对话至少该产出这四类事件（少了说明这条用例已经不再覆盖真实路径）
TURN_EVENTS = {"chat.delta", "chat.tool_call", "chat.tool_result", "chat.done"}


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    registry.clear_registry()
    yield
    registry.clear_registry()


def _echo(handler_ctx: Any, query: str) -> dict[str, Any]:
    del handler_ctx
    return {"ok": True, "data": {"query": query}, "refs": {"source_ids": [f"src_{query}"]}}


class _InterleavingStub:
    """桩模型：每一轮的**模型调用前**都在屏障上汇合，所以两轮的帧必然交错。

    ``timeline`` 记录每轮的记序（`(label, 阶段)`），供"真的并行了"这条断言 —— 用
    `threading.Barrier` 而不是 `sleep` 赌时序（照 `tests/rpc/test_serve_concurrency.py` 的写法）。
    **每一轮一道屏障**：见模块 docstring —— 只挡第一轮时，收口轮会抢在对端被唤醒之前记下来。
    """

    def __init__(
        self,
        label: str,
        barriers: tuple[threading.Barrier, ...],
        timeline: list[tuple[str, str]],
        lock: threading.Lock,
    ) -> None:
        self.label = label
        self.barriers = barriers
        self.timeline = timeline
        self.lock = lock
        self.rounds = 0

    def _note(self, phase: str) -> None:
        with self.lock:
            self.timeline.append((self.label, phase))

    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None) -> ChatReply:
        raise AssertionError("主循环一律走流式；这个桩只实现 chat_stream")

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Any = None,
        on_delta: Any = None,
    ) -> ChatReply:
        self.rounds += 1
        if self.rounds <= len(self.barriers):
            # 模型调用前汇合：第 1 轮证明"两轮都在跑"，第 2 轮证明"任一方进收口轮时，
            # 另一方第一轮已经结束"。任一方缺席 → 屏障超时 → 用例带着明确原因红。
            self.barriers[self.rounds - 1].wait(timeout=10)
        if self.rounds == 1:
            self._note("第一轮")
            if on_delta is not None:
                on_delta(f"{self.label}-前言")
            return reply(
                f"{self.label}-前言",
                tool_calls=[
                    tool_call(
                        ECHO_TOOL, json.dumps({"query": self.label}), call_id=f"c_{self.label}"
                    )
                ],
            )
        self._note("收口轮")
        for piece in (f"{self.label}-正文", "-1"):
            if on_delta is not None:
                on_delta(piece)
        return reply(f"{self.label}-正文-1")


class _StreamStub:
    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None) -> ChatReply:
        raise AssertionError("有 on_delta 时不该走非流式")

    def chat_stream(self, messages: list[dict[str, Any]], *, on_delta: Any = None) -> ChatReply:
        for piece in ("什么", "是 RAG？"):
            if on_delta is not None:
                on_delta(piece)
        return ChatReply(content="什么是 RAG？", usage={"prompt_tokens": 3, "completion_tokens": 2})


class _PlainStub:
    def __init__(self, text: str) -> None:
        self.text = text

    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None) -> ChatReply:
        return ChatReply(content=self.text, usage={"completion_tokens": 1})


def _ctx_with_events(
    config: Config,
    db: Database,
    llm: Any,
    events: list[tuple[str, dict[str, Any]]],
) -> AppContext:
    runtime = dataclasses.replace(config.runtime, stream=True)
    return AppContext(
        config=dataclasses.replace(config, runtime=runtime),
        db=db,
        llm=llm,
        emit=lambda kind, payload: events.append((kind, payload)),
    )


def test_two_interleaved_sessions_never_cross_events(
    config: Config, db: Database, tmp_path
) -> None:
    """两个会话同时在跑：每条事件都带自己的 `session_id`，A 的正文在 B 的帧里 0 命中。"""
    registry.register(
        ToolSpec(
            name=ECHO_TOOL,
            description="回显查询",
            parameters=ECHO_SCHEMA,
            handler=_echo,
            toolset="research",
        )
    )
    other = Database(tmp_path / "events_other.db")
    apply_migrations(other)
    try:
        session_a = sessions.create_session(db, model=config.chat.model)
        session_b = sessions.create_session(other, model=config.chat.model)
        barrier_round1 = threading.Barrier(2)
        barrier_round2 = threading.Barrier(2)
        barriers = (barrier_round1, barrier_round2)
        timeline: list[tuple[str, str]] = []
        lock = threading.Lock()
        events_a: list[tuple[str, dict[str, Any]]] = []
        events_b: list[tuple[str, dict[str, Any]]] = []
        ctx_a = _ctx_with_events(
            config, db, _InterleavingStub("A", barriers, timeline, lock), events_a
        )
        ctx_b = _ctx_with_events(
            config, other, _InterleavingStub("B", barriers, timeline, lock), events_b
        )

        errors: list[str] = []

        def run(ctx: AppContext, session_id: str, text: str) -> None:
            try:
                dispatch(ctx, "chat.send", {"session_id": session_id, "text": text})
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [
            threading.Thread(target=run, args=(ctx_a, session_a, "A 的问题")),
            threading.Thread(target=run, args=(ctx_b, session_b, "B 的问题")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(thread.is_alive() for thread in threads), "轮次没跑完（barrier 超时？）"
        assert errors == [], f"轮次里抛异常了：{errors}"

        # ① 信封：每条通知的 params.session_id 都非空，且等于发起它的那个会话
        assert events_a and events_b, "两个会话都该有事件"
        for kind, payload in events_a:
            assert payload.get("session_id") == session_a, f"{kind} 的 session_id 不对：{payload}"
        for kind, payload in events_b:
            assert payload.get("session_id") == session_b, f"{kind} 的 session_id 不对：{payload}"

        # ② 不串会话：A 的正文标记不许出现在 B 的任何一帧里（反过来也一样）
        blob_a = json.dumps(events_a, ensure_ascii=False)
        blob_b = json.dumps(events_b, ensure_ascii=False)
        assert "B-前言" not in blob_a and "B-正文-1" not in blob_a, "B 的正文串进了 A 的帧"
        assert "A-前言" not in blob_b and "A-正文-1" not in blob_b, "A 的正文串进了 B 的帧"

        # ③ turn_id 与会话一一对应（一个会话一轮只该有一个 turn_id）
        turns_a = {payload["turn_id"] for _, payload in events_a if "turn_id" in payload}
        turns_b = {payload["turn_id"] for _, payload in events_b if "turn_id" in payload}
        assert len(turns_a) == 1 and len(turns_b) == 1, "一个会话的这一轮只该有一个 turn_id"
        assert turns_a.isdisjoint(turns_b), "两个会话的 turn_id 不能撞"

        # ④ 覆盖度与契约：这一轮真的产出了这些事件，且全部登记在 NOTIFICATION_METHODS 里
        methods_a = {kind for kind, _ in events_a}
        assert methods_a >= TURN_EVENTS, f"这条用例已经不再覆盖真实路径：{sorted(methods_a)}"
        assert (methods_a | {kind for kind, _ in events_b}) <= set(NOTIFICATION_METHODS)

        # ⑤ 真的交错：时间线的前两项必须是**两个不同会话**的"第一轮"，且各只有一条收口轮。
        # 为什么这条现在零计时依赖：两道屏障都堵在**模型调用前** —— 任一方要进收口轮，都得等
        # 另一方第一轮已经结束，所以"A第一轮 / A收口轮 / B第一轮"这种顺序在结构上不可能。
        # 产品若真把两轮串起来（或屏障位置挪到了写库路径上），屏障根本不会放行 → 上面的
        # BrokenBarrierError / 线程未结束会先红，而不是在这里变成一句"没真并行"。
        phases = [phase for _, phase in timeline]
        assert len(timeline) == 4 and (phases.count("第一轮"), phases.count("收口轮")) == (2, 2), (
            f"两轮没跑满（屏障超时？）：{timeline}"
        )
        assert phases[:2] == ["第一轮", "第一轮"] and {label for label, _ in timeline[:2]} == {
            "A",
            "B",
        }, f"两轮没有真并行：一个会话在另一个进第一轮前就跑到了收口轮 {timeline}"
    finally:
        other.close()


def test_practice_delta_carries_session_id(config: Config, db: Database) -> None:
    """抽背的流式增量也要能分派回会话（它是会话里的动作，不是全局事件）。"""
    events: list[tuple[str, dict[str, Any]]] = []
    session_id = sessions.create_session(db, model=config.chat.model)
    ctx = _ctx_with_events(config, db, _StreamStub(), events)
    note_id = learning.save_note(
        db,
        kind="card",
        content="## Q1. [简答] 什么是 RAG？\n答案：检索增强生成",
        title="卡组",
        session_id=session_id,
    )

    dispatch(ctx, "note.help", {"note_id": note_id, "card_index": 1})

    deltas = [payload for kind, payload in events if kind == "practice.delta"]
    assert deltas, "开了流式却没发 practice.delta"
    assert all(payload.get("session_id") == session_id for payload in deltas), (
        "practice.delta 必须带上抽背所属会话（界面按它归位，否则增量会写进当前显示的会话）"
    )


def test_subtask_events_carry_session_id(config: Config, db: Database) -> None:
    """子任务事件（`chat.subtask_*`）同样带 `session_id` —— 直接拿 ToolContext 起子任务也不能漏。

    `emit=tool_ctx.emit` 与真实调用同形（`tools/delegate.py` 就是这么传的）。
    """
    events: list[tuple[str, dict[str, Any]]] = []
    runtime = dataclasses.replace(config.runtime, subtask_enabled=True)
    tool_ctx = ToolContext(
        db=db,
        config=dataclasses.replace(config, runtime=runtime),
        llm=_PlainStub("子任务结论"),
        session_id="ses_subtask01",
        turn_id="trn_subtask01",
        emit=lambda kind, payload: events.append((kind, payload)),
    )

    run_subtask(tool_ctx, "查一下 DKT 的核心论文", emit=tool_ctx.emit)

    kinds = {kind for kind, _ in events}
    assert {"chat.subtask_start", "chat.subtask_done"} <= kinds, f"子任务生命周期事件没发：{kinds}"
    for kind, payload in events:
        assert payload.get("session_id") == "ses_subtask01", f"{kind} 少了 session_id：{payload}"
        assert payload.get("turn_id") == "trn_subtask01", f"{kind} 的 turn_id 不对：{payload}"


def test_bind_emitter_is_a_noop_without_emit_or_session() -> None:
    """没有事件通道 / 没有会话时**不伪造**信封 —— 否则调用点会以为有通道，或以为字段齐了。"""
    from scholarscout.runtime.loop import bind_emitter

    assert bind_emitter(None, "ses_x") is None
    seen: list[tuple[str, dict[str, Any]]] = []
    plain = lambda kind, payload: seen.append((kind, payload))  # noqa: E731
    assert bind_emitter(plain, "") is plain
    bound = bind_emitter(plain, "ses_x")
    assert bound is not None
    bound("chat.delta", {"turn_id": "trn_1", "text": "正文", "session_id": "假的"})
    assert seen == [("chat.delta", {"turn_id": "trn_1", "text": "正文", "session_id": "ses_x"})], (
        "绑定值必须覆盖调用点写错的 session_id"
    )
