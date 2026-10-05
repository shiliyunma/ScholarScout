"""RPC 方法表（与 docs/01 §3 一一对应）。

M2 已实现：system.*、config.*、session.*、chat.*、trace.list、archive.list/get/paths
M3 起补：research/search 相关的工具在 runtime/registry 里注册，RPC 层不需要改。
"""

from __future__ import annotations

import math
import os
import platform
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scholarscout import __version__, paths
from scholarscout.config import Config
from scholarscout.fetch.http import HttpClient
from scholarscout.providers.llm import LlmClient, LlmError
from scholarscout.rpc.errors import Code, RpcError
from scholarscout.runtime import loop, registry
from scholarscout.state import archive, learning, sessions, traces
from scholarscout.state.db import Database

Handler = Callable[["AppContext", dict[str, Any]], Any]


@dataclass
class AppContext:
    """一次进程运行期的全部依赖。测试里可以塞桩模型与桩出网。"""

    config: Config
    db: Database
    llm: LlmClient | None = None
    http: HttpClient | None = None
    toolsets: Sequence[str] = ("research",)
    emit: Callable[[str, dict[str, Any]], None] | None = None
    cancelled: set[str] = field(default_factory=set)

    def require_llm(self) -> LlmClient:
        if self.llm is None:
            raise RpcError(Code.NOT_CONFIGURED, "尚未配置模型客户端（检查 .env 的 LLM_API_KEY）")
        return self.llm

    def turn_context(self) -> loop.TurnContext:
        return loop.TurnContext(
            db=self.db,
            config=self.config,
            llm=self.require_llm(),
            http=self.http,
            cancelled=self.cancelled,
        )

    def close(self) -> None:
        """关掉出网客户端与数据库（HTTP 连接池不关会留句柄）。"""
        if self.http is not None:
            self.http.close()
        self.db.close()


_METHODS: dict[str, Handler] = {}


def method(name: str) -> Callable[[Handler], Handler]:
    def decorator(func: Handler) -> Handler:
        if name in _METHODS:
            raise ValueError(f"RPC 方法重复注册：{name}")
        _METHODS[name] = func
        return func

    return decorator


def method_names() -> list[str]:
    """已注册的方法名（桌面壳的资源校验拿它比对前端调用，见 docs/01 §3）。"""
    return sorted(_METHODS)


def _params(params: dict[str, Any] | None) -> dict[str, Any]:
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise RpcError(Code.INVALID_PARAMS, "params 必须是对象")
    return params


def _require(params: dict[str, Any], key: str, kind: type | tuple[type, ...] = str) -> Any:
    if key not in params:
        raise RpcError(Code.INVALID_PARAMS, f"缺参数：{key}")
    value = params[key]
    if not isinstance(value, kind):
        raise RpcError(Code.INVALID_PARAMS, f"参数 {key} 类型不对：期望 {kind}")
    return value


def _int_param(params: dict[str, Any], key: str, default: int) -> int:
    value = params.get(key, default)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise RpcError(Code.INVALID_PARAMS, f"参数 {key} 必须是数字")
    return int(value)


# --------------------------------------------------------------------- 系统


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


@method("config.get")
def config_get(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """**绝不含密钥**（docs/01 §3.1）。"""
    return {
        "llm": {
            "model": ctx.config.chat.model,
            "base_url": ctx.config.chat.base_url,
            "api_key_set": bool(ctx.config.api_key()),
        },
        "runtime": {
            "max_tool_calls_per_turn": ctx.config.runtime.max_tool_calls_per_turn,
            "allow_clarify": ctx.config.runtime.allow_clarify,
        },
        "features": {
            "enable_kb": ctx.config.features.enable_kb,
            "enable_mineru": ctx.config.features.enable_mineru,
            "enable_topic_map": ctx.config.features.enable_topic_map,
            "enable_route_plan": ctx.config.features.enable_route_plan,
        },
        "paths": {
            "data_dir": str(ctx.config.data_dir),
            "db_path": str(ctx.config.db_path),
        },
    }


# --------------------------------------------------------------------- 会话


@method("session.create")
def session_create(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    title = params.get("title")
    session_key = params.get("session_key")
    session_id = sessions.create_session(
        ctx.db,
        title=title if isinstance(title, str) else None,
        session_key=session_key if isinstance(session_key, str) else None,
        source="desktop" if ctx.emit else "cli",
        model=ctx.config.chat.model,
    )
    loop.mark_system_prompt(ctx.db, session_id, toolsets=ctx.toolsets, config=ctx.config)
    row = sessions.get_session(ctx.db, session_id) or {}
    return {"session_id": session_id, "created_at": row.get("created_at")}


@method("session.list")
def session_list(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    limit = _int_param(params, "limit", 50)
    offset = _int_param(params, "offset", 0)
    return {"items": sessions.list_sessions(ctx.db, limit=limit, offset=offset)}


@method("session.messages")
def session_messages(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    session_id = _require(params, "session_id")
    limit = _int_param(params, "limit", 50)
    before = params.get("before_idx")
    rows, has_more = sessions.list_messages(
        ctx.db,
        session_id,
        limit=limit,
        before_idx=int(before) if isinstance(before, int | float) else None,
    )
    return {"items": rows, "has_more": has_more}


# --------------------------------------------------------------------- 对话


@method("chat.send")
def chat_send(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    session_id = _require(params, "session_id")
    text = _require(params, "text")
    if not text.strip():
        raise RpcError(Code.INVALID_PARAMS, "text 不能为空")
    if sessions.get_session(ctx.db, session_id) is None:
        raise RpcError(Code.NOT_FOUND, f"会话不存在：{session_id}")
    toolsets = params.get("toolsets") or list(ctx.toolsets)
    if not isinstance(toolsets, list):
        raise RpcError(Code.INVALID_PARAMS, "toolsets 必须是数组")

    result = loop.run_turn(
        ctx.turn_context(),
        session_id=session_id,
        text=text,
        toolsets=[str(t) for t in toolsets],
        emit=ctx.emit,
    )
    return result.as_dict()


@method("chat.cancel")
def chat_cancel(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    turn_id = _require(params, "turn_id")
    ctx.cancelled.add(turn_id)
    return {"ok": True, "note": "取消在下一步生效（当前实现为步间检查）"}


# --------------------------------------------------------------------- 轨迹


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


@method("archive.list")
def archive_list(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    query = params.get("query")
    limit = _int_param(params, "limit", 50)
    offset = _int_param(params, "offset", 0)
    tier = params.get("tier")

    if isinstance(query, str) and query.strip():
        from scholarscout.state.search import search_summaries

        hits = search_summaries(ctx.db, query, limit=limit)
        items = []
        for hit in hits:
            row = ctx.db.query_one(
                """
                SELECT s.doc_id, s.source_id, s.title_zh, s.summary_short,
                       d.raw_path, d.text_path, d.extract_status
                FROM summaries s JOIN documents d ON d.id = s.doc_id
                WHERE s.id = ?
                """,
                (hit.id,),
            )
            if row is not None:
                items.append({**dict(row), "hit_path": hit.path, "snippet": hit.snippet})
        return {
            "items": items,
            "total": len(items),
            "search_path": "fts" if hits and hits[0].path == "fts" else "like",
        }

    rows, total = archive.list_documents(
        ctx.db,
        source_type=params.get("source_type")
        if isinstance(params.get("source_type"), str)
        else None,
        tier=int(tier) if isinstance(tier, int | float) else None,
        year_from=(
            int(params["year_from"]) if isinstance(params.get("year_from"), int | float) else None
        ),
        limit=limit,
        offset=offset,
    )
    return {"items": rows, "total": total}


@method("archive.get")
def archive_get(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    doc_id = _require(params, "doc_id")
    payload = archive.get_document(ctx.db, doc_id)
    if payload is None:
        raise RpcError(Code.NOT_FOUND, f"资料不存在：{doc_id}")
    payload["paths"] = archive.document_paths(ctx.db, doc_id)
    if params.get("text"):
        rel = payload["document"].get("text_path")
        if rel:
            file_path = paths.home() / str(rel)
            payload["text"] = file_path.read_text(encoding="utf-8") if file_path.is_file() else None
        else:
            payload["text"] = None
    return payload


@method("archive.paths")
def archive_paths(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    doc_id = _require(params, "doc_id")
    payload = archive.document_paths(ctx.db, doc_id)
    if payload is None:
        raise RpcError(Code.NOT_FOUND, f"资料不存在：{doc_id}")
    return payload


def _launch_with_system(target: Path) -> None:
    """用系统默认程序打开文件（只在桌面壳拉起的进程里跑）。"""
    system = platform.system()
    if system == "Windows":
        os.startfile(str(target))
    elif system == "Darwin":
        subprocess.run(["open", str(target)], check=False)
    else:
        subprocess.run(["xdg-open", str(target)], check=False)


def _reveal_in_file_manager(target: Path) -> None:
    """在文件管理器里定位这个文件（Windows 用 explorer /select）。"""
    system = platform.system()
    if system == "Windows":
        subprocess.run(["explorer", "/select,", str(target)], check=False)
    elif system == "Darwin":
        subprocess.run(["open", "-R", str(target)], check=False)
    else:
        subprocess.run(["xdg-open", str(target.parent)], check=False)


@method("archive.open_raw")
def archive_open_raw(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """用系统默认程序打开资料原件（相对路径必须落在 data/ 下）。"""
    params = _params(params)
    doc_id = _require(params, "doc_id")
    payload = archive.document_paths(ctx.db, doc_id)
    if payload is None:
        raise RpcError(Code.NOT_FOUND, f"资料不存在：{doc_id}")
    rel = str(payload["raw_path"])
    target = paths.resolve_rel(rel)
    if not target.is_file():
        raise RpcError(Code.NOT_FOUND, f"原件不在磁盘上：{rel}")
    _launch_with_system(target)
    return {"ok": True, "path": rel}


@method("archive.reveal")
def archive_reveal(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """在文件管理器中定位文件：给 ``doc_id``（原件/全文）或直接给 ``path``（笔记、路线导出）。"""
    params = _params(params)
    which = params.get("which") if isinstance(params.get("which"), str) else "raw"
    rel: str
    raw_path = params.get("path")
    if isinstance(raw_path, str) and raw_path.strip():
        rel = raw_path.strip()
    else:
        doc_id = _require(params, "doc_id")
        payload = archive.document_paths(ctx.db, doc_id)
        if payload is None:
            raise RpcError(Code.NOT_FOUND, f"资料不存在：{doc_id}")
        key = "text_path" if which == "text" else "raw_path"
        rel = str(payload[key])
    target = paths.resolve_rel(rel)
    if not target.exists():
        raise RpcError(Code.NOT_FOUND, f"文件不在磁盘上：{rel}")
    _reveal_in_file_manager(target)
    return {"ok": True, "path": rel, "which": which}


# --------------------------------------------------------------------- 学习产物


@method("topic.list")
def topic_list(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """列出历史主题图（轻量条目，正文走 `topic.get`）。

    前端原先只能看本会话刚生成的产物，进程一退就再也点不开 —— 这个方法让
    库里攒下的产物都能翻出来看。
    """
    params = _params(params)
    limit = int(params.get("limit") or 50)
    return {"items": learning.list_topic_maps(ctx.db, limit=limit)}


@method("route.list")
def route_list(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    limit = int(params.get("limit") or 50)
    return {"items": learning.list_routes(ctx.db, limit=limit)}


@method("topic.get")
def topic_get(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    topic_id = _require(params, "topic_id")
    payload = learning.get_topic_map(ctx.db, topic_id)
    if payload is None:
        raise RpcError(Code.NOT_FOUND, f"主题地图不存在：{topic_id}")
    return payload


@method("route.get")
def route_get(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    route_id = _require(params, "route_id")
    payload = learning.get_route(ctx.db, route_id)
    if payload is None:
        raise RpcError(Code.NOT_FOUND, f"学习路线不存在：{route_id}")
    return payload


@method("route.export")
def route_export(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """把路线导出成可直接看的 Markdown（条目带 why 与来源链接）。"""
    params = _params(params)
    route_id = _require(params, "route_id")
    payload = learning.get_route(ctx.db, route_id)
    if payload is None:
        raise RpcError(Code.NOT_FOUND, f"学习路线不存在：{route_id}")

    plan = payload["plan"]
    lines = [f"# 学习路线：{payload['topic']}", ""]
    lines.append(
        f"- 水平：{payload.get('level') or '未指定'}｜周数：{payload.get('weeks') or len(plan.get('weeks', []))}"
    )
    lines.append(f"- 生成方式：{plan.get('mode', 'unknown')}")
    lines.append("")
    for week in plan.get("weeks", []):
        lines.append(f"## 第 {week['idx']} 周：{week.get('goal', '')}")
        lines.append("")
        for item in week.get("items", []):
            source = item.get("source_id")
            ref = f"（`{source}`）" if source else "（无出处）"
            lines.append(
                f"- **{item.get('title', '')}**｜{item.get('kind', '')}｜约 {item.get('est_minutes', 0)} 分钟{ref}"
            )
            if item.get("why"):
                lines.append(f"  - 为什么读它：{item['why']}")
        if week.get("checkpoint"):
            lines.append("")
            lines.append(f"**检查点**：{week['checkpoint']}")
        lines.append("")

    out = params.get("out")
    if isinstance(out, str) and out.strip():
        target = paths.under_data(out.strip())
    else:
        target = paths.under_data(Path("notes") / f"route_{route_id}.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    return {"ok": True, "path": paths.rel_to_home(target), "route_id": route_id}


@method("note.list")
def note_list(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    kind = params.get("kind") if isinstance(params.get("kind"), str) else None
    limit = _int_param(params, "limit", 50)
    return {"items": learning.list_notes(ctx.db, kind=kind, limit=limit)}


@method("note.get")
def note_get(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    params = _params(params)
    note_id = _require(params, "note_id")
    payload = learning.get_note(ctx.db, note_id)
    if payload is None:
        raise RpcError(Code.NOT_FOUND, f"笔记不存在：{note_id}")
    return payload


# --------------------------------------------------------------------- 分发


def dispatch(ctx: AppContext, name: str, params: dict[str, Any] | None = None) -> Any:
    handler = _METHODS.get(name)
    if handler is None:
        raise RpcError(Code.METHOD_NOT_FOUND, f"未知方法：{name}")
    return handler(ctx, _params(params))


def llm_ok(ctx: AppContext) -> bool:
    """给前端做能力探测用；不抛异常。"""
    try:
        ctx.require_llm()
        return True
    except (RpcError, LlmError):
        return False


def clamp_limit(value: Any, default: int, maximum: int = 500) -> int:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return default
    return max(1, min(int(value), maximum))


def is_finite(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
