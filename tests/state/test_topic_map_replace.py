"""`replace_topic_map_graph`：原地替换整份图的两道保险（先序列化 / 单事务 UPDATE）。

为什么单开一个文件：这是本轮唯一一处**会覆盖用户已有数据**的写路径，"失败不丢图"必须
有独立的用例钉住 —— 混在 RPC 用例里，一旦别处断言先失败，这条最要紧的保证就没被验证。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from scholarscout.state.db import Database
from scholarscout.state.learning import (
    get_topic_map,
    replace_topic_map_graph,
    save_topic_map,
)

OLD_GRAPH = {
    "topic": "教育智能体",
    "nodes": [{"nid": "n1", "label": "RAG", "kind": "method", "source_ids": ["src_a"]}],
    "edges": [],
}


def test_replace_keeps_topic_id_and_swaps_the_graph(db: Database) -> None:
    """替换后 `topic_id` 不变（外键还指着同一行），读回来是新图。"""
    topic_id = save_topic_map(db, topic="教育智能体", graph=OLD_GRAPH)
    new_graph = {
        "topic": "教育智能体",
        "nodes": [
            *OLD_GRAPH["nodes"],
            {"nid": "n2", "label": "重排", "kind": "method", "source_ids": ["src_b"]},
        ],
        "edges": [{"src": "n2", "dst": "n1", "rel": "prerequisite"}],
    }

    assert replace_topic_map_graph(db, topic_id=topic_id, graph=new_graph) is True

    saved = get_topic_map(db, topic_id)
    assert saved is not None and saved["id"] == topic_id
    assert [node["nid"] for node in saved["graph"]["nodes"]] == ["n1", "n2"]
    assert saved["graph"]["edges"] == [{"src": "n2", "dst": "n1", "rel": "prerequisite"}]


def test_unserialisable_graph_raises_and_old_graph_survives(db: Database) -> None:
    """图里混进不可 JSON 化的东西：异常在**写之前**抛出，旧图一个字节都没变。"""
    topic_id = save_topic_map(db, topic="教育智能体", graph=OLD_GRAPH)
    before = json.dumps(get_topic_map(db, topic_id)["graph"], ensure_ascii=False)

    bad_graph: dict[str, Any] = {"nodes": [], "extra": {1, 2}}
    with pytest.raises(TypeError):
        replace_topic_map_graph(db, topic_id=topic_id, graph=bad_graph)

    after = json.dumps(get_topic_map(db, topic_id)["graph"], ensure_ascii=False)
    assert after == before
    assert get_topic_map(db, topic_id)["graph"]["nodes"][0]["nid"] == "n1"


def test_replace_missing_row_returns_false(db: Database) -> None:
    """行不存在回 `False`（不静默新建 —— 生长只作用于已有图）。"""
    assert replace_topic_map_graph(db, topic_id="tpm_missing", graph=OLD_GRAPH) is False
    assert db.scalar_int("SELECT count(*) FROM topic_maps") == 0
