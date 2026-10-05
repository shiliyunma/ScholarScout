"""Google 可编程搜索（Programmable Search / Custom Search JSON API）。

**商用**（Google Cloud 计费，但每天 100 次免费额度）—— 注册即得，是"通用搜索"
最省心的合规来源之一：官方 API、返回 JSON、覆盖整个 Google 索引。
需要两个值：``GOOGLE_CSE_KEY``（API key，进 ``.env``）与 ``google_cse_cx``（搜索引擎 ID，
不是密钥，进 ``config.yaml``；在 https://programmablesearchengine.google.com/ 创建）。

接口：``GET https://www.googleapis.com/customsearch/v1?key=..&cx=..&q=..``
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query, SourceCard
from scholarscout.providers.websearch import card_from

NAME = "google_cse"
ENDPOINT = "https://www.googleapis.com/customsearch/v1"
API_KEY_ENV = "GOOGLE_CSE_KEY"
MIN_INTERVAL_S = 0.5


def parse_results(payload: dict[str, Any], *, limit: int) -> list[SourceCard]:
    cards: list[SourceCard] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        card = card_from(
            url=str(item.get("link") or ""),
            title=str(item.get("title") or ""),
            snippet=str(item.get("snippet") or ""),
            source_type="web",
            extra={"provider": NAME},
        )
        if card is not None:
            cards.append(card)
        if len(cards) >= limit:
            break
    return cards


class GoogleCseProvider:
    name = NAME

    def __init__(
        self,
        http: HttpClient,
        *,
        api_key: str,
        cx: str,
        endpoint: str = ENDPOINT,
        min_interval_s: float = MIN_INTERVAL_S,
    ) -> None:
        if not api_key.strip():
            raise ValueError(f"google_cse 需要 {API_KEY_ENV}（写在 .env 里）")
        if not cx.strip():
            raise ValueError("google_cse 需要 config.yaml 里的 search.google_cse_cx")
        self.http = http
        self.api_key = api_key
        self.cx = cx
        self.endpoint = endpoint
        self.min_interval_s = min_interval_s

    def search(self, query: Query) -> list[SourceCard]:
        params = {
            "key": self.api_key,
            "cx": self.cx,
            "q": query.text,
            "num": max(1, min(query.limit, 10)),  # CSE 单次最多 10 条
        }
        url = f"{self.endpoint}?{urlencode(params)}"
        response = self.http.fetch(
            url, accept="application/json", min_interval_s=self.min_interval_s
        )
        body = response.json()
        if not isinstance(body, dict):
            return []
        return parse_results(body, limit=query.limit)
