"""``source.fetch`` —— 取回原始资料并出摘要（docs/01 §5.2）。

一次调用完成：抓取 → 原件落盘 → 抽正文 → 全文落 ``data/text/`` → 入库 → 摘要入库。
**正文不进库、不进 FTS**（决议 5/6）；库里只有元数据、摘要与文件路径。
"""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlparse

from scholarscout import paths
from scholarscout.fetch.html_text import extract_html
from scholarscout.providers.base import canonical_url
from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state import sessions
from scholarscout.state.ingest import (
    latest_summary,
    record_mention,
    save_document,
    save_summary,
    upsert_source,
)
from scholarscout.summary.pipeline import SummaryInput, summarize

DESCRIPTION = (
    "取回一条资料的原文：下载原件存到本地、抽取正文、生成中文摘要并入库，返回 doc_id、"
    "文件路径与摘要。摘要里的数字都能在原文里找到；拿不到正文时会标记 extract_status。"
)

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "资料 URL（与 source_id 二选一）"},
        "source_id": {"type": "string", "description": "检索结果里的 source_id（与 url 二选一）"},
        "force": {"type": "boolean", "description": "忽略缓存与既有结果，强制重抓"},
    },
    "additionalProperties": False,
}


def guess_source_type(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "arxiv.org" in host:
        return "arxiv"
    if "github.com" in host:
        return "github"
    if host.startswith("docs.") or "/docs/" in url:
        return "official"
    return "web"


def _extension(media_type: str, url: str, is_pdf: bool) -> str:
    if is_pdf:
        return "pdf"
    lowered = media_type.lower()
    if "html" in lowered or not lowered:
        return "html"
    if "json" in lowered:
        return "json"
    if "xml" in lowered or "atom" in lowered:
        return "xml"
    if "text/plain" in lowered:
        return "txt"
    return "bin"


def _pdf_text(content: bytes) -> tuple[str, str, str]:
    """PDF 抽正文。pymupdf 不可用时如实返回 ``no_text_layer``（MinerU 降级在 M3 末接）。"""
    try:
        import pymupdf
    except ImportError:
        return "", "no_text_layer", "none"
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
        try:
            pages = [document.load_page(index).get_text() for index in range(document.page_count)]
        finally:
            document.close()
    except Exception:
        return "", "no_text_layer", "local"
    text = "\n".join(pages).strip()
    return (text, "ok", "local") if text else ("", "no_text_layer", "local")


def _document_row(ctx: ToolContext, source_id: str, sha256: str) -> dict[str, Any] | None:
    row = ctx.db.query_one(
        "SELECT * FROM documents WHERE source_id = ? AND sha256 = ?", (source_id, sha256)
    )
    return dict(row) if row is not None else None


def source_fetch(
    ctx: ToolContext,
    url: str | None = None,
    source_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    if not url and not source_id:
        raise ValueError("必须给 url 或 source_id 之一")
    if not url:
        row = ctx.db.query_one("SELECT url FROM sources WHERE id = ?", (source_id,))
        if row is None:
            raise ValueError(f"未知 source_id：{source_id}")
        url = str(row["url"])
    assert url is not None  # 上面的分支已保证

    http = ctx.require_http()
    response = http.fetch(url, cache=not force)

    content: bytes = response.content
    sha256 = hashlib.sha256(content).hexdigest()
    media_type = response.media_type or ""
    is_pdf = "pdf" in media_type.lower() or url.lower().endswith(".pdf")

    # ---- 去重快路径：同来源同内容已经抓过，直接返回既有结果（不重复调模型）
    existing_source = ctx.db.query_one(
        "SELECT id FROM sources WHERE url_canonical = ?", (canonical_url(url),)
    )
    if existing_source is not None and not force:
        found = _document_row(ctx, str(existing_source["id"]), sha256)
        if found is not None:
            summary = latest_summary(ctx.db, str(found["id"]))
            return {
                "ok": True,
                "data": {
                    "doc_id": found["id"],
                    "source_id": found["source_id"],
                    "mime": found["mime"],
                    "chars": found["chars"],
                    "lang": found["lang"],
                    "raw_path": found["raw_path"],
                    "text_path": found["text_path"],
                    "extract_status": found["extract_status"],
                    "extract_engine": found["extract_engine"],
                    "truncated": bool(found["truncated"]),
                    "cached": True,
                    "summary": _summary_payload(summary),
                },
                "refs": {
                    "source_ids": [str(found["source_id"])],
                    "doc_ids": [str(found["id"])],
                    "summary_ids": [str(summary["id"])] if summary else [],
                },
            }

    # ---- 抽正文 + 落文件
    doc_id = sessions.new_id("doc")
    if is_pdf:
        text, extract_status, engine = _pdf_text(content)
        title = ""
        lang = None
    else:
        html_doc = extract_html(response.text)
        text = html_doc.text
        title = html_doc.title
        lang = html_doc.lang
        extract_status = "ok" if text else "empty"
        engine = "local"

    max_chars = ctx.config.summary_cfg.max_text_chars
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]

    extension = _extension(media_type, url, is_pdf)
    raw_file = paths.raw_path_for(sha256, extension)
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.write_bytes(content)

    text_file = paths.text_path_for(doc_id)
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text(text, encoding="utf-8")

    # ---- 入库
    source_row: dict[str, Any] = {
        "url": url,
        "url_canonical": canonical_url(url),
        "domain": (urlparse(url).hostname or "").lower(),
        "title": title or url,
        "authors_json": [],
        "year": None,
        "venue": None,
        "doi": None,
        "source_type": guess_source_type(url),
        "meta_json": {"mime": media_type},
    }
    resolved_source_id, _created = upsert_source(ctx.db, source_row, source_id=source_id)
    doc_id, _doc_created = save_document(
        ctx.db,
        source_id=resolved_source_id,
        sha256=sha256,
        title=title or url,
        mime=media_type,
        lang=lang,
        chars=len(text),
        raw_path=paths.rel_to_home(raw_file),
        text_path=paths.rel_to_home(text_file),
        extract_status=extract_status,
        extract_engine=engine,
        truncated=truncated,
        http_status=response.status,
        doc_id=doc_id,
    )

    # ---- 摘要（没有模型就如实不生成）
    summary_payload: dict[str, Any] | None = None
    summary_ids: list[str] = []
    if ctx.llm is None:
        summary_payload = {"error": "llm_not_configured", "degraded": True}
    else:
        draft = summarize(
            ctx.llm,
            SummaryInput(
                title=title or url,
                body=text,
                source_type=str(source_row["source_type"]),
                source_name=str(source_row["domain"]),
                extract_status=extract_status,
            ),
            settings=ctx.config.summary_cfg,
        )
        summary_id = save_summary(
            ctx.db,
            draft,
            doc_id=doc_id,
            source_id=resolved_source_id,
            session_id=ctx.session_id,
        )
        summary_ids.append(summary_id)
        summary_payload = {
            "summary_id": summary_id,
            "title_zh": draft.title_zh,
            "summary_short": draft.summary_short,
            "points": list(draft.points),
            "prompt_version": draft.prompt_version,
            "degraded": draft.degraded,
            "guard_failed": draft.guard_failed,
            "ungrounded": list(draft.ungrounded),
            "error": draft.error,
        }

    _record_use(ctx, source_id=resolved_source_id, doc_id=doc_id, summary_ids=summary_ids)

    return {
        "ok": True,
        "data": {
            "doc_id": doc_id,
            "source_id": resolved_source_id,
            "mime": media_type,
            "chars": len(text),
            "lang": lang,
            "raw_path": paths.rel_to_home(raw_file),
            "text_path": paths.rel_to_home(text_file),
            "extract_status": extract_status,
            "extract_engine": engine,
            "truncated": truncated,
            "cached": False,
            "summary": summary_payload,
        },
        "refs": {
            "source_ids": [resolved_source_id],
            "doc_ids": [doc_id],
            "summary_ids": summary_ids,
        },
    }


def _record_use(ctx: ToolContext, *, source_id: str, doc_id: str, summary_ids: list[str]) -> None:
    """记下"这条资料是在哪次会话被取用的"（docs/02 §6）。没有会话上下文就跳过。"""
    if not ctx.session_id:
        return
    record_mention(
        ctx.db,
        source_id=source_id,
        session_id=ctx.session_id,
        doc_id=doc_id,
        summary_id=summary_ids[0] if summary_ids else None,
        turn_id=ctx.turn_id,
        role="tool",
    )


def _summary_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    import json

    points = json.loads(row["points_json"]) if row.get("points_json") else []
    return {
        "summary_id": row["id"],
        "title_zh": row["title_zh"],
        "summary_short": row["summary_short"],
        "points": points,
        "prompt_version": row["prompt_version"],
        "degraded": bool(row["degraded"]),
        "guard_failed": bool(row["guard_failed"]),
    }


SPEC = ToolSpec(
    name="source.fetch",
    description=DESCRIPTION,
    parameters=PARAMETERS,
    handler=source_fetch,
    toolset="research",
)


def register() -> None:
    from scholarscout.runtime import registry

    registry.register(SPEC, replace=True)
