"""`channel.send_file`：把**生成物 / 已入库资料 / 本地路径**发到飞书私聊（docs/17 §3.12）。

## 为什么这个 RPC 在 `rpc/` 而不在渠道层

渠道层（`channels/feishu.py`）只做「意图解析」（`parse_file_request`），**不读库、不出网**
（`tests/test_layering.py::RULES["channels"]`）。要发文件必须同时用两样它不许碰的东西：

- `state`（查 `topic_maps` / `learning_routes` / `notes` / `documents`）；
- `fetch`（上传 + 发送，唯一出网口 `fetch/http.py`）。

所以整条链路落在协议层这一个方法里，渠道层只递三个字段（谁要、哪一类、关键词）：

    飞书消息 → channels/feishu.py（规则识别）→ channel.send_file → 查库/导出/上传/发送

## 三类目标（用户拍板：生成物和已入库资料都要能发）

| kind | 目标 | 取法 |
|---|---|---|
| `artifact` | 本会话最近的生成物（知识地图 / 学习路线 / 笔记） | 先按 `session_id` 找，找不到退到全库最新 |
| `library` | 用户点名的已入库资料 | `documents.title` / `sources.title` 模糊匹配（`LIKE` 语义，纯 Python 过滤），发原件 |
| `path` | 用户给的本地路径 | 只允许 `data/` 与 `run/` 之下（`paths.resolve_rel`），其它一律拒绝并说清原因 |

## 三条硬约定

1. **找不到就不发**：回一句可读的话 + 候选清单（最多 6 条），绝不猜一个文件发出去；
2. **失败一律 `ok=false` + `reply`**：不抛异常（渠道层那边要拿 `reply` 原样回给用户）；
3. **只私聊**：`fetch/feishu_send.send_file` 的接收者是 `open_id`，形参里没有 chat_id
   —— 结构上就发不到群里（项目的 `dm_only` 口径）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scholarscout import paths
from scholarscout.fetch import feishu_send
from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.registry import AppContext, _params, method
from scholarscout.state import learning

#: 允许的三类目标（拼错一律按 `artifact` 处理，并如实记在回包 `kind` 里）
FILE_KINDS: tuple[str, ...] = ("artifact", "library", "path")
#: 生成物类型 → 展示名（候选清单里用）
ARTIFACT_LABELS: dict[str, str] = {
    "topic": "知识地图",
    "route": "学习路线",
    "note": "笔记",
}
#: 导出目录（`data/exports/`；`paths.under_data` 允许 data/ 下任意子目录）
EXPORT_DIR = "exports"
#: 候选清单最多列几条（再多用户也选不过来）
MAX_CANDIDATES = 6


@dataclass(frozen=True)
class FileTarget:
    """解析出来的"要发的那个文件"。`path` 一定真实存在（调用方据此才敢上传）。"""

    path: Path
    name: str
    title: str
    kind: str


# ------------------------------------------------------------------ 库里的目标


def _session_id(db: Any, session_key: str) -> str:
    """`chat_id` → `session_id`（**只读**：不为一次发文件凭空建会话）。"""
    key = (session_key or "").strip()
    if not key:
        return ""
    row = db.query_one("SELECT id FROM sessions WHERE session_key = ?", (key,))
    return str(row["id"]) if row is not None else ""


def _pick(items: list[dict[str, Any]], *, query: str, session_id: str) -> dict[str, Any] | None:
    """先按 `query` 收窄，再优先本会话，最后退到全库最新（列表本身已按时间倒序）。"""
    pool = items
    if query:
        needle = query.lower()
        pool = [
            item
            for item in pool
            if needle in str(item.get("topic") or item.get("title") or "").lower()
        ]
    if session_id:
        scoped = [item for item in pool if str(item.get("session_id") or "") == session_id]
        if scoped:
            return scoped[0]
    return pool[0] if pool else None


def _write_export(name: str, text: str) -> Path:
    """把导出件写到 `data/exports/<name>`（覆盖写：同一件产物重复发不该越积越多）。"""
    target = paths.under_data(Path(EXPORT_DIR) / name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _safe_stem(text: str, fallback: str) -> str:
    """把标题变成文件名（沿用 `paths.slugify` 的规则：非法字符与超长都收干净）。"""
    return paths.slugify(text, fallback=fallback)


def _topic_markdown(payload: dict[str, Any]) -> str:
    # 真正的类型收窄：先把"什么类型都可能"的字段落进变量，再做 `isinstance` 判断。
    # （直接对 `payload.get("graph")` 这种调用结果做判断，收窄落不到任何名字上，
    #   mypy 只看到 `Any | dict | None`，下面就会报 union-attr / index / arg-type。）
    raw_graph = payload.get("graph")
    graph: dict[str, Any] = {}
    if isinstance(raw_graph, dict):
        graph = raw_graph
    raw_nodes = graph.get("nodes")
    nodes: list[Any] = []
    if isinstance(raw_nodes, list):
        nodes = raw_nodes
    lines = [f"# 知识地图：{payload.get('topic') or '未命名'}", ""]
    if graph.get("mode"):
        lines.append(f"- 生成方式：{graph['mode']}")
    lines.append(f"- 节点数：{len(nodes)}")
    if payload.get("created_at"):
        lines.append(f"- 生成时间：{payload['created_at']}")
    lines.append("")
    for node in nodes:
        if not isinstance(node, dict):
            continue
        label = str(node.get("label") or node.get("nid") or "（未命名节点）")
        lines.append(f"## {label}")
        if node.get("kind"):
            lines.append(f"类型：{node['kind']}")
        sources = node.get("source_ids") or []
        refs = "、".join(str(item) for item in sources) if sources else "（无出处）"
        lines.append(f"出处：{refs}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _note_markdown(payload: dict[str, Any]) -> str:
    lines = [f"# {payload.get('title') or '笔记'}", ""]
    if payload.get("kind"):
        lines.append(f"- 类型：{payload['kind']}")
    tags = payload.get("tags") or []
    if tags:
        lines.append(f"- 标签：{'、'.join(str(item) for item in tags)}")
    if payload.get("created_at"):
        lines.append(f"- 生成时间：{payload['created_at']}")
    lines.append("")
    lines.append(str(payload.get("content") or ""))
    return "\n".join(lines).rstrip() + "\n"


def _artifact_target(
    ctx: AppContext, *, kind: str, session_id: str
) -> tuple[FileTarget | None, list[str], str]:
    """生成物：知识地图 / 学习路线 / 笔记。`kind` 为空 = 三类里**最新的那一件**。"""
    wanted = kind if kind in ARTIFACT_LABELS else ""
    candidates: list[str] = []
    picks: list[tuple[str, str, dict[str, Any]]] = []

    if wanted in ("", "topic"):
        maps = learning.list_topic_maps(ctx.db, limit=50)
        candidates.extend(f"{ARTIFACT_LABELS['topic']}：{item['topic']}" for item in maps)
        picked = _pick(maps, query="", session_id=session_id) if maps else None
        if picked is not None:
            picks.append((str(picked.get("created_at") or ""), "topic", picked))

    if wanted in ("", "route"):
        routes = learning.list_routes(ctx.db, limit=50)
        candidates.extend(f"{ARTIFACT_LABELS['route']}：{item['topic']}" for item in routes)
        picked = _pick(routes, query="", session_id=session_id) if routes else None
        if picked is not None:
            picks.append((str(picked.get("created_at") or ""), "route", picked))

    if wanted in ("", "note"):
        notes = learning.list_notes(ctx.db, limit=50)
        candidates.extend(
            f"{ARTIFACT_LABELS['note']}：{item.get('title') or item.get('id')}" for item in notes
        )
        picked = _pick(notes, query="", session_id=session_id) if notes else None
        if picked is not None:
            picks.append((str(picked.get("created_at") or ""), "note", picked))

    if not picks:
        if not candidates:
            return None, [], "库里还没有可发的生成物（知识地图 / 学习路线 / 笔记）。"
        return None, candidates, "没找到最近的那件生成物。"

    # 三类里挑最新的（要的是"最近的那件生成物"）；显式点名某类时就只有一条候选
    picks.sort(key=lambda item: (item[0], item[1]), reverse=True)
    picked_kind, picked = picks[0][1], picks[0][2]
    if picked_kind == "topic":
        payload = learning.get_topic_map(ctx.db, str(picked["topic_id"]))
        if payload is not None:
            title = str(payload.get("topic") or "知识地图")
            name = f"topic_{_safe_stem(title, 'map')}.md"
            path = _write_export(name, _topic_markdown(payload))
            return FileTarget(path, name, title, "topic"), candidates, ""
    elif picked_kind == "route":
        exported = learning_route_export(ctx, str(picked["route_id"]))
        if exported is not None:
            rel, title = exported
            try:
                abs_path: Path | None = paths.resolve_rel(rel)
            except Exception:
                abs_path = None
            if abs_path is not None and abs_path.is_file():
                return FileTarget(abs_path, abs_path.name, title, "route"), candidates, ""
    else:
        payload = learning.get_note(ctx.db, str(picked["id"]))
        if payload is not None:
            title = str(payload.get("title") or "笔记")
            name = f"note_{_safe_stem(title, 'note')}.md"
            path = _write_export(name, _note_markdown(payload))
            return FileTarget(path, name, title, "note"), candidates, ""

    return None, candidates, "找到了这件生成物，但导出文件时失败了（详见运行日志）。"


def learning_route_export(ctx: AppContext, route_id: str) -> tuple[str, str] | None:
    """借 `route.export`（同一个 markdown 渲染）把路线写到 `data/exports/`，回 `(相对路径, 标题)`。"""
    from scholarscout.rpc.learning import route_export

    payload = learning.get_route(ctx.db, route_id)
    if payload is None:
        return None
    title = str(payload.get("topic") or "学习路线")
    out = f"{EXPORT_DIR}/route_{_safe_stem(title, 'route')}.md"
    result = route_export(ctx, {"route_id": route_id, "out": out})
    rel = str(result.get("path") or "")
    return (rel, title) if rel else None


def _document_target(ctx: AppContext, *, query: str) -> tuple[FileTarget | None, list[str], str]:
    """已入库资料：按标题模糊匹配 `documents`（含 `sources.title`），发**原件**。"""
    rows = ctx.db.query(
        """
        SELECT d.id AS doc_id, d.title, d.raw_path, d.text_path, d.fetched_at,
               s.title AS source_title
        FROM documents d LEFT JOIN sources s ON s.id = d.source_id
        ORDER BY d.fetched_at DESC
        """
    )
    candidates: list[str] = []
    matched: list[dict[str, Any]] = []
    needle = query.lower()
    for row in rows:
        item = dict(row)
        title = str(item.get("title") or item.get("source_title") or "")
        if title:
            candidates.append(title)
        haystack = f"{item.get('title') or ''} {item.get('source_title') or ''}".lower()
        if not needle or needle in haystack:
            matched.append(item)

    for item in matched:
        for key in ("raw_path", "text_path"):
            rel = str(item.get(key) or "").strip()
            if not rel:
                continue
            try:
                abs_path = paths.resolve_rel(rel)
            except Exception:
                continue
            if abs_path.is_file() and abs_path.stat().st_size > 0:
                title = str(item.get("title") or item.get("source_title") or abs_path.stem)
                return FileTarget(abs_path, abs_path.name, title, "library"), [], ""
    if query and not matched:
        return None, candidates[:MAX_CANDIDATES], f"没找到标题里带「{query}」的已入库资料。"
    if matched:
        return (
            None,
            candidates[:MAX_CANDIDATES],
            "库里记着这份资料，但原件在盘上找不到（可能被移动/删除）。",
        )
    return None, [], "资料库还是空的：先检索并入库一份资料。"


def _path_target(query: str) -> tuple[FileTarget | None, list[str], str]:
    """用户点名的本地路径：**只允许项目里的 `data/` 与 `run/`**（`paths.resolve_rel` 的口径）。"""
    raw = (query or "").strip().strip("\"'“”")
    if not raw:
        return None, [], "没说要发哪个文件（给个路径，或说要哪件生成物/资料）。"
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            rel = paths.rel_to_home(candidate)
        except ValueError:
            return None, [], f"只允许发项目目录（data/ 与 run/）里的文件，这个不在里面：{raw}"
    else:
        rel = raw
    try:
        abs_path = paths.resolve_rel(rel)
    except Exception:
        return None, [], f"只允许发项目目录（data/ 与 run/）里的文件：{raw}"
    if not abs_path.is_file():
        return None, [], f"这个路径没有文件：{raw}"
    return FileTarget(abs_path, abs_path.name, abs_path.stem, "path"), [], ""


# ------------------------------------------------------------------ RPC


def _reply_enabled(ctx: AppContext) -> bool:
    """出站开关（`runtime.feishu_reply_enabled`，默认关：关着一次外网都不发）。"""
    runtime = getattr(getattr(ctx, "config", None), "runtime", None)
    return bool(getattr(runtime, "feishu_reply_enabled", False))


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _not_found_reply(query: str, candidates: list[str], error: str) -> str:
    """找不到时回给用户的整段话（**列候选**，不瞎发）。"""
    lines = [error or (f"没找到「{query}」对应的文件。" if query else "没找到可发的文件。")]
    if candidates:
        lines.append("现在能发这些：")
        lines.extend(f"· {name}" for name in candidates[:MAX_CANDIDATES])
    return "\n".join(lines)


@method("channel.send_file")
def channel_send_file(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """把一份文件**私聊**发给发来消息的人（生成物 / 已入库资料 / 项目内路径）。

    回包只有 `ok` / `kind` / `file_name` / `message_id` / `reply` / `candidates`：
    渠道层拿到 `reply` 原样发回去，**不在渠道层拼文案**（那里看不见候选清单）。
    """
    params = _params(params)
    platform = str(params.get("platform") or "feishu").strip().lower()
    if platform != "feishu":
        return {"ok": False, "reply": "本渠道目前只支持飞书，发文件功能还没做到别的平台。"}
    open_id = str(params.get("open_id") or "").strip()
    if not open_id:
        return {
            "ok": False,
            "reply": "发不了：这条消息里没有发送者的 open_id（无法定位私聊对象）。",
        }
    if ctx.db is None:
        raise RpcError(Code.NOT_CONFIGURED, "没有数据库，无法解析要发哪份文件")

    kind = str(params.get("kind") or "artifact").strip().lower()
    if kind not in FILE_KINDS:
        kind = "artifact"
    query = str(params.get("query") or "").strip()
    session_id = _session_id(ctx.db, str(params.get("session_key") or ""))

    if kind == "library":
        target, candidates, error = _document_target(ctx, query=query)
    elif kind == "path":
        target, candidates, error = _path_target(query)
    else:
        # `query` 在 artifact 这一类里装的是**生成物子类型**（topic/route/note，见渠道层的解析）
        target, candidates, error = _artifact_target(ctx, kind=query, session_id=session_id)
    if target is None:
        return {
            "ok": False,
            "kind": kind,
            "reply": _not_found_reply(query, candidates, error),
            "candidates": candidates[:MAX_CANDIDATES],
        }

    sender = feishu_send.build_file_sender(
        ctx.http,
        enabled=_reply_enabled(ctx),
        app_id=_env(feishu_send.APP_ID_ENV),
        app_secret=_env(feishu_send.APP_SECRET_ENV),
        domain=str(params.get("domain") or feishu_send.DEFAULT_DOMAIN),
    )
    outcome = sender(open_id, str(target.path))
    size_kb = max(1, round(int(outcome.get("size_bytes") or 0) / 1024))
    if not outcome.get("ok"):
        return {
            "ok": False,
            "kind": kind,
            "file_name": target.name,
            "reply": f"没发出去：{outcome.get('error') or '未知原因（详见运行日志）'}",
        }
    return {
        "ok": True,
        "kind": kind,
        "file_name": target.name,
        "message_id": str(outcome.get("message_id") or ""),
        "reply": f"已发送：{target.name}（{size_kb} KB）",
    }
