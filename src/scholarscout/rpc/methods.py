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

from scholarscout import __version__, paths, settings_io
from scholarscout.config import Config
from scholarscout.fetch.http import HttpClient
from scholarscout.providers.llm import LlmClient, LlmError, create_client
from scholarscout.providers.settings import provider_statuses
from scholarscout.rpc.errors import Code, RpcError
from scholarscout.runtime import loop, registry, titles
from scholarscout.state import archive, export_session, learning, sessions, traces
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
    #: `session_id -> turn_id`：正在跑的轮次（界面按会话取消时用）
    running: dict[str, str] = field(default_factory=dict)

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
            running=self.running,
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


@method("session.rename")
def session_rename(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """改会话标题（改过之后就不再被自动命名覆盖）。"""
    params = _params(params)
    session_id = _require(params, "session_id")
    title = _require(params, "title")
    stored = sessions.rename_session(ctx.db, session_id, str(title))
    if stored is None:
        raise RpcError(Code.NOT_FOUND, f"会话不存在：{session_id}")
    return {"ok": True, "session_id": session_id, "title": stored}


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


@method("session.pin")
def session_pin(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """置顶 / 取消置顶。不碰 `updated_at`（置顶只是换位置，不该假装"最近更新过"）。"""
    params = _params(params)
    session_id = _require(params, "session_id")
    pinned = bool(params.get("pinned", True))
    if not sessions.set_pinned(ctx.db, session_id, pinned):
        raise RpcError(Code.NOT_FOUND, f"会话不存在：{session_id}")
    return {"ok": True, "session_id": session_id, "pinned": pinned}


@method("session.delete")
def session_delete(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """删会话。返回各表删掉的行数 —— 界面要如实告诉用户删掉了什么。

    `keep_artifacts=true` 只删对话，留下这次会话产出的路线/地图/笔记。
    """
    params = _params(params)
    session_id = _require(params, "session_id")
    keep = bool(params.get("keep_artifacts", False))
    removed = sessions.delete_session(ctx.db, session_id, keep_artifacts=keep)
    if removed is None:
        raise RpcError(Code.NOT_FOUND, f"会话不存在：{session_id}")
    return {"ok": True, "session_id": session_id, "removed": removed, "kept_artifacts": keep}


@method("session.export")
def session_export(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """导出成可复制的 Markdown：既落一份文件到 `data/exports/`，也把正文回给界面复制。"""
    params = _params(params)
    session_id = _require(params, "session_id")
    try:
        result = export_session.export_to_file(ctx.db, session_id)
    except KeyError:
        raise RpcError(Code.NOT_FOUND, f"会话不存在：{session_id}") from None
    return {
        "ok": True,
        "session_id": session_id,
        "path": result["path"],
        "text": result["text"],
        "chars": len(result["text"]),
    }


# --------------------------------------------------------------------- 对话


def _title_before_turn(ctx: AppContext, session_id: str, text: str) -> None:
    """轮次开始前先把标题定下来 —— 用户一发送就能在左侧栏看到名字。

    顺序很重要（踩过）：用户消息一落库，`sessions.retitle_if_unnamed` 就会用**本地截断**
    起名；那个名字看起来像真标题，之后模型总结的标题**再也写不进去**。
    所以必须"先总结标题 → 再跑轮次"。

    任何失败都静默放过：标题不值得让一轮对话失败，本地兜底会补上。
    """
    if not ctx.config.generation.title_by_model or ctx.llm is None:
        return
    row = sessions.get_session(ctx.db, session_id)
    if row is not None and not sessions.is_unnamed(row["title"]):
        return  # 已经有真标题（含用户手动改名）→ 一次调用都不花
    suggestion = titles.suggest_title(ctx.llm, text, prompts_dir=ctx.config.summary_cfg.prompts_dir)
    if suggestion is None:
        return
    # 这一次调用的钱也要记（purpose="title"）—— 否则"关掉标题总结能省多少"没人答得出来
    traces.record_usage(
        ctx.db,
        model=ctx.config.chat.model,
        purpose="title",
        session_id=session_id,
        prompt_tokens=suggestion.prompt_tokens,
        completion_tokens=suggestion.completion_tokens,
    )
    applied = sessions.set_title_if_unnamed(ctx.db, session_id, suggestion.title)
    if applied and ctx.emit is not None:
        ctx.emit("session.updated", {"session_id": session_id, "title": applied, "source": "model"})


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

    _title_before_turn(ctx, session_id, text)  # 标题在轮次前定下来（用户一发送就能看到）
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
    """请求取消一轮对话。

    **可以按 `session_id` 取消**（界面用这个）：整轮结束前前端拿不到 `turn_id`，
    只按 turn_id 取消的话，这个 RPC 从设计上就无法被界面调用。
    `turn_id` 仍然接受（脚本/测试用）。

    执行时机是**步间检查**：正在跑的那一步（模型调用或工具调用）结束后才生效 ——
    界面上必须如实这么提示，别让用户以为点了就立刻停。
    """
    params = _params(params)
    turn_id = str(params.get("turn_id") or "").strip()
    session_id = str(params.get("session_id") or "").strip()
    if not turn_id and session_id:
        turn_id = ctx.running.get(session_id, "")
    if not turn_id:
        return {
            "ok": False,
            "reason": "no_running_turn",
            "note": "这个会话当前没有正在跑的轮次（可能已经结束）",
        }
    ctx.cancelled.add(turn_id)
    return {
        "ok": True,
        "turn_id": turn_id,
        "note": "已请求取消：当前这一步结束后生效（步间检查）",
    }


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


@method("session.usage")
def session_usage(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """这个会话一共花了多少（token 与调用次数）。

    为什么值得单列：一轮 3–12 分钟、多次模型调用，用户只看得到"这一轮的 token"，
    看不到"这次会话累计"。口径：直接来自 `usage_records`（唯一取数入口），
    单价未配置时 `est_cost` 回 None —— **不猜价格**，只报可核对的 token。
    """
    params = _params(params)
    session_id = _require(params, "session_id")
    total = ctx.db.query_one(
        "SELECT count(*) AS calls, coalesce(sum(prompt_tokens),0) AS prompt_tokens,"
        " coalesce(sum(completion_tokens),0) AS completion_tokens,"
        " coalesce(sum(est_cost),0.0) AS est_cost FROM usage_records WHERE session_id = ?",
        (session_id,),
    )
    by_purpose = ctx.db.query(
        "SELECT coalesce(purpose,'?') AS purpose, count(*) AS calls,"
        " coalesce(sum(prompt_tokens),0) AS prompt_tokens,"
        " coalesce(sum(completion_tokens),0) AS completion_tokens"
        " FROM usage_records WHERE session_id = ? GROUP BY 1 ORDER BY calls DESC",
        (session_id,),
    )
    row = dict(total) if total is not None else {}
    cost = float(row.get("est_cost") or 0.0)
    return {
        "session_id": session_id,
        "calls": int(row.get("calls") or 0),
        "tokens": {
            "prompt": int(row.get("prompt_tokens") or 0),
            "completion": int(row.get("completion_tokens") or 0),
        },
        # 单价没配就不报金额（报 0 会让人以为"免费"）
        "est_cost": cost if cost > 0 else None,
        "by_purpose": [dict(item) for item in by_purpose],
    }


# --------------------------------------------------------------------- 设置


@method("settings.get")
def settings_get(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """当前设置（**密钥只报"是否已配置"，值永不回传**）。"""
    return settings_io.read_settings(ctx.config)


@method("settings.update")
def settings_update(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """改设置：校验 → 写回 `config.yaml`（密钥写 `.env`）→ **立即生效**（重建模型客户端）。"""
    params = _params(params)
    updates = params.get("updates")
    if not isinstance(updates, dict):
        raise RpcError(Code.INVALID_PARAMS, "updates 必须是一个对象")
    try:
        notes: list[str] = []
        new_config = settings_io.apply_updates(ctx.config, updates, notes=notes)
    except settings_io.SettingsError as exc:
        raise RpcError(Code.INVALID_PARAMS, str(exc)) from exc
    ctx.config = new_config
    try:
        ctx.llm = create_client(new_config.chat)
    except LlmError:
        # 换了地址/模型但密钥没配好 → 客户端置空，health 会如实显示"未配置模型"
        ctx.llm = None
    return {"ok": True, "settings": settings_io.read_settings(new_config), "notes": notes}


@method("settings.usage")
def settings_usage(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """磁盘占用（数据库、抓取缓存、原件、全文、笔记、run/）。"""
    return {"usage": settings_io.data_usage(), "cleanable": settings_io.CLEANABLE}


@method("settings.cleanup")
def settings_cleanup(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """清理**缓存类**目录。**资料本体（data/raw、text、notes）与数据库不在可清理范围内。**

    `older_than_days` 给定时只删够旧的文件 —— 全清会让下次检索重新下载一遍。
    """
    params = _params(params)
    target = str(params.get("target") or "fetch_cache")
    raw_days = params.get("older_than_days")
    days = (
        float(raw_days)
        if isinstance(raw_days, int | float) and not isinstance(raw_days, bool)
        else None
    )
    try:
        removed = settings_io.cleanup(target=target, older_than_days=days)
    except settings_io.SettingsError as exc:
        raise RpcError(Code.INVALID_PARAMS, str(exc)) from exc
    return {"ok": True, "target": target, "older_than_days": days, "removed": removed}


@method("settings.optimize")
def settings_optimize(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """整理数据库（`VACUUM` + `PRAGMA optimize`）：**只整理、不删数据**。

    为什么值得单独给个入口：删会话/删资料不会让库文件变小，用户会以为"删了没用"。
    """
    del params
    result = settings_io.optimize_database(ctx.db)
    return {"ok": True, **result, "usage": settings_io.data_usage().get("database", {})}


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
