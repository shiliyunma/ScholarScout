"""调研相关：`topic.*`（知识地图）/ `research.*`（看板）/ `search.*` / `trace.*` / `system.*`。

方法实现分散在这里，注册仍然走 `registry.method` 装饰器 —— 注册表只有一份。
"""

from __future__ import annotations

from typing import Any

from scholarscout import __version__
from scholarscout.learning import knowledge_graph
from scholarscout.providers.settings import provider_statuses
from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.registry import AppContext, _int_param, _params, _require, method
from scholarscout.runtime import registry
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state import learning, source_context, traces


@method("system.health")
def system_health(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "version": __version__,
        "db_path": str(ctx.config.db_path),
        "counts": ctx.db.counts(),
        "tools": registry.registered_names(),
        "llm_configured": bool(ctx.config.api_key()),
    }


@method("trace.list")
def trace_list(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    limit = _int_param(params, "limit", 100)
    items = traces.list_traces(
        ctx.db,
        session_id=params.get("session_id") if isinstance(params.get("session_id"), str) else None,
        turn_id=params.get("turn_id") if isinstance(params.get("turn_id"), str) else None,
        tool_name=params.get("tool_name") if isinstance(params.get("tool_name"), str) else None,
        limit=limit,
    )
    return {"items": items}


# --------------------------------------------------------------------- 资料库


@method("search.providers")
def search_providers(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """哪些搜索引擎/来源现在能用、不能用的原因、以及怎么才能用。

    界面把这张表和"填 key 的输入框"放在一起，用户填完 key 立刻能看到它变成可用
    （`settings.update` 写 `.env` 的同时会更新进程环境，不必重启）。
    """
    del params
    settings = ctx.config.search_cfg
    items = [status.as_dict() for status in provider_statuses(settings)]
    return {
        "items": items,
        "auto": list(settings.auto_names()),
        "usable_count": sum(1 for item in items if item["usable"]),
        "note": "「auto」只带此刻真能用的来源；填了 key 立即生效（不必重启）。",
    }


@method("topic.list")
def topic_list(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """列出历史主题图（轻量条目，正文走 `topic.get`）。

    前端原先只能看本会话刚生成的产物，进程一退就再也点不开 —— 这个方法让
    库里攒下的产物都能翻出来看。
    """
    params = _params(params)
    limit = int(params.get("limit") or 50)
    return {"items": learning.list_topic_maps(ctx.db, limit=limit)}


@method("topic.get")
def topic_get(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    topic_id = _require(params, "topic_id")
    payload = learning.get_topic_map(ctx.db, topic_id)
    if payload is None:
        raise RpcError(Code.NOT_FOUND, f"主题地图不存在：{topic_id}")
    return payload


@method("topic.get_map")
def topic_get_map(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """知识地图的**只读可视化视图**（`topic.get` 的图形化投影）。

    为什么不让界面直接读 `topic.get`：`topic.get` 回的是落库原样
    （`nid` / `src` / `dst` / `rel`，且没有度数）。画图要的是 `id/source/target/kind`
    与**度数**（圆点大小、大图截断排序都靠它）；把这套归一规则放进界面层，
    等于同一件事写第二份，迟早与后端漂移。这里只归一并算 `weight`，
    **不重建图、不调模型**。
    """
    params = _params(params)
    topic_id = _require(params, "topic_id")
    payload = learning.get_topic_map(ctx.db, topic_id)
    if payload is None:
        raise RpcError(Code.NOT_FOUND, f"主题地图不存在：{topic_id}")
    return _map_view(payload)


#: 节点类型 / 关系类型：与 `tools/topic_map.py` 的 `ALLOWED_*` **同一口径**。
#: 这里重写一份常量而不是 import 工具模块：RPC 层不该为了几个字符串把工具层
#: （模型客户端、提示词包）一起拖进来。
MAP_NODE_KINDS = ("concept", "method", "dataset", "tool", "venue", "source")
MAP_EDGE_KINDS = ("prerequisite", "variant", "uses", "evaluates")

#: 两张可查的图：`concept` = 落库的概念图（`graph_json`），`source` = 按共同支撑现算的资料图。
#: `find_path` / `search_nodes` 都吃这一个 `layer` 参数 —— 两层拓扑不同，但"找路径/找节点"
#: 的口径应当一致（界面换个开关就行，不必记两套返回形状）。
MAP_LAYERS = ("concept", "source")


def _layer_param(params: dict[str, Any]) -> str:
    """取 `layer`（默认 `concept`）。不认识的名字 → `-32602`，**不静默回概念层**。

    静默回退在这里格外危险：用户以为在资料层找，拿到的却是概念图结果，形状还一样（都是
    `id/label/kind`），看不出错。
    """
    raw = params.get("layer", MAP_LAYERS[0])
    if not isinstance(raw, str):
        raise RpcError(Code.INVALID_PARAMS, "参数 layer 类型不对：期望 str")
    layer = raw.strip().lower() or MAP_LAYERS[0]
    if layer not in MAP_LAYERS:
        raise RpcError(
            Code.INVALID_PARAMS, f"参数 layer 只能是 {' / '.join(MAP_LAYERS)}，收到：{raw}"
        )
    return layer


def _min_shared_param(params: dict[str, Any]) -> int:
    raw = params.get("min_shared", knowledge_graph.DEFAULT_MIN_SHARED)
    if not isinstance(raw, int | float) or isinstance(raw, bool):
        raise RpcError(Code.INVALID_PARAMS, "参数 min_shared 必须是数字")
    return int(raw)


def _concept_graph(payload: dict[str, Any]) -> dict[str, Any]:
    """落库的概念图（坏数据当空图：`find_path`/`search_nodes` 该回"没结果"而不是崩）。"""
    graph = payload.get("graph")
    return graph if isinstance(graph, dict) else {}


def _layer_graph(
    ctx: AppContext, payload: dict[str, Any], *, layer: str, min_shared: int
) -> dict[str, Any]:
    """按 `layer` 交出那张图（同一形状：`nodes[{nid,label,kind,source_ids}]` + `edges[{src,dst,rel}]`）。

    资料层**现算**（`build_source_graph`），不落库：多存一份就等于多一处会过期的真相。
    """
    concept_graph = _concept_graph(payload)
    if layer != "source":
        return concept_graph
    ids = sorted(knowledge_graph.applied_source_ids(concept_graph))
    rows = source_context.sources_with_text(ctx.db, ids)
    return knowledge_graph.build_source_graph(concept_graph, rows, min_shared=min_shared)


@method("topic.grow_map")
def topic_grow_map_rpc(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """让已有知识地图**增量长大**（`topic.build_map` 的增量版）。

    与 `topic.build_map` 的差别只有一条：它**不重生成整张图**，只把库里还没进过这张图的
    来源交给模型，产出局部补丁后并进旧图（旧节点 `nid` 不变、旧边不删）。同一批来源重复
    调用**不产生任何变化**（按 `source_ids` 集合判，见 `learning/knowledge_graph.py`）。

    参数校验留在这里、合并与落库留给工具层：RPC 只负责把「缺参数 / 类型不对 / 图不存在」
    翻成 `-32602` / `-32002`，不在协议层重写一遍合并规则。
    """
    params = _params(params)
    topic_id = params.get("topic_id")
    if topic_id is not None and not isinstance(topic_id, str):
        raise RpcError(Code.INVALID_PARAMS, "参数 topic_id 类型不对：期望 str")
    topic = params.get("topic")
    if topic is not None and not isinstance(topic, str):
        raise RpcError(Code.INVALID_PARAMS, "参数 topic 类型不对：期望 str")
    if not (isinstance(topic_id, str) and topic_id.strip()) and not (
        isinstance(topic, str) and topic.strip()
    ):
        raise RpcError(Code.INVALID_PARAMS, "topic_id 与 topic 至少要给一个")
    source_ids = params.get("source_ids")
    if source_ids is not None and (
        not isinstance(source_ids, list) or not all(isinstance(item, str) for item in source_ids)
    ):
        raise RpcError(Code.INVALID_PARAMS, "参数 source_ids 必须是字符串数组")
    use_model = params.get("use_model", True)
    if not isinstance(use_model, bool):
        raise RpcError(Code.INVALID_PARAMS, "参数 use_model 必须是布尔")
    max_nodes = _int_param(params, "max_nodes", knowledge_graph.TOTAL_NODE_CAP)

    from scholarscout.tools.topic_map import topic_grow_map

    try:
        return topic_grow_map(
            ToolContext(db=ctx.db, config=ctx.config, http=ctx.http, llm=ctx.llm),
            topic_id=str(topic_id).strip() if isinstance(topic_id, str) else None,
            topic=topic if isinstance(topic, str) else None,
            source_ids=source_ids,
            max_nodes=max_nodes,
            use_model=use_model,
        )
    except LookupError as exc:
        raise RpcError(Code.NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise RpcError(Code.INVALID_PARAMS, str(exc)) from exc


@method("topic.get_source_graph")
def topic_get_source_graph(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """**资料层图**（第二层）：节点是来源，边是"两个来源共同支撑 ≥ min_shared 个概念"。

    这一层**不落库、不调模型**：边完全从概念层每个节点的 `source_ids` 推导而来 ——
    同一份概念图 + 同一批来源行，永远算出同一张资料图。阈值默认 2（一条共同概念太容易
    撞：任何两篇讲 RAG 的都会共享一个泛概念），`min_shared=1` 可以把"只要有一点交集"
    也连起来（调试/小主题时看得到全貌）。

    空图 / 单来源回空图**不报错** —— 刚建图、只有一篇资料，本来就该没有边。
    """
    params = _params(params)
    topic_id = _require(params, "topic_id")
    min_shared = (
        _int_param(params, "min_shared", knowledge_graph.DEFAULT_MIN_SHARED)
        if "min_shared" in params
        else knowledge_graph.DEFAULT_MIN_SHARED
    )
    payload = learning.get_topic_map(ctx.db, topic_id)
    if payload is None:
        raise RpcError(Code.NOT_FOUND, f"主题地图不存在：{topic_id}")
    return _source_graph_view(ctx, payload, min_shared=min_shared)


@method("topic.find_path")
def topic_find_path(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """两个节点之间的**最短路径**（`topic.get_map` 上的"这两块怎么连起来"）。

    `layer` 决定查哪张图：`concept`（默认，落库的概念图）或 `source`（共同支撑推导的资料图）。
    两层共用同一套返回形状：`paths:[{nodes:[id…], edges:[{src,dst,rel(,weight)}]}]`，
    `nodes` 顺序是**起点到终点**，边保留存储方向（界面照原方向画箭头）。

    为什么走**无向**连通：概念图上边是有方向的（`prerequisite`），但用户问的是"有没有关系"，
    按方向判可达会让一半查询变成"没有路径"（`learning/knowledge_graph._adjacency` 有详述）。

    找不到 / 超出 `max_depth` 回 `paths:[]`，**不报错** —— "这两个概念没关系"是正常结论。
    """
    params = _params(params)
    topic_id = _require(params, "topic_id")
    start = _require(params, "from")
    end = _require(params, "to")
    layer = _layer_param(params)
    depth = knowledge_graph.clamp_depth(
        _int_param(params, "max_depth", knowledge_graph.DEFAULT_PATH_DEPTH)
    )
    min_shared = _min_shared_param(params)
    payload = learning.get_topic_map(ctx.db, topic_id)
    if payload is None:
        raise RpcError(Code.NOT_FOUND, f"主题地图不存在：{topic_id}")
    graph = _layer_graph(ctx, payload, layer=layer, min_shared=min_shared)
    paths = knowledge_graph.find_paths(graph, start, end, max_depth=depth)
    return {
        "topic_id": payload.get("id"),
        "topic": payload.get("topic"),
        "layer": layer,
        "from": start,
        "to": end,
        "max_depth": depth,
        "paths": paths,
        "counts": {"paths": len(paths), "nodes": len(knowledge_graph.node_index(graph))},
    }


@method("topic.search_nodes")
def topic_search_nodes(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """按标签**子串**找节点（`casefold`：`rag` 能搜到 `RAG`、`Rag 检索`）。

    `layer` 同上（两层通用）。回 `{nodes:[{id,label,kind,weight}]}`，`weight` 是度数
    （与 `topic.get_map` 同口径，界面直接拿去排）。**空查询回 `{nodes:[]}` 而不是全图** ——
    空串"匹配所有"会在界面上表现为"刚打开搜索框就糊一屏节点"。
    """
    params = _params(params)
    topic_id = _require(params, "topic_id")
    query = _require(params, "q")
    layer = _layer_param(params)
    limit = knowledge_graph.clamp_search_limit(
        _int_param(params, "limit", knowledge_graph.DEFAULT_SEARCH_LIMIT)
    )
    min_shared = _min_shared_param(params)
    payload = learning.get_topic_map(ctx.db, topic_id)
    if payload is None:
        raise RpcError(Code.NOT_FOUND, f"主题地图不存在：{topic_id}")
    graph = _layer_graph(ctx, payload, layer=layer, min_shared=min_shared)
    nodes = knowledge_graph.search_nodes(graph, query, limit=limit)
    return {
        "topic_id": payload.get("id"),
        "topic": payload.get("topic"),
        "layer": layer,
        "q": query,
        "limit": limit,
        "nodes": nodes,
        "counts": {"nodes": len(nodes)},
    }


def _source_graph_view(
    ctx: AppContext, payload: dict[str, Any], *, min_shared: int
) -> dict[str, Any]:
    """概念图 → 资料层图的可视化视图（与 `topic.get_map` 同一套 `id/source/target/kind` 口径）。

    `has_text` 取自 `documents` 有没有抽取记录：界面据此决定"点进去能看全文"还是"只有元数据"。
    """
    built = _layer_graph(ctx, payload, layer="source", min_shared=min_shared)

    degree: dict[str, int] = {}
    edges: list[dict[str, Any]] = []
    for edge in built["edges"]:
        edges.append(
            {
                "source": edge["src"],
                "target": edge["dst"],
                "kind": edge["rel"],
                "weight": edge["weight"],
            }
        )
        degree[edge["src"]] = degree.get(edge["src"], 0) + 1
        degree[edge["dst"]] = degree.get(edge["dst"], 0) + 1

    nodes: list[dict[str, Any]] = []
    for node in built["nodes"]:
        meta = node["meta"]
        nodes.append(
            {
                "id": node["nid"],
                "label": node["label"],
                "kind": "source",
                "weight": degree.get(node["nid"], 0),
                "sources": [node["nid"]],
                "url": meta["url"],
                "source_type": meta["source_type"],
                "year": meta["year"],
                "has_text": meta["has_text"],
                "concept_count": meta["concept_count"],
                "missing": meta["missing"],
            }
        )

    return {
        "topic_id": payload.get("id"),
        "session_id": payload.get("session_id"),
        "topic": payload.get("topic"),
        "created_at": payload.get("created_at"),
        "layer": "source",
        "min_shared": built["min_shared"],
        "nodes": nodes,
        "edges": edges,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "missing_sources": built["counts"]["missing_sources"],
            "threshold": built["min_shared"],
        },
    }


def _map_view(payload: dict[str, Any]) -> dict[str, Any]:
    """把 `graph_json` 归一成界面能直接画的图：节点带 `weight`（= 度数）、边两端是节点 id。

    坏数据（手工改过库、写一半崩过）逐项跳过，**不整体抛错** —— 界面宁可少画几个点，
    也不该整张图打不开（与 `learning.list_topic_maps` 的容错口径一致）。
    """
    graph = payload.get("graph")
    if not isinstance(graph, dict):
        graph = {}
    raw_nodes = graph.get("nodes")
    raw_edges = graph.get("edges")

    nodes: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for item in raw_nodes if isinstance(raw_nodes, list) else []:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("nid") or item.get("id") or "").strip()
        if not node_id or node_id in index:
            continue
        kind = str(item.get("kind") or "concept")
        sources = item.get("source_ids")
        # 显式标注：不标的话 mypy 会把字面量推成 dict[str, object]，
        # 下面统计 kinds 时拿 object 当键就会报类型错。
        node: dict[str, Any] = {
            "id": node_id,
            "label": str(item.get("label") or node_id),
            "kind": kind if kind in MAP_NODE_KINDS else "concept",
            "weight": 0,
            "sources": [str(ref) for ref in sources if str(ref).strip()]
            if isinstance(sources, list)
            else [],
        }
        index[node_id] = node
        nodes.append(node)

    edges: list[dict[str, Any]] = []
    for item in raw_edges if isinstance(raw_edges, list) else []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("src") or item.get("source") or "").strip()
        target = str(item.get("dst") or item.get("target") or "").strip()
        # 端点不存在 / 自环：画不出来也不该画（与建图时的守卫同一口径）
        if source == target or source not in index or target not in index:
            continue
        kind = str(item.get("rel") or item.get("kind") or "uses")
        edges.append(
            {
                "source": source,
                "target": target,
                "kind": kind if kind in MAP_EDGE_KINDS else "uses",
            }
        )
        index[source]["weight"] += 1
        index[target]["weight"] += 1

    kinds: dict[str, int] = {}
    for node in nodes:
        kinds[node["kind"]] = kinds.get(node["kind"], 0) + 1

    return {
        "topic_id": payload.get("id"),
        "session_id": payload.get("session_id"),
        "topic": payload.get("topic"),
        "created_at": payload.get("created_at"),
        "mode": graph.get("mode"),
        "mode_reason": graph.get("mode_reason"),
        "nodes": nodes,
        "edges": edges,
        "counts": {"nodes": len(nodes), "edges": len(edges), "kinds": kinds},
    }


@method("research.board")
def research_board(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """研究看板（B2 证据表 + B4 时间线 + B7 看板）：**只读**，零模型调用。

    为什么合成一个 RPC：这三节是同一次"用户打开右栏"的动作，拆成三个方法会让前端连打三枪、
    还可能出现"证据表是新的、时间线是旧的"这种不一致。
    """
    params = _params(params)
    session_id = _require(params, "session_id")
    from scholarscout.runtime import research_view

    return research_view.build_board(ctx.db, session_id)


@method("research.set_question")
def research_set_question(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """勾选子问题是否已覆盖（看板可编辑）。**只写状态，不调模型**。

    编号从 1 开始，与界面上的序号一致；越界或非法状态直接报错，不静默改。
    """
    params = _params(params)
    session_id = _require(params, "session_id")
    try:
        index = int(params.get("index") or 0)
    except (TypeError, ValueError):
        index = 0
    status = str(params.get("status") or "")
    from scholarscout.runtime import research_state

    try:
        state = research_state.set_question_status(ctx.db, session_id, index, status)
    except ValueError as exc:
        raise RpcError(Code.INVALID_PARAMS, str(exc)) from exc
    return {"ok": True, "questions": state.get("questions") or []}


@method("research.overview")
def research_overview(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """跨会话的研究看板汇总（⑦）：**只读**，零模型调用。

    为什么单独开一个方法而不是让前端循环调 research.board：前端循环 N 次就是把
    "N 个会话各打一枪"搬到界面层，还可能出现半新半旧的混合结果。
    """
    params = _params(params)
    limit = int(params.get("limit") or 30)
    from scholarscout.runtime import research_view

    return research_view.build_overview(ctx.db, limit=limit)
