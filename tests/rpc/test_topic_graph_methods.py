"""`topic.grow_map`（增量生长）与 `topic.get_source_graph`（资料层）的 RPC 契约。

两条这轮新加的口径，都在这里钉住：

- **生长是有记忆的**：同一批来源第二次调用不产生任何变化，也**不再调模型**
  （模型桩第二次被调用会直接 AssertionError，等于把"省了一次钱"变成可断言的）；
- **资料层的边是推导出来的**：同一份概念图 + 同一批来源行，阈值 1/2 出的边必须不同，
  且单个来源不会给自己连一条自环。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scholarscout.config import Config
from scholarscout.providers.llm import ChatReply
from scholarscout.rpc.errors import RpcError
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.runtime import registry
from scholarscout.state.db import Database
from scholarscout.state.ingest import upsert_source
from scholarscout.state.learning import get_topic_map, save_topic_map
from scholarscout.summary.settings import SummarySettings
from scholarscout.tools import register_tools

from ..conftest import StubLlm, redirect_home, reply

#: 仓库里的真提示词目录（`grow_map` 真会 compose `build-topic-map.md`）
REPO_PROMPTS = Path(__file__).resolve().parents[2] / "prompts"


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


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return redirect_home(monkeypatch, tmp_path)


@pytest.fixture
def ctx(db: Database, config: Config) -> Iterator[AppContext]:
    """与 conftest 的 `ctx` 同形状，**只把提示词目录钉到仓库 `prompts/`**。

    conftest 的 `Config` 走默认 `summary_cfg`：`prompts_dir` 会被解到 `redirect_home`
    重定向之后的临时 home（`<tmp>/prompts`），那里没有提示词文件 —— 不钉这一处，
    `grow_map` 在夹具层就 `PromptError`，测的就不是合并逻辑了。
    产品不为测试让路：真实配置的 `prompts_dir` 本来就指向仓库/安装目录的 `prompts/`。
    """
    context = AppContext(
        config=replace(config, summary_cfg=SummarySettings(prompts_dir=REPO_PROMPTS)),
        db=db,
        llm=StubLlm(),
        toolsets=("research",),
    )
    try:
        yield context
    finally:
        registry.clear_registry()


def _seed_sources(ctx: AppContext) -> tuple[str, str, str]:
    a, _ = upsert_source(ctx.db, _row("https://a.example/abs/1", "A 论文"))
    b, _ = upsert_source(ctx.db, _row("https://b.example/abs/2", "B 论文"))
    c, _ = upsert_source(ctx.db, _row("https://c.example/docs/3", "C 指南", kind="docs"))
    return a, b, c


def _prompt_of(llm: StubLlm) -> str:
    """取桩模型收到的第一条消息正文（不写 `type: ignore`：逐层断言形状）。"""
    call = llm.calls[0]
    messages = call["messages"]
    assert isinstance(messages, list) and messages
    first = messages[0]
    assert isinstance(first, dict)
    return str(first.get("content") or "")


def _graph(a: str, b: str) -> dict[str, Any]:
    return {
        "topic": "教育智能体",
        "mode": "model",
        "nodes": [
            {"nid": "n1", "label": "RAG", "kind": "method", "source_ids": [a]},
            {"nid": "n2", "label": "教育智能体", "kind": "concept", "source_ids": [a, b]},
        ],
        "edges": [{"src": "n1", "dst": "n2", "rel": "prerequisite"}],
    }


def test_grow_map_is_idempotent_and_does_not_call_model_twice(ctx: AppContext) -> None:
    """同一批来源重复应用：第二次 `changed=False`，图逐字节不变，模型不再被调用。"""
    a, b, c = _seed_sources(ctx)
    topic_id = save_topic_map(ctx.db, topic="教育智能体", graph=_graph(a, b))
    ctx.llm = StubLlm(
        reply(f"node: n1 | RAG | method | {a}, {c}\nnode: n3 | 重排 | method | {c}\n")
    )

    first = dispatch(ctx, "topic.grow_map", {"topic_id": topic_id, "source_ids": [c]})
    assert first["changed"] is True
    assert first["added"] == ["n3"]
    assert first["updated"] == ["n1"]

    before = json.dumps(get_topic_map(ctx.db, topic_id)["graph"], ensure_ascii=False)
    second = dispatch(ctx, "topic.grow_map", {"topic_id": topic_id, "source_ids": [c]})
    after = json.dumps(get_topic_map(ctx.db, topic_id)["graph"], ensure_ascii=False)
    assert second["changed"] is False
    assert "已经在图里" in str(second["reason"])
    assert after == before
    assert len(ctx.llm.calls) == 1


def test_grow_map_only_sends_new_sources_to_the_model(ctx: AppContext) -> None:
    """旧来源不进第二次的模型上下文（否则每长一次就把整库重新喂一遍）。"""
    a, b, c = _seed_sources(ctx)
    topic_id = save_topic_map(ctx.db, topic="教育智能体", graph=_graph(a, b))
    ctx.llm = StubLlm(reply(f"node: n3 | 重排 | method | {c}"))

    dispatch(ctx, "topic.grow_map", {"topic_id": topic_id, "source_ids": [a, b, c]})

    prompt = _prompt_of(ctx.llm)
    assert c in prompt
    assert a not in prompt and b not in prompt


def test_grow_map_keeps_existing_ids_and_never_drops_old_edges(ctx: AppContext) -> None:
    """生长只加不减：旧 `nid` 不变、旧边还在，新边并进来。"""
    a, b, c = _seed_sources(ctx)
    topic_id = save_topic_map(ctx.db, topic="教育智能体", graph=_graph(a, b))
    ctx.llm = StubLlm(
        reply(
            f"node: n3 | 重排 | method | {c}\nnode: n4 | 评测 | dataset | {c}\n"
            "edge: n3 -> n4 | uses\n"
        )
    )

    dispatch(ctx, "topic.grow_map", {"topic_id": topic_id, "source_ids": [c]})

    graph = get_topic_map(ctx.db, topic_id)["graph"]
    assert [node["nid"] for node in graph["nodes"]] == ["n1", "n2", "n3", "n4"]
    assert {"src": "n1", "dst": "n2", "rel": "prerequisite"} in graph["edges"]
    assert {"src": "n3", "dst": "n4", "rel": "uses"} in graph["edges"]
    assert graph["applied_source_ids"] == sorted([a, b, c])


def test_grow_map_without_model_falls_back_to_source_nodes(ctx: AppContext) -> None:
    """没有模型也要能长大：新来源落成 `source` 节点（如实标 flat，不编概念边）。"""
    a, b, c = _seed_sources(ctx)
    topic_id = save_topic_map(ctx.db, topic="教育智能体", graph=_graph(a, b))
    ctx.llm = None

    out = dispatch(ctx, "topic.grow_map", {"topic_id": topic_id, "source_ids": [c]})
    assert out["changed"] is True
    graph = get_topic_map(ctx.db, topic_id)["graph"]
    assert graph["nodes"][-1]["kind"] == "source"
    assert graph["nodes"][-1]["source_ids"] == [c]
    assert out["mode_reason"] and "未使用模型" in out["mode_reason"]


def test_grow_map_rejects_unknown_topic_and_bad_params(ctx: AppContext) -> None:
    """图不存在 → -32002；两个键都不给 / 类型不对 → -32602（不静默建图、不猜）。"""
    with pytest.raises(RpcError) as not_found:
        dispatch(ctx, "topic.grow_map", {"topic_id": "tpm_missing"})
    assert not_found.value.code == -32002

    with pytest.raises(RpcError) as no_target:
        dispatch(ctx, "topic.grow_map", {})
    assert no_target.value.code == -32602

    with pytest.raises(RpcError) as bad_ids:
        dispatch(ctx, "topic.grow_map", {"topic": "教育智能体", "source_ids": "src_a"})
    assert bad_ids.value.code == -32602


def test_grow_map_creates_a_map_when_topic_has_none(ctx: AppContext) -> None:
    """按 topic 生长、库里还没有图 → 从空图长起并新建一行（`created=True`）。"""
    a, b, c = _seed_sources(ctx)
    ctx.llm = None
    out = dispatch(ctx, "topic.grow_map", {"topic": "教育智能体", "source_ids": [a, b, c]})
    assert out["created"] is True
    assert out["topic_id"].startswith("tpm")
    saved = get_topic_map(ctx.db, out["topic_id"])
    assert saved is not None
    assert {node["source_ids"][0] for node in saved["graph"]["nodes"]} == {a, b, c}


def test_grow_map_degrades_with_a_recorded_reason(ctx: AppContext) -> None:
    """模型这次胡言乱语 → 走扁平兜底，但**原因必须留在返回值里**（可诊断）。"""
    a, b, c = _seed_sources(ctx)
    topic_id = save_topic_map(ctx.db, topic="教育智能体", graph=_graph(a, b))
    ctx.llm = StubLlm(ChatReply(content="", tool_calls=[], finish_reason="length", model="stub"))

    out = dispatch(ctx, "topic.grow_map", {"topic_id": topic_id, "source_ids": [c]})
    assert out["changed"] is True
    assert "max_tokens" in str(out["mode_reason"])


def test_source_graph_single_source_has_no_edge(ctx: AppContext) -> None:
    """只有一个来源：回 1 个节点、0 条边（空图/单来源不报错）。"""
    a, _b, _c = _seed_sources(ctx)
    topic_id = save_topic_map(
        ctx.db,
        topic="教育智能体",
        graph={
            "nodes": [{"nid": "n1", "label": "RAG", "kind": "method", "source_ids": [a]}],
            "edges": [],
        },
    )
    out = dispatch(ctx, "topic.get_source_graph", {"topic_id": topic_id})
    assert out["layer"] == "source"
    assert [node["id"] for node in out["nodes"]] == [a]
    assert out["edges"] == []
    assert out["nodes"][0]["label"] == "A 论文"
    assert out["nodes"][0]["source_type"] == "arxiv"
    assert out["nodes"][0]["year"] == 2025
    assert out["nodes"][0]["has_text"] is False


def test_source_graph_min_shared_switch(ctx: AppContext) -> None:
    """阈值 1 / 2 出不同的边：共同支撑 1 个概念的来源对只在 `min_shared=1` 时连上。"""
    a, b, c = _seed_sources(ctx)
    graph = {
        "nodes": [
            {"nid": "n1", "label": "RAG", "kind": "method", "source_ids": [a, b]},
            {"nid": "n2", "label": "重排", "kind": "method", "source_ids": [a, c]},
            {"nid": "n3", "label": "评测", "kind": "dataset", "source_ids": [a, c]},
        ],
        "edges": [],
    }
    topic_id = save_topic_map(ctx.db, topic="教育智能体", graph=graph)

    loose = dispatch(ctx, "topic.get_source_graph", {"topic_id": topic_id, "min_shared": 1})
    strict = dispatch(ctx, "topic.get_source_graph", {"topic_id": topic_id})

    assert loose["min_shared"] == 1
    assert len(loose["edges"]) == 2
    assert [edge["kind"] for edge in loose["edges"]] == ["shared_concept", "shared_concept"]
    # 只钉"哪一对 + 权重"，**不钉方向**：资料层的端点按 `source_id` 字典序排（`build_source_graph`
    # 的稳定顺序），而 source_id 是入库时生成的随机串 —— 把方向写死等于把随机 id 当契约。
    pairs = [
        (tuple(sorted((edge["source"], edge["target"]))), edge["weight"])
        for edge in strict["edges"]
    ]
    assert pairs == [(tuple(sorted((a, c))), 2)]
    assert strict["min_shared"] == 2
    # 度数按连边算（画图时圆点大小用它）
    weights = {node["id"]: node["weight"] for node in strict["nodes"]}
    assert weights == {a: 1, b: 0, c: 1}


def test_source_graph_empty_topic_returns_empty_graph(ctx: AppContext) -> None:
    """空主题（图里没节点）：回空图，不崩、不报错。"""
    topic_id = save_topic_map(ctx.db, topic="空主题", graph={"nodes": [], "edges": []})
    out = dispatch(ctx, "topic.get_source_graph", {"topic_id": topic_id})
    assert out["nodes"] == []
    assert out["edges"] == []
    assert out["counts"] == {"nodes": 0, "edges": 0, "missing_sources": 0, "threshold": 2}

    with pytest.raises(RpcError) as missing:
        dispatch(ctx, "topic.get_source_graph", {"topic_id": "tpm_missing"})
    assert missing.value.code == -32002
