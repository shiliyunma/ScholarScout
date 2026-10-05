"""方法表行为：会话落库、对话一轮、轨迹与资料库（模型用桩）。"""

from __future__ import annotations

import pytest

from scholarscout.providers.llm import ChatReply, LlmError
from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state import sessions, traces

from ..conftest import StubLlm, insert_document, insert_source, insert_summary, reply, tool_call


class FailingLlm:
    """永远报错的模型桩，用来验证错误收敛。"""

    def __init__(self, kind: str = "auth") -> None:
        self.kind = kind

    def chat(self, messages, *, tools=None):
        raise LlmError(self.kind, "桩模型故意失败")


def _new_session(ctx: AppContext) -> str:
    return dispatch(ctx, "session.create", {})["session_id"]


# --------------------------------------------------------------------- 会话


def test_create_session_writes_system_prompt_hash(ctx: AppContext) -> None:
    session_id = _new_session(ctx)
    row = sessions.get_session(ctx.db, session_id)
    assert row is not None
    assert row["system_prompt_hash"], "会话创建时应记下系统提示词哈希"
    assert row["model"] == ctx.config.chat.model


def test_list_sessions_reports_message_count(ctx: AppContext) -> None:
    session_id = _new_session(ctx)
    ctx.llm = StubLlm(reply("收到"))
    dispatch(ctx, "chat.send", {"session_id": session_id, "text": "你好"})
    items = dispatch(ctx, "session.list", {})["items"]
    assert [i["session_id"] for i in items] == [session_id]
    assert items[0]["message_count"] == 2
    assert items[0]["title"] == "你好"


def test_session_messages_pagination(ctx: AppContext) -> None:
    session_id = _new_session(ctx)
    for i in range(5):
        sessions.append_message(ctx.db, session_id, role="user", content=f"第{i}条")
    page, has_more = sessions.list_messages(ctx.db, session_id, limit=2)
    assert has_more is True
    assert [row["content"] for row in page] == ["第3条", "第4条"]
    older, _ = sessions.list_messages(ctx.db, session_id, limit=2, before_idx=page[0]["idx"])
    assert [row["content"] for row in older] == ["第1条", "第2条"]


def test_session_messages_via_rpc(ctx: AppContext) -> None:
    session_id = _new_session(ctx)
    ctx.llm = StubLlm(reply("答"))
    dispatch(ctx, "chat.send", {"session_id": session_id, "text": "问"})
    payload = dispatch(ctx, "session.messages", {"session_id": session_id})
    assert [row["role"] for row in payload["items"]] == ["user", "assistant"]
    assert payload["has_more"] is False


# --------------------------------------------------------------------- 对话


def test_chat_send_persists_both_messages_and_usage(ctx: AppContext) -> None:
    session_id = _new_session(ctx)
    ctx.llm = StubLlm(reply("这是回答"))
    result = dispatch(ctx, "chat.send", {"session_id": session_id, "text": "这是问题"})

    assert result["stop_reason"] == "completed"
    assert result["text"] == "这是回答"
    assert result["usage"] == {"prompt_tokens": 11, "completion_tokens": 7}
    rows, _ = sessions.list_messages(ctx.db, session_id, limit=10)
    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "这是问题"),
        ("assistant", "这是回答"),
    ]
    assert ctx.db.scalar_int("SELECT count(*) FROM usage_records") == 1


def test_chat_send_passes_system_prompt_and_no_tools(ctx: AppContext) -> None:
    session_id = _new_session(ctx)
    stub = StubLlm(reply("好"))
    ctx.llm = stub
    dispatch(ctx, "chat.send", {"session_id": session_id, "text": "你好"})
    messages = stub.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert "ScholarScout" in str(messages[0]["content"])
    assert stub.calls[0]["tools"] is None, "M2 没有注册工具时不应传 tools 字段"


def test_chat_send_unknown_session(ctx: AppContext) -> None:
    with pytest.raises(RpcError) as info:
        dispatch(ctx, "chat.send", {"session_id": "ses_nope", "text": "嗨"})
    assert info.value.code == Code.NOT_FOUND


def test_chat_send_rejects_blank_text(ctx: AppContext) -> None:
    session_id = _new_session(ctx)
    with pytest.raises(RpcError) as info:
        dispatch(ctx, "chat.send", {"session_id": session_id, "text": "   "})
    assert info.value.code == Code.INVALID_PARAMS


def test_chat_send_model_error_becomes_stop_reason(ctx: AppContext) -> None:
    session_id = _new_session(ctx)
    ctx.llm = FailingLlm("auth")
    result = dispatch(ctx, "chat.send", {"session_id": session_id, "text": "在吗"})
    assert result["stop_reason"] == "error"
    assert "auth" in str(result["error"])


def test_chat_cancel_marks_turn(ctx: AppContext) -> None:
    assert dispatch(ctx, "chat.cancel", {"turn_id": "trn_x"})["ok"] is True
    assert "trn_x" in ctx.cancelled


def test_chat_send_records_clarify_free_flow(ctx: AppContext) -> None:
    """M2 没有工具：一次调用即完成，不应产生 tool 消息。"""
    session_id = _new_session(ctx)
    ctx.llm = StubLlm(reply("没有工具也能聊"))
    dispatch(ctx, "chat.send", {"session_id": session_id, "text": "你好"})
    assert ctx.db.scalar_int("SELECT count(*) FROM messages WHERE role = 'tool'") == 0


# --------------------------------------------------------------------- 轨迹


def test_unregistered_tool_call_is_traced_as_failure(ctx: AppContext) -> None:
    session_id = _new_session(ctx)
    ctx.llm = StubLlm(
        reply("", tool_calls=[tool_call("research.search", '{"query":"教育智能体"}')]),
        reply("工具还没接上，我先解释概念"),
    )
    result = dispatch(ctx, "chat.send", {"session_id": session_id, "text": "找资料"})

    assert result["stop_reason"] == "completed"
    rows = dispatch(ctx, "trace.list", {})["items"]
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "research.search"
    assert rows[0]["ok"] == 0
    assert rows[0]["error"] == "tool_not_registered"
    tool_messages = ctx.db.query("SELECT content FROM messages WHERE role = 'tool'")
    assert "未注册" in str(tool_messages[0]["content"])


def test_trace_digest_is_stable() -> None:
    assert traces.digest({"a": 1, "b": 2}) == traces.digest({"b": 2, "a": 1})
    assert len(traces.digest("x")) == 16


# --------------------------------------------------------------------- 资料库


def test_archive_list_empty(ctx: AppContext) -> None:
    payload = dispatch(ctx, "archive.list", {})
    assert payload == {"items": [], "total": 0}


def test_archive_list_and_paths(ctx: AppContext) -> None:
    insert_source(ctx.db)
    insert_document(ctx.db)
    insert_summary(ctx.db)
    payload = dispatch(ctx, "archive.list", {})
    assert payload["total"] == 1
    assert payload["items"][0]["doc_id"] == "doc_test0001"
    assert payload["items"][0]["title_zh"] == "教育智能体的检索增强方法"

    paths_payload = dispatch(ctx, "archive.paths", {"doc_id": "doc_test0001"})
    assert paths_payload["raw_path"].startswith("data/raw/")
    assert paths_payload["exists"] == {"raw_path": False, "text_path": False}


def test_archive_list_query_uses_like_for_short_query(ctx: AppContext) -> None:
    insert_source(ctx.db)
    insert_document(ctx.db)
    insert_summary(ctx.db)
    payload = dispatch(ctx, "archive.list", {"query": "教育"})
    assert payload["total"] == 1
    assert payload["search_path"] == "like"


def test_archive_list_query_uses_fts_for_long_query(ctx: AppContext) -> None:
    insert_source(ctx.db)
    insert_document(ctx.db)
    insert_summary(ctx.db)
    payload = dispatch(ctx, "archive.list", {"query": "智能体"})
    assert payload["total"] == 1
    assert payload["search_path"] == "fts"


def test_archive_get_reads_text_file(ctx: AppContext, tmp_path) -> None:
    insert_source(ctx.db)
    insert_document(ctx.db)
    insert_summary(ctx.db)
    target = tmp_path / "data" / "text" / "doc_test0001.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("正文在文件里，不在库里", encoding="utf-8")

    from scholarscout import paths as paths_mod

    original = paths_mod.home

    class _FakeHome:
        pass

    monkeypatch_home = lambda: tmp_path  # noqa: E731
    paths_mod.home = monkeypatch_home  # type: ignore[assignment]
    try:
        payload = dispatch(ctx, "archive.get", {"doc_id": "doc_test0001", "text": True})
        assert payload["text"] == "正文在文件里，不在库里"
        assert payload["paths"]["exists"]["text_path"] is True
    finally:
        paths_mod.home = original  # type: ignore[assignment]


def test_archive_get_unknown_doc(ctx: AppContext) -> None:
    with pytest.raises(RpcError) as info:
        dispatch(ctx, "archive.get", {"doc_id": "doc_none"})
    assert info.value.code == Code.NOT_FOUND


def test_chat_reply_type_is_used(ctx: AppContext) -> None:
    """确认 ChatReply 的默认 usage 结构没被改坏（其它用例依赖它）。"""
    assert ChatReply(content="x").usage == {}
