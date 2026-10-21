"""``topic.build_map`` —— 把已入库资料整理成方向知识地图（docs/01 §5.4）。

守则：
- **每个节点至少要挂一个真实 source_id**；挂不上的节点保留但标 ``[无出处]`` 并计入 ``ungrounded``
- 端点不存在的边一律丢弃（宁可少，不许错）
- 没有模型时退化成"扁平图"（每个来源一个节点、无边），并如实标 ``mode="flat"``
"""

from __future__ import annotations

import re
from typing import Any

from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state.learning import save_topic_map
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
NO_SOURCE_PREFIX = "[无出处]"

_NODE_LINE = re.compile(r"^\s*node\s*[:：]\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(.*)$")
_EDGE_LINE = re.compile(r"^\s*edge\s*[:：]\s*([^|]+?)\s*->\s*([^|]+?)\s*\|\s*(.+)$")


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
            src, dst, rel = (part.strip() for part in edge_match.groups())
            edges.append({"src": src, "dst": dst, "rel": rel})

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
    dropped_edges = 0
    for edge in raw.get("edges") or []:
        if edge.get("src") not in known or edge.get("dst") not in known:
            dropped_edges += 1
            continue
        rel = edge.get("rel") if edge.get("rel") in ALLOWED_RELATIONS else "uses"
        edges.append({"src": edge["src"], "dst": edge["dst"], "rel": rel})

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
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
