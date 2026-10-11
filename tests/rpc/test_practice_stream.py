"""``note.help`` 的 ``practice.delta`` 接线（RPC 层）。

钉住：**有 emit 且开了流式**时才把增量发成 ``practice.delta``；没有 emit（CLI/测试）时
仍走一次性返回、不发事件。计费如实：服务端真回了 usage 才记账，断开时不留假数字。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from scholarscout.config import Config
from scholarscout.providers.llm import ChatReply, LlmError
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state import learning
from scholarscout.state.db import Database


class _StreamStub:
    def __init__(self, pieces: list[str]) -> None:
        self.pieces = pieces

    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None) -> ChatReply:
        raise AssertionError("有 on_delta 时不该走非流式")

    def chat_stream(self, messages: list[dict[str, Any]], *, on_delta: Any = None) -> ChatReply:
        for piece in self.pieces:
            if on_delta is not None:
                on_delta(piece)
        return ChatReply(
            content="".join(self.pieces), usage={"prompt_tokens": 3, "completion_tokens": 2}
        )


class _PlainStub:
    def __init__(self, text: str) -> None:
        self.text = text

    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None) -> ChatReply:
        return ChatReply(content=self.text, usage={"completion_tokens": 1})


class _BrokenStub:
    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None) -> ChatReply:
        raise AssertionError("有 on_delta 时不该走非流式")

    def chat_stream(self, messages: list[dict[str, Any]], *, on_delta: Any = None) -> ChatReply:
        text = "讲了半句"
        if on_delta is not None:
            on_delta(text)
        raise LlmError("network", f"流式响应中断（已收到 {len(text)} 字符）", partial=text)


def _ctx(
    config: Config, db: Database, llm: Any, events: list[tuple[str, Any]] | None
) -> AppContext:
    runtime = dataclasses.replace(config.runtime, stream=True)
    emit = None if events is None else (lambda kind, payload: events.append((kind, payload)))
    return AppContext(
        config=dataclasses.replace(config, runtime=runtime), db=db, llm=llm, emit=emit
    )


def _card_note(db: Database) -> str:
    return learning.save_note(
        db, kind="card", content="## Q1. [简答] 什么是 RAG？\n答案：检索增强生成", title="卡组"
    )


def test_note_help_emits_practice_delta_when_streaming(config: Config, db: Database) -> None:
    events: list[tuple[str, Any]] = []
    ctx = _ctx(config, db, _StreamStub(["什么", "是 ", "RAG？"]), events)
    note_id = _card_note(db)

    result = dispatch(ctx, "note.help", {"note_id": note_id, "card_index": 1})

    deltas = [payload for kind, payload in events if kind == "practice.delta"]
    assert [item["delta"] for item in deltas] == ["什么", "是 ", "RAG？"], (
        "增量按顺序发给 practice.delta"
    )
    assert all(item["note_id"] == note_id and item["card_index"] == 1 for item in deltas)
    assert result["text"] == "什么是 RAG？"
    assert result["error"] is None
    # 计费如实：流式回包里真带了 usage 才记账
    row = db.query_one(
        "SELECT prompt_tokens, completion_tokens FROM usage_records WHERE purpose = 'practice_help'"
    )
    assert row is not None and row["prompt_tokens"] == 3 and row["completion_tokens"] == 2


def test_note_help_stays_non_streaming_without_emit(config: Config, db: Database) -> None:
    ctx = _ctx(config, db, _PlainStub("一次性讲解"), None)  # 没有 emit → 不发事件
    note_id = _card_note(db)

    result = dispatch(ctx, "note.help", {"note_id": note_id, "card_index": 1})

    assert result["text"] == "一次性讲解"
    assert result["error"] is None


def test_note_help_keeps_partial_and_reports_reason_on_midstream_break(
    config: Config, db: Database
) -> None:
    events: list[tuple[str, Any]] = []
    ctx = _ctx(config, db, _BrokenStub(), events)
    note_id = _card_note(db)

    result = dispatch(ctx, "note.help", {"note_id": note_id, "card_index": 1})

    assert result["text"] == "讲了半句", "已收到的部分要保留"
    assert "network" in str(result["error"]), "真实原因如实回给界面"
    # 断开时没有 usage → 不写一条假的计费记录
    assert (
        db.query_one("SELECT count(*) AS n FROM usage_records WHERE purpose = 'practice_help'")["n"]
        == 0
    )
