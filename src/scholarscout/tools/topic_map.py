"""``topic.build_map`` —— 把已入库资料整理成方向知识地图（docs/01 §5.4）。

守则：
- **每个节点至少要挂一个真实 source_id**；挂不上的节点保留但标 ``[无出处]`` 并计入 ``ungrounded``
- 端点不存在 / 自环的边一律丢弃（宁可少，不许错）
- 边带**置信度**（``strong`` / ``weak``）：模型有直接证据的标 strong，只有「同领域 / 可能前置」的标
  weak。**弱边照样进图**（界面可画成虚线让人自己验证），不因没把握就把关系整条丢掉 —— 旧提示词
  「拿不准就不给」在真机上产出 12 节点 3 条边的散点图，把「不确定性」和「不输出」混成了一件事。
  **给弱边不等于允许编节点**：端点不存在的边仍然丢弃。
- 没有模型时退化成「扁平图」（每个来源一个节点、无边），并如实标 ``mode="flat"``
"""

from __future__ import annotations

import re
from typing import Any

from scholarscout.learning import knowledge_graph
from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state.learning import (
    get_topic_map,
    latest_topic_map,
    replace_topic_map_graph,
    save_topic_map,
)
from scholarscout.summary.prompts import PromptPack
from scholarscout.tools.envelope import ToolResult, ok
from scholarscout.tools.source_context import (
    candidate_sources,
    degrade_reason,
    render_sources,
    valid_ids,
)

DESCRIPTION = (
    "把一个方向的已有资料整理成知识地图：节点是概念/方法/数据集/工具，边是前置、变体、使用、评测关系。"
    "每个节点都挂真实 source_id；挂不上出处的节点会被标成 [无出处] 并计数。"
)

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "description": "要梳理的方向或主题"},
        "source_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "限定用哪些来源",
        },
        "max_nodes": {"type": "integer", "minimum": 3, "maximum": 30, "description": "节点上限"},
        "use_model": {"type": "boolean", "description": "是否用模型梳理，默认 true"},
    },
    "required": ["topic"],
    "additionalProperties": False,
}

ALLOWED_NODE_KINDS = ("concept", "method", "dataset", "tool", "venue", "source")
ALLOWED_RELATIONS = ("prerequisite", "variant", "uses", "evaluates")
#: 边的置信度：`strong` = 模型有直接证据，`weak` = 同领域 / 可能前置但没证实。
#: **只分两档，不新增关系类型** —— 弱边是"关系本身待验证"，不是"另一种关系"。
ALLOWED_CONFIDENCE = ("strong", "weak")
DEFAULT_CONFIDENCE = "strong"
NO_SOURCE_PREFIX = "[无出处]"

_NODE_LINE = re.compile(r"^\s*node\s*[:：]\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(.*)$")
_EDGE_LINE = re.compile(r"^\s*edge\s*[:：]\s*([^|]+?)\s*->\s*([^|]+?)\s*\|\s*(.+)$")
_EDGE_SEPARATOR = re.compile(r"[|｜]")


def edge_confidence(raw: Any) -> str:
    """置信度归一：只有明确写 ``weak`` 才算弱边（老格式没第三段 → strong，模型写怪词也归 strong）。"""
    return raw if raw in ALLOWED_CONFIDENCE else DEFAULT_CONFIDENCE


def _parse_edge_tail(tail: str) -> tuple[str, str]:
    """``<rel>`` 或 ``<rel> | <confidence>`` → (rel, confidence)。"""
    parts = _EDGE_SEPARATOR.split(tail, maxsplit=1)
    rel = parts[0].strip()
    confidence = edge_confidence(parts[1].strip() if len(parts) > 1 else None)
    return rel, confidence


def parse_map(text: str) -> dict[str, Any]:
    """解析模型输出（只做语法层，不做事实校验）。"""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        node_match = _NODE_LINE.match(line)
        if node_match is not None:
            node_id, label, kind, sources_raw = (part.strip() for part in node_match.groups())
            if not node_id or node_id in seen:
                continue
            seen.add(node_id)
            refs = [
                part.strip()
                for part in re.split(r"[,，、\s]+", sources_raw)
                if part.strip() and part.strip() != "-"
            ]
            nodes.append(
                {
                    "nid": node_id,
                    "label": label,
                    "kind": kind if kind in ALLOWED_NODE_KINDS else "concept",
                    "source_ids": refs,
                }
            )
            continue

        edge_match = _EDGE_LINE.match(line)
        if edge_match is not None:
            src, dst, tail = (part.strip() for part in edge_match.groups())
            rel, confidence = _parse_edge_tail(tail)
            edges.append({"src": src, "dst": dst, "rel": rel, "confidence": confidence})

    return {"nodes": nodes, "edges": edges}


def apply_guards(
    raw: dict[str, Any],
    *,
    allowed_ids: set[str],
    max_nodes: int,
) -> dict[str, Any]:
    """事实校验：来源白名单、节点数上限、边端点存在性。返回带 ``stats`` 的图。"""
    nodes: list[dict[str, Any]] = []
    ungrounded: list[str] = []
    for node in raw.get("nodes") or []:
        valid = [ref for ref in node.get("source_ids", []) if ref in allowed_ids]
        dropped = [ref for ref in node.get("source_ids", []) if ref not in allowed_ids]
        label = str(node.get("label") or "").strip()
        if not valid:
            label = label if label.startswith(NO_SOURCE_PREFIX) else f"{NO_SOURCE_PREFIX}{label}"
            ungrounded.append(label)
        nodes.append(
            {
                "nid": node["nid"],
                "label": label,
                "kind": node.get("kind", "concept"),
                "source_ids": valid,
                "dropped_source_ids": dropped,
            }
        )
    nodes = nodes[:max_nodes]

    known = {node["nid"] for node in nodes}
    edges: list[dict[str, Any]] = []
    by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    dropped_edges = 0
    for edge in raw.get("edges") or []:
        src = edge.get("src")
        dst = edge.get("dst")
        # 端点不存在 / 自环：画不出来也不该画
        if src == dst or src not in known or dst not in known:
            dropped_edges += 1
            continue
        rel = edge.get("rel") if edge.get("rel") in ALLOWED_RELATIONS else "uses"
        confidence = edge_confidence(edge.get("confidence"))
        existing = by_pair.get((src, dst))
        if existing is not None:
            # 同一对端点只留一条：**strong 优先**（后到的 strong 把先到的 weak 升级；
            # weak 不覆盖 strong，也不改已定的 rel/方向 —— 免得同一关系来回翻烧饼）
            if confidence == "strong":
                existing["confidence"] = "strong"
            continue
        item = {"src": src, "dst": dst, "rel": rel, "confidence": confidence}
        by_pair[(src, dst)] = item
        edges.append(item)

    degree: dict[str, int] = {node["nid"]: 0 for node in nodes}
    for edge in edges:
        degree[edge["src"]] += 1
        degree[edge["dst"]] += 1

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "weak_edges": sum(1 for edge in edges if edge["confidence"] == "weak"),
            # 度数为 0 的节点数：模型「只交散点」时这个数会很大，是这张图太稀的直接指标
            "isolated_nodes": sum(1 for value in degree.values() if value == 0),
            "ungrounded_nodes": len(ungrounded),
            "dropped_edges": dropped_edges,
            "ungrounded_labels": ungrounded,
        },
    }


def build_flat_map(rows: list[dict[str, Any]], *, max_nodes: int) -> dict[str, Any]:
    """无模型兜底：每个来源一个节点，没有边（不做任何推断）。"""
    nodes = [
        {
            "nid": f"n{index + 1}",
            "label": str(row.get("_display_title") or row.get("title") or row["id"])[:40],
            "kind": "source",
            "source_ids": [str(row["id"])],
            "dropped_source_ids": [],
        }
        for index, row in enumerate(rows[:max_nodes])
    ]
    return {
        "nodes": nodes,
        "edges": [],
        "stats": {
            "nodes": len(nodes),
            "edges": 0,
            "weak_edges": 0,
            # 扁平图本来就没有边：每个节点都是孤岛（这里如实报，不粉饰）
            "isolated_nodes": len(nodes),
            "ungrounded_nodes": 0,
            "dropped_edges": 0,
            "ungrounded_labels": [],
        },
    }


def topic_build_map(
    ctx: ToolContext,
    topic: str,
    source_ids: list[str] | None = None,
    max_nodes: int = 20,
    use_model: bool = True,
) -> ToolResult:
    topic = topic.strip()
    if not topic:
        raise ValueError("topic 不能为空")
    limit = max(3, min(int(max_nodes), 30))

    rows = candidate_sources(ctx, source_ids=source_ids, topic=topic)
    allowed = valid_ids(rows)
    pack = PromptPack(ctx.config.summary_cfg.prompts_dir)

    mode = "flat"
    reason: str | None = None
    graph: dict[str, Any]
    if use_model and ctx.llm is not None and rows:
        prompt = pack.compose("build-topic-map", topic=topic, sources=render_sources(rows))
        content = ""
        finish_reason = ""
        try:
            reply = ctx.llm.chat([{"role": "user", "content": prompt}])
            content = str(getattr(reply, "content", ""))
            finish_reason = str(getattr(reply, "finish_reason", "") or "")
            parsed = parse_map(content)
        except Exception:
            parsed = {"nodes": [], "edges": []}
        if parsed["nodes"]:
            mode = "model"
            graph = apply_guards(parsed, allowed_ids=allowed, max_nodes=limit)
        else:
            graph = build_flat_map(rows, max_nodes=limit)
            reason = degrade_reason(finish_reason=finish_reason, content=content, parsed_ok=False)
    else:
        graph = build_flat_map(rows, max_nodes=limit)
        reason = "未使用模型（use_model=false 或没有可用模型）"

    topic_id = save_topic_map(
        ctx.db,
        topic=topic,
        graph={**graph, "topic": topic, "mode": mode, "mode_reason": reason},
        session_id=ctx.session_id,
    )

    return ok(
        {
            "topic_id": topic_id,
            "topic": topic,
            "mode": mode,
            "mode_reason": reason,
            "source_count": len(rows),
            "nodes": graph["nodes"],
            "edges": graph["edges"],
            "stats": graph["stats"],
        },
        refs={"topic_ids": [topic_id], "source_ids": sorted(allowed)},
    )


def grow_map_graph(
    ctx: ToolContext,
    *,
    topic: str,
    existing_graph: dict[str, Any],
    source_ids: list[str] | None = None,
    max_nodes: int = knowledge_graph.TOTAL_NODE_CAP,
    use_model: bool = True,
) -> dict[str, Any]:
    """**只算不写**：拿旧图 + 新来源，回合并后的新图与一份可核对的报告。

    与 `topic_build_map` 的区别只有一处：模型**只看没进过图的来源**（按 `source_ids`
    集合判，见 `knowledge_graph.applied_source_ids`），产出的是**局部补丁**，由
    `merge_concept_graph` 并进旧图。所以：旧节点 `nid` 不变、旧边一条不删、整批来源都
    已在图里时一个字段都不动。
    """
    limit = max(3, min(int(max_nodes), knowledge_graph.TOTAL_NODE_CAP))
    applied = knowledge_graph.applied_source_ids(existing_graph)
    if source_ids is None:
        rows = candidate_sources(ctx, topic=topic)
        pending = [str(row["id"]) for row in rows if str(row["id"]) not in applied]
    else:
        pending = [str(ref) for ref in source_ids if str(ref) not in applied]
    pending = list(dict.fromkeys(pending))

    if not pending:
        return {
            "graph": existing_graph,
            "changed": False,
            "mode": str(existing_graph.get("mode") or "flat"),
            "mode_reason": existing_graph.get("mode_reason"),
            "batch": [],
            "new_source_ids": [],
            "added": [],
            "updated": [],
            "added_edges": 0,
            "dropped_nodes": 0,
            "untouched": 0,
            "reason": "没有新来源（这批来源都已经在图里了）",
        }

    rows = candidate_sources(ctx, source_ids=pending)
    allowed = valid_ids(rows)
    pack = PromptPack(ctx.config.summary_cfg.prompts_dir)

    mode = "flat"
    reason: str | None = None
    patch: dict[str, Any]
    if use_model and ctx.llm is not None and rows:
        prompt = pack.compose("build-topic-map", topic=topic, sources=render_sources(rows))
        content = ""
        finish_reason = ""
        try:
            reply = ctx.llm.chat([{"role": "user", "content": prompt}])
            content = str(getattr(reply, "content", ""))
            finish_reason = str(getattr(reply, "finish_reason", "") or "")
            parsed = parse_map(content)
        except Exception:
            parsed = {"nodes": [], "edges": []}
        if parsed["nodes"]:
            mode = "model"
            patch = apply_guards(parsed, allowed_ids=allowed, max_nodes=limit)
        else:
            patch = build_flat_map(rows, max_nodes=limit)
            reason = degrade_reason(finish_reason=finish_reason, content=content, parsed_ok=False)
    else:
        patch = build_flat_map(rows, max_nodes=limit)
        reason = "未使用模型（use_model=false 或没有可用模型）"

    outcome = knowledge_graph.merge_concept_graph(
        existing_graph,
        {"nodes": patch["nodes"], "edges": patch["edges"]},
        max_nodes=limit,
    )
    return {
        "graph": outcome.graph,
        "changed": outcome.changed,
        "mode": mode,
        "mode_reason": reason,
        "batch": sorted(allowed),
        "new_source_ids": list(outcome.new_source_ids),
        "added": list(outcome.added),
        "updated": list(outcome.updated),
        "added_edges": outcome.added_edges,
        "dropped_nodes": outcome.dropped_nodes,
        "untouched": outcome.untouched,
        "reason": outcome.reason,
    }


def topic_grow_map(
    ctx: ToolContext,
    *,
    topic_id: str | None = None,
    topic: str | None = None,
    source_ids: list[str] | None = None,
    max_nodes: int = knowledge_graph.TOTAL_NODE_CAP,
    use_model: bool = True,
) -> dict[str, Any]:
    """让**已有**知识地图增量长大（`topic.build_map` 的增量版，RPC `topic.grow_map` 走这里）。

    给了 `topic_id` 就在那一张图上长；否则按 `topic` 取最近一张图。找不到现成图时
    **从空图长起**（等价于建一张新图，`created=True`）。

    失败不丢图：合并结果先在内存里算完、`json.dumps` 成功之后才 `UPDATE` 那一行
    （`state.learning_topics.replace_topic_map_graph`），序列化或写库失败时旧图原样还在。
    """
    payload: dict[str, Any] | None = None
    if topic_id:
        payload = get_topic_map(ctx.db, topic_id)
        if payload is None:
            raise LookupError(f"主题地图不存在：{topic_id}")
    name = str(topic or "").strip()
    if not name and payload is not None:
        name = str(payload.get("topic") or "").strip()
    if not name:
        raise ValueError("topic 不能为空")

    if payload is None:
        payload = latest_topic_map(ctx.db, name)
    existing_graph: dict[str, Any] = {}
    if payload is not None:
        raw = payload.get("graph")
        existing_graph = dict(raw) if isinstance(raw, dict) else {}

    result = grow_map_graph(
        ctx,
        topic=name,
        existing_graph=existing_graph,
        source_ids=source_ids,
        max_nodes=max_nodes,
        use_model=use_model,
    )

    created = False
    target_id = str(payload["id"]) if payload is not None else None
    if result["changed"]:
        graph = dict(result["graph"])
        graph["topic"] = name
        # 旧图的 mode 才是"这张图怎么来的"；本次退化成 flat 只记进 growth，不改旧口径。
        if not graph.get("mode"):
            graph["mode"] = result["mode"]
            graph["mode_reason"] = result["mode_reason"]
        if target_id is None:
            target_id = save_topic_map(ctx.db, topic=name, graph=graph, session_id=ctx.session_id)
            created = True
        elif not replace_topic_map_graph(ctx.db, topic_id=target_id, graph=graph):
            raise LookupError(f"主题地图不存在：{target_id}")
        result = {**result, "graph": graph}

    return {
        "topic_id": target_id,
        "topic": name,
        "created": created,
        "changed": result["changed"],
        # 这两个是**本次调用**的模式与原因（图上的 mode 记的是"这张图怎么来的"，不该被改），
        # 图里存的那份另给 `graph_mode*` —— 一次退化只留在返回值里，丢了就没法诊断
        "mode": result["mode"],
        "mode_reason": result["mode_reason"],
        "graph_mode": result["graph"].get("mode"),
        "graph_mode_reason": result["graph"].get("mode_reason"),
        "batch": result["batch"],
        "new_source_ids": result["new_source_ids"],
        "added": result["added"],
        "updated": result["updated"],
        "added_edges": result["added_edges"],
        "dropped_nodes": result["dropped_nodes"],
        "untouched": result["untouched"],
        "reason": result["reason"],
        "nodes": result["graph"].get("nodes", []),
        "edges": result["graph"].get("edges", []),
        "stats": {
            "nodes": len(result["graph"].get("nodes") or []),
            "edges": len(result["graph"].get("edges") or []),
            "batch": len(result["batch"]),
        },
    }


def grow_map_on_library_update(
    ctx: ToolContext,
    *,
    topic: str,
    source_ids: list[str],
    max_nodes: int = knowledge_graph.TOTAL_NODE_CAP,
    use_model: bool = True,
) -> dict[str, Any]:
    """**留给 `library.updated` 的入口（本轮只导出、不接线）**。

    为什么单列一个函数而不是让事件直接调 `topic_grow_map`：事件是"资料入库了"，
    它对"这个主题有没有图"没有把握，**没有图时应当安静跳过**（不能因为一次入库就凭空
    造一张图，那会把用户没看过的主题塞满列表）。手动入口则相反：没图就从空图长起。
    """
    payload = latest_topic_map(ctx.db, topic.strip())
    if payload is None:
        return {
            "topic": topic,
            "topic_id": None,
            "changed": False,
            "reason": "该主题还没有知识地图（先 topic.build_map 或 topic.grow_map）",
        }
    result = topic_grow_map(
        ctx,
        topic_id=str(payload["id"]),
        source_ids=source_ids,
        max_nodes=max_nodes,
        use_model=use_model,
    )
    return {"topic": topic, **result}


SPEC = ToolSpec(
    name="topic.build_map",
    description=DESCRIPTION,
    parameters=PARAMETERS,
    handler=topic_build_map,
    toolset="research",
)


def register() -> None:
    from scholarscout.runtime import registry

    registry.register(SPEC, replace=True)
