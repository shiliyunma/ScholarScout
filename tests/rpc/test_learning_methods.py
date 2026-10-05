"""M5 的 RPC 方法契约：topic.get / route.get / route.export / note.list / note.get。"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholarscout import paths
from scholarscout.rpc.errors import RpcError
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state.learning import save_note, save_route, save_topic_map
from scholarscout.tools import register_tools

from ..conftest import redirect_home

GRAPH = {
    "nodes": [{"nid": "n1", "label": "RAG", "kind": "method", "source_ids": ["src_a"]}],
    "edges": [],
}
PLAN = {
    "weeks": [
        {
            "idx": 1,
            "goal": "能跑通检索",
            "checkpoint": "能解释每一步",
            "items": [
                {
                    "kind": "doc",
                    "title": "官方指南",
                    "source_id": "src_a",
                    "why": "先跑起来",
                    "est_minutes": 60,
                },
                {
                    "kind": "video",
                    "title": "补充视频",
                    "source_id": None,
                    "why": "清单里没有",
                    "est_minutes": 30,
                },
            ],
        }
    ],
    "mode": "model",
    "topic": "教育智能体",
    "level": "入门",
    "weekly_hours": 6,
}


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_tools()


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return redirect_home(monkeypatch, tmp_path)


def test_topic_get_roundtrip(ctx: AppContext) -> None:
    topic_id = save_topic_map(ctx.db, topic="教育智能体", graph=GRAPH, session_id="ses_x")
    payload = dispatch(ctx, "topic.get", {"topic_id": topic_id})
    assert payload["topic"] == "教育智能体"
    assert payload["graph"]["nodes"][0]["label"] == "RAG"


def test_topic_get_unknown_id_is_not_found(ctx: AppContext) -> None:
    with pytest.raises(RpcError) as exc:
        dispatch(ctx, "topic.get", {"topic_id": "tpm_nope"})
    assert exc.value.code == -32002


def test_route_get_roundtrip(ctx: AppContext) -> None:
    route_id = save_route(ctx.db, topic="教育智能体", plan=PLAN, level="入门", weeks=1)
    payload = dispatch(ctx, "route.get", {"route_id": route_id})
    assert payload["topic"] == "教育智能体"
    assert payload["plan"]["weeks"][0]["items"][0]["source_id"] == "src_a"


def test_route_get_unknown_id_is_not_found(ctx: AppContext) -> None:
    with pytest.raises(RpcError) as exc:
        dispatch(ctx, "route.get", {"route_id": "rte_nope"})
    assert exc.value.code == -32002


def test_route_export_writes_markdown_with_sources(ctx: AppContext) -> None:
    route_id = save_route(ctx.db, topic="教育智能体", plan=PLAN, level="入门", weeks=1)
    result = dispatch(ctx, "route.export", {"route_id": route_id})

    assert result["route_id"] == route_id
    assert result["path"] == f"data/notes/route_{route_id}.md"
    body = (paths.home() / result["path"]).read_text(encoding="utf-8")
    assert "# 学习路线：教育智能体" in body
    assert "第 1 周：能跑通检索" in body
    assert "`src_a`" in body
    assert "（无出处）" in body
    assert "**检查点**：能解释每一步" in body


def test_route_export_respects_custom_path_under_data(ctx: AppContext) -> None:
    route_id = save_route(ctx.db, topic="教育智能体", plan=PLAN, weeks=1)
    result = dispatch(ctx, "route.export", {"route_id": route_id, "out": "notes/我的路线.md"})
    assert result["path"] == "data/notes/我的路线.md"
    assert (paths.home() / result["path"]).is_file()


def test_route_export_refuses_escape(ctx: AppContext) -> None:
    route_id = save_route(ctx.db, topic="教育智能体", plan=PLAN, weeks=1)
    with pytest.raises(ValueError):
        dispatch(ctx, "route.export", {"route_id": route_id, "out": "../逃逸.md"})


def test_note_list_and_get(ctx: AppContext) -> None:
    card_id = save_note(ctx.db, kind="card", title="Q", content="A")
    save_note(ctx.db, kind="note", content="随手记")

    listed = dispatch(ctx, "note.list", {"kind": "card"})["items"]
    assert [row["id"] for row in listed] == [card_id]
    assert len(dispatch(ctx, "note.list", {})["items"]) == 2

    payload = dispatch(ctx, "note.get", {"note_id": card_id})
    assert payload["kind"] == "card" and payload["content"] == "A"


def test_note_get_unknown_id_is_not_found(ctx: AppContext) -> None:
    with pytest.raises(RpcError) as exc:
        dispatch(ctx, "note.get", {"note_id": "not_nope"})
    assert exc.value.code == -32002


def test_topic_list_is_lightweight_and_newest_first(ctx: AppContext) -> None:
    save_topic_map(ctx.db, topic="RAG", graph=GRAPH)
    save_topic_map(
        ctx.db,
        topic="知识追踪",
        graph={**GRAPH, "mode": "flat", "mode_reason": "模型输出空"},
        session_id="ses_x",
    )

    items = dispatch(ctx, "topic.list", {})["items"]
    assert [row["topic"] for row in items] == ["知识追踪", "RAG"], "最新在前"
    first = items[0]
    assert first["nodes"] == 1
    assert first["mode"] == "flat" and first["mode_reason"] == "模型输出空"
    assert first["session_id"] == "ses_x"
    assert "graph" not in first and "graph_json" not in first, "列表要轻量，正文走 topic.get"


def test_route_list_counts_entries_and_respects_limit(ctx: AppContext) -> None:
    for topic in ("A", "B", "C"):
        save_route(ctx.db, topic=topic, plan=PLAN, level="入门", weeks=1)

    items = dispatch(ctx, "route.list", {})["items"]
    assert [row["topic"] for row in items] == ["C", "B", "A"], "最新在前"
    assert items[0]["entries"] == 2, "计划里 2 个 item 要数出来"
    assert items[0]["mode"] == "model"
    assert items[0]["level"] == "入门"
    assert "plan" not in items[0], "列表要轻量"

    limited = dispatch(ctx, "route.list", {"limit": 2})["items"]
    assert [row["topic"] for row in limited] == ["C", "B"]


def test_lists_survive_corrupt_json(ctx: AppContext) -> None:
    """库里出现坏 JSON（手工改过、写一半崩过）时，列表**不能整体炸**。"""
    save_topic_map(ctx.db, topic="正常", graph=GRAPH)
    ctx.db.execute(
        "INSERT INTO topic_maps (id, session_id, topic, graph_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("tpm_bad", None, "坏数据", "{不是 json", "2026-10-05T00:00:00.000000+08:00"),
    )
    save_route(ctx.db, topic="正常路线", plan=PLAN)
    ctx.db.execute(
        "INSERT INTO learning_routes (id, session_id, topic_map_id, topic, level, weeks, "
        "plan_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("rte_bad", None, None, "坏路线", None, None, "[[[", "2026-10-05T00:00:00.000000+08:00"),
    )

    topics = dispatch(ctx, "topic.list", {})["items"]
    assert len(topics) == 2, "坏的那份也要列出来（只是计数为 0），不能整表读不出来"
    assert all(row["nodes"] >= 0 for row in topics)
    routes = dispatch(ctx, "route.list", {})["items"]
    assert len(routes) == 2
    assert all(row["entries"] >= 0 for row in routes)
