"""OpenAlex 检索（**免 key**，官方开放学术图谱）。

为什么补这一路：arXiv 只覆盖预印本、GitHub 只覆盖代码，而**期刊/会议版论文与综述**
（`Knowledge Tracing: A Survey` 这类一手综述）在 OpenAlex 里——它是判权层 tier 2
（"学术索引与同行评议"）的主要来源，也是 `docs/01 §5.1` 三层来源里"学术"那一层的另一半。

真机实测（2026-10-05 本机、国内网络、免 key）：
- `GET https://api.openalex.org/works?search=knowledge tracing` → **1.5s**、569k 命中
- `robots.txt` 是 ``User-agent: * / Allow: /``（HTTP 200）→ **明确允许，不需要进豁免名单**
- 返回里 `abstract_inverted_index` 是**倒排索引**（词 → 位置数组），要自己还原成摘要文本；
  不给 `mailto` 也返回，但官方按"礼貌池"给更好的服务，所以支持配 `search.openalex_mailto`
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query, SourceCard

NAME = "openalex"
ENDPOINT = "https://api.openalex.org/works"
API_KEY_ENV = None  # 免 key
MIN_INTERVAL_S = 0.5  # 官方建议 ≤10 次/秒，留出余量
#: 倒排摘要还原后的长度上限（摘要用不上全文，太长只会挤占上下文）
MAX_ABSTRACT_CHARS = 4000
MAX_AUTHORS = 8


def _as_dict(value: Any) -> dict[str, Any]:
    """``dict`` 就返回，否则给空 dict —— 免得 mypy 对 ``item.get(...) if isinstance(...)`` 反复收窄失败。"""
    return value if isinstance(value, dict) else {}


def abstract_from_inverted_index(index: Any) -> str | None:
    """OpenAlex 的摘要以**倒排索引**给出（词 → 位置数组），按位置还原成文本。"""
    if not isinstance(index, dict) or not index:
        return None
    positioned: list[tuple[int, str]] = []
    for word, positions in index.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int):
                positioned.append((position, str(word)))
    if not positioned:
        return None
    positioned.sort(key=lambda item: item[0])
    text = " ".join(word for _position, word in positioned)
    return text[:MAX_ABSTRACT_CHARS]


def _restore_doi(raw: Any) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    return value.replace("https://doi.org/", "").replace("http://dx.doi.org/", "")


def card_from_work(item: dict[str, Any]) -> SourceCard | None:
    """把一条 OpenAlex work 压成 SourceCard。"""
    title = str(item.get("display_name") or item.get("title") or "").strip()
    location = _as_dict(item.get("primary_location"))
    open_access = _as_dict(item.get("open_access"))
    doi = _restore_doi(item.get("doi"))
    url = (
        str(location.get("landing_page_url") or "").strip()
        or str(open_access.get("oa_url") or "").strip()
        or (f"https://doi.org/{doi}" if doi else "")
        or str(item.get("id") or "").strip()
    )
    if not title or not url:
        return None
    source_info = _as_dict(location.get("source"))
    authors: list[str] = []
    for authorship in item.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        author = _as_dict(authorship.get("author"))
        name = str(author.get("display_name") or "").strip()
        if name:
            authors.append(name)
        if len(authors) >= MAX_AUTHORS:
            break
    year = item.get("publication_year")
    return SourceCard(
        url=url,
        title=title,
        source_type="openalex",
        abstract=abstract_from_inverted_index(item.get("abstract_inverted_index")),
        authors=tuple(authors),
        year=int(year) if isinstance(year, int) else None,
        venue=str(source_info.get("display_name") or "").strip() or None,
        doi=doi,
        external_id=str(item.get("id") or "").strip() or None,
        pdf_url=str(location.get("pdf_url") or open_access.get("oa_url") or "").strip() or None,
        extra={
            "provider": NAME,
            "cited_by": str(
                item.get("cited_by_count") if item.get("cited_by_count") is not None else ""
            ),
            "type": str(item.get("type") or ""),
            "oa_status": str(open_access.get("oa_status") or ""),
        },
    )


def parse_results(payload: dict[str, Any], *, limit: int) -> list[SourceCard]:
    cards: list[SourceCard] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        card = card_from_work(item)
        if card is not None:
            cards.append(card)
        if len(cards) >= limit:
            break
    return cards


def works_url(*, endpoint: str, text: str, limit: int, mailto: str | None = None) -> str:
    params = {"search": text, "per-page": str(max(1, min(limit, 50)))}
    if mailto:
        params["mailto"] = mailto
    return f"{endpoint}?{urlencode(params)}"


class OpenAlexProvider:
    name = NAME

    def __init__(
        self,
        http: HttpClient,
        *,
        endpoint: str = ENDPOINT,
        mailto: str | None = None,
        min_interval_s: float = MIN_INTERVAL_S,
    ) -> None:
        self.http = http
        self.endpoint = endpoint
        self.mailto = (mailto or "").strip() or None
        self.min_interval_s = min_interval_s

    def search(self, query: Query) -> list[SourceCard]:
        response = self.http.fetch(
            works_url(
                endpoint=self.endpoint,
                text=query.text,
                limit=query.limit,
                mailto=self.mailto,
            ),
            accept="application/json",
            min_interval_s=self.min_interval_s,
            cache=False,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            return []
        cards = parse_results(payload, limit=query.limit)
        if query.year_from is None:
            return cards
        # OpenAlex 支持年份过滤，但检索阶段先取回再本地筛，避免一次请求两种查询形态
        return [card for card in cards if card.year is None or card.year >= query.year_from]
