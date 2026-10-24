"""DOAJ 检索（**免 key**，开放获取期刊目录）。

为什么补这一路：arXiv 是预印本、Crossref 是"全部 DOI"，而 **DOAJ 只收同行评议的 OA 期刊**
—— 命中它基本等于"这篇有正式发表、且合法开放全文"，对"能不能给学生看原文"这件事最直接。
判权层把 ``doaj`` 归 tier 2（同行评议期刊）。

真机约定（``doaj.org/api`` 官方文档）：
- ``GET /api/search/articles/{query}`` → ``{"total": n, "results": [{"id", "bibjson": {...}}]}``
  —— 注意查询是**路径段**（不是 ``?q=``），所以必须 ``quote`` 编码：中文查询不编码会 404
- ``bibjson`` 里：``title`` / ``year``（字符串）/ ``author[].name`` / ``abstract``
  / ``journal.title`` / ``link[]``（``type == "fulltext"`` 是全文页）/ ``identifier[]``（DOI）
- 官方限 **2 次/秒** → ``MIN_INTERVAL_S = 0.5`` 正好贴着上限，**不许再调低**
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query, SourceCard
from scholarscout.providers.jsonapi import as_dict, list_at, payload_of
from scholarscout.providers.websearch import text_of

NAME = "doaj"
LABEL = "DOAJ"
ENDPOINT = "https://doaj.org/api/search/articles"
API_KEY_ENV = None  # 免 key
MIN_INTERVAL_S = 0.5  # 官方 2 rps
MAX_AUTHORS = 8
MAX_ABSTRACT_CHARS = 3000


def year_of(raw: Any) -> int | None:
    """``bibjson.year`` 是字符串（"2021"）；不是 4 位数字就当没给。"""
    text = str(raw or "").strip()
    return int(text) if len(text) == 4 and text.isdigit() else None


def article_url(entry: dict[str, Any], bibjson: dict[str, Any]) -> str:
    """落地页优先级：``fulltext`` 链接 > DOI > DOAJ 文章页（后者一定存在）。"""
    for link in bibjson.get("link") or []:
        if not isinstance(link, dict):
            continue
        url = str(link.get("url") or "").strip()
        if url and str(link.get("type") or "").lower() == "fulltext":
            return url
    for identifier in bibjson.get("identifier") or []:
        if not isinstance(identifier, dict):
            continue
        if str(identifier.get("type") or "").lower() == "doi":
            doi = str(identifier.get("id") or "").strip()
            if doi:
                return f"https://doi.org/{doi}"
    article_id = str(entry.get("id") or "").strip()
    return f"https://doaj.org/article/{article_id}" if article_id else ""


def doi_of(bibjson: dict[str, Any]) -> str | None:
    for identifier in bibjson.get("identifier") or []:
        if isinstance(identifier, dict) and str(identifier.get("type") or "").lower() == "doi":
            value = str(identifier.get("id") or "").strip()
            if value:
                return value
    return None


def card_from_result(entry: dict[str, Any]) -> SourceCard | None:
    """把一条搜索结果压成 SourceCard；没标题或拿不到落地页的脏数据返回 ``None``。"""
    bibjson = as_dict(entry.get("bibjson"))
    title = text_of(str(bibjson.get("title") or ""))
    url = article_url(entry, bibjson)
    if not title or not url:
        return None
    authors: list[str] = []
    for author in bibjson.get("author") or []:
        if not isinstance(author, dict):
            continue
        name = str(author.get("name") or "").strip()
        if name:
            authors.append(name)
        if len(authors) >= MAX_AUTHORS:
            break
    journal = as_dict(bibjson.get("journal"))
    languages = journal.get("language")
    # 先落到局部变量再窄化：直接写 ``journal.get(...) if isinstance(journal.get(...), list)``
    # 时 mypy 窄化的是那次调用表达式，循环目标仍是 ``Any | list | None`` → union-attr。
    license_items = journal.get("license")
    if not isinstance(license_items, list):
        license_items = []
    license_codes = ",".join(
        str(item.get("type") or "") for item in license_items if isinstance(item, dict)
    )
    return SourceCard(
        url=url,
        title=title,
        source_type=NAME,
        abstract=text_of(str(bibjson.get("abstract") or ""))[:MAX_ABSTRACT_CHARS] or None,
        authors=tuple(authors),
        year=year_of(bibjson.get("year")),
        venue=str(journal.get("title") or "").strip() or None,
        doi=doi_of(bibjson),
        external_id=str(entry.get("id") or "").strip() or None,
        extra={
            "provider": NAME,
            "publisher": str(journal.get("publisher") or ""),
            "languages": ",".join(str(code) for code in languages)
            if isinstance(languages, list)
            else "",
            "license": license_codes,
        },
    )


def parse_results(payload: dict[str, Any], *, limit: int) -> list[SourceCard]:
    """``{"total": n, "results": [...]}`` → SourceCard 列表。"""
    cards: list[SourceCard] = []
    for entry in list_at(payload, "results", label=LABEL):
        if not isinstance(entry, dict):
            continue
        card = card_from_result(entry)
        if card is not None:
            cards.append(card)
        if len(cards) >= limit:
            break
    return cards


def articles_url(*, endpoint: str, text: str, limit: int) -> str:
    """查询放在**路径段**：``/search/articles/<quote(query)>?pageSize=N``。

    ``quote(..., safe="")`` 是必须的：中文与空格原样拼进路径会被服务端当成非法请求
    （空格甚至会截断路径），表现为"永远 0 条"或 404 —— 真机上最难判的一类。
    """
    path = quote(text.strip(), safe="")
    return f"{endpoint}/{path}?{urlencode({'pageSize': str(max(1, min(limit, 100)))})}"


class DoajProvider:
    name = NAME

    def __init__(
        self,
        http: HttpClient,
        *,
        endpoint: str = ENDPOINT,
        min_interval_s: float = MIN_INTERVAL_S,
    ) -> None:
        self.http = http
        self.endpoint = endpoint
        self.min_interval_s = min_interval_s

    def search(self, query: Query) -> list[SourceCard]:
        response = self.http.fetch(
            articles_url(endpoint=self.endpoint, text=query.text, limit=query.limit),
            accept="application/json",
            min_interval_s=self.min_interval_s,
            cache=False,
        )
        return parse_results(payload_of(response, label=LABEL), limit=query.limit)
