"""SearxNG 网页检索（**免 key**，指向自建或可信实例）。

为什么留这个：商用 API 要钱、Bing 的 DOM 会变、DDG 要梯子。SearxNG 是唯一
**自己可控**的免 key 路线（一条 docker 命令起一个实例），而且它返回 JSON，比抓 HTML 稳。

本机实测：公共实例（searx.be / searxng.site）在国内网络下超时或被重置，
所以**不预置公共实例**，必须由配置显式给出 ``base_url``（自建实例或可用的私有实例）。
没配 base_url 时这个来源在 ``research.search`` 里显示为未配置，而不是静默不返回结果。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query, SourceCard
from scholarscout.providers.websearch import cards_from_pairs

NAME = "searxng"
MIN_INTERVAL_S = 1.0


def parse_results(payload: dict[str, Any], *, limit: int) -> list[tuple[str, str, str | None]]:
    found: list[tuple[str, str, str | None]] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        title = str(item.get("title") or "")
        if not url or not title:
            continue
        snippet = item.get("content") or item.get("snippet")
        found.append((url, title, str(snippet) if snippet else None))
        if len(found) >= limit:
            break
    return found


class SearxngProvider:
    name = NAME

    def __init__(
        self,
        http: HttpClient,
        *,
        base_url: str,
        min_interval_s: float = MIN_INTERVAL_S,
    ) -> None:
        if not base_url.strip():
            raise ValueError("searxng 需要配置 search.searxng_base_url（自建实例地址）")
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s

    def search(self, query: Query) -> list[SourceCard]:
        params = {"q": query.text, "format": "json", "language": "auto"}
        response = self.http.fetch(
            f"{self.base_url}/search?{urlencode(params)}",
            accept="application/json",
            min_interval_s=self.min_interval_s,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            return []
        return cards_from_pairs(parse_results(payload, limit=query.limit))[: query.limit]
