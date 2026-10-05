"""Bing 网页检索（**免 key**，国内可直连）。

为什么是 Bing：2026-10-05 实测（本机、国内网络、无梯子）——
``html.duckduckgo.com`` / ``lite.duckduckgo.com`` 连接超时，公共 SearxNG 实例超时或被重置，
而 ``cn.bing.com/search`` 返回 HTTP 200 且带 10 条 ``b_algo`` 结果块。免 key 的通用检索
只能按实测选，不能按偏好选（见 docs/03 §7 坑清单）。

解析策略：先切 ``b_algo`` 块，再取块内 ``<h2><a href>`` 与首个 ``<p>``；
跳转壳 ``/ck/a?...&u=a1<base64>`` 由 :mod:`websearch` 解开。Bing 的 DOM 会变，
所以解析器有单独的离线 fixture 测试，一旦结构变了测试立刻红。
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query, SourceCard
from scholarscout.providers.websearch import cards_from_pairs, text_of, unwrap_redirect

NAME = "bing"
#: 国内可直连；想用国际版把 base_url 配成 https://www.bing.com/search
DEFAULT_ENDPOINT = "https://cn.bing.com/search"
MIN_INTERVAL_S = 1.0

_ALGO_BLOCK = re.compile(r'<li class="b_algo.*?(?=<li class="b_algo|</ol>)', re.S)
_H2_ANCHOR = re.compile(r"<h2[^>]*>\s*<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S)
_ATTRIBUTION = re.compile(r'<div class="b_attribution".*?>(.*?)</div>', re.S)
_PARAGRAPH = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
_CITE = re.compile(r"<cite[^>]*>(.*?)</cite>", re.S)


def parse_results(html: str, *, limit: int) -> list[tuple[str, str, str | None]]:
    """从 Bing 结果页里抽出 ``(url, title, snippet)``。"""
    found: list[tuple[str, str, str | None]] = []
    for block in _ALGO_BLOCK.findall(html or ""):
        anchor = _H2_ANCHOR.search(block)
        if anchor is None:
            continue
        url = unwrap_redirect(anchor.group(1))
        title = text_of(anchor.group(2))
        if not url or not title:
            continue
        paragraph = _PARAGRAPH.search(block)
        snippet = text_of(paragraph.group(1)) if paragraph is not None else None
        if not snippet:
            citation = _CITE.search(block)
            snippet = text_of(citation.group(1)) if citation is not None else None
        found.append((url, title, snippet))
        if len(found) >= limit:
            break
    return found


class BingProvider:
    """免 key 的通用网页检索。"""

    name = NAME

    def __init__(
        self,
        http: HttpClient,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        min_interval_s: float = MIN_INTERVAL_S,
        market: str = "zh-CN",
    ) -> None:
        self.http = http
        self.endpoint = endpoint
        self.min_interval_s = min_interval_s
        self.market = market

    def search(self, query: Query) -> list[SourceCard]:
        params = {"q": query.text, "count": max(1, min(query.limit, 20))}
        if self.market:
            params["mkt"] = self.market
        url = f"{self.endpoint}?{urlencode(params)}"
        response = self.http.fetch(
            url,
            accept="text/html,application/xhtml+xml",
            min_interval_s=self.min_interval_s,
            cache=False,
        )
        return cards_from_pairs(parse_results(response.text, limit=query.limit))[: query.limit]


def attributes(block: str) -> str:
    """（留给测试/调试用）取出 b_attribution 里的域名文本。"""
    match = _ATTRIBUTION.search(block)
    return text_of(match.group(1)) if match is not None else ""
