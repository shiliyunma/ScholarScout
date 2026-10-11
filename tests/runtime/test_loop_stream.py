"""主窗口（``run_turn``）的流式接线：最后一轮走 ``chat_stream``，增量经 ``chat.delta`` 实时推出。

为什么单列这个文件：``chat_stream`` 本身有单测，但"循环有没有真的用上它"是**接线**，
接线错了单测全绿也没用（界面照样一次性蹦出整段）。这里用桩模型钉住三条接线：

- 最后一轮（tools=None）确实走 ``chat_stream``，且增量按**累计**正文发 ``chat.delta``；
- 关掉 ``runtime.stream`` 时**一字不变**（走 ``chat``、收尾只发一条最终 delta）；
- 流式中途断开：已收到的部分**保留为答案**，真实原因如实进 ``error``。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Any

import pytest

from scholarscout.config import Config
from scholarscout.providers.llm import ChatReply, LlmError
from scholarscout.rpc.methods import AppContext
from scholarscout.runtime import loop, registry
from scholarscout.state import sessions

from ..conftest import StubLlm, reply


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    registry.clear_registry()
    yield
    registry.clear_registry()


class StreamingStub:
    """支持 ``chat_stream`` 的桩：把 pieces 逐段回调，并记录两套路径各被调了几次。"""

    def __init__(self, pieces: list[str]) -> None:
        self.pieces = pieces
        self.chat_calls = 0
        self.stream_calls = 0

    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None) -> ChatReply:
        self.chat_calls += 1
        return reply("（不该走到非流式）")

    def chat_stream(self, messages: list[dict[str, Any]], *, on_delta: Any = None) -> ChatReply:
        self.stream_calls += 1
        for piece in self.pieces:
            if on_delta is not None:
                on_delta(piece)
        return ChatReply(
            content="".join(self.pieces),
            usage={"prompt_tokens": 5, "completion_tokens": 3},
        )


def _streaming_ctx(config: Config, db: Any) -> AppContext:
    """把 ``runtime.stream`` 打开（测试夹具默认关，以保护既有用例）。"""
    runtime = dataclasses.replace(config.runtime, stream=True)
    return AppContext(config=dataclasses.replace(config, runtime=runtime), db=db)


def test_final_round_streams_and_emits_cumulative_chat_delta(config: Config, db: Any) -> None:
    ctx = _streaming_ctx(config, db)
    ctx.llm = StreamingStub(["第一", "句", "。"])
    session_id = sessions.create_session(db, model=ctx.config.chat.model)
    events: list[tuple[str, dict[str, Any]]] = []

    result = loop.run_turn(
        ctx.turn_context(),
        session_id=session_id,
        text="嗨",
        emit=lambda kind, payload: events.append((kind, payload)),
    )

    stub = ctx.llm
    assert isinstance(stub, StreamingStub)
    assert stub.stream_calls == 1, "最后一轮必须走流式"
    assert stub.chat_calls == 0, "没有工具轮时不该调用非流式 chat"

    deltas = [payload for kind, payload in events if kind == "chat.delta"]
    assert [item["text"] for item in deltas] == ["第一", "第一句", "第一句。"], (
        "chat.delta 要发**累计**正文（前端按替换正文接管）"
    )
    assert [item["seq"] for item in deltas] == [1, 2, 3]
    assert result.text == "第一句。"
    # 收尾只发一次 chat.done，且流式路径不再补发重复的最终 chat.delta
    assert [kind for kind, _ in events].count("chat.done") == 1

    # 会话落盘完整：最终全文必须入库（不能只停在界面）
    rows, _ = sessions.list_messages(db, session_id)
    assert rows[-1]["role"] == "assistant" and rows[-1]["content"] == "第一句。"


def test_stream_off_keeps_the_plain_path_with_one_final_delta(config: Config, db: Any) -> None:
    """``runtime.stream=False``（测试夹具默认）时行为一字不变：走 chat、只发一条最终 delta。"""
    ctx = AppContext(config=config, db=db)  # config.runtime.stream 默认 False
    ctx.llm = StubLlm(reply("完整回答"))
    session_id = sessions.create_session(db, model=config.chat.model)
    events: list[tuple[str, dict[str, Any]]] = []

    result = loop.run_turn(
        ctx.turn_context(),
        session_id=session_id,
        text="嗨",
        emit=lambda kind, payload: events.append((kind, payload)),
    )

    deltas = [payload for kind, payload in events if kind == "chat.delta"]
    assert len(deltas) == 1 and deltas[0]["seq"] == 0 and deltas[0]["text"] == "完整回答"
    assert result.text == "完整回答"


class _PartialFailStub:
    """流式到一半断开：先给一段增量，再抛带 partial 的 LlmError。"""

    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None) -> ChatReply:
        raise AssertionError("流式轮不该走非流式")

    def chat_stream(self, messages: list[dict[str, Any]], *, on_delta: Any = None) -> ChatReply:
        text = "已经讲了半句……"
        if on_delta is not None:
            on_delta(text)
        raise LlmError("network", f"流式响应中断（已收到 {len(text)} 字符）", partial=text)


def test_midstream_break_in_the_loop_keeps_the_partial_as_the_answer(
    config: Config, db: Any
) -> None:
    ctx = _streaming_ctx(config, db)
    ctx.llm = _PartialFailStub()
    session_id = sessions.create_session(db, model=ctx.config.chat.model)
    events: list[tuple[str, dict[str, Any]]] = []

    result = loop.run_turn(
        ctx.turn_context(),
        session_id=session_id,
        text="嗨",
        emit=lambda kind, payload: events.append((kind, payload)),
    )

    assert result.stop_reason == "error"
    assert result.text == "已经讲了半句……", "已收到的部分不能丢"
    assert "network" in str(result.error), "真实原因要如实报出来"
    # 界面已按增量收到过这半句
    deltas = [payload for kind, payload in events if kind == "chat.delta"]
    assert deltas and deltas[-1]["text"] == "已经讲了半句……"
    # 落盘的助手消息也是这半句（会话可回放）
    rows, _ = sessions.list_messages(db, session_id)
    assert rows[-1]["content"] == "已经讲了半句……"
