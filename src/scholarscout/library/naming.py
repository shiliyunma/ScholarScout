"""命名要的元数据：**类型 / 年-月 / 真标题** —— 全从 provider 元数据推，不编（docs/19 §1/§2）。

三条口径（都对应一个曾经会犯的错法）：

1. **年-月优先取 arXiv 号自带的 YYMM**（``2410.24164`` → ``2024-10``）—— 它是资料自己的时间戳，
   比「什么时候被我们抓到」准得多。
2. **取不到月份落 ``unknown``**，绝不用抓取日期冒充发表日期（与 docs/05「未知年份不编」同口径）。
3. **标题取不到就回落到域名**，绝不用 URL 或抓取时间编一个看着像真标题的字符串。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

#: arXiv 新式编号 ``YYMM.NNNNN``（月份必须 01–12，所以 ``9946.2026`` 这类 DOI 片段不会被误判）。
_ARXIV_ID_RE = re.compile(r"(?<!\d)(\d{2})(0[1-9]|1[0-2])\.\d{4,5}(?!\d)")

#: meta_json 里可能出现的\"发表\"日期字段（优先）。
_PUBLISHED_KEYS: tuple[str, ...] = ("published", "published_at", "publication_date", "date")

#: \"更新\"日期字段（次之）。
_UPDATED_KEYS: tuple[str, ...] = (
    "pushed_at",
    "updated",
    "updated_at",
    "last_modified",
    "modified",
    "created_at",
)

#: 只认 ``YYYY-MM``（月份合法），``YYYY-MM-DD`` 也能命中前两段。
_MONTH_RE = re.compile(r"(\d{4})-(0[1-9]|1[0-2])")

#: 看着像 URL 的\"标题\"（``sources.title`` 缺真标题时会回落成 url）—— 不能当标题用。
_URL_RE = re.compile(r"^\s*(?:https?://|www\.)", re.IGNORECASE)


def digits_to_year_month(raw: str) -> str | None:
    """把 arXiv 的 ``YYMM.NNNNN`` 谱式转成 ``YYYY-MM``；不是这个形状就回 ``None``。"""
    match = _ARXIV_ID_RE.search(raw)
    if match is None:
        return None
    yy = int(match.group(1))
    month = int(match.group(2))
    # arXiv 的两位年份：1990 年前后是 19YY，之后按 20YY（本项目数据全在 20YY，这条兜底而已）
    year = 1900 + yy if yy >= 90 else 2000 + yy
    return f"{year:04d}-{month:02d}"


def year_month_from_arxiv(url: str | None, meta: Mapping[str, Any]) -> str | None:
    """从 arXiv 号推年-月。先看 ``external_id``（最干净），再看 url 与 pdf_url（需 arxiv 域名）。"""
    external = str(meta.get("external_id") or "")
    if _ARXIV_ID_RE.fullmatch(external.strip()):
        return digits_to_year_month(external)
    for candidate in (url, meta.get("pdf_url")):
        text = str(candidate or "")
        if "arxiv.org" in text.lower():
            found = digits_to_year_month(text)
            if found:
                return found
    return None


def year_month_from_meta(meta: Mapping[str, Any]) -> str | None:
    """从元数据的日期字段推年-月：发表优先，更新次之；认不出来回 ``None``（不猜）。"""
    for keys in (_PUBLISHED_KEYS, _UPDATED_KEYS):
        for key in keys:
            match = _MONTH_RE.search(str(meta.get(key) or ""))
            if match is not None:
                return f"{match.group(1)}-{match.group(2)}"
    return None


def year_month_for(url: str | None, meta: Mapping[str, Any] | None = None) -> str:
    """资料自己的\"发表/更新\"月份（``YYYY-MM``）；推不出来回 ``unknown``。"""
    table = meta or {}
    return year_month_from_arxiv(url, table) or year_month_from_meta(table) or "unknown"


def pick_title(*candidates: str | None) -> str:
    """挑一个**真标题**：跳过空值与 URL 形状的字符串（那些是回落值，不是标题）。"""
    for candidate in candidates:
        text = (candidate or "").strip()
        if text and not _URL_RE.match(text):
            return text
    return ""


def domain_of(url: str | None) -> str:
    """URL → 主机名（小写、去 ``www.``）；解析不出来回空串（由调用方回落到 ``untitled``）。"""
    from urllib.parse import urlparse

    host = (urlparse(str(url or "")).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host
