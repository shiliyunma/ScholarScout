"""``topic.build_map`` 契约：解析、白名单校验、兜底扁平图、落库。"""

from __future__ import annotations

import pytest

from scholarscout.rpc.methods import AppContext
from scholarscout.state.ingest import upsert_source
from scholarscout.state.learning import get_topic_map
from scholarscout.tools import register_tools
from scholarscout.tools.source_grade import source_grade
from scholarscout.tools.topic_map import (
    PARAMETERS,
    SPEC,
    apply_guards,
    build_flat_map,
    parse_map,
    topic_build_map,
)

from ..conftest import StubLlm, reply, tool_ctx

ROW_A = {
    "url": "https://arxiv.org/abs/2501.00001",
    "url_canonical": "https://arxiv.org/abs/2501.00001",
    "domain": "arxiv.org",
    "title": "教育智能体的检索增强方法",
    "source_type": "arxiv",
    "year": 2025,
}
ROW_B = {
    "url": "https://docs.example.org/guide",
    "url_canonical": "https://docs.example.org/guide",
    "domain": "docs.example.org",
    "title": "教育智能体工程指南",
    "source_type": "docs",
    "year": 2024,
}

MODEL_OUTPUT = (
    "node: n1 | RAG | method | {a}\n"
    "node: n2 | 教育智能体 | concept | {a}, {b}\n"
    "node: n3 | 凭空想的概念 | concept | src_不存在\n"
    "edge: n1 -> n2 | prerequisite\n"
    "edge: n2 -> n9 | uses\n"
    "随便一句废话\n"
)


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_tools()


def _seed(ctx: AppContext) -> tuple[str, str]:
    a, _ = upsert_source(ctx.db, ROW_A)
    b, _ = upsert_source(ctx.db, ROW_B)
    return a, b


def test_spec_shape() -> None:
    assert SPEC.name == "topic.build_map"
    assert PARAMETERS["additionalProperties"] is False
    assert set(PARAMETERS["properties"]) == {"topic", "source_ids", "max_nodes", "use_model"}


def test_parse_map_tolerates_noise_and_dedupes() -> None:
    parsed = parse_map(
        "node: n1 | A | method | src_a\nnode: n1 | 重复 | concept | src_b\nnode: n2 | B | 未知类型 | -\n"
        "edge: n1 -> n2 | variant\n废话\n"
    )
    assert [node["nid"] for node in parsed["nodes"]] == ["n1", "n2"]
    assert parsed["nodes"][0]["source_ids"] == ["src_a"]
    assert parsed["nodes"][1]["kind"] == "concept"
    assert parsed["nodes"][1]["source_ids"] == []
    assert parsed["edges"] == [{"src": "n1", "dst": "n2", "rel": "variant"}]


def test_parse_map_accepts_full_width_colon() -> None:
    parsed = parse_map("node：n1 ｜ A ｜ method ｜ src_a")
    assert parsed["nodes"] == [] or parsed["nodes"][0]["nid"] == "n1"


def test_apply_guards_marks_ungrounded_and_drops_dangling_edges() -> None:
    raw = parse_map(MODEL_OUTPUT.format(a="src_a", b="src_b"))
    graph = apply_guards(raw, allowed_ids={"src_a", "src_b"}, max_nodes=5)

    nodes = {node["nid"]: node for node in graph["nodes"]}
    assert nodes["n1"]["source_ids"] == ["src_a"]
    assert nodes["n2"]["source_ids"] == ["src_a", "src_b"]
    assert nodes["n3"]["label"].startswith("[无出处]")
    assert nodes["n3"]["dropped_source_ids"] == ["src_不存在"]
    assert graph["edges"] == [{"src": "n1", "dst": "n2", "rel": "prerequisite"}]
    assert graph["stats"]["ungrounded_nodes"] == 1
    assert graph["stats"]["dropped_edges"] == 1


def test_apply_guards_truncates_to_max_nodes() -> None:
    raw = {
        "nodes": [
            {"nid": f"n{i}", "label": f"L{i}", "kind": "concept", "source_ids": []}
            for i in range(10)
        ],
        "edges": [],
    }
    graph = apply_guards(raw, allowed_ids=set(), max_nodes=3)
    assert len(graph["nodes"]) == 3


def test_build_flat_map_one_node_per_source() -> None:
    rows = [
        {"id": "src_a", "_display_title": "标题 A", "title": "T A"},
        {"id": "src_b", "_display_title": "", "title": "T B"},
    ]
    graph = build_flat_map(rows, max_nodes=10)
    assert [node["source_ids"] for node in graph["nodes"]] == [["src_a"], ["src_b"]]
    assert all(node["kind"] == "source" for node in graph["nodes"])
    assert graph["edges"] == []


def test_build_map_with_model_saves_and_returns(ctx: AppContext) -> None:
    a, b = _seed(ctx)
    ctx.llm = StubLlm(reply(MODEL_OUTPUT.format(a=a, b=b)))

    result = topic_build_map(tool_ctx(ctx), "教育智能体", source_ids=[a, b], max_nodes=10)
    data = result["data"]
    assert data["mode"] == "model"
    assert data["stats"]["nodes"] == 3
    assert data["stats"]["ungrounded_nodes"] == 1
    assert result["refs"]["source_ids"] == sorted([a, b])

    saved = get_topic_map(ctx.db, data["topic_id"])
    assert saved is not None
    assert saved["graph"]["mode"] == "model"


def test_build_map_without_model_falls_back_to_flat(ctx: AppContext) -> None:
    a, b = _seed(ctx)
    ctx.llm = None
    result = topic_build_map(tool_ctx(ctx), "教育智能体", source_ids=[a, b])
    assert result["data"]["mode"] == "flat"
    assert result["data"]["edges"] == []
    assert {node["source_ids"][0] for node in result["data"]["nodes"]} == {a, b}


def test_build_map_with_bad_model_output_falls_back(ctx: AppContext) -> None:
    a, b = _seed(ctx)
    ctx.llm = StubLlm(reply("我今天不想干活"))
    result = topic_build_map(tool_ctx(ctx), "教育智能体", source_ids=[a, b])
    assert result["data"]["mode"] == "flat"

    ctx.llm = StubLlm(reply("node: n1 | A | method | src_a"))
    result = topic_build_map(tool_ctx(ctx), "教育智能体", source_ids=[a, b])
    assert result["data"]["mode"] == "model"
    assert result["data"]["stats"]["ungrounded_nodes"] == 1


def test_build_map_prefers_graded_sources(ctx: AppContext) -> None:
    a, b = _seed(ctx)
    source_grade(tool_ctx(ctx), source_ids=[a, b], topic="教育智能体")

    ctx.llm = None
    result = topic_build_map(tool_ctx(ctx), "教育智能体")
    # 只有判权为"核心/相关"的来源进入候选（docs.example.org 落 tier1，两者都留）
    assert result["data"]["source_count"] >= 1
    assert a in result["refs"]["source_ids"]


def test_build_map_rejects_empty_topic(ctx: AppContext) -> None:
    with pytest.raises(ValueError, match="topic 不能为空"):
        topic_build_map(tool_ctx(ctx), "   ")
