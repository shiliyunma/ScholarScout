"""DuckDuckGo 网页检索（**免 key**）。

实测提醒：本机（国内网络、无梯子）``html.duckduckgo.com`` 与 ``lite.duckduckgo.com``
都是 ConnectTimeout —— 这个来源**要梯子**。所以它是可选来源，默认不开；
国内直连的免 key 通用检索是 ``bing``（见 providers/bing.py 顶部说明）。
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query, SourceCard
from scholarscout.providers.websearch import cards_from_pairs, snippet_of, text_of, unwrap_redirect

NAME = "duckduckgo"
ENDPOINT = "https://html.duckduckgo.com/html/"
MIN_INTERVAL_S = 1.5

_RESULT_BLOCK = re.compile(r'<div class="result[^"]*results_links.*?</div>\s*</div>', re.S)
_ANCHOR = re.compile(r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SNIPPET = re.compile(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.S)


def parse_results(html: str, *, limit: int) -> list[tuple[str, str, str | None]]:
    found: list[tuple[str, str, str | None]] = []
    for block in _RESULT_BLOCK.findall(html or "") or [html or ""]:
        for anchor in _ANCHOR.finditer(block):
            url = unwrap_redirect(anchor.group(1))
            title = text_of(anchor.group(2))
            if not url or not title:
                continue
            snippet_match = _SNIPPET.search(block[anchor.end() :])
            snippet = snippet_of(snippet_match.group(1)) if snippet_match is not None else None
            found.append((url, title, snippet or None))
            if len(found) >= limit:
                return found
    return found


class DuckDuckGoProvider:
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
        url = f"{self.endpoint}?{urlencode({'q': query.text})}"
        response = self.http.fetch(
            url,
            accept="text/html,application/xhtml+xml",
            min_interval_s=self.min_interval_s,
        )
        return cards_from_pairs(parse_results(response.text, limit=query.limit))[: query.limit]
