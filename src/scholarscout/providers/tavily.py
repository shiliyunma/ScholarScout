"""Tavily 检索（**商用，需 API key**）。

Tavily 是专为 LLM 检索设计的商用搜索，返回结构化 JSON（含 content 摘要与相关性 score），
对"做 agent"这个用途最省事。密钥放 ``.env`` 的 ``TAVILY_API_KEY``（设置与密钥分离，docs/03 §5）。

接口：``POST https://api.tavily.com/search``，认证用请求体里的 ``api_key``。
"""

from __future__ import annotations

from typing import Any

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query, SourceCard
from scholarscout.providers.websearch import card_from

NAME = "tavily"
ENDPOINT = "https://api.tavily.com/search"
API_KEY_ENV = "TAVILY_API_KEY"
MIN_INTERVAL_S = 0.5


def parse_results(payload: dict[str, Any], *, limit: int) -> list[SourceCard]:
    cards: list[SourceCard] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        card = card_from(
            url=str(item.get("url") or ""),
            title=str(item.get("title") or ""),
            snippet=str(item.get("content") or item.get("raw_content") or ""),
            source_type="web",
            extra={"provider": NAME, "score": str(item.get("score", ""))},
        )
        if card is not None:
            cards.append(card)
        if len(cards) >= limit:
            break
    return cards


class TavilyProvider:
    name = NAME

    def __init__(
        self,
        http: HttpClient,
        *,
        api_key: str,
        endpoint: str = ENDPOINT,
        search_depth: str = "basic",
        min_interval_s: float = MIN_INTERVAL_S,
    ) -> None:
        if not api_key.strip():
            raise ValueError(f"tavily 需要 {API_KEY_ENV}（写在 .env 里）")
        self.http = http
        self.api_key = api_key
        self.endpoint = endpoint
        self.search_depth = search_depth
        self.min_interval_s = min_interval_s

    def search(self, query: Query) -> list[SourceCard]:
        payload: dict[str, Any] = {
            "api_key": self.api_key,
            "query": query.text,
            "max_results": max(1, min(query.limit, 20)),
            "search_depth": self.search_depth,
            "include_answer": False,
        }
        if query.year_from is not None:
            # Tavily 不做年份过滤，交给上层 year_from 过滤（research_search 会再筛一次）
            payload["topic"] = "general"
        response = self.http.post_json(self.endpoint, payload, min_interval_s=self.min_interval_s)
        body = response.json()
        if not isinstance(body, dict):
            return []
        return parse_results(body, limit=query.limit)
