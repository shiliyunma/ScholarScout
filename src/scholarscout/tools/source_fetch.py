"""``source.fetch`` —— 取回原始资料并出摘要（docs/01 §5.2）。

一次调用完成：抓取 → 原件落盘 → 抽正文 → 全文落 ``data/text/`` → 入库 → 摘要入库。
**正文不进库、不进 FTS**（决议 5/6）；库里只有元数据、摘要与文件路径。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlparse

from scholarscout import paths
from scholarscout.fetch.html_text import extract_html
from scholarscout.library import naming
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


#: 音视频后缀：用户明确要求**不**抓（只保留资料链接，不下载、不抽正文）。

_MEDIA_SUFFIXES = {
    "mp3",
    "wav",
    "flac",
    "aac",
    "ogg",
    "m4a",
    "wma",
    "opus",
    "mp4",
    "avi",
    "mkv",
    "mov",
    "wmv",
    "flv",
    "webm",
    "m4v",
    "mpg",
    "mpeg",
    "ts",
}


#: Office 三件套：走 fetch/office.py 的标准库抽取器（zip + XML，不引依赖）。

_OFFICE_SUFFIXES = {"docx", "xlsx", "pptx", "xls"}


#: 文本类后缀：**原样解码**，不要拿 HTML 提取器去洗（洗了就没了缩进和代码结构）。

_TEXT_SUFFIXES = {
    "md",
    "markdown",
    "txt",
    "py",
    "js",
    "ts",
    "jsx",
    "tsx",
    "json",
    "yaml",
    "yml",
    "toml",
    "ini",
    "cfg",
    "csv",
    "tsv",
    "ipynb",
    "c",
    "cc",
    "cpp",
    "h",
    "hpp",
    "java",
    "go",
    "rs",
    "rb",
    "sh",
    "bat",
    "ps1",
    "sql",
    "tex",
    "r",
    "jl",
    "m",
    "patch",
    "diff",
    "log",
}


def _extension(media_type: str, url: str, is_pdf: bool) -> str:
    """落盘与展示用的类型标签。**先看 URL 后缀**再看 media type —— 很多站点把 .md/.py
    用 text/plain 甚至 application/octet-stream 发出来，只看 media type 会一律认成 txt。
    """
    if is_pdf:
        return "pdf"
    lowered = media_type.lower()
    name = url.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1]
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if suffix in _MEDIA_SUFFIXES:
        return "media"
    if suffix in _OFFICE_SUFFIXES or "officedocument" in lowered or "msword" in lowered:
        return (
            "docx"
            if (suffix == "docx" or "word" in lowered)
            else (
                "xlsx"
                if (suffix in {"xlsx", "xls"} or "excel" in lowered or "spreadsheet" in lowered)
                else ("pptx" if (suffix == "pptx" or "presentation" in lowered) else suffix)
            )
        )
    if suffix in _TEXT_SUFFIXES:
        return suffix
    if "html" in lowered or not lowered:
        return "html"
    if "json" in lowered:
        return "json"
    if "xml" in lowered or "atom" in lowered:
        return "xml"
    if "text/plain" in lowered:
        return "txt"
    return "bin"


def _pdf_text(content: bytes) -> tuple[str, str, str, str]:
    """本地 PDF 抽正文（pymupdf）。抽不到文字时如实返回 ``no_text_layer``，由调用方决定是否走 MinerU。"""
    try:
        import pymupdf
    except ImportError:
        return "", "no_text_layer", "none", "pymupdf 未安装"
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
        try:
            pages = [document.load_page(index).get_text() for index in range(document.page_count)]
        finally:
            document.close()
    except Exception as exc:
        return "", "no_text_layer", "local", f"pymupdf 打开失败：{type(exc).__name__}"
    text = "\n".join(pages).strip()
    if text:
        return text, "ok", "local", ""
    return "", "no_text_layer", "local", "没有文字层（多为扫描版）→ 交给 MinerU"


def _mineru_fallback(
    ctx: ToolContext, url: str, *, content: bytes | None = None, filename: str = "scan.pdf"
) -> tuple[str, str, str, str]:
    """落地 MinerU 兜底（决议第 8 题）：本地抽不出文字时，交给官方 API。

    **顺序**：手里已经有字节就**优先直传**（预签名上传），失败再退回"提交 URL 让 MinerU 自己去下"。
    为什么这么排：URL 那条要求 MinerU 能下到那个 PDF（内网地址、需要登录、CDN 抖动、
    robots 拒爬都会失败），而我们**刚刚才把字节下载到本地** —— 直传少一个失败点，
    而且免 Token 也能用（Agent 轻量接口 ≤10MB/≤20 页）。实测 2026-10-07：本地造的
    无文字层扫描版样本直传 **10.3 秒**拿到中文 Markdown。

    失败要**如实**记 ``mineru_failed`` + 原因 —— 服务暂不可用与"文档确实没有内容"是两件事。
    """
    from scholarscout.fetch.mineru import parse_pdf_bytes, parse_pdf_url
    from scholarscout.fetch.settings import mineru_token

    http = ctx.require_http()
    token = mineru_token()
    direct_detail = ""

    if content:
        direct = parse_pdf_bytes(http, content, filename=filename, token=token)
        if direct.ok:
            return direct.text, "ok", "mineru", f"{direct.engine} 解析（本地直传）"
        direct_detail = f"直传 {direct.state}: {direct.detail}"

    if not url:
        return "", "mineru_failed", "mineru", direct_detail or "既没有本地字节也没有 URL"

    result = parse_pdf_url(http, url, token=token)
    if result.ok:
        return result.text, "ok", "mineru", f"{result.engine} 解析（URL 提交）"
    url_detail = f"URL 提交 {result.state}: {result.detail}"
    detail = f"{direct_detail}；{url_detail}" if direct_detail else url_detail
    return "", "mineru_failed", "mineru", detail


def _document_row(ctx: ToolContext, source_id: str, sha256: str) -> dict[str, Any] | None:
    row = ctx.db.query_one(
        "SELECT * FROM documents WHERE source_id = ? AND sha256 = ?", (source_id, sha256)
    )
    return dict(row) if row is not None else None


def _meta_of(raw: Any) -> dict[str, Any]:
    """``sources.meta_json`` → dict；坏 JSON 就当空（命名回落，不因为一个坏字段炸掉抓取）。"""
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
    # 顺手把来源元数据取出来：命名要的类型 / 标题 / 发表月份此刻就能拿到（docs/19 §4）。
    existing_source = ctx.db.query_one(
        "SELECT id, title, source_type, meta_json FROM sources WHERE url_canonical = ?",
        (canonical_url(url),),
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
                    "extract_detail": found.get("extract_detail"),
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
    extract_detail = ""
    if is_pdf:
        text, extract_status, engine, extract_detail = _pdf_text(content)
        if not text and ctx.config.features.enable_mineru:
            # 扫描版/无文字层：走 MinerU 官方 API（契约与实测见 fetch/mineru.py 顶部）。
            # 优先用刚下载到手的字节直传，少一个"对方下不到"的失败点。
            # 文件名按 URL 末段取：官方**按后缀判类型**，所以别把后缀丢了。
            name = str(url).rsplit("/", 1)[-1].split("?")[0].strip() or "scan.pdf"
            if "." not in name:
                name += ".pdf"
            text, extract_status, engine, mineru_detail = _mineru_fallback(
                ctx, url, content=content, filename=name
            )
            extract_detail = (
                f"{extract_detail}；{mineru_detail}" if extract_detail else mineru_detail
            )
        title = ""
        lang = None
    elif _extension(media_type, url, is_pdf) == "media":
        # 音视频不抽正文（用户明确排除）：清晰记一条，别让它变成一句含糊的"提取失败"

        text, title, lang = "", "", None

        extract_status, engine = "skipped_media", "none"

        extract_detail = "音视频不提取正文（" + (media_type or "未知类型") + "）"

    elif _extension(media_type, url, is_pdf) in _OFFICE_SUFFIXES:
        # .docx/.xlsx/.pptx：标准库解 zip + XML 抽文字（见 fetch/office.py）

        from scholarscout.fetch import office

        _ok, text, extract_detail = office.office_text(_extension(media_type, url, is_pdf), content)

        title, lang = "", None

        extract_status = "ok" if (text or _ok) else "extract_failed"

        engine = "local-office"

    elif _extension(media_type, url, is_pdf) in _TEXT_SUFFIXES:
        # 文本类：**原样解码**，不走 HTML 提取器（洗了会把缩进/代码结构搞没）

        text = response.text

        title, lang = "", None

        extract_status = "ok" if text.strip() else "empty"

        engine = "local"

        extract_detail = "按纯文本读取"

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
    source_type = (
        str(existing_source["source_type"])
        if existing_source is not None
        else guess_source_type(url)
    )
    source_title = str(existing_source["title"]) if existing_source is not None else ""
    meta = _meta_of(existing_source["meta_json"]) if existing_source is not None else {}
    domain = naming.domain_of(url)
    year_month = naming.year_month_for(url, meta)
    name_title = naming.pick_title(title, source_title)

    # 新布局：data/raw/<类型>/<年-月>/<标题>--<sha8>.<ext>，全文同桶同名只差后缀（docs/19 §1/§2）。
    raw_file = paths.raw_path_for_doc(
        source_type, year_month, name_title, sha256, extension, domain=domain
    )
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.write_bytes(content)

    text_file = paths.text_path_for_doc(source_type, year_month, name_title, sha256, domain=domain)
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
        extract_detail=extract_detail or None,
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
            "extract_detail": extract_detail or None,
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
