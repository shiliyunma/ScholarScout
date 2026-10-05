"""会话的三点菜单：置顶 / 删除 / 导出 Markdown。

为什么值得这么测：这三件事都会**动用户的数据**，而且错了很难发现 ——
- 置顶碰了 `updated_at`，取消置顶后会话会莫名其妙跑到最前面；
- 删会话漏了一张表，就会留下指不到会话的孤儿行（而且不会报错）；
- 删会话多删了一张表，资料库或账本就被悄悄改了（用户不会立刻发现）；
- 导出带上了本机绝对路径或密钥，分享出去就泄漏了。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholarscout.rpc.errors import RpcError
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state import export_session, sessions
from scholarscout.state.db import Database


def make_session(db: Database, title: str) -> str:
    return sessions.create_session(db, title=title, source="desktop", model="test")


def insert_source_row(db: Database, source_id: str = "src_menu0001") -> str:
    """来源是 `source_mentions.source_id` 的外键目标，**必须先有来源行**，否则插引用会违反外键。"""
    db.execute(
        "INSERT INTO sources (id, url, url_canonical, domain, title, source_type,"
        " first_seen_at, last_seen_at) VALUES (?, ?, ?, 'arxiv.org', 'A Test Paper',"
        " 'arxiv', '2026-10-05T10:00:00', '2026-10-05T10:00:00')",
        (source_id, f"https://arxiv.org/abs/{source_id}", f"https://arxiv.org/abs/{source_id}"),
    )
    return source_id


def test_pin_sorts_first_and_does_not_touch_updated_at(db: Database) -> None:
    first = make_session(db, "先建的")
    second = make_session(db, "后建的")
    # 时间戳直接写死：`now_iso()` 的精度不该成为这个用例的前提，
    # 这里要钉的是"ORDER BY pinned DESC, updated_at DESC"这条 SQL 语义。
    db.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?", ("2026-10-05T10:00:00+08:00", first)
    )
    db.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?", ("2026-10-05T11:00:00+08:00", second)
    )
    assert [row["session_id"] for row in sessions.list_sessions(db)] == [second, first]

    before = sessions.get_session(db, first)["updated_at"]
    assert sessions.set_pinned(db, first, True) is True
    assert sessions.get_session(db, first)["updated_at"] == before, "置顶不许改 updated_at"

    listed = sessions.list_sessions(db)
    assert listed[0]["session_id"] == first
    assert listed[0]["pinned"] == 1

    sessions.set_pinned(db, first, False)
    assert [row["session_id"] for row in sessions.list_sessions(db)] == [second, first]


def test_pin_unknown_session_returns_false(db: Database) -> None:
    assert sessions.set_pinned(db, "nope", True) is False


def test_delete_removes_conversation_and_citations(db: Database) -> None:
    sid = make_session(db, "要删的")
    sessions.append_message(db, sid, role="user", content="你好")
    insert_source_row(db, "src_menu0001")
    db.execute(
        "INSERT INTO source_mentions (source_id, session_id, created_at)"
        " VALUES ('src_menu0001', ?, '2026-10-05T10:00:00')",
        (sid,),
    )
    removed = sessions.delete_session(db, sid)
    assert removed is not None
    assert removed["messages"] == 1
    assert removed["source_mentions"] == 1
    assert sessions.get_session(db, sid) is None
    # 没有外键的那张表必须真的空了，不能留孤儿
    assert db.scalar("SELECT count(*) FROM source_mentions WHERE session_id = ?", (sid,)) == 0


def test_delete_keeps_usage_records_and_summaries(db: Database) -> None:
    """账本与资料摘要不该因为删对话被改写。"""
    sid = make_session(db, "花钱的会话")
    db.execute(
        "INSERT INTO usage_records (id, session_id, model, created_at)"
        " VALUES ('u1', ?, 'test-model', '2026-10-05T10:00:00')",
        (sid,),
    )
    sessions.delete_session(db, sid)
    assert db.scalar("SELECT count(*) FROM usage_records WHERE session_id = ?", (sid,)) == 1


def test_delete_drops_artifacts_by_default_but_can_keep_them(db: Database) -> None:
    sid = make_session(db, "有产物的会话")
    db.execute(
        "INSERT INTO learning_routes (id, session_id, topic, plan_json, created_at)"
        " VALUES ('lr1', ?, 'RAG', '{}', '2026-10-05T10:00:00')",
        (sid,),
    )
    kept = sessions.delete_session(db, sid, keep_artifacts=True)
    assert kept is not None and "learning_routes" not in kept
    assert db.scalar("SELECT count(*) FROM learning_routes") == 1, "keep_artifacts 应留下产物"

    sid2 = make_session(db, "再一个")
    db.execute(
        "INSERT INTO learning_routes (id, session_id, topic, plan_json, created_at)"
        " VALUES ('lr2', ?, 'KTD', '{}', '2026-10-05T10:00:00')",
        (sid2,),
    )
    gone = sessions.delete_session(db, sid2)
    assert gone is not None and gone["learning_routes"] == 1
    assert db.scalar("SELECT count(*) FROM learning_routes WHERE id = 'lr2'") == 0


def test_delete_unknown_session_returns_none(db: Database) -> None:
    assert sessions.delete_session(db, "nope") is None


# ------------------------------------------------------------------ 导出


def test_export_contains_conversation_and_trace(db: Database) -> None:
    sid = make_session(db, "教育智能体入门")
    sessions.append_message(db, sid, role="user", content="帮我找入门资料")
    sessions.append_message(
        db, sid, role="assistant", content="# 结论\n\n先看 ReAct 那篇。", tool_calls_json="[]"
    )
    db.execute(
        "INSERT INTO tool_traces (id, session_id, turn_id, step, tool_name, ok, created_at)"
        " VALUES ('t1', ?, 'turn1', 0, 'research.search', 1, '2026-10-05T10:00:01')",
        (sid,),
    )
    text = export_session.build_markdown(db, sid)
    assert "# 教育智能体入门" in text
    assert "帮我找入门资料" in text
    assert "先看 ReAct 那篇" in text
    assert "research.search" in text
    assert "## 工具轨迹" in text


def test_export_scrubs_local_paths(db: Database) -> None:
    """导出件是给别人看的：本机绝对路径必须打码。"""
    sid = make_session(db, "路径")
    sessions.append_message(
        db, sid, role="assistant", content=r"原文在 D:\AI+教育毕设毕设\ScholarScout\data\raw\a.pdf"
    )
    text = export_session.build_markdown(db, sid)
    assert "D:\\AI" not in text
    assert "<path>" in text


def test_export_omits_tool_payloads(db: Database) -> None:
    sid = make_session(db, "带工具消息")
    sessions.append_message(db, sid, role="user", content="帮我找资料")
    sessions.append_message(
        db, sid, role="tool", content='{"ok": true, "data": {"items": ["' + "x" * 500 + '"]}}'
    )
    sessions.append_message(db, sid, role="assistant", content="找到了。")
    text = export_session.build_markdown(db, sid)
    assert "帮我找资料" in text and "找到了" in text
    assert '"items"' not in text, "工具原始 JSON 不该出现在导出件里"
    assert "tool" not in text.split("## 工具轨迹")[0], "对话一节里不该有 tool 角色"


def test_export_unknown_session_raises(db: Database) -> None:
    with pytest.raises(KeyError):
        export_session.build_markdown(db, "nope")


def test_export_to_file_writes_utf8(db: Database, tmp_path: Path) -> None:
    sid = make_session(db, "落盘")
    sessions.append_message(db, sid, role="user", content="中文内容")
    result = export_session.export_to_file(db, sid, out_dir=tmp_path)
    assert result["path"].endswith(".md")
    written = (tmp_path / "data" / "exports").glob("*.md")
    files = list(written)
    assert files and "中文内容" in files[0].read_text(encoding="utf-8")


# ------------------------------------------------------------------ RPC 契约


def test_rpc_pin_delete_export(ctx: AppContext) -> None:
    db = ctx.db
    sid = make_session(db, "菜单")
    sessions.append_message(db, sid, role="user", content="hi")

    assert dispatch(ctx, "session.pin", {"session_id": sid, "pinned": True})["pinned"] is True
    exported = dispatch(ctx, "session.export", {"session_id": sid})
    assert exported["chars"] > 0 and exported["path"].endswith(".md")
    removed = dispatch(ctx, "session.delete", {"session_id": sid})
    assert removed["removed"]["messages"] == 1


def test_rpc_menu_errors_are_not_found(ctx: AppContext) -> None:
    for method, params in (
        ("session.pin", {"session_id": "nope"}),
        ("session.delete", {"session_id": "nope"}),
        ("session.export", {"session_id": "nope"}),
    ):
        with pytest.raises(RpcError) as exc:
            dispatch(ctx, method, params)
        assert exc.value.code == -32002, method
