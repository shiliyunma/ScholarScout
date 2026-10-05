"""Brave Search 检索（**商用，需 API key**）。

Brave 的独立索引（不代理 Google/Bing），免费额度对毕设演示够用。
密钥：``.env`` 的 ``BRAVE_API_KEY``。

接口：``GET https://api.search.brave.com/res/v1/web/search``，
认证与限速相关的要求都走请求头：``X-Subscription-Token``，``Accept: application/json``。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from urllib.parse import urlencode

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query, SourceCard
from scholarscout.providers.websearch import card_from, parse_year

NAME = "brave"
ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
API_KEY_ENV = "BRAVE_API_KEY"
MIN_INTERVAL_S = 1.0


def parse_results(payload: dict[str, Any], *, limit: int) -> list[SourceCard]:
    web = payload.get("web") if isinstance(payload.get("web"), dict) else {}
    cards: list[SourceCard] = []
    for item in (web.get("results") if isinstance(web, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        snippet = str(item.get("description") or "")
        card = card_from(
            url=str(item.get("url") or ""),
            title=str(item.get("title") or ""),
            snippet=snippet,
            source_type="web",
            extra={"provider": NAME},
        )
        if card is None:
            continue
        age = parse_year(str(item.get("age") or item.get("page_age") or ""))
        if age is not None and age != card.year:
            card = replace(card, year=age)
        cards.append(card)
        if len(cards) >= limit:
            break
    return cards


class BraveProvider:
    name = NAME

    def __init__(
        self,
        http: HttpClient,
        *,
        api_key: str,
        endpoint: str = ENDPOINT,
        country: str = "cn",
        min_interval_s: float = MIN_INTERVAL_S,
    ) -> None:
        if not api_key.strip():
            raise ValueError(f"brave 需要 {API_KEY_ENV}（写在 .env 里）")
        self.http = http
        self.api_key = api_key
        self.endpoint = endpoint
        self.country = country
        self.min_interval_s = min_interval_s

    def search(self, query: Query) -> list[SourceCard]:
        params = {
            "q": query.text,
            "count": max(1, min(query.limit, 20)),
            "country": self.country,
            "search_lang": "zh",
        }
        response = self.http.fetch(
            f"{self.endpoint}?{urlencode(params)}",
            accept="application/json",
            headers={"X-Subscription-Token": self.api_key},
            min_interval_s=self.min_interval_s,
            cache=False,
        )
        body = response.json()
        if not isinstance(body, dict):
            return []
        return parse_results(body, limit=query.limit)
