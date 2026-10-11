"""资料库：`archive.*`（列表 / 取详情 / 打开文件 / 导出）。

方法实现分散在这里，注册仍然走 `registry.method` 装饰器 —— 注册表只有一份。
"""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from scholarscout import paths
from scholarscout.library import index as library_index
from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.registry import AppContext, _int_param, _params, _require, method
from scholarscout.state import archive


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
        source_id=params.get("source_id")
        if isinstance(params.get("source_id"), str) and params.get("source_id")
        else None,
        tier=int(tier) if isinstance(tier, int | float) else None,
        year_from=(
            int(params["year_from"]) if isinstance(params.get("year_from"), int | float) else None
        ),
        limit=limit,
        offset=offset,
    )
    return {"items": rows, "total": total}


@method("archive.text")
def archive_text(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """读一份资料的正文（按原格式展示用）。**只读**、零模型调用。

    返回 format 字段告诉界面怎么渲染：markdown / plain / pdf / other ——
    md 走 Markdown 渲染，代码与数据等宽显示，pdf 只能"打开原件"（不在界面里假装能预览）。
    """
    params = _params(params)
    doc_id = _require(params, "doc_id")
    limit = _int_param(params, "limit", archive.TEXT_PREVIEW_LIMIT)
    return archive.read_text(ctx.db, doc_id, limit=limit)


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


@method("archive.reindex")
def archive_reindex(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """重建 ``data/library/`` 下的资料索引（全库 + 各桶）。**只读库、零模型调用**。

    改名/迁移后索引会过期，界面靠它一键重算，不必手改 Markdown（docs/01 §3.3、docs/19 §2）。
    布局取 ``ctx.config.library.layout``（默认 ``<会话>/<格式>``）—— 返回里的 ``layout`` 是**实际用的**那串段名。
    """
    _params(params)
    return library_index.write_indexes(ctx.db, layout=ctx.config.library.layout)


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
