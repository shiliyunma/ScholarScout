"""DataCite 检索（**免 key**，数据集/软件/预注册的 DOI 注册机构）。

为什么补这一路：Crossref 与 OpenAlex 都以**论文**为主，而"这个方向有没有公开数据集 / 官方
实现 / benchmark 发布"往往要看 **DataCite** —— 它注册的是数据集、软件、预注册与部分学位论文
（Zenodo、Dryad、Figshare、DataVerse 的 DOI 都走这里）。判权层把 ``datacite`` 归 tier 2。

真机约定（``api.datacite.org`` 官方文档）：
- ``GET /dois?query=<q>`` → ``{"data": [{"id": "10.5061/…", "attributes": {...}}]}``
- 元数据在 ``attributes`` 里：``titles[].title`` / ``creators[].name`` / ``publicationYear``
  / ``types.resourceTypeGeneral`` / ``descriptions[]``（``descriptionType == "Abstract"``）
- **没有 PDF 直链**：能给的落地页是 ``attributes.url`` 或 ``https://doi.org/{doi}``
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query, SourceCard
from scholarscout.providers.jsonapi import as_dict, list_at, payload_of
from scholarscout.providers.websearch import text_of

NAME = "datacite"
LABEL = "DataCite"
ENDPOINT = "https://api.datacite.org/dois"
API_KEY_ENV = None  # 免 key
MIN_INTERVAL_S = 0.5
MAX_AUTHORS = 8
MAX_ABSTRACT_CHARS = 3000


def first_text(value: Any) -> str:
    """``[{"title": …}]`` 这类结构取第一项非空（DataCite 的标题/描述都是对象数组）。"""
    if not isinstance(value, list):
        return ""
    for item in value:
        if isinstance(item, dict):
            cleaned = text_of(str(item.get("title") or ""))
        else:
            cleaned = text_of(str(item or ""))
        if cleaned:
            return cleaned
    return ""


def abstract_of(attributes: dict[str, Any]) -> str | None:
    """摘要：优先 ``descriptionType == "Abstract"``，没有就取第一条描述（不猜正文）。"""
    descriptions = attributes.get("descriptions")
    if not isinstance(descriptions, list):
        return None
    fallback = ""
    for entry in descriptions:
        if not isinstance(entry, dict):
            continue
        body = text_of(str(entry.get("description") or ""))
        if not body:
            continue
        if str(entry.get("descriptionType") or "").lower() == "abstract":
            return body[:MAX_ABSTRACT_CHARS]
        fallback = fallback or body
    return fallback[:MAX_ABSTRACT_CHARS] or None


def year_of(attributes: dict[str, Any]) -> int | None:
    """``publicationYear`` 可能是数字也可能是字符串（官方两种都给过）。"""
    raw = attributes.get("publicationYear")
    if isinstance(raw, int) and not isinstance(raw, bool) and 1000 <= raw <= 3000:
        return raw
    text = str(raw or "").strip()
    return int(text) if len(text) == 4 and text.isdigit() else None


def authors_of(attributes: dict[str, Any]) -> tuple[str, ...]:
    """``creators[]``：优先 ``name``（机构/姓名合成串），退化到 ``givenName familyName``。"""
    names: list[str] = []
    for creator in attributes.get("creators") or []:
        if not isinstance(creator, dict):
            continue
        name = str(creator.get("name") or "").strip()
        if not name:
            given = str(creator.get("givenName") or "").strip()
            family = str(creator.get("familyName") or "").strip()
            name = f"{given} {family}".strip()
        if name:
            names.append(name)
        if len(names) >= MAX_AUTHORS:
            break
    return tuple(names)


def card_from_doi(entry: dict[str, Any]) -> SourceCard | None:
    """把一条 ``data[]`` 条目压成 SourceCard；没标题或没 DOI 的脏数据返回 ``None``。"""
    attributes = as_dict(entry.get("attributes"))
    title = first_text(attributes.get("titles"))
    doi = str(attributes.get("doi") or entry.get("id") or "").strip() or None
    url = str(attributes.get("url") or "").strip() or (f"https://doi.org/{doi}" if doi else "")
    if not title or not url:
        return None
    types = as_dict(attributes.get("types"))
    resource_type = str(types.get("resourceTypeGeneral") or "").strip()
    return SourceCard(
        url=url,
        title=title,
        source_type=NAME,
        abstract=abstract_of(attributes),
        authors=authors_of(attributes),
        year=year_of(attributes),
        venue=str(attributes.get("publisher") or "").strip() or None,
        doi=doi,
        external_id=doi,
        extra={
            "provider": NAME,
            # 数据集/软件与论文的用法不同（判权之外还要决定"这是不是可直接跑的实现"）
            "type": str(types.get("resourceType") or ""),
            "resource_type": resource_type,
        },
    )


def parse_results(payload: dict[str, Any], *, limit: int) -> list[SourceCard]:
    """``{"data": [...]}`` → SourceCard 列表（结构变了会抛可读 FetchError）。"""
    cards: list[SourceCard] = []
    for entry in list_at(payload, "data", label=LABEL):
        if not isinstance(entry, dict):
            continue
        card = card_from_doi(entry)
        if card is not None:
            cards.append(card)
        if len(cards) >= limit:
            break
    return cards


def dois_url(*, endpoint: str, text: str, limit: int) -> str:
    """``query`` + ``page[size]``（DataCite 用 JSON:API 风格的分页参数）。"""
    params = {"query": text, "page[size]": str(max(1, min(limit, 100)))}
    return f"{endpoint}?{urlencode(params)}"


class DataCiteProvider:
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
            dois_url(endpoint=self.endpoint, text=query.text, limit=query.limit),
            accept="application/json",
            min_interval_s=self.min_interval_s,
            cache=False,
        )
        return parse_results(payload_of(response, label=LABEL), limit=query.limit)
