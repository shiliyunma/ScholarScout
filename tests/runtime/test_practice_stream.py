"""抽背「不会」的流式接线（runtime 层）：``build_help`` 透传 ``on_delta``、断开时保留半句。"""

from __future__ import annotations

from typing import Any

from scholarscout.providers.llm import ChatReply, LlmError
from scholarscout.runtime import practice
from scholarscout.state import learning
from scholarscout.state.db import Database


class _StreamStub:
    """支持流式的桩：逐段回调，返回拼好的完整回复。"""

    def __init__(self, pieces: list[str]) -> None:
        self.pieces = pieces
        self.streamed = False

    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None) -> ChatReply:
        raise AssertionError("给了 on_delta 就该走流式，不该回退到非流式")

    def chat_stream(self, messages: list[dict[str, Any]], *, on_delta: Any = None) -> ChatReply:
        self.streamed = True
        for piece in self.pieces:
            if on_delta is not None:
                on_delta(piece)
        return ChatReply(content="".join(self.pieces), usage={"completion_tokens": 2})


class _PlainStub:
    """只有 chat（不支持流式）—— 验证没有 on_delta 时仍走一次性路径。"""

    def __init__(self, text: str) -> None:
        self.text = text

    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None) -> ChatReply:
        return ChatReply(content=self.text)


class _BrokenStub:
    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None) -> ChatReply:
        raise AssertionError("流式轮不该走非流式")

    def chat_stream(self, messages: list[dict[str, Any]], *, on_delta: Any = None) -> ChatReply:
        text = "先讲到一半"
        if on_delta is not None:
            on_delta(text)
        raise LlmError("network", f"流式响应中断（已收到 {len(text)} 字符）", partial=text)


def _card(db: Database) -> dict[str, str]:
    content = "## Q1. [填空] 中文检索用的 FTS5 分词器是？\n答案：trigram"
    note_id = learning.save_note(db, kind="card", content=content, title="卡组")
    return {**learning.parse_cards(content)[0], "note_id": note_id, "card_index": 1}


def test_build_help_streams_deltas_and_joins_to_the_full_text(db: Database) -> None:
    stub = _StreamStub(["分词器", "是 ", "trigram。"])
    seen: list[str] = []

    outcome = practice.build_help(
        stub, db, session_id=None, card=_card(db), response="不会", on_delta=seen.append
    )

    assert seen == ["分词器", "是 ", "trigram。"], "增量要逐段回调"
    assert outcome["text"] == "".join(seen) == "分词器是 trigram。"
    assert stub.streamed


def test_build_help_without_on_delta_uses_plain_chat(db: Database) -> None:
    outcome = practice.build_help(
        _PlainStub("一次性回答"), db, session_id=None, card=_card(db), response="不会"
    )
    assert outcome["text"] == "一次性回答"
    assert outcome.get("error") is None


def test_build_help_keeps_partial_and_reports_reason_on_midstream_break(db: Database) -> None:
    seen: list[str] = []
    outcome = practice.build_help(
        _BrokenStub(), db, session_id=None, card=_card(db), response="不会", on_delta=seen.append
    )

    assert outcome["text"] == "先讲到一半", "已收到的部分要原样交回"
    assert "network" in str(outcome["error"]), "真实原因要如实带出"
    assert seen == ["先讲到一半"]
    assert outcome["usage"] == {}, "断开时没有 usage 就如实留空，不编数字"
