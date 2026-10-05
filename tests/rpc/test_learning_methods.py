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
