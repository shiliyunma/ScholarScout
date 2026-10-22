"""``source.fetch`` —— 取回原始资料并出摘要（docs/01 §5.2）。

一次调用完成：抓取 → 原件落盘 → 抽正文 → 全文落 ``data/text/`` → 入库 → 摘要入库。
**正文不进库、不进 FTS**（决议 5/6）；库里只有元数据、摘要与文件路径。

各阶段的耗时与超时（2026-10-12 实测 + 补）——出网/抽取/摘要三段各自有预算，不再共用一个总数：

| 阶段 | 实测（本机，arXiv 800KB PDF） | 超时口径 |
|---|---|---|
| 出网（DNS+TCP+TLS+首字节+下载） | 21ms + 240ms + 70–160ms ≈ 0.3–0.6s | 分阶段：建连 5s / 读取 20s / 整次 30s（``fetch/http.py``） |
| robots.txt 探针（非豁免主机） | 0.3s（正常）/ 最坏原先 63s | 5s、只试一次 |
| 抽取（pymupdf 逐页） | 0.07–0.2s | ``fetch.extract_timeout_s``（默认 60s） |
| 落盘（原件 + 全文） | 1.1ms + 1.2ms | — |
| 写库（3 条语句） | 0.8ms | — |
| 摘要（模型调用） | 由模型决定（``summary`` 档 ``timeout_s``，默认 60s） | 摘要档自己的超时 |

**单条来源失败绝不终止整轮**：抓取阶段的 ``FetchError``（超时/403/404/过大/robots 拒绝）
在工具内就地兜住，改成 ``ok=false`` + 可读原因返回 —— 调用方跳过这一条，其余照旧完成。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from urllib.parse import urlparse

from scholarscout import paths
from scholarscout.fetch.formats import (
    LEGACY_OFFICE_SUFFIXES,
    OFFICE_SUFFIXES,
    TEXT_SUFFIXES,
    guess_extension,
    legacy_office_reason,
)
from scholarscout.fetch.html_text import extract_html
from scholarscout.fetch.http import FetchError, FetchResult
from scholarscout.fetch.throttle import interval_for_url
from scholarscout.fetch.urls import guess_source_type, prefer_direct_file
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
from scholarscout.state.migrate import now_iso
from scholarscout.summary.pipeline import SummaryInput, summarize
from scholarscout.tools.envelope import ToolResult, fail, ok
from scholarscout.tools.mineru_fallback import _mineru_fallback

#: 本模块对外的名字。``_mineru_fallback`` 已搬到 ``tools/mineru_fallback.py``（扫描版兜底的
#: 降级顺序策略），这里**重导出**它：``tests/tools/test_mineru_fallback_order.py`` 按老路径
#: ``from scholarscout.tools.source_fetch import _mineru_fallback`` 取，一字不改。
__all__ = [
    "DESCRIPTION",
    "PARAMETERS",
    "SPEC",
    "_mineru_fallback",
    "guess_source_type",
    "register",
    "source_fetch",
]

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


# 来源类型判定（`guess_source_type`）与「落地页 → 真文件」改写（`prefer_direct_file`）
# **已移到 `fetch/urls.py`** —— 协议层（`rpc/archive.py` 的按需下载）也要用同一份口径，
# 放 `fetch/`（最底层）两边都能 import。顶部 import 进来的名字在本模块照常使用，
# 外部 `from scholarscout.tools.source_fetch import guess_source_type` 一字不改（测试这么用）。


def _file_detail(size_bytes: int, *, kind: str) -> str:
    """跳过抽取时写进 `extract_detail` 的**可核查**说明（大小 + 类型 + 去原站/下载指引）。"""
    mib = size_bytes / 1_000_000
    return (
        f"{kind}：{size_bytes} 字节（{mib:.2f}MB）；已保留源地址，可「下载」到本地或「去原站」查看"
    )


def _pdf_text(content: bytes, *, budget_s: float) -> tuple[str, str, str, str]:
    """本地 PDF 抽正文（pymupdf）。抽不到文字时如实返回 ``no_text_layer``，由调用方决定是否走 MinerU。

    ``budget_s`` 是**抽取阶段的墙钟预算**（分阶段超时里的"抽取"那一段）：逐页循环里检查，
    超了就停下并标 ``extract_timeout`` —— 宁可这一条没摘要（如实标注），也不让整轮卡在本地解析上。
    实测 arXiv 800KB / 54k 字符的 PDF 只要 0.07–0.2s，所以正常路径永远碰不到它。
    """
    try:
        import pymupdf
    except ImportError:
        return "", "no_text_layer", "none", "pymupdf 未安装"
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
        try:
            total = document.page_count
            deadline = time.monotonic() + max(0.0, budget_s)
            pages: list[str] = []
            for index in range(total):
                # ``>=`` 而不是 ``>``：Windows 上 ``time.monotonic()`` 走 GetTickCount64，
                # 粒度约 15.6ms —— 同一刻度内两次读到**相等**的值，用 ``>`` 会让"预算已用尽"
                # 变成漏判（预算 0 也照样把整份 PDF 抽完）。
                if index and time.monotonic() >= deadline:
                    return (
                        "",
                        "extract_timeout",
                        "local",
                        f"本地抽取超过 {budget_s:g}s（已抽 {index}/{total} 页）",
                    )
                pages.append(document.load_page(index).get_text())
        finally:
            document.close()
    except Exception as exc:
        return "", "no_text_layer", "local", f"pymupdf 打开失败：{type(exc).__name__}"
    text = "\n".join(pages).strip()
    if text:
        return text, "ok", "local", ""
    return "", "no_text_layer", "local", "没有文字层（多为扫描版）→ 交给 MinerU"


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


def _fetch_failure_reason(exc: FetchError, url: str) -> str:
    """把抓取失败翻译成**给模型看**的一句话：类型 + 原因 + 是哪个地址。

    地址必须带上：模型的下一步是"换一条来源"，看不到是哪个地址失败了它只会重试同一个。
    （``FetchError`` 的 message 有的自带 URL（超时那类），有的不带（连接失败那类），
    所以只在没出现时才补，不重复播报。）
    """
    reason = f"抓取失败（{exc.kind}）：{exc}"
    target = exc.url or url
    if target and target not in reason:
        reason = f"{reason}；地址：{target}"
    return reason


def _source_row(url: str, media_type: str, *, title: str = "") -> dict[str, Any]:
    """拼 `sources` 表的一行（抓取与「音视频不下载」两条路共用一份口径，免得元数据漂移）。"""
    return {
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


def _declared_size(headers: dict[str, str]) -> int:
    """``Content-Length`` → 字节数；缺失 / 不是数字 / 负数一律 ``0``（= 大小未知，不编数字）。"""
    try:
        return max(0, int(headers.get("content-length", "")))
    except ValueError:
        return 0


def _media_without_body(
    ctx: ToolContext,
    *,
    url: str,
    source_id: str | None,
    requested_url: str,
    fetched_url: str,
    response: FetchResult,
) -> ToolResult:
    """音视频：**body 一个字节都没下载**（出网层按 Content-Type / 魔数就判掉了）。

    没有原件可落盘，所以只登记来源，**不写 `documents` 行**：写一行指向"0 字节原件"的文档
    等于告诉下游「原件在这儿」，比没有更糟（下载按钮点开是空文件；两份不同的视频还会撞上
    同一个空串哈希 → 假去重）。状态口径与"下载完才发现是音视频"那条**完全一致**
    （`skipped_media`），调用方不用分辨走的是哪条路。
    """
    media_type = response.media_type or ""
    size_bytes = _declared_size(response.headers)
    kind = f"音视频不提取正文（{media_type or '未知类型'}）；按响应头判定，未下载正文"
    detail = _file_detail(size_bytes, kind=kind) if size_bytes else f"{kind}，大小未知"
    resolved_source_id, _created = upsert_source(
        ctx.db, _source_row(url, media_type), source_id=source_id
    )
    return ok(
        {
            "doc_id": "",
            "source_id": resolved_source_id,
            "mime": media_type,
            "chars": 0,
            "lang": None,
            "bytes": size_bytes,
            "ext": "media",
            "raw_path": "",
            "text_path": "",
            "requested_url": requested_url,
            "fetched_url": fetched_url,
            "used_landing_page": fetched_url == url,
            "extract_status": "skipped_media",
            "extract_engine": "none",
            "extract_detail": detail,
            "truncated": False,
            "cached": False,
            "summary": None,
        },
        refs={"source_ids": [resolved_source_id], "doc_ids": [], "summary_ids": []},
    )


def source_fetch(
    ctx: ToolContext,
    url: str | None = None,
    source_id: str | None = None,
    force: bool = False,
) -> ToolResult:
    if not url and not source_id:
        raise ValueError("必须给 url 或 source_id 之一")
    if not url:
        row = ctx.db.query_one("SELECT url FROM sources WHERE id = ?", (source_id,))
        if row is None:
            raise ValueError(f"未知 source_id：{source_id}")
        url = str(row["url"])
    assert url is not None  # 上面的分支已保证

    http = ctx.require_http()
    # 抓正文也按 host 限速：复用 `search.min_interval_s`（arXiv 3s），表里没有的 host 用 1s。
    # 原来这里没传 min_interval_s → 出网层默认 0 → 同一 host 可以连发（限速只作用于检索）。
    intervals = ctx.config.search_cfg.min_interval_s
    requested_url = url
    # 优先抓真文件（arxiv `/abs/<id>` → `/pdf/<id>`）；拿不到再回落原 URL。
    # 抓的哪一种**如实记在**返回里（requested_url / fetched_url / used_landing_page）。
    direct = prefer_direct_file(url)
    fetched_url = url
    try:
        # 优先抓真文件（arxiv `/abs/<id>` → `/pdf/<id>`）；拿不到再回落原 URL。
        # 抓的哪一种**如实记在**返回里（requested_url / fetched_url / used_landing_page）。
        if direct is not None:
            try:
                response = http.fetch(
                    direct,
                    cache=not force,
                    min_interval_s=interval_for_url(direct, intervals),
                    peek_media=True,
                )
            except FetchError:
                response = http.fetch(
                    url,
                    cache=not force,
                    min_interval_s=interval_for_url(url, intervals),
                    peek_media=True,
                )
            else:
                fetched_url = direct
        else:
            response = http.fetch(
                url,
                cache=not force,
                min_interval_s=interval_for_url(url, intervals),
                peek_media=True,
            )
    except FetchError as exc:
        # **单源失败绝不终止整轮**（真机 2026-10-12 实测：一轮 6 次 source.fetch 合计 836s，
        # 界面判\"调用失败\"整链停摆）。抓取阶段的超时 / 403 / 404 / 过大 / robots 拒绝
        # 在工具内就地兜成 ``ok=false`` + 可读原因 —— 调用方跳过这一条，其余来源照旧完成。
        # 以前 FetchError 会冒泡出工具边界：模型只看到一句异常，以为\"这轮废了\"，
        # 于是反复重试同一批不可达 URL，直到烧光工具预算并开始编造。
        return fail(
            exc.kind,
            _fetch_failure_reason(exc, requested_url),
            data={
                "requested_url": requested_url,
                "fetched_url": requested_url,
                "used_landing_page": True,
                "error_kind": exc.kind,
                "status": exc.status,
            },
            refs={},
        )

    if response.body_skipped:
        # 音视频：出网层判在 **body 之前**（`peek_media`），这里连原件都没有
        return _media_without_body(
            ctx,
            url=url,
            source_id=source_id,
            requested_url=requested_url,
            fetched_url=fetched_url,
            response=response,
        )

    content: bytes = response.content
    sha256 = hashlib.sha256(content).hexdigest()
    media_type = response.media_type or ""
    is_pdf = "pdf" in media_type.lower() or fetched_url.lower().endswith(".pdf")
    extension = guess_extension(media_type, fetched_url, is_pdf, content)
    size_bytes = len(content)
    # 大文件策略（docs/19 §8.3）：超过抽取阈值就不抽正文，只登记元数据 + 保留源地址。
    large_file = size_bytes > ctx.config.fetch_cfg.large_file_bytes
    used_landing_page = fetched_url == url

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
            return ok(
                {
                    "doc_id": found["id"],
                    "source_id": found["source_id"],
                    "mime": found["mime"],
                    "chars": found["chars"],
                    "lang": found["lang"],
                    "bytes": size_bytes,
                    "ext": extension,
                    "raw_path": found["raw_path"],
                    "text_path": found["text_path"],
                    "requested_url": requested_url,
                    "fetched_url": fetched_url,
                    "used_landing_page": used_landing_page,
                    "extract_status": found["extract_status"],
                    "extract_engine": found["extract_engine"],
                    "extract_detail": found.get("extract_detail"),
                    "truncated": bool(found["truncated"]),
                    "cached": True,
                    "summary": _summary_payload(summary),
                },
                refs={
                    "source_ids": [str(found["source_id"])],
                    "doc_ids": [str(found["id"])],
                    "summary_ids": [str(summary["id"])] if summary else [],
                },
            )

    # ---- 抽正文 + 落文件
    doc_id = sessions.new_id("doc")
    extract_detail = ""
    if large_file:
        # 大文件策略（docs/19 §8.3）：**不抽正文**，只登记大小 / 类型 / 源地址。
        # 原件照常落盘；界面给「下载」与「去原站」两个出口。
        text, title, lang = "", "", None
        extract_status, engine = "skipped_large", "none"
        extract_detail = _file_detail(
            size_bytes,
            kind=f"超过抽取阈值 {ctx.config.fetch_cfg.large_file_mb:g}MB，未抽正文",
        )
    elif is_pdf:
        text, extract_status, engine, extract_detail = _pdf_text(
            content, budget_s=ctx.config.fetch_cfg.extract_timeout_s
        )
        if not text and ctx.config.features.enable_mineru:
            # 扫描版/无文字层：走 MinerU 官方 API（契约与实测见 fetch/mineru.py 顶部）。
            # 优先用刚下载到手的字节直传，少一个"对方下不到"的失败点。
            # 文件名按 URL 末段取：官方**按后缀判类型**，所以别把后缀丢了。
            name = str(fetched_url).rsplit("/", 1)[-1].split("?")[0].strip() or "scan.pdf"
            if "." not in name:
                name += ".pdf"
            text, extract_status, engine, mineru_detail = _mineru_fallback(
                ctx, fetched_url, content=content, filename=name
            )
            extract_detail = (
                f"{extract_detail}；{mineru_detail}" if extract_detail else mineru_detail
            )
        title = ""
        lang = None
    elif extension == "media":
        # 音视频不抽正文（用户明确排除）：清晰记一条（含大小/类型），别让它变成一句含糊的"提取失败"

        text, title, lang = "", "", None

        extract_status, engine = "skipped_media", "none"

        extract_detail = _file_detail(
            size_bytes, kind="音视频不提取正文（" + (media_type or "未知类型") + "）"
        )

    elif extension == "epub":
        # .epub：zip + XHTML 按章抽字（fetch/formats.py，不引依赖）

        from scholarscout.fetch import formats

        _ok, text, extract_detail = formats.epub_text(content)

        title, lang = "", None

        extract_status = "ok" if (text or _ok) else "extract_failed"

        engine = "local-ebook"

    elif extension == "rtf":
        # .rtf：控制字剥离成纯文本（fetch/formats.py）

        from scholarscout.fetch import formats

        _ok, text, extract_detail = formats.rtf_text(content)

        title, lang = "", None

        extract_status = "ok" if text.strip() else ("empty" if _ok else "extract_failed")

        engine = "local"

    elif extension == "archive":
        # .zip/.tar*：**只列目录当元数据**，不解内容（fetch/formats.py）

        from scholarscout.fetch import formats

        _ok, text, extract_detail = formats.archive_listing(content, extension)

        title, lang = "", None

        extract_status = "ok" if (text or _ok) else "extract_failed"

        engine = "local-archive"

    elif extension in LEGACY_OFFICE_SUFFIXES:
        # .doc/.xls/.ppt：OLE 复合二进制，标准库读不了 —— 不猜、不洗，如实说"需先转换"
        text, title, lang = "", "", None

        extract_status, engine = "unsupported_legacy_office", "none"

        extract_detail = legacy_office_reason(extension)

    elif extension in OFFICE_SUFFIXES:
        # .docx/.xlsx/.pptx：标准库解 zip + XML 抽文字（见 fetch/office.py）

        from scholarscout.fetch import office

        _ok, text, extract_detail = office.office_text(extension, content)

        title, lang = "", None

        extract_status = "ok" if (text or _ok) else "extract_failed"

        engine = "local-office"

    elif extension in TEXT_SUFFIXES:
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

    source_type = (
        str(existing_source["source_type"])
        if existing_source is not None
        else guess_source_type(url)
    )
    source_title = str(existing_source["title"]) if existing_source is not None else ""
    meta = _meta_of(existing_source["meta_json"]) if existing_source is not None else {}
    domain = naming.domain_of(url)
    # 两个时间口径（docs/19 §2）：发表年月取资料自己的时间戳；收录年月就是"现在"这次抓取
    # （同一个 `fetched_at` 也写进 documents，落盘目录与库里那一列必须是同一个月份）。
    published_month = naming.year_month_for(url, meta)
    name_title = naming.pick_title(title, source_title)
    fetched_at = now_iso()
    collected_month = naming.collected_month(fetched_at)

    # 布局是**参数**：默认 `<会话>/<格式>`（会话目录名含同名短 id；没有会话上下文就落 unknown/）。
    # 值来自 config.yaml 的 `library.layout`，代码与迁移脚本都从它读，不写死布局（docs/19 §2）。
    layout = ctx.config.library.layout
    session_dir = naming.session_dir_for(ctx.db, ctx.session_id)
    raw_file = paths.raw_path_for_layout(
        layout,
        session=session_dir,
        fmt=paths.doc_type_for(source_type),
        collected=collected_month,
        published=published_month,
        title=name_title,
        sha256=sha256,
        domain=domain,
        ext=extension,
    )
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.write_bytes(content)

    text_file = paths.text_path_for_layout(
        layout,
        session=session_dir,
        fmt=paths.doc_type_for(source_type),
        collected=collected_month,
        published=published_month,
        title=name_title,
        sha256=sha256,
        domain=domain,
    )
    text_file.parent.mkdir(parents=True, exist_ok=True)
    text_file.write_text(text, encoding="utf-8")

    # ---- 入库
    source_row: dict[str, Any] = _source_row(url, media_type, title=title)
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
        fetched_at=fetched_at,
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

    return ok(
        {
            "doc_id": doc_id,
            "source_id": resolved_source_id,
            "mime": media_type,
            "chars": len(text),
            "lang": lang,
            "bytes": size_bytes,
            "ext": extension,
            "raw_path": paths.rel_to_home(raw_file),
            "text_path": paths.rel_to_home(text_file),
            "requested_url": requested_url,
            "fetched_url": fetched_url,
            "used_landing_page": used_landing_page,
            "extract_status": extract_status,
            "extract_engine": engine,
            "extract_detail": extract_detail or None,
            "truncated": truncated,
            "cached": False,
            "summary": summary_payload,
        },
        refs={
            "source_ids": [resolved_source_id],
            "doc_ids": [doc_id],
            "summary_ids": summary_ids,
        },
    )


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
