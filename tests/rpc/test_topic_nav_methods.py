"""`topic.find_path` / `topic.search_nodes` 的 RPC 契约（**两图层通用**）。

这一组钉的是"在图上找东西"的口径，与 `test_topic_graph_methods.py`（生长/推演）分开：

- **找不到不是错误**：没有关系就回 `paths:[]`、空查询就回 `nodes:[]` —— 界面据此显示
  "没有这条关系"，而不是弹一个报错；
- **两层同一个形状**：`layer="concept"` 与 `layer="source"` 回的 `paths` / `nodes` 字段一致，
  只是节点 id 从概念 `nid` 变成来源 `source_id`；
- **方向不进契约**：路径按无向连通算，边仍带存储方向（`src -> dst`）—— 断言两边都钉住。
"""

from __future__ import annotations

from typing import Any

import pytest

from scholarscout.rpc.errors import RpcError
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state.ingest import upsert_source
from scholarscout.state.learning import save_topic_map
from scholarscout.tools import register_tools


def _row(url: str, title: str, *, year: int | None = 2025, kind: str = "arxiv") -> dict[str, Any]:
    return {
        "url": url,
        "url_canonical": url,
        "domain": "example.org",
        "title": title,
        "source_type": kind,
        "year": year,
    }


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_tools()


def _graph(a: str, b: str) -> dict[str, Any]:
    """一张有链、有分叉、还有孤立点的概念图。

    n1 - n2 - n3、n2 - n4，n5 谁也不连 —— 最短路径 / 不可达 / 深度三条断言各用其中一段。
    """
    return {
        "topic": "教育智能体",
        "mode": "model",
        "nodes": [
            {"nid": "n1", "label": "RAG", "kind": "method", "source_ids": [a, b]},
            {"nid": "n2", "label": "向量检索", "kind": "method", "source_ids": [a, b]},
            {"nid": "n3", "label": "RAG 重排", "kind": "method", "source_ids": [a]},
            {"nid": "n4", "label": "评测集", "kind": "dataset", "source_ids": [a]},
            {"nid": "n5", "label": "孤立概念", "kind": "concept", "source_ids": []},
        ],
        "edges": [
            {"src": "n1", "dst": "n2", "rel": "prerequisite"},
            {"src": "n2", "dst": "n3", "rel": "uses"},
            {"src": "n2", "dst": "n4", "rel": "evaluates"},
        ],
    }


def _topic(ctx: AppContext) -> tuple[str, str, str, str]:
    """建主题 + 两个来源，回 `(topic_id, a, b, ...)`。"""
    a, _ = upsert_source(ctx.db, _row("https://a.example/abs/1", "A 论文"))
    b, _ = upsert_source(ctx.db, _row("https://b.example/abs/2", "B 论文"))
    topic_id = save_topic_map(ctx.db, topic="教育智能体", graph=_graph(a, b))
    return topic_id, a, b


def test_find_path_returns_the_shortest_chain(ctx: AppContext) -> None:
    """`n1 → n4` 隔两跳：回 `n1,n2,n4` 与两条边（**不从 n3 绕**）。"""
    topic_id, _a, _b = _topic(ctx)
    out = dispatch(ctx, "topic.find_path", {"topic_id": topic_id, "from": "n1", "to": "n4"})

    assert out["layer"] == "concept"
    assert out["from"] == "n1" and out["to"] == "n4"
    assert len(out["paths"]) == 1
    path = out["paths"][0]
    assert path["nodes"] == ["n1", "n2", "n4"]
    assert path["edges"] == [
        {"src": "n1", "dst": "n2", "rel": "prerequisite"},
        {"src": "n2", "dst": "n4", "rel": "evaluates"},
    ]
    assert out["counts"]["paths"] == 1


def test_find_path_walks_connectivity_but_keeps_stored_direction(ctx: AppContext) -> None:
    """反向也能查（`n3 → n1`），但边仍报存储方向：`n2 -> n3` 而不是 `n3 -> n2`。

    这一条是**故意的口径**：`prerequisite`/`uses` 有方向，可用户问的是"有没有关系"。
    方向不写进路径断言，界面照 `src -> dst` 画箭头，免得把箭头方向也编一遍。
    """
    topic_id, _a, _b = _topic(ctx)
    out = dispatch(ctx, "topic.find_path", {"topic_id": topic_id, "from": "n3", "to": "n1"})

    path = out["paths"][0]
    assert path["nodes"] == ["n3", "n2", "n1"]
    # 走的是 n3 → n2，但存储的边是 n2 -> n3（方向如实保留）
    assert path["edges"][0] == {"src": "n2", "dst": "n3", "rel": "uses"}
    assert path["edges"][1] == {"src": "n1", "dst": "n2", "rel": "prerequisite"}


def test_find_path_returns_empty_when_unreachable_or_same_node(ctx: AppContext) -> None:
    """孤立点 → `paths:[]`（不报错）；起点即终点 → 一条零长度路径（如实说"同一个点"）。"""
    topic_id, _a, _b = _topic(ctx)

    isolated = dispatch(ctx, "topic.find_path", {"topic_id": topic_id, "from": "n5", "to": "n1"})
    assert isolated["paths"] == []
    assert isolated["counts"]["paths"] == 0

    missing = dispatch(ctx, "topic.find_path", {"topic_id": topic_id, "from": "nope", "to": "n1"})
    assert missing["paths"] == []

    same = dispatch(ctx, "topic.find_path", {"topic_id": topic_id, "from": "n1", "to": "n1"})
    assert same["paths"] == [{"nodes": ["n1"], "edges": []}]


def test_find_path_max_depth_is_a_hard_gate_but_clamped_not_rejected(ctx: AppContext) -> None:
    """深度就是那条线：`n1 → n3` 要 2 跳，`max_depth=1` 回空；`0` 夹成 1、`999` 夹成 8。"""
    topic_id, _a, _b = _topic(ctx)

    shallow = dispatch(
        ctx, "topic.find_path", {"topic_id": topic_id, "from": "n1", "to": "n3", "max_depth": 1}
    )
    assert shallow["paths"] == []
    assert shallow["max_depth"] == 1

    exact = dispatch(
        ctx, "topic.find_path", {"topic_id": topic_id, "from": "n1", "to": "n3", "max_depth": 2}
    )
    assert exact["paths"][0]["nodes"] == ["n1", "n2", "n3"]

    floored = dispatch(
        ctx, "topic.find_path", {"topic_id": topic_id, "from": "n1", "to": "n3", "max_depth": 0}
    )
    assert floored["max_depth"] == 1 and floored["paths"] == []

    capped = dispatch(
        ctx, "topic.find_path", {"topic_id": topic_id, "from": "n1", "to": "n3", "max_depth": 999}
    )
    assert capped["max_depth"] == 8 and capped["paths"][0]["nodes"] == ["n1", "n2", "n3"]


def test_find_path_switches_to_the_source_layer(ctx: AppContext) -> None:
    """`layer="source"` 查资料图：节点是来源 id、边是 `shared_concept`（带共同概念数）。"""
    topic_id, a, b = _topic(ctx)

    concept = dispatch(ctx, "topic.find_path", {"topic_id": topic_id, "from": a, "to": b})
    assert concept["layer"] == "concept"
    assert concept["paths"] == []  # 来源 id 不是概念节点

    source = dispatch(
        ctx,
        "topic.find_path",
        {"topic_id": topic_id, "from": a, "to": b, "layer": "source"},
    )
    assert source["layer"] == "source"
    assert source["counts"]["nodes"] == 2
    path = source["paths"][0]
    assert path["nodes"] == [a, b]  # a、b 共同支撑 n1+n2 两个概念
    # 端点的先后取决于 `source_id` 字典序（随机串），所以只钉"哪一对 + 关系 + 权重"，
    # 不钉方向 —— 与 `test_source_graph_min_shared_switch` 同一条教训。
    assert len(path["edges"]) == 1
    edge = path["edges"][0]
    assert tuple(sorted((edge["src"], edge["dst"]))) == tuple(sorted((a, b)))
    assert edge["rel"] == "shared_concept"
    assert edge["weight"] == 2


def test_search_nodes_is_case_insensitive_substring(ctx: AppContext) -> None:
    """`Rag` 能搜到 `RAG` 与 `RAG 重排`；前缀命中排在前，回带 `kind` 与 `weight`（度数）。"""
    topic_id, _a, _b = _topic(ctx)
    out = dispatch(ctx, "topic.search_nodes", {"topic_id": topic_id, "q": "Rag"})

    assert out["layer"] == "concept"
    assert out["q"] == "Rag"
    assert [(node["id"], node["label"]) for node in out["nodes"]] == [
        ("n1", "RAG"),
        ("n3", "RAG 重排"),
    ]
    assert {node["kind"] for node in out["nodes"]} == {"method"}
    assert {node["id"]: node["weight"] for node in out["nodes"]} == {"n1": 1, "n3": 1}
    assert out["counts"] == {"nodes": 2}


def test_search_nodes_empty_query_returns_nothing(ctx: AppContext) -> None:
    """空串 / 纯空白 → `nodes:[]`（不是全图）；`limit` 夹取但不报错。"""
    topic_id, _a, _b = _topic(ctx)

    for query in ("", "   "):
        out = dispatch(ctx, "topic.search_nodes", {"topic_id": topic_id, "q": query})
        assert out["nodes"] == []
        assert out["counts"] == {"nodes": 0}

    limited = dispatch(ctx, "topic.search_nodes", {"topic_id": topic_id, "q": "RAG", "limit": 1})
    assert [node["id"] for node in limited["nodes"]] == ["n1"]
    assert limited["limit"] == 1

    capped = dispatch(ctx, "topic.search_nodes", {"topic_id": topic_id, "q": "RAG", "limit": 9999})
    assert capped["limit"] == 100


def test_search_nodes_works_on_the_source_layer(ctx: AppContext) -> None:
    """资料层搜的是来源标题（节点 id 是 `source_id`），两层同一个返回形状。"""
    topic_id, a, b = _topic(ctx)
    out = dispatch(
        ctx, "topic.search_nodes", {"topic_id": topic_id, "q": "论文", "layer": "source"}
    )

    assert out["layer"] == "source"
    assert {node["id"] for node in out["nodes"]} == {a, b}
    assert {node["kind"] for node in out["nodes"]} == {"source"}
    assert all(node["weight"] == 1 for node in out["nodes"])  # a-b 一条边，各 1 度


def test_nav_methods_reject_unknown_topic_bad_layer_and_missing_params(ctx: AppContext) -> None:
    """图不存在 → `-32002`；`layer` 不认识 / 缺 `from` / 缺 `q` → `-32602`（**不静默回退**）。"""
    topic_id, _a, _b = _topic(ctx)

    with pytest.raises(RpcError) as missing_topic:
        dispatch(ctx, "topic.find_path", {"topic_id": "tpm_missing", "from": "n1", "to": "n2"})
    assert missing_topic.value.code == -32002

    with pytest.raises(RpcError) as missing_topic_search:
        dispatch(ctx, "topic.search_nodes", {"topic_id": "tpm_missing", "q": "RAG"})
    assert missing_topic_search.value.code == -32002

    with pytest.raises(RpcError) as bad_layer:
        dispatch(
            ctx,
            "topic.search_nodes",
            {"topic_id": topic_id, "q": "RAG", "layer": "documents"},
        )
    assert bad_layer.value.code == -32602

    with pytest.raises(RpcError) as no_from:
        dispatch(ctx, "topic.find_path", {"topic_id": topic_id, "to": "n2"})
    assert no_from.value.code == -32602

    with pytest.raises(RpcError) as no_query:
        dispatch(ctx, "topic.search_nodes", {"topic_id": topic_id})
    assert no_query.value.code == -32602
