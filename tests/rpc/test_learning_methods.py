"""M5 的 RPC 方法契约：topic.get / route.get / route.export / note.list / note.get。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from scholarscout import paths
from scholarscout.rpc.errors import RpcError
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state import source_context
from scholarscout.state.db import Database
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


def test_topic_get_map_normalizes_ids_and_degrees(ctx: AppContext) -> None:
    """`topic.get_map` 是画图用的视图：id/source/target/kind 归一，weight = 度数。"""
    graph = {
        "nodes": [
            {"nid": "a", "label": "RAG", "kind": "method", "source_ids": ["src_a"]},
            {"nid": "b", "label": "检索", "kind": "concept", "source_ids": ["src_a", "src_b"]},
            {"nid": "c", "label": "奇怪类型", "kind": "not-a-kind", "source_ids": []},
        ],
        "edges": [
            {"src": "a", "dst": "b", "rel": "prerequisite"},
            {"src": "b", "dst": "c", "rel": "rel-unknown"},
        ],
        "mode": "model",
        "mode_reason": None,
    }
    topic_id = save_topic_map(ctx.db, topic="教育智能体", graph=graph, session_id="ses_x")
    payload = dispatch(ctx, "topic.get_map", {"topic_id": topic_id})

    assert payload["topic_id"] == topic_id and payload["topic"] == "教育智能体"
    assert payload["mode"] == "model" and payload["session_id"] == "ses_x"
    assert "graph" not in payload, "视图不该把落库原文再带一份"
    assert [node["id"] for node in payload["nodes"]] == ["a", "b", "c"]
    assert {node["id"]: node["weight"] for node in payload["nodes"]} == {"a": 1, "b": 2, "c": 1}
    assert payload["nodes"][0]["sources"] == ["src_a"]
    assert payload["nodes"][2]["kind"] == "concept", "未知 kind 归一成 concept"
    assert [(edge["source"], edge["target"], edge["kind"]) for edge in payload["edges"]] == [
        ("a", "b", "prerequisite"),
        ("b", "c", "uses"),
    ], "未知 rel 归一成 uses"
    assert payload["counts"]["nodes"] == 3 and payload["counts"]["edges"] == 2
    assert payload["counts"]["kinds"] == {"method": 1, "concept": 2}


def test_topic_get_map_drops_dangling_and_self_edges(ctx: AppContext) -> None:
    graph = {
        "nodes": [{"nid": "a", "label": "A", "kind": "concept", "source_ids": []}],
        "edges": [
            {"src": "a", "dst": "a", "rel": "uses"},
            {"src": "a", "dst": "ghost", "rel": "uses"},
        ],
    }
    topic_id = save_topic_map(ctx.db, topic="图", graph=graph)
    payload = dispatch(ctx, "topic.get_map", {"topic_id": topic_id})
    assert payload["edges"] == [], "自环与端点不存在的边都画不出来"
    assert payload["nodes"][0]["weight"] == 0


def test_topic_get_map_unknown_id_is_not_found(ctx: AppContext) -> None:
    with pytest.raises(RpcError) as exc:
        dispatch(ctx, "topic.get_map", {"topic_id": "tpm_nope"})
    assert exc.value.code == -32002


#: 固定时间，与 `tests/conftest.py` 同口径
_T0 = "2026-10-05T10:00:00+08:00"


def _source(
    db: Database,
    source_id: str,
    *,
    title: str | None = "真实标题",
    url: str | None = None,
    domain: str | None = None,
    source_type: str = "arxiv",
    year: int | None = 2025,
) -> str:
    """插一条来源；`title` / `url` / `domain` 给 `None` 就是"库里这项是空的"。"""
    real_url = url if url is not None else f"https://{source_id}.test/a.pdf"
    db.execute(
        "INSERT INTO sources (id, url, url_canonical, domain, title, source_type, year,"
        " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            source_id,
            real_url,
            f"https://canon.test/{source_id}",
            domain if domain is not None else f"{source_id}.test",
            title,
            source_type,
            year,
            _T0,
            _T0,
        ),
    )
    return source_id


def _doc(db: Database, source_id: str) -> str:
    """给来源插一条正文抽取记录（`has_text` 就是按这张表判的）。"""
    doc_id = f"doc_{source_id}"
    db.execute(
        "INSERT INTO documents (id, source_id, sha256, chars, extract_status, fetched_at)"
        " VALUES (?,?,?,?,?,?)",
        (doc_id, source_id, f"sha256-{source_id}", 1234, "ok", _T0),
    )
    return doc_id


def _graph(nodes: dict[str, list[str]]) -> dict[str, object]:
    return {
        "nodes": [
            {"nid": nid, "label": nid.upper(), "kind": "concept", "source_ids": sids}
            for nid, sids in nodes.items()
        ],
        "edges": [],
    }


def _nodes_by_id(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    raw = payload["nodes"]
    assert isinstance(raw, list)
    return {str(node["id"]): node for node in raw}


def test_topic_get_map_source_refs_carry_real_title(ctx: AppContext) -> None:
    """节点详情要能显示真标题：`source_refs` 回的可读字段，不只是 id。"""
    _source(ctx.db, "src_a", title="教育智能体的检索增强方法", year=2024)
    topic_id = save_topic_map(ctx.db, topic="教育智能体", graph=_graph({"n1": ["src_a"]}))

    node = _nodes_by_id(dispatch(ctx, "topic.get_map", {"topic_id": topic_id}))["n1"]
    assert node["source_refs"] == [
        {
            "id": "src_a",
            "title": "教育智能体的检索增强方法",
            "year": 2024,
            "has_text": False,
            "domain": "src_a.test",
            "source_type": "arxiv",
        }
    ]
    assert node["sources"] == ["src_a"], "老字段原样保留（老消费方不破）"
    assert node["dropped_sources"] == 0


def test_topic_get_map_source_title_falls_back_to_url_tail(ctx: AppContext) -> None:
    """没有标题就用 URL 尾段 —— 不能留空，更不能编号成「资料 1」。"""
    _source(ctx.db, "src_a", title=None, url="https://arxiv.org/abs/2501.00001")
    _source(ctx.db, "src_b", title="   ", url="https://openreview.net/forum/AbC123")
    topic_id = save_topic_map(ctx.db, topic="图", graph=_graph({"n1": ["src_a"], "n2": ["src_b"]}))

    nodes = _nodes_by_id(dispatch(ctx, "topic.get_map", {"topic_id": topic_id}))
    assert nodes["n1"]["source_refs"][0]["title"] == "2501.00001"
    assert nodes["n2"]["source_refs"][0]["title"] == "AbC123", "空白标题也算没有标题"


def test_topic_get_map_source_title_falls_back_to_domain(ctx: AppContext) -> None:
    """URL 没有路径段（裸域名/根路径）时退到 `domain`。"""
    _source(ctx.db, "src_a", title=None, url="https://export.arxiv.org/", domain="export.arxiv.org")
    topic_id = save_topic_map(ctx.db, topic="图", graph=_graph({"n1": ["src_a"]}))

    node = _nodes_by_id(dispatch(ctx, "topic.get_map", {"topic_id": topic_id}))["n1"]
    assert node["source_refs"][0]["title"] == "export.arxiv.org"


def test_topic_get_map_source_title_falls_back_to_id_prefix(ctx: AppContext) -> None:
    """标题、URL、域名全空时退到 id 前 12 位：仍然能认出是哪条，且不为空。"""
    _source(ctx.db, "src_0123456789abcdef", title=None, url="", domain="")
    topic_id = save_topic_map(ctx.db, topic="图", graph=_graph({"n1": ["src_0123456789abcdef"]}))

    node = _nodes_by_id(dispatch(ctx, "topic.get_map", {"topic_id": topic_id}))["n1"]
    assert node["source_refs"][0]["title"] == "src_01234567"


def test_topic_get_map_drops_sources_missing_from_library(ctx: AppContext) -> None:
    """库里查不到的 id 只计数、不进 `source_refs` —— 否则用户点了必弹「还没抓取正文」。"""
    _source(ctx.db, "src_a")
    topic_id = save_topic_map(
        ctx.db, topic="图", graph=_graph({"n1": ["src_a", "src_gone", "src_gone2"]})
    )

    node = _nodes_by_id(dispatch(ctx, "topic.get_map", {"topic_id": topic_id}))["n1"]
    assert [ref["id"] for ref in node["source_refs"]] == ["src_a"]
    assert node["dropped_sources"] == 2
    assert node["sources"] == ["src_a", "src_gone", "src_gone2"], "原始 id 列表不裁剪"


def test_topic_get_map_source_refs_are_sorted_by_id(ctx: AppContext) -> None:
    """一个节点多来源时按 id 排序：两次调用、两次打开顺序都不能变。"""
    for source_id in ("src_c", "src_a", "src_b"):
        _source(ctx.db, source_id)
    graph = _graph({"n1": ["src_c", "src_a", "src_b"]})
    topic_id = save_topic_map(ctx.db, topic="图", graph=graph)

    first = _nodes_by_id(dispatch(ctx, "topic.get_map", {"topic_id": topic_id}))["n1"]
    second = _nodes_by_id(dispatch(ctx, "topic.get_map", {"topic_id": topic_id}))["n1"]
    assert [ref["id"] for ref in first["source_refs"]] == ["src_a", "src_b", "src_c"]
    assert first["source_refs"] == second["source_refs"]


def test_topic_get_map_node_without_sources_is_empty(ctx: AppContext) -> None:
    """没来源的节点：`source_refs=[]`、`dropped_sources=0`（不是 0 个来源也报 1 条缺）。"""
    _source(ctx.db, "src_a")
    topic_id = save_topic_map(ctx.db, topic="图", graph=_graph({"n1": ["src_a"], "n2": []}))

    nodes = _nodes_by_id(dispatch(ctx, "topic.get_map", {"topic_id": topic_id}))
    assert nodes["n2"]["source_refs"] == []
    assert nodes["n2"]["dropped_sources"] == 0
    assert nodes["n2"]["sources"] == []


def test_topic_get_map_has_text_separates_full_text_from_metadata(ctx: AppContext) -> None:
    """`has_text` 要能分出"能看全文"和"只有元数据"两种。"""
    _source(ctx.db, "src_a")
    _source(ctx.db, "src_b")
    _doc(ctx.db, "src_a")
    topic_id = save_topic_map(ctx.db, topic="图", graph=_graph({"n1": ["src_a", "src_b"]}))

    refs = _nodes_by_id(dispatch(ctx, "topic.get_map", {"topic_id": topic_id}))["n1"]["source_refs"]
    assert {ref["id"]: ref["has_text"] for ref in refs} == {"src_a": True, "src_b": False}


def test_topic_get_map_queries_sources_once_for_whole_graph(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全图来源**一次**批量查（不许按节点 N+1）。"""
    for source_id in ("src_a", "src_b", "src_c"):
        _source(ctx.db, source_id)
    topic_id = save_topic_map(
        ctx.db,
        topic="图",
        graph=_graph({"n1": ["src_a", "src_b"], "n2": ["src_b", "src_c"]}),
    )

    calls: list[list[str]] = []
    real = source_context.sources_with_text

    def counting(db: Database, ids: Sequence[str]) -> dict[str, dict[str, object]]:
        calls.append([str(item) for item in ids])
        return real(db, ids)

    monkeypatch.setattr(source_context, "sources_with_text", counting)
    nodes = _nodes_by_id(dispatch(ctx, "topic.get_map", {"topic_id": topic_id}))

    assert len(calls) == 1, "两个节点、四个引用，只该有一次查询"
    assert set(calls[0]) == {"src_a", "src_b", "src_c"}, "一次查的是全图合并后的 id 集合"
    assert [ref["id"] for ref in nodes["n2"]["source_refs"]] == ["src_b", "src_c"]


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
