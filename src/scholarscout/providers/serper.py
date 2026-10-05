"""Serper.dev 检索（**商用，需 API key**）。

Serper 是 Google SERP 的商用代理（按次计费），适合补"通用互联网"那一层：
它给的是真实 Google 结果（含 title/link/snippet/date），但**没有摘要正文**，
所以命中后通常还需要 ``source.fetch`` 取原文。密钥：``.env`` 的 ``SERPER_API_KEY``。

接口：``POST https://google.serper.dev/search``，认证用 ``X-API-KEY`` 头。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query, SourceCard
from scholarscout.providers.websearch import card_from, parse_year

NAME = "serper"
ENDPOINT = "https://google.serper.dev/search"
API_KEY_ENV = "SERPER_API_KEY"
MIN_INTERVAL_S = 0.5


def parse_results(payload: dict[str, Any], *, limit: int) -> list[SourceCard]:
    cards: list[SourceCard] = []
    for item in payload.get("organic") or []:
        if not isinstance(item, dict):
            continue
        snippet = str(item.get("snippet") or "")
        card = card_from(
            url=str(item.get("link") or ""),
            title=str(item.get("title") or ""),
            snippet=snippet,
            source_type="web",
            extra={"provider": NAME, "position": str(item.get("position", ""))},
        )
        if card is None:
            continue
        # Serper 有时给 date（"Jan 5, 2025" / "2025-01-05"），比从标题里猜更准
        dated = parse_year(str(item.get("date") or "")) or card.year
        if dated != card.year:
            card = replace(card, year=dated)
        cards.append(card)
        if len(cards) >= limit:
            break
    return cards


class SerperProvider:
    name = NAME

    def __init__(
        self,
        http: HttpClient,
        *,
        api_key: str,
        endpoint: str = ENDPOINT,
        gl: str = "cn",
        hl: str = "zh-cn",
        min_interval_s: float = MIN_INTERVAL_S,
    ) -> None:
        if not api_key.strip():
            raise ValueError(f"serper 需要 {API_KEY_ENV}（写在 .env 里）")
        self.http = http
        self.api_key = api_key
        self.endpoint = endpoint
        self.gl = gl
        self.hl = hl
        self.min_interval_s = min_interval_s

    def search(self, query: Query) -> list[SourceCard]:
        payload: dict[str, Any] = {
            "q": query.text,
            "num": max(1, min(query.limit, 20)),
            "gl": self.gl,
            "hl": self.hl,
        }
        response = self.http.post_json(
            self.endpoint,
            payload,
            headers={"X-API-KEY": self.api_key},
            min_interval_s=self.min_interval_s,
            cache=False,
        )
        body = response.json()
        if not isinstance(body, dict):
            return []
        return parse_results(body, limit=query.limit)
