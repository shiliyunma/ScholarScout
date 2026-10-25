"""知识地图的**两层图**：概念层的增量生长 + 资料层的共同支撑推导。

## 为什么要有这一层

`topic.build_map` 原来是"一次性重生成"：每调一次就把整张概念图重新写一遍，节点 `nid`
重排、位置提示丢失、模型每次措辞不同 —— 资料一多，图反而越看越乱。用户（大浩）要的是
**图随资料收录长大**：新来源进来只动受影响的节点与边，旧节点保持原 id。

这一层只放**纯函数**（不碰数据库、不调模型、不联网），与 `srs.py` / `route_patch.py`
同一层级，所以能直接用字典试、用集合判幂等。

## 两层图

1. **概念层**（`topic_maps.graph_json`）：`nodes[{nid,label,kind,source_ids,dropped_source_ids}]`
   + `edges[{src,dst,rel}]`。`kind ∈ concept/method/dataset/tool/venue/source`，
   `rel ∈ prerequisite/variant/uses/evaluates`。
2. **资料层**（不落库，按需现算）：节点 = 本主题出现过的来源（`sources` 行），
   边 = **两个来源共同支撑 ≥ min_shared 个概念**，权重 = 共同概念数。边全部从概念节点的
   `source_ids` 推导 —— 不需要任何新数据。

## 三条口径（改之前先读）

- **受影响的判定只看 `source_ids` 集合**：一批来源里只要有一条没被现有图吸收过，这批就是
  "新的"；整批都被吸收过 → 原地返回、一个字段都不动（幂等）。所以重复应用同一批来源
  **必然**产出逐字节相同的图，不依赖模型这次说了什么。
- **生长是单调的**：既有节点、既有边**永不删除**（宁可多留一条旧边，也不让用户上次看到的
  关系凭空消失）；节点数达到上限时只截**新加的尾巴**。
- **标签是身份**：同一标签（去 `[无出处]` 前缀、去首尾空白、`casefold`）视为同一个概念，
  新来源挂到它上面而不是再造一个同义节点。`nid` 只是给界面用的稳定句柄 —— 而且**补丁里的
  nid 是局部的**：模型只看这一批新来源，编号从 n1 重新数。所以合并时 id 撞上了还得标签
  对得上才算同一个节点（否则扁平补丁的第一个节点会顶掉旧图里的 n1）。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

#: 无出处节点的标签前缀（与 `tools/topic_map.py` 同一口径）
NO_SOURCE_PREFIX = "[无出处]"
#: 资料层边的类型：两个来源共同支撑同一概念
SHARED_CONCEPT_REL = "shared_concept"
#: 概念层节点总数上限（与 `topic.build_map` 的 `max_nodes` 上界一致）
TOTAL_NODE_CAP = 30
#: 资料层连边阈值默认值：**一条**共同概念太容易撞（任何两篇讲 RAG 的都会共享一个泛概念），
#: 默认 2 才能表达"这两篇在讲同一件事"
DEFAULT_MIN_SHARED = 2
#: 资料层阈值下限/上限（夹取，不报错）
MIN_SHARED_BOUNDS = (1, 20)
#: `topic.find_path` 的默认深度（边数）：四跳基本已是两个分支的尽头，再远就只是噪声
DEFAULT_PATH_DEPTH = 4
#: 深度上限（夹取，不报错）：BFS 再深也不会比节点数更有意义，这里给个硬顶
MAX_PATH_DEPTH = 8
#: 同长最短路径最多回几条：界面问的是"这两块怎么连起来"，一两条就够看
MAX_PATHS = 3
#: `topic.search_nodes` 的默认条数 / 上限
DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 100


def _refs(value: object) -> list[str]:
    """把 ``source_ids`` / ``dropped_source_ids`` 归一成去重保序的字符串列表。"""
    out: list[str] = []
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for item in value:
            text = str(item).strip()
            if text and text not in out:
                out.append(text)
    return out


def _iter_dicts(value: object) -> list[dict[str, Any]]:
    """取 ``nodes`` / ``edges`` 这类列表里的字典项；坏数据（非列表、混了字符串）逐项跳过。"""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _iter_nodes(graph: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    return _iter_dicts((graph or {}).get("nodes"))


def _iter_edges(graph: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    return _iter_dicts((graph or {}).get("edges"))


def _norm_label(label: object) -> str:
    """标签身份：去前缀、去空白、大小写不敏感（中文没大小写，英文概念有）。"""
    text = str(label or "").strip()
    if text.startswith(NO_SOURCE_PREFIX):
        text = text[len(NO_SOURCE_PREFIX) :]
    return text.strip().casefold()


def _node_id(item: Mapping[str, Any]) -> str:
    """节点 id：落库形状是 ``nid``，界面视图是 ``id``，两种都认。"""
    return str(item.get("nid") or item.get("id") or "").strip()


def _edge_ends(item: Mapping[str, Any]) -> tuple[str, str]:
    """边两端：落库形状是 ``src/dst``，界面视图是 ``source/target``。"""
    src = str(item.get("src") or item.get("source") or "").strip()
    dst = str(item.get("dst") or item.get("target") or "").strip()
    return src, dst


def _edge_rel(item: Mapping[str, Any]) -> str:
    return str(item.get("rel") or item.get("kind") or "uses").strip() or "uses"


def _edge_weight(item: Mapping[str, Any]) -> int:
    """边权重：资料层是共同概念数，概念层没有权重（默认 1）。"""
    raw = item.get("weight")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return 1
    return max(1, int(raw))


def _label_from_url(url: str) -> str:
    """无标题时用 URL 尾段（裸 ``/`` 结尾就退到域名）。"""
    if not url:
        return ""
    parts = urlsplit(url)
    tail = parts.path.rstrip("/").rsplit("/", 1)[-1]
    return unquote(tail) if tail else parts.netloc


def applied_source_ids(graph: Mapping[str, Any] | None) -> set[str]:
    """**已经进入这张图**的来源集合 = 水位标记 ∪ 所有节点挂着的 ``source_ids``。

    为什么还要扫节点：`topic.build_map` 早先生成的图没有水位标记（`applied_source_ids`），
    只看水位会把图上已有来源当成"新的"，白调一次模型。两者取并集，老图一进来就是幂等的。
    """
    seen: set[str] = set(_refs((graph or {}).get("applied_source_ids")))
    for node in _iter_nodes(graph):
        seen.update(_refs(node.get("source_ids")))
    return seen


@dataclass(frozen=True)
class MergeOutcome:
    """一次增量合并的结果：新图 + 够写进日志/界面的计数。

    ``graph`` 是**可以直接落库**的整份图（不是补丁）；``changed=False`` 时它与传入的
    旧图逐字段相同 —— 调用方据此跳过写库。
    """

    graph: dict[str, Any]
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    added_edges: int = 0
    dropped_nodes: int = 0
    untouched: int = 0
    changed: bool = False
    batch: tuple[str, ...] = ()
    new_source_ids: tuple[str, ...] = ()
    reason: str | None = None


def _copy_graph(graph: Mapping[str, Any] | None) -> dict[str, Any]:
    """浅拷贝整份图（``nodes`` / ``edges`` 逐项复制，调用方改动不会污染入参）。"""
    base = dict(graph or {})
    base["nodes"] = [dict(item) for item in _iter_nodes(graph)]
    base["edges"] = [dict(item) for item in _iter_edges(graph)]
    return base


def merge_concept_graph(
    existing: Mapping[str, Any] | None,
    incoming: Mapping[str, Any] | None,
    *,
    max_nodes: int = TOTAL_NODE_CAP,
) -> MergeOutcome:
    """把 ``incoming``（新来源的**局部图**）增量并进 ``existing``（整张图）。

    只做四件事：

    1. 同一标签的节点 → 合并 `source_ids`（并集，保序），旧 `nid` 不变；
    2. 新标签 → 追加节点，`nid` 优先沿用模型给的（撞了才换），保证下一次合并能认回来；
    3. 新边 → 两端解析到最终 `nid` 后追加（去重、丢自环、丢悬空）；
    4. 既有节点/边**一条不删**；节点数到顶只截新加的尾巴。

    整批来源都已在图里时**不做任何改动**（幂等）：`changed=False`、`graph` 与旧图逐字段相同。
    """
    base = _copy_graph(existing)
    batch = tuple(
        sorted({ref for node in _iter_nodes(incoming) for ref in _refs(node.get("source_ids"))})
    )
    applied = applied_source_ids(base)
    new_source_ids = tuple(ref for ref in batch if ref not in applied)
    if not new_source_ids:
        return MergeOutcome(
            graph=base,
            batch=batch,
            reason="这批来源已经在图里了（按 source_ids 集合判，未做任何改动）",
        )

    nodes: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    by_label: dict[str, dict[str, Any]] = {}
    for item in _iter_nodes(base):
        nid = _node_id(item)
        if not nid or nid in by_id:
            continue
        node = {
            "nid": nid,
            "label": str(item.get("label") or nid),
            "kind": str(item.get("kind") or "concept"),
            "source_ids": _refs(item.get("source_ids")),
            "dropped_source_ids": _refs(item.get("dropped_source_ids")),
        }
        nodes.append(node)
        by_id[nid] = node
        label_key = _norm_label(node["label"])
        if label_key:
            by_label.setdefault(label_key, node)

    edges: list[dict[str, Any]] = []
    edge_keys: set[tuple[str, str, str]] = set()
    for item in _iter_edges(base):
        src, dst = _edge_ends(item)
        rel = _edge_rel(item)
        if src == dst or src not in by_id or dst not in by_id:
            continue
        key = (src, dst, rel)
        if key in edge_keys:
            continue
        edge_keys.add(key)
        edges.append({"src": src, "dst": dst, "rel": rel})

    added: list[str] = []
    updated: list[str] = []
    untouched = 0
    resolve: dict[str, str] = {}
    for item in _iter_nodes(incoming):
        raw_nid = _node_id(item)
        label = str(item.get("label") or "").strip()
        refs = _refs(item.get("source_ids"))
        dropped = _refs(item.get("dropped_source_ids"))

        target = by_id.get(raw_nid) if raw_nid else None
        # 补丁里的 id 是**局部**的（模型只看这一批新来源）：撞上旧 id 但标签对不上，
        # 说明它指的不是同一个概念 —— 按新节点处理，绝不把新来源挂到旧概念上
        if target is not None and label and _norm_label(label) != _norm_label(str(target["label"])):
            target = None
        if target is None and label:
            target = by_label.get(_norm_label(label))

        if target is not None:
            final = str(target["nid"])
            if raw_nid:
                resolve[raw_nid] = final
            fresh = [ref for ref in refs if ref not in target["source_ids"]]
            fresh_dropped = [ref for ref in dropped if ref not in target["dropped_source_ids"]]
            relabel = ""
            current = str(target["label"])
            if label and label != current and current.startswith(NO_SOURCE_PREFIX):
                relabel = label
            if not fresh and not fresh_dropped and not relabel:
                untouched += 1
                continue
            target["source_ids"] = [*target["source_ids"], *fresh]
            target["dropped_source_ids"] = [*target["dropped_source_ids"], *fresh_dropped]
            if relabel:
                target["label"] = relabel
                by_label.setdefault(_norm_label(relabel), target)
            updated.append(final)
            continue

        nid = raw_nid if raw_nid and raw_nid not in by_id else _fresh_id(by_id)
        node = {
            "nid": nid,
            "label": label or nid,
            "kind": str(item.get("kind") or "concept"),
            "source_ids": refs,
            "dropped_source_ids": dropped,
        }
        nodes.append(node)
        by_id[nid] = node
        label_key = _norm_label(node["label"])
        if label_key:
            by_label.setdefault(label_key, node)
        if raw_nid:
            resolve[raw_nid] = nid
        added.append(nid)

    added_edges = 0
    for item in _iter_edges(incoming):
        src, dst = _edge_ends(item)
        src = resolve.get(src, src)
        dst = resolve.get(dst, dst)
        rel = _edge_rel(item)
        if src == dst or src not in by_id or dst not in by_id:
            continue
        key = (src, dst, rel)
        if key in edge_keys:
            continue
        edge_keys.add(key)
        edges.append({"src": src, "dst": dst, "rel": rel})
        added_edges += 1

    cap = max(3, int(max_nodes))
    dropped_nodes = 0
    if len(nodes) > cap:
        kept = nodes[:cap]
        dropped_nodes = len(nodes) - len(kept)
        kept_ids = {str(node["nid"]) for node in kept}
        nodes = kept
        edges = [edge for edge in edges if edge["src"] in kept_ids and edge["dst"] in kept_ids]
        # 被截掉的尾巴不算"这次加进来的"：报告要能对上图里真实的净增
        added = [nid for nid in added if nid in kept_ids]

    if not added and not updated and not added_edges:
        return MergeOutcome(
            graph=base,
            batch=batch,
            new_source_ids=new_source_ids,
            reason="新来源没有带来任何新节点或新边（模型这次没给出可用结构）",
        )

    previous_growth = base.get("growth")
    batches = 0
    if isinstance(previous_growth, Mapping):
        raw_batches = previous_growth.get("batches")
        if isinstance(raw_batches, int) and not isinstance(raw_batches, bool):
            batches = raw_batches

    merged = dict(base)
    merged["nodes"] = nodes
    merged["edges"] = edges
    merged["applied_source_ids"] = sorted(applied | set(batch))
    merged["growth"] = {
        "batches": batches + 1,
        "last_batch": list(batch),
        "last_added": len(added),
        "last_updated": len(updated),
    }
    return MergeOutcome(
        graph=merged,
        added=tuple(added),
        updated=tuple(updated),
        added_edges=added_edges,
        dropped_nodes=dropped_nodes,
        untouched=untouched,
        changed=True,
        batch=batch,
        new_source_ids=new_source_ids,
    )


def _fresh_id(by_id: Mapping[str, Any]) -> str:
    """给新节点找一个没被占用的 ``n<数字>`` id（既有 id 一律不动）。"""
    index = len(by_id) + 1
    while f"n{index}" in by_id:
        index += 1
    return f"n{index}"


def build_source_graph(
    concept_graph: Mapping[str, Any] | None,
    sources: Mapping[str, Mapping[str, Any]],
    *,
    min_shared: int = DEFAULT_MIN_SHARED,
) -> dict[str, Any]:
    """从概念图**推导**资料层图：节点是来源，边是"共同支撑 ≥ min_shared 个概念"。

    ``sources`` 是 ``source_id -> 行``（`title/url/source_type/year/has_text`）。图上出现过
    但库里查不到的来源**也保留**（节点标 ``meta.missing=True``，标签退到 id）—— 图不该因为
    一条来源被删了就悄悄少一个点，那会让用户以为"这段关系从来不存在"。

    空图 / 单来源都回空边图，**不抛错**（新主题刚建图时就是这个形状）。
    """
    low, high = MIN_SHARED_BOUNDS
    threshold = max(low, min(int(min_shared), high))

    concept_ids: dict[str, set[str]] = {}
    for node in _iter_nodes(concept_graph):
        nid = _node_id(node)
        if not nid:
            continue
        for ref in _refs(node.get("source_ids")):
            concept_ids.setdefault(ref, set()).add(nid)

    nodes: list[dict[str, Any]] = []
    for source_id in sorted(concept_ids):
        row = sources.get(source_id) or {}
        url = str(row.get("url") or "").strip()
        title = str(row.get("title") or "").strip()
        nodes.append(
            {
                "nid": source_id,
                "label": title or _label_from_url(url) or source_id,
                "kind": "source",
                "source_ids": [source_id],
                "dropped_source_ids": [],
                "meta": {
                    "url": url or None,
                    "source_type": row.get("source_type") or None,
                    "year": row.get("year"),
                    "has_text": bool(row.get("has_text")),
                    "concept_count": len(concept_ids[source_id]),
                    "missing": not row,
                },
            }
        )

    ordered = sorted(concept_ids)
    edges: list[dict[str, Any]] = []
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            shared = len(concept_ids[left] & concept_ids[right])
            if shared < threshold:
                continue
            edges.append({"src": left, "dst": right, "rel": SHARED_CONCEPT_REL, "weight": shared})
    edges.sort(key=lambda edge: (-int(edge["weight"]), str(edge["src"]), str(edge["dst"])))

    return {
        "layer": "source",
        "min_shared": threshold,
        "nodes": nodes,
        "edges": edges,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "threshold": threshold,
            "missing_sources": sum(1 for node in nodes if node["meta"]["missing"]),
        },
    }


# --------------------------------------------------------------- 路径与搜索（两图层通用）


def clamp_depth(value: object) -> int:
    """把请求的 `max_depth` 夹到 ``1..MAX_PATH_DEPTH``。

    夹取而不报错：越界是"问得比图还深"，不是参数错 —— 回一张短图比回 `-32602` 有用。
    完全给不出数字时才退默认值（正常路径上 RPC 已经挡过类型）。
    """
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return DEFAULT_PATH_DEPTH
    try:
        depth = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PATH_DEPTH
    return max(1, min(depth, MAX_PATH_DEPTH))


def clamp_search_limit(value: object) -> int:
    """把请求的 `limit` 夹到 ``1..MAX_SEARCH_LIMIT``（与 `clamp_depth` 同一条口径：夹取不报错）。"""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return DEFAULT_SEARCH_LIMIT
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SEARCH_LIMIT
    return max(1, min(limit, MAX_SEARCH_LIMIT))


def node_index(graph: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """节点表 ``nid -> {nid,label,kind,source_ids}``（重复 id 取第一个，坏项跳过）。"""
    index: dict[str, dict[str, Any]] = {}
    for item in _iter_nodes(graph):
        nid = _node_id(item)
        if not nid or nid in index:
            continue
        index[nid] = {
            "nid": nid,
            "label": str(item.get("label") or nid),
            "kind": str(item.get("kind") or "concept"),
            "source_ids": _refs(item.get("source_ids")),
        }
    return index


def degree_map(graph: Mapping[str, Any] | None) -> dict[str, int]:
    """每个节点的度数（相邻边数，自环与悬空端点不计）。与 `topic.get_map` 的 `weight` 同口径。"""
    index = node_index(graph)
    degree = dict.fromkeys(index, 0)
    for item in _iter_edges(graph):
        src, dst = _edge_ends(item)
        if src == dst or src not in degree or dst not in degree:
            continue
        degree[src] += 1
        degree[dst] += 1
    return degree


def _path_edge(item: Mapping[str, Any]) -> dict[str, Any]:
    """路径里回一条边：**保留存储方向**（`src -> dst`），有 `weight` 才带（概念层不凭空造权重）。"""
    src, dst = _edge_ends(item)
    edge: dict[str, Any] = {"src": src, "dst": dst, "rel": _edge_rel(item)}
    if "weight" in item:
        edge["weight"] = _edge_weight(item)
    return edge


def _adjacency(graph: Mapping[str, Any] | None) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """**无向**邻接表（邻居按 id 排序，保证同样的图永远回同样的路径）。

    为什么无向：用户问的是"这两个概念怎么连起来"，不是"能不能顺着箭头走"。概念图上
    `prerequisite` 是有方向的，但"评测 → RAG"与"RAG → 评测"之间确实存在一条关系，
    按方向把可达判死会让一半查询变成"没有路径"。边上仍保留存储方向，界面照原方向画箭头。
    """
    index = node_index(graph)
    neighbors: dict[str, list[tuple[str, dict[str, Any]]]] = {nid: [] for nid in index}
    for item in _iter_edges(graph):
        src, dst = _edge_ends(item)
        if src == dst or src not in index or dst not in index:
            continue
        edge = _path_edge(item)
        neighbors[src].append((dst, edge))
        neighbors[dst].append((src, edge))
    for pairs in neighbors.values():
        pairs.sort(key=lambda pair: pair[0])
    return neighbors


def _distances(
    adjacency: Mapping[str, list[tuple[str, dict[str, Any]]]], start: str, *, limit: int
) -> dict[str, int]:
    """BFS 距离（边数计），只走到 `limit` 跳为止。"""
    dist = {start: 0}
    queue: deque[str] = deque([start])
    while queue:
        node = queue.popleft()
        if dist[node] >= limit:
            continue
        for nxt, _edge in adjacency.get(node, []):
            if nxt in dist:
                continue
            dist[nxt] = dist[node] + 1
            queue.append(nxt)
    return dist


def find_paths(
    graph: Mapping[str, Any] | None,
    start: str,
    end: str,
    *,
    max_depth: int = DEFAULT_PATH_DEPTH,
    max_paths: int = MAX_PATHS,
) -> list[dict[str, Any]]:
    """两个节点之间的**最短路径**（BFS 定长，再在最短路上枚举，最多 `max_paths` 条）。

    - 找不到 / 超出 `max_depth` → `[]`（**不报错**：没有关系是正常结论）；
    - 起点 == 终点 → 一条零长度路径（`nodes` 1 个、`edges` 空），如实说"就是同一个点"；
    - 命中的节点必须满足 ``dist(起点) + dist(终点) == 最短长度``，所以枚举出来的
      每一条都是最短路径，不会因为图稠密就退化成"随便找条路"；
    - 同长的多条只在节点序列不同时报多条（同一序列不同 `rel` 不重复报）。
    """
    index = node_index(graph)
    src_id = str(start or "").strip()
    dst_id = str(end or "").strip()
    if not src_id or not dst_id or src_id not in index or dst_id not in index:
        return []
    if src_id == dst_id:
        return [{"nodes": [src_id], "edges": []}]

    limit = clamp_depth(max_depth)
    adjacency = _adjacency(graph)
    forward = _distances(adjacency, src_id, limit=limit)
    depth = forward.get(dst_id)
    if depth is None or depth > limit:
        return []
    backward = _distances(adjacency, dst_id, limit=depth)

    cap = max(1, int(max_paths))
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    # 迭代式 DFS（深度 ≤ 8，不用递归也不用栈帧技巧）：只沿"离起点逐跳 +1 且在最短路上"的邻居走
    stack: list[tuple[str, list[str], list[dict[str, Any]]]] = [(src_id, [src_id], [])]
    while stack and len(found) < cap:
        node, nodes, edges = stack.pop()
        if node == dst_id:
            key = tuple(nodes)
            if key not in seen:
                seen.add(key)
                found.append({"nodes": list(nodes), "edges": list(edges)})
            continue
        for nxt, edge in adjacency.get(node, []):
            if forward.get(nxt) != forward.get(node, -1) + 1:
                continue
            if forward.get(nxt, 0) + backward.get(nxt, depth + 1) != depth:
                continue
            if nxt in nodes:
                continue
            stack.append((nxt, [*nodes, nxt], [*edges, edge]))
    return found


def search_nodes(
    graph: Mapping[str, Any] | None,
    query: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> list[dict[str, Any]]:
    """按标签**子串**找节点（`casefold`，中英文一视同仁）；空查询回 `[]`。

    排序：前缀命中在前 → 度数高的在前（连得多的概念更可能是用户要找的）→ 标签/id 字典序。
    回 `{id,label,kind,weight}`：`weight` 与 `topic.get_map` 同口径（度数），界面直接用它排。
    """
    text = str(query or "").strip().casefold()
    if not text:
        return []
    cap = clamp_search_limit(limit)
    degree = degree_map(graph)
    scored: list[tuple[int, int, str, str, dict[str, Any]]] = []
    for nid, node in node_index(graph).items():
        label = str(node["label"])
        pos = label.casefold().find(text)
        if pos < 0:
            continue
        hit = {
            "id": nid,
            "label": label,
            "kind": node["kind"],
            "weight": degree.get(nid, 0),
        }
        scored.append((0 if pos == 0 else 1, -hit["weight"], label.casefold(), nid, hit))
    scored.sort(key=lambda item: item[:4])
    return [item[4] for item in scored[:cap]]
