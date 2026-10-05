"""arXiv 检索源（Atom API）。

两个必须记住的坑（docs/03 §7）：
1. **必须显式带 ``Accept`` 头**，否则 arXiv 一律返回 406。
2. **限速**：连续请求会被封一段时间 —— ``min_interval_s = 3.0``，交给 fetch/http.py 串行化。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from scholarscout.fetch.http import FetchError, HttpClient
from scholarscout.providers.base import Query, SourceCard, year_from_iso

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
API_URL = "https://export.arxiv.org/api/query"
#: arXiv 对不带 Accept 的请求返回 406 —— 这里显式声明
ACCEPT = "application/atom+xml,application/xml;q=0.9,*/*;q=0.8"
_WS = re.compile(r"\s+")


def _clean(text: str | None) -> str:
    return _WS.sub(" ", (text or "").strip())


def build_url(query: Query) -> str:
    """按 arXiv API 语法拼检索 URL（短语加引号，避免被拆词）。"""
    params = {
        "search_query": f'all:"{query.text}"',
        "start": 0,
        "max_results": max(1, query.limit),
        "sortBy": "submittedDate" if query.sort == "date" else "relevance",
        "sortOrder": "descending",
    }
    return f"{API_URL}?{urlencode(params)}"


def parse_feed(xml_text: str) -> list[SourceCard]:
    """解析 Atom feed（stdlib ElementTree，不引额外依赖）。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FetchError("bad_request", f"arXiv 返回的不是合法 XML：{exc}") from exc

    cards: list[SourceCard] = []
    for entry in root.findall(f"{ATOM}entry"):
        abs_url = _clean(entry.findtext(f"{ATOM}id"))
        if not abs_url:
            continue
        authors = tuple(
            _clean(node.findtext(f"{ATOM}name"))
            for node in entry.findall(f"{ATOM}author")
            if _clean(node.findtext(f"{ATOM}name"))
        )
        pdf_url = None
        for link in entry.findall(f"{ATOM}link"):
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf_url = link.get("href")
                break
        primary = entry.find(f"{ARXIV}primary_category")
        cards.append(
            SourceCard(
                url=_abs_url(abs_url),
                title=_clean(entry.findtext(f"{ATOM}title")),
                source_type="arxiv",
                abstract=_clean(entry.findtext(f"{ATOM}summary")),
                authors=authors,
                year=year_from_iso(entry.findtext(f"{ATOM}published")),
                venue="arXiv",
                doi=_clean(entry.findtext(f"{ARXIV}doi")) or None,
                external_id=_arxiv_id(abs_url),
                pdf_url=pdf_url,
                snippet=_clean(entry.findtext(f"{ATOM}summary"))[:300] or None,
                extra={"primary_category": primary.get("term", "") if primary is not None else ""},
            )
        )
    return cards


def _abs_url(feed_id: str) -> str:
    """``http://arxiv.org/abs/2501.00001v1`` → ``https://arxiv.org/abs/2501.00001``（去版本号）。"""
    url = feed_id.replace("http://", "https://")
    return re.sub(r"v\d+$", "", url)


def _arxiv_id(feed_id: str) -> str | None:
    match = re.search(r"/abs/([^v]+)", feed_id)
    return match.group(1) if match else None


class ArxivProvider:
    name = "arxiv"
    min_interval_s = 3.0  # 坑 2：连续请求会被限速

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def search(self, query: Query) -> list[SourceCard]:
        url = build_url(query)
        result = self.http.fetch(url, accept=ACCEPT, min_interval_s=self.min_interval_s)
        return parse_feed(result.text)
