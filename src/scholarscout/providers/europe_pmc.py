"""Europe PMC 检索（**免 key**，生命科学与医学文献）。

为什么单独补它：生命科学/医学的文献在 arXiv 与 OpenAlex 里覆盖都薄，而 Europe PMC 是
PubMed + PMC 全文 + 预印本的**统一检索口**（EBI 官方维护），免 key、返回 JSON。
判权层把 ``europe_pmc`` 归 tier 2（同行评议索引/全文库）。

真机约定（``europepmc.org/RestfulWebService`` 官方文档）：
- ``GET /europepmc/webservices/rest/search?query=<q>&format=json``
  → ``{"hitCount": n, "resultList": {"result": [...]}}``
- ``resultType=core`` 才带 ``abstractText`` 与 ``fullTextUrlList``（默认 ``lite`` 字段很少，
  少了摘要等于让判权层裸判）
- 落地页优先级：``fullTextUrlList.fullTextUrl`` 里的 **OA + html** 那一条 > ``https://doi.org/{doi}``
  > ``https://europepmc.org/article/{source}/{id}``
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query, SourceCard
from scholarscout.providers.jsonapi import as_dict, list_at, payload_of
from scholarscout.providers.websearch import text_of

NAME = "europe_pmc"
LABEL = "Europe PMC"
ENDPOINT = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
API_KEY_ENV = None  # 免 key
MIN_INTERVAL_S = 0.5
MAX_AUTHORS = 8
MAX_ABSTRACT_CHARS = 3000
#: 摘要与全文链接只在 `core` 里有（lite 太瘦，判权层没法用）
RESULT_TYPE = "core"


def year_of(raw: Any) -> int | None:
    """``pubYear`` 是字符串（"2021"）；不是 4 位数字就当没给。"""
    text = str(raw or "").strip()
    return int(text) if len(text) == 4 and text.isdigit() else None


def authors_of(raw: Any) -> tuple[str, ...]:
    """``authorString`` 是一条 ``"Piech C, Bassen J, Huang K."`` 的串（末尾带句点）。"""
    text = str(raw or "").strip().rstrip(".")
    if not text:
        return ()
    names = [part.strip() for part in text.split(",") if part.strip()]
    return tuple(names[:MAX_AUTHORS])


def full_text_url(item: dict[str, Any]) -> str:
    """从 ``fullTextUrlList`` 里挑**开放获取的 html** 那一条（拿不到回空串）。"""
    entries = as_dict(item.get("fullTextUrlList")).get("fullTextUrl")
    if not isinstance(entries, list):
        return ""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        if str(entry.get("availabilityCode") or "").upper() == "OA":
            return url
    return ""


def card_from_result(item: dict[str, Any]) -> SourceCard | None:
    """把一条 ``resultList.result`` 压成 SourceCard；没标题且没落地页的脏数据返回 ``None``。"""
    title = text_of(str(item.get("title") or ""))
    doi = str(item.get("doi") or "").strip() or None
    source = str(item.get("source") or "").strip()
    external_id = str(item.get("id") or "").strip()
    url = (
        full_text_url(item)
        or (f"https://doi.org/{doi}" if doi else "")
        or (
            f"https://europepmc.org/article/{source}/{external_id}"
            if source and external_id
            else ""
        )
    )
    if not title or not url:
        return None
    pub_type = item.get("pubTypeList")
    pub_types = ""
    if isinstance(pub_type, dict) and isinstance(pub_type.get("pubType"), list):
        pub_types = ",".join(str(kind) for kind in pub_type["pubType"])
    return SourceCard(
        url=url,
        title=title,
        source_type=NAME,
        abstract=text_of(str(item.get("abstractText") or ""))[:MAX_ABSTRACT_CHARS] or None,
        authors=authors_of(item.get("authorString")),
        year=year_of(item.get("pubYear")),
        venue=str(item.get("journalTitle") or "").strip() or None,
        doi=doi,
        external_id=f"{source}:{external_id}" if source and external_id else None,
        pdf_url=full_text_url(item) or None,
        extra={
            "provider": NAME,
            "source": source,  # MED / PMC / PPR（预印本）…
            "cited_by": str(
                item.get("citedByCount") if item.get("citedByCount") is not None else ""
            ),
            "pub_types": pub_types,
        },
    )


def parse_results(payload: dict[str, Any], *, limit: int) -> list[SourceCard]:
    """``{"hitCount": n, "resultList": {"result": [...]}}`` → SourceCard 列表。"""
    cards: list[SourceCard] = []
    for item in list_at(payload, "resultList", "result", label=LABEL):
        if not isinstance(item, dict):
            continue
        card = card_from_result(item)
        if card is not None:
            cards.append(card)
        if len(cards) >= limit:
            break
    return cards


def search_url(*, endpoint: str, text: str, limit: int) -> str:
    """``query`` + ``format=json`` + ``resultType=core`` + ``pageSize``。"""
    params = {
        "query": text,
        "format": "json",
        "resultType": RESULT_TYPE,
        "pageSize": str(max(1, min(limit, 100))),
    }
    return f"{endpoint}?{urlencode(params)}"


class EuropePmcProvider:
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
