"""资料库：`archive.*`（列表 / 取详情 / 打开文件 / 导出）。

方法实现分散在这里，注册仍然走 `registry.method` 装饰器 —— 注册表只有一份。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scholarscout import paths
from scholarscout.fetch.formats import guess_extension
from scholarscout.fetch.http import FetchError
from scholarscout.fetch.urls import guess_source_type, prefer_direct_file
from scholarscout.library import index as library_index
from scholarscout.library import naming
from scholarscout.providers.base import canonical_url
from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.registry import AppContext, _int_param, _params, _require, method
from scholarscout.state import archive
from scholarscout.state.ingest import document_for_source, save_document, upsert_source
from scholarscout.state.migrate import now_iso


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


#: 开关关着时的统一话术（**不动网**，docs/19 §8.4）。
_DOWNLOAD_DISABLED_REASON = "按需下载已关闭（config.yaml 的 features.enable_download=false）"


def _meta_dict(raw: Any) -> dict[str, Any]:
    """``sources.meta_json`` → dict；坏 JSON 当空（命名回落，不因为一个坏字段炸掉下载）。"""
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_session(db: Any, source_id: str) -> str | None:
    """这份资料**最早**被哪个会话带出来（与 `archive.list` 的分组口径同一套，docs/19 §2）。"""
    row = db.query_one(
        "SELECT session_id FROM source_mentions WHERE source_id = ? ORDER BY rowid LIMIT 1",
        (source_id,),
    )
    return str(row["session_id"]) if row is not None else None


def _ext_from_rel(raw_path: str, mime: str) -> str:
    """从已有的原件相对路径取扩展名（给 ``already`` 那条路回包用；取不到再退回 media type）。"""
    name = str(raw_path or "").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else guess_extension(mime, "")


def _target_row(row: Any, *, session_id: str | None) -> dict[str, Any]:
    """``documents``/``sources`` 的查询行 → 下载上下文（标题取真标题，meta 解析成 dict）。"""
    return {
        "doc_id": str(row["doc_id"]) if row["doc_id"] else None,
        "source_id": str(row["source_id"]) if row["source_id"] else None,
        "url": str(row["url"] or ""),
        "raw_path": row["raw_path"],
        "mime": row["mime"],
        "http_status": row["http_status"],
        "title": str(row["title"] or row["source_title"] or ""),
        "source_type": str(row["source_type"] or ""),
        "session_id": session_id,
        "meta": _meta_dict(row["meta_json"]),
    }


def _download_target(
    ctx: AppContext, *, doc_id: str | None, source_id: str | None, url: str | None
) -> dict[str, Any] | None:
    """把 ``doc_id`` / ``source_id`` / ``url`` 解析成一次下载要用的上下文；解析不出来回 None。

    优先级 ``doc_id`` > ``source_id`` > ``url``（都给时以更具体的那条为准）。
    ``raw_path`` / ``mime`` / ``http_status`` 是**库里现有的**那条记录（可能为 None）。
    """
    if doc_id:
        row = ctx.db.query_one(
            """
            SELECT d.id AS doc_id, d.source_id, d.raw_path, d.mime, d.http_status,
                   d.title, s.url, s.source_type, s.title AS source_title, s.meta_json
            FROM documents d JOIN sources s ON s.id = d.source_id
            WHERE d.id = ?
            """,
            (doc_id,),
        )
        if row is None:
            return None
        return _target_row(row, session_id=_first_session(ctx.db, str(row["source_id"])))
    if source_id:
        src = ctx.db.query_one(
            "SELECT id, url, title, source_type, meta_json FROM sources WHERE id = ?",
            (source_id,),
        )
        if src is None:
            return None
        doc = document_for_source(ctx.db, str(src["id"]))
        merged_row = {
            "doc_id": doc["id"] if doc is not None else None,
            "source_id": src["id"],
            "raw_path": doc["raw_path"] if doc is not None else None,
            "mime": doc["mime"] if doc is not None else None,
            "http_status": doc["http_status"] if doc is not None else None,
            "title": src["title"],
            "url": src["url"],
            "source_type": src["source_type"],
            "source_title": src["title"],
            "meta_json": src["meta_json"],
        }
        return _target_row(merged_row, session_id=_first_session(ctx.db, str(src["id"])))
    return {
        "doc_id": None,
        "source_id": None,
        "url": str(url or ""),
        "raw_path": None,
        "mime": None,
        "http_status": None,
        "title": "",
        "source_type": "",
        "session_id": None,
        "meta": {},
    }


def _record_download(
    ctx: AppContext,
    *,
    target: dict[str, Any],
    source_type: str,
    title: str,
    sha256: str,
    media_type: str,
    raw_rel: str,
    size: int,
    http_status: int,
    large: bool,
    fetched_at: str,
    requested_url: str,
) -> tuple[str, str, bool]:
    """把这次下载写进库：已有 ``documents`` 行就更新，没有就新建。回 ``(doc_id, source_id, already)``。

    - **更新**（``doc_id`` 已知）：只改 ``raw_path`` / ``mime`` / ``http_status`` / ``fetched_at``
      —— ``sha256`` 是去重与不可变的锚（docs/19 §0），**不在这里改**；``extract_status`` 描述的是
      "有没有抽正文"，下载不改它（``skipped_large`` / ``ok`` 都原样留着）。
    - **新建**（只有 ``source_id`` 或只有 ``url``）：先确保 ``sources`` 行存在，再插 ``documents``。
      ``extract_status``：超阈值 ``skipped_large``（只落盘不抽正文），否则 ``pending``
      —— 原件已落盘、正文**尚未抽取**（按需下载不抽正文，所以不假装 ``ok``）。
      ``already`` 为 True 表示同来源同 sha 已在库（``save_document`` 命中去重）。
    """
    resolved_source_id = target["source_id"]
    resolved_doc_id = target["doc_id"]
    if resolved_doc_id:
        archive.set_document_download(
            ctx.db,
            resolved_doc_id,
            raw_path=raw_rel,
            mime=media_type,
            http_status=http_status,
            fetched_at=fetched_at,
        )
        return resolved_doc_id, resolved_source_id, False

    if not resolved_source_id:
        resolved_source_id, _created = upsert_source(
            ctx.db,
            {
                "url": requested_url,
                "url_canonical": canonical_url(requested_url),
                "domain": (urlparse(requested_url).hostname or "").lower(),
                "title": title,
                "authors_json": [],
                "year": None,
                "venue": None,
                "doi": None,
                "source_type": source_type,
                "meta_json": {"mime": media_type},
            },
        )
    detail = f"按需下载原件：{size} 字节（{size / 1_000_000:.2f}MB）"
    detail += "；超过抽取阈值，未抽正文" if large else "；正文尚未抽取"
    doc_id, created = save_document(
        ctx.db,
        source_id=resolved_source_id,
        sha256=sha256,
        title=title,
        mime=media_type,
        raw_path=raw_rel,
        extract_status="skipped_large" if large else "pending",
        extract_engine="none",
        extract_detail=detail,
        http_status=http_status,
        fetched_at=fetched_at,
    )
    if not created:
        # 同来源同 sha 已有一行，但它的 raw_path 可能指向已不在盘上的旧位置 → 对齐到这次落盘的位置
        archive.set_document_raw_ref(
            ctx.db, doc_id, raw_path=raw_rel, mime=media_type, http_status=http_status
        )
    return doc_id, resolved_source_id, not created


@method("archive.download")
def archive_download(ctx: AppContext, params: dict[str, Any]) -> dict[str, Any]:
    """按需下载：把 ``doc_id`` / ``source_id`` / ``url`` 对应的原件真下载到 ``data/raw/``。

    契约见 ``docs/01 §3.3``，口径见 ``docs/19 §8.4``：

    - **默认关**（``features.enable_download=false``）：**不动网**，回 ``{ok:false, reason}``；
    - **大文件**（超过 ``fetch.large_file_mb``，默认 20MB）：默认拒绝，回
      ``{ok:false, reason, large:true, threshold_bytes}``；只有 ``force:true`` 才落盘；
    - ``already:true``：同一条原件的文件已在盘上 → 不重复下载（``force`` 才重抓）；
    - **失败一律 ``ok:false`` + ``reason``，不抛回调用方**（网络异常 / 非 2xx / 落盘失败都算）。

    出网只走 ``ctx.http``（``fetch/http.py``：限速/退避/缓存/UA/体积上限都在那一层）；
    落盘路径用 ``ctx.config.library.layout``（默认 ``<会话>/<格式>``，人类可读名）；
    同时写库：已有 ``documents`` 行就更新它的 ``raw_path``，没有就新建一行。
    """
    params = _params(params)
    doc_id = params.get("doc_id") if isinstance(params.get("doc_id"), str) else None
    source_id = params.get("source_id") if isinstance(params.get("source_id"), str) else None
    url = params.get("url") if isinstance(params.get("url"), str) else None
    force = bool(params.get("force", False))
    if not (doc_id or source_id or url):
        raise RpcError(Code.INVALID_PARAMS, "必须给 doc_id / source_id / url 之一")

    threshold = int(ctx.config.fetch_cfg.large_file_bytes)
    if not ctx.config.features.enable_download:
        return {"ok": False, "reason": _DOWNLOAD_DISABLED_REASON, "threshold_bytes": threshold}

    target = _download_target(ctx, doc_id=doc_id, source_id=source_id, url=url)
    if target is None:
        return {
            "ok": False,
            "reason": f"库里没有这条资料：{doc_id or source_id}",
            "threshold_bytes": threshold,
        }

    requested_url = str(target["url"] or url or "")
    identity = {
        "doc_id": target["doc_id"],
        "source_id": target["source_id"],
        "url": requested_url,
    }
    if not requested_url:
        return {
            "ok": False,
            "reason": "这条资料没有记录源地址，无法下载",
            "threshold_bytes": threshold,
            **identity,
        }

    # 去重快路径：原件已在盘上 → 不重复下载（force 才重抓）
    existing_rel = target["raw_path"]
    if existing_rel and not force:
        on_disk = paths.resolve_rel(str(existing_rel))
        if on_disk.is_file():
            size = on_disk.stat().st_size
            return {
                "ok": True,
                "path": str(existing_rel),
                "bytes": size,
                "mime": target["mime"],
                "ext": _ext_from_rel(str(existing_rel), str(target["mime"] or "")),
                "http_status": target["http_status"],
                "large": size > threshold,
                "already": True,
                "threshold_bytes": threshold,
                **identity,
            }

    http = ctx.http
    if http is None:
        return {
            "ok": False,
            "reason": "没有可用的出网客户端",
            "threshold_bytes": threshold,
            **identity,
        }

    # 优先抓真文件（arxiv `/abs/<id>` → `/pdf/<id>`）；拿不到由 fetch 层按原 URL 的失败如实报。
    fetch_url = prefer_direct_file(requested_url) or requested_url
    try:
        response = http.fetch(fetch_url, cache=not force)
    except FetchError as exc:
        return {
            "ok": False,
            "reason": str(exc),
            "http_status": exc.status,
            "threshold_bytes": threshold,
            **identity,
        }

    content = response.content
    size = len(content)
    media_type = response.media_type or ""
    large = size > threshold
    if large and not force:
        # 大文件默认拒绝：**不落盘**（字节已拿到，但不写文件、不写库）。
        return {
            "ok": False,
            "reason": (
                f"文件 {size} 字节超过抽取阈值 {threshold} 字节"
                f"（{ctx.config.fetch_cfg.large_file_mb:g}MB）；需 force=true 才落盘"
            ),
            "large": True,
            "bytes": size,
            "mime": media_type,
            "http_status": response.status,
            "threshold_bytes": threshold,
            **identity,
        }

    sha256 = hashlib.sha256(content).hexdigest()
    is_pdf = "pdf" in media_type.lower() or fetch_url.lower().endswith(".pdf")
    ext = guess_extension(media_type, fetch_url, is_pdf, content)
    fetched_at = now_iso()
    source_type = str(target["source_type"] or "") or guess_source_type(requested_url)
    name_title = naming.pick_title(str(target["title"] or ""))
    raw_file = paths.raw_path_for_layout(
        ctx.config.library.layout,
        session=naming.session_dir_for(ctx.db, target["session_id"]),
        fmt=paths.doc_type_for(source_type),
        collected=naming.collected_month(fetched_at),
        published=naming.year_month_for(requested_url, target["meta"]),
        title=name_title,
        sha256=sha256,
        domain=naming.domain_of(requested_url),
        ext=ext,
    )
    try:
        raw_file.parent.mkdir(parents=True, exist_ok=True)
        raw_file.write_bytes(content)
    except OSError as exc:
        return {
            "ok": False,
            "reason": f"落盘失败：{exc}",
            "bytes": size,
            "mime": media_type,
            "http_status": response.status,
            "threshold_bytes": threshold,
            **identity,
        }

    raw_rel = paths.rel_to_home(raw_file)
    resolved_doc_id, resolved_source_id, already = _record_download(
        ctx,
        target=target,
        source_type=source_type,
        title=name_title or requested_url,
        sha256=sha256,
        media_type=media_type,
        raw_rel=raw_rel,
        size=size,
        http_status=response.status,
        large=large,
        fetched_at=fetched_at,
        requested_url=requested_url,
    )
    return {
        "ok": True,
        "path": raw_rel,
        "bytes": size,
        "mime": media_type,
        "ext": ext,
        "http_status": response.status,
        "large": large,
        "already": already,
        "threshold_bytes": threshold,
        "doc_id": resolved_doc_id,
        "source_id": resolved_source_id,
        "url": requested_url,
    }


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
