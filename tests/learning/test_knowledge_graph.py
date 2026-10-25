"""知识地图数据层的纯函数：增量合并（幂等 / 单调）与资料层推导（共同支撑阈值）。

这一组用例**不碰数据库**：`merge_concept_graph` / `build_source_graph` 是纯函数，
用字典就能钉住口径（与 `test_srs.py` / `test_route_patch.py` 同一做法）。
"""

from __future__ import annotations

import json
from typing import Any

from scholarscout.learning import knowledge_graph


def _graph() -> dict[str, Any]:
    """一张两节点的旧图：n1 只挂 src_a，n2 挂 src_a + src_b。"""
    return {
        "topic": "教育智能体",
        "mode": "model",
        "nodes": [
            {"nid": "n1", "label": "RAG", "kind": "method", "source_ids": ["src_a"]},
            {
                "nid": "n2",
                "label": "教育智能体",
                "kind": "concept",
                "source_ids": ["src_a", "src_b"],
            },
        ],
        "edges": [{"src": "n1", "dst": "n2", "rel": "prerequisite"}],
    }


def _patch(nodes: list[dict[str, Any]], edges: list[dict[str, Any]] | None = None) -> dict:
    return {"nodes": nodes, "edges": edges or []}


def test_merge_is_idempotent_for_the_same_batch() -> None:
    """同一批来源应用两次：第二次 `changed=False`，图逐字节相同（幂等）。"""
    first = knowledge_graph.merge_concept_graph(
        _graph(),
        _patch([{"nid": "n9", "label": "检索重排", "kind": "method", "source_ids": ["src_c"]}]),
    )
    assert first.changed is True
    second = knowledge_graph.merge_concept_graph(
        first.graph,
        _patch([{"nid": "n9", "label": "检索重排", "kind": "method", "source_ids": ["src_c"]}]),
    )
    assert second.changed is False
    assert second.reason and "已经在图里" in second.reason
    assert json.dumps(second.graph, ensure_ascii=False) == json.dumps(
        first.graph, ensure_ascii=False
    )


def test_merge_only_touches_affected_nodes() -> None:
    """新来源让**同一标签**的节点变胖，别的节点一个字段都不动、`nid` 不变。"""
    outcome = knowledge_graph.merge_concept_graph(
        _graph(),
        _patch(
            [
                {"nid": "m1", "label": "RAG", "kind": "method", "source_ids": ["src_c"]},
                {"nid": "m2", "label": "评测集", "kind": "dataset", "source_ids": ["src_c"]},
            ],
            [{"src": "m2", "dst": "m1", "rel": "evaluates"}],
        ),
    )
    nodes = {node["nid"]: node for node in outcome.graph["nodes"]}
    # 标签相同的 n1 被并上 src_c（模型给的新 id m1 不能顶掉旧 id）
    assert nodes["n1"]["source_ids"] == ["src_a", "src_c"]
    assert "m1" not in nodes
    # 没被点到的 n2 原样保留（只补了 `dropped_source_ids` 这个空列表，与守卫层同形状）
    assert nodes["n2"] == {**_graph()["nodes"][1], "dropped_source_ids": []}
    assert outcome.updated == ("n1",)
    assert len(outcome.added) == 1
    assert nodes[outcome.added[0]]["label"] == "评测集"
    # 新边两端解析到最终 id（m2 → n1）
    assert {"src": outcome.added[0], "dst": "n1", "rel": "evaluates"} in outcome.graph["edges"]
    assert outcome.added_edges == 1


def test_merge_never_drops_existing_nodes_or_edges() -> None:
    """生长是单调的：新补丁里**没有**旧边/旧节点，旧图也不能少东西。"""
    outcome = knowledge_graph.merge_concept_graph(
        _graph(),
        _patch([{"nid": "n7", "label": "新概念", "kind": "concept", "source_ids": ["src_c"]}]),
    )
    assert [node["nid"] for node in outcome.graph["nodes"]][:2] == ["n1", "n2"]
    assert {"src": "n1", "dst": "n2", "rel": "prerequisite"} in outcome.graph["edges"]
    assert outcome.graph["applied_source_ids"] == ["src_a", "src_b", "src_c"]


def test_merge_treats_legacy_graph_as_already_applied() -> None:
    """老图没有水位标记：已经挂在节点上的来源**不算新来源**（不白调一次模型）。"""
    assert knowledge_graph.applied_source_ids(_graph()) == {"src_a", "src_b"}
    outcome = knowledge_graph.merge_concept_graph(
        _graph(),
        _patch([{"nid": "n1", "label": "RAG", "kind": "method", "source_ids": ["src_b"]}]),
    )
    assert outcome.changed is False
    assert outcome.new_source_ids == ()


def test_merge_caps_total_nodes_without_dropping_existing() -> None:
    """到上限时只截**新加的尾巴**：旧节点一条不删（宁可少长，不许把图弄丢）。"""
    existing = {
        "nodes": [
            {"nid": f"n{i}", "label": f"概念{i}", "kind": "concept", "source_ids": ["src_a"]}
            for i in range(1, 4)
        ],
        "edges": [],
    }
    incoming = _patch(
        [
            {"nid": f"x{i}", "label": f"新增{i}", "kind": "concept", "source_ids": [f"src_x{i}"]}
            for i in range(1, 4)
        ]
    )
    outcome = knowledge_graph.merge_concept_graph(existing, incoming, max_nodes=4)
    assert [node["nid"] for node in outcome.graph["nodes"]] == ["n1", "n2", "n3", "x1"]
    assert outcome.dropped_nodes == 2
    assert len(outcome.added) == 1


def test_merge_handles_empty_inputs() -> None:
    """空图 + 空补丁：不崩、不改、`changed=False`（新主题刚建图时的形状）。"""
    outcome = knowledge_graph.merge_concept_graph(None, None)
    assert outcome.changed is False
    assert outcome.graph["nodes"] == []
    assert outcome.graph["edges"] == []


def test_source_graph_threshold_changes_edges() -> None:
    """阈值就是那条线：`a-b` 只共享 1 个概念、`a-c` 共享 2 个。

    `min_shared=1` 两条都连；默认 2 只连 `a-c`（"只是都提过 RAG"不足以说明两篇在讲同一件事）。
    """
    concept = {
        "nodes": [
            {"nid": "n1", "label": "RAG", "kind": "method", "source_ids": ["src_a", "src_b"]},
            {"nid": "n2", "label": "重排", "kind": "method", "source_ids": ["src_a", "src_c"]},
            {"nid": "n3", "label": "评测", "kind": "dataset", "source_ids": ["src_a", "src_c"]},
        ],
        "edges": [],
    }
    rows = {
        "src_a": {"title": "A 论文", "url": "https://a.example/abs/1", "source_type": "arxiv"},
        "src_b": {"title": "", "url": "https://b.example/docs/guide", "source_type": "docs"},
        "src_c": {"title": "C 论文", "url": "https://c.example/abs/3", "source_type": "arxiv"},
    }
    loose = knowledge_graph.build_source_graph(concept, rows, min_shared=1)
    strict = knowledge_graph.build_source_graph(concept, rows, min_shared=2)

    assert loose["counts"]["edges"] == 2
    assert loose["edges"][0] == {
        "src": "src_a",
        "dst": "src_c",
        "rel": "shared_concept",
        "weight": 2,
    }
    assert [(edge["src"], edge["dst"], edge["weight"]) for edge in strict["edges"]] == [
        ("src_a", "src_c", 2)
    ]
    assert strict["min_shared"] == 2
    assert [node["nid"] for node in strict["nodes"]] == ["src_a", "src_b", "src_c"]
    assert strict["nodes"][1]["label"] == "guide"  # 无标题 → URL 尾段


def test_source_graph_weight_counts_shared_concepts() -> None:
    """权重 = 共同概念数（不是"有没有边"），阈值 2 时 2 个共同概念的边权重是 2。"""
    concept = {
        "nodes": [
            {"nid": "n1", "label": "A", "kind": "concept", "source_ids": ["src_a", "src_b"]},
            {"nid": "n2", "label": "B", "kind": "concept", "source_ids": ["src_a", "src_b"]},
        ]
    }
    built = knowledge_graph.build_source_graph(concept, {}, min_shared=2)
    assert built["edges"] == [
        {"src": "src_a", "dst": "src_b", "rel": "shared_concept", "weight": 2}
    ]
    # 库里没有这两条来源 → 节点保留但标 missing（图不该因为删了来源就少一个点）
    assert all(node["meta"]["missing"] is True for node in built["nodes"])


def test_source_graph_single_source_has_no_self_loop() -> None:
    """只有一个来源：节点 1 个、边 0 条（绝不给自己连一条自环）。"""
    concept = {"nodes": [{"nid": "n1", "label": "RAG", "kind": "method", "source_ids": ["src_a"]}]}
    built = knowledge_graph.build_source_graph(concept, {"src_a": {"title": "A"}}, min_shared=1)
    assert len(built["nodes"]) == 1
    assert built["nodes"][0]["label"] == "A"
    assert built["edges"] == []


def test_source_graph_normalises_missing_title_and_year() -> None:
    """无标题用 URL 尾段，无年份留 None（不编 0、不编空串）。"""
    concept = {
        "nodes": [
            {"nid": "n1", "label": "X", "kind": "concept", "source_ids": ["src_a", "src_b"]},
            {"nid": "n2", "label": "Y", "kind": "concept", "source_ids": ["src_a", "src_b"]},
        ]
    }
    rows = {
        "src_a": {"title": "", "url": "https://docs.example.org/guide/", "source_type": "docs"},
        "src_b": {"title": "B", "url": "https://b.example/x", "source_type": "arxiv", "year": None},
    }
    built = knowledge_graph.build_source_graph(concept, rows, min_shared=2)
    nodes = {node["nid"]: node for node in built["nodes"]}
    assert nodes["src_a"]["label"] == "guide"
    assert nodes["src_a"]["meta"]["year"] is None
    assert nodes["src_b"]["meta"]["year"] is None
    assert nodes["src_b"]["meta"]["source_type"] == "arxiv"
