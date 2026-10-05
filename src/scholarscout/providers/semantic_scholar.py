"""Semantic Scholar 检索（**需要 API key**）。

为什么它是"需要 key"而不是免 key：真机实测（2026-10-05）无 key 直接打
``GET https://api.semanticscholar.org/graph/v1/paper/search`` → **HTTP 429**
（``Too Many Requests … apply for a key for higher rate limits``），也就是说
没有 key 时这条路**根本不可用** —— 把它塞进 `auto` 只会每次检索都白等一次 429。

所以按商用来源那套处理：key 放 `.env` 的 ``S2_API_KEY``，没配就在
``provider_status`` 里如实报 ``missing_api_key``（不再是含糊的 ``not_implemented``）。
配了 key 后它给的补充价值是：跨库引用图与被引数（用于判权与"这篇是不是被广泛接受"）。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query, SourceCard

NAME = "semantic_scholar"
ENDPOINT = "https://api.semanticscholar.org/graph/v1/paper/search"
API_KEY_ENV = "S2_API_KEY"
#: 官方限速约 1 次/秒（有 key 更高），留余量
MIN_INTERVAL_S = 1.5
FIELDS = (
    "title,abstract,year,venue,externalIds,authors,openAccessPdf,"
    "citationCount,influentialCitationCount,publicationTypes"
)


def _as_dict(value: Any) -> dict[str, Any]:
    """``dict`` 就返回，否则给空 dict（同 openalex.py，便于 mypy 收窄）。"""
    return value if isinstance(value, dict) else {}


def card_from_paper(item: dict[str, Any]) -> SourceCard | None:
    title = str(item.get("title") or "").strip()
    external = _as_dict(item.get("externalIds"))
    doi = str(external.get("DOI") or "").strip() or None
    arxiv_id = str(external.get("ArXiv") or "").strip()
    paper_id = str(item.get("paperId") or "").strip()
    url = (
        (f"https://doi.org/{doi}" if doi else "")
        or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "")
        or (f"https://www.semanticscholar.org/paper/{paper_id}" if paper_id else "")
    )
    if not title or not url:
        return None
    pdf = _as_dict(item.get("openAccessPdf"))
    authors: list[str] = []
    for author in item.get("authors") or []:
        if isinstance(author, dict) and str(author.get("name") or "").strip():
            authors.append(str(author["name"]).strip())
        if len(authors) >= 8:
            break
    year = item.get("year")
    return SourceCard(
        url=url,
        title=title,
        source_type="semantic_scholar",
        abstract=str(item.get("abstract") or "").strip() or None,
        authors=tuple(authors),
        year=int(year) if isinstance(year, int) else None,
        venue=str(item.get("venue") or "").strip() or None,
        doi=doi,
        external_id=paper_id or None,
        pdf_url=str(pdf.get("url") or "").strip() or None,
        extra={
            "provider": NAME,
            "cited_by": str(
                item.get("citationCount") if item.get("citationCount") is not None else ""
            ),
            "types": ",".join(str(kind) for kind in (item.get("publicationTypes") or [])),
        },
    )


def parse_results(payload: dict[str, Any], *, limit: int) -> list[SourceCard]:
    cards: list[SourceCard] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        card = card_from_paper(item)
        if card is not None:
            cards.append(card)
        if len(cards) >= limit:
            break
    return cards


class SemanticScholarProvider:
    name = NAME

    def __init__(
        self,
        http: HttpClient,
        *,
        api_key: str,
        endpoint: str = ENDPOINT,
        min_interval_s: float = MIN_INTERVAL_S,
    ) -> None:
        if not api_key.strip():
            raise ValueError(f"semantic_scholar 需要 {API_KEY_ENV}（写在 .env 里）")
        self.http = http
        self.api_key = api_key
        self.endpoint = endpoint
        self.min_interval_s = min_interval_s

    def search(self, query: Query) -> list[SourceCard]:
        params = {
            "query": query.text,
            "limit": str(max(1, min(query.limit, 20))),
            "fields": FIELDS,
        }
        response = self.http.fetch(
            f"{self.endpoint}?{urlencode(params)}",
            accept="application/json",
            headers={"x-api-key": self.api_key},
            min_interval_s=self.min_interval_s,
            cache=False,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            return []
        return parse_results(payload, limit=query.limit)
