"""Hacker News（Algolia 检索 API，**免 key**，技术社区/讨论）。

为什么补这一路：学术源回答"这方向有什么论文"，GitHub 回答"有什么实现"，而**技术社区**
回答的是第三种问题 ——"大家实际踩过什么坑、哪个方案被推荐/被劝退"。这类一手经验
在论文与 README 里都没有，对"该怎么入门/该选哪个框架"最有用。判权层把 ``hn_algolia``
归 tier 3（社区口碑，质量需自判，不能当权威结论）。

真机约定（``hn.algolia.com/api`` 官方文档，Algolia 托管的 HN 检索）：
- ``GET https://hn.algolia.com/api/v1/search?query=<q>`` → ``{"nbHits": n, "hits": [...]}``
- 每条 hit：``objectID`` / ``title`` / ``url``（Ask HN 这类自帖没有 url）/ ``author``
  / ``created_at``（ISO）/ ``points`` / ``num_comments`` / ``story_text``
- ``tags=story`` 只取主帖：评论没有标题、也没法当"资料"引用（会污染来源列表）
- 没有任何 ``Api-Key``：官方要求客户端带 ``User-Agent``（本项目的 HttpClient 全局带）
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query, SourceCard, year_from_iso
from scholarscout.providers.jsonapi import list_at, payload_of
from scholarscout.providers.websearch import snippet_of, text_of

NAME = "hn_algolia"
LABEL = "Hacker News"
ENDPOINT = "https://hn.algolia.com/api/v1/search"
API_KEY_ENV = None  # 免 key
MIN_INTERVAL_S = 0.2  # Algolia 免费公共检索，压力很小；但仍留出间隔不连打
#: 只要主帖（见模块文档）；评论没有标题，引不成"资料"
TAGS = "story"
ITEM_URL = "https://news.ycombinator.com/item?id={object_id}"
SNIPPET_CHARS = 300


def item_url(item: dict[str, Any]) -> str:
    """外部链接优先；Ask HN/Show HN 这类自帖没有 url，回落到 HN 自己的条目页。"""
    url = str(item.get("url") or "").strip()
    if url:
        return url
    object_id = str(item.get("objectID") or "").strip()
    return ITEM_URL.format(object_id=object_id) if object_id else ""


def title_of(item: dict[str, Any], url: str) -> str:
    """``title`` 缺失时用 ``story_title``（评论形状）或正文首句；都没有就用 HN 条目页标题。"""
    for key in ("title", "story_title"):
        cleaned = text_of(str(item.get(key) or ""))
        if cleaned:
            return cleaned
    first_line = snippet_of(str(item.get("story_text") or ""), limit=120)
    if first_line:
        return first_line
    return f"Hacker News 讨论 {url}" if url else ""


def card_from_hit(item: dict[str, Any]) -> SourceCard | None:
    """把一条 hit 压成 SourceCard；既没标题也没链接的脏数据返回 ``None``。"""
    url = item_url(item)
    title = title_of(item, url)
    if not title or not url:
        return None
    author = str(item.get("author") or "").strip()
    object_id = str(item.get("objectID") or "").strip()
    return SourceCard(
        url=url,
        title=title,
        source_type=NAME,
        # HN 的正文是**讨论串的开场白**，不是论文摘要；放进 snippet 让模型知道这是社区发言
        snippet=snippet_of(str(item.get("story_text") or ""), limit=SNIPPET_CHARS) or None,
        authors=(author,) if author else (),
        year=year_from_iso(str(item.get("created_at") or "")),
        venue="Hacker News",
        external_id=object_id or None,
        extra={
            "provider": NAME,
            "points": str(item.get("points") if item.get("points") is not None else ""),
            "comments": str(
                item.get("num_comments") if item.get("num_comments") is not None else ""
            ),
            "discussion": ITEM_URL.format(object_id=object_id) if object_id else "",
        },
    )


def parse_results(payload: dict[str, Any], *, limit: int) -> list[SourceCard]:
    """``{"hits": [...]}`` → SourceCard 列表（结构变了会抛可读 FetchError）。"""
    cards: list[SourceCard] = []
    for item in list_at(payload, "hits", label=LABEL):
        if not isinstance(item, dict):
            continue
        card = card_from_hit(item)
        if card is not None:
            cards.append(card)
        if len(cards) >= limit:
            break
    return cards


def search_url(*, endpoint: str, text: str, limit: int) -> str:
    """``query`` + ``hitsPerPage`` + ``tags=story``。"""
    params = {
        "query": text,
        "hitsPerPage": str(max(1, min(limit, 100))),
        "tags": TAGS,
    }
    return f"{endpoint}?{urlencode(params)}"


class HnAlgoliaProvider:
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
            search_url(endpoint=self.endpoint, text=query.text, limit=query.limit),
            accept="application/json",
            min_interval_s=self.min_interval_s,
            cache=False,
        )
        return parse_results(payload_of(response, label=LABEL), limit=query.limit)
