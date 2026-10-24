"""Crossref 检索（**免 key**，全学科 DOI 底座）。

为什么补这一路：arXiv 只覆盖预印本，OpenAlex 是"二次索引"，而 Crossref 是 **DOI 的注册源头**
—— 期刊卷期、出版社、类型、被引数都直接来自这里。判权层把 ``crossref`` 归 tier 2（学术索引）。

真机约定（``api.crossref.org`` 官方文档）：
- ``GET /works?query=<q>&rows=N`` → ``{"message": {"items": [...]}}``
- ``mailto`` 不是必填，但官方按"礼貌池"（polite pool）给更稳定的服务，所以支持带；
  本项目**复用** ``search.openalex_mailto`` —— 两者都是 Crossref 系生态的同一个联系邮箱，
  不为它再开一个配置键（少一个用户永远不知道要填的开关）
- 标题与摘要里带 **JATS XML**（``<jats:p>`` 之类），必须去标签再入库，否则提示词里全是尖括号
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query, SourceCard
from scholarscout.providers.jsonapi import as_dict, list_at, payload_of
from scholarscout.providers.websearch import text_of

NAME = "crossref"
LABEL = "Crossref"
ENDPOINT = "https://api.crossref.org/works"
API_KEY_ENV = None  # 免 key
MIN_INTERVAL_S = 0.5
MAX_AUTHORS = 8
MAX_ABSTRACT_CHARS = 4000
#: 只取用得上的字段：`/works` 的默认回包很大（几十个字段 × N 条），出网与内存都浪费
SELECT = ",".join(
    (
        "DOI",
        "URL",
        "title",
        "author",
        "issued",
        "published-print",
        "published-online",
        "created",
        "container-title",
        "publisher",
        "abstract",
        "type",
        "is-referenced-by-count",
    )
)
#: 年份取值顺序：**先发表、后退回登记时间**（`issued` 是官方语义上的"出版日期"；
#: 预印本常没有 `issued`，此时 `created` 的登记年是最接近的近似，宁可有年份也别丢）
DATE_FIELDS: tuple[str, ...] = (
    "issued",
    "published-print",
    "published-online",
    "created",
)


def first_text(value: Any) -> str:
    """Crossref 的标题/期刊名是**数组**（多语言时有多项）；取第一项非空并去 JATS 标签。"""
    if isinstance(value, str):
        return text_of(value)
    if not isinstance(value, list):
        return ""
    for item in value:
        cleaned = text_of(str(item)) if item is not None else ""
        if cleaned:
            return cleaned
    return ""


def year_of(item: dict[str, Any]) -> int | None:
    """``issued.date-parts=[[2022, 1, 1]]`` → ``2022``；取不到就 ``None``（不猜）。"""
    for field in DATE_FIELDS:
        parts = as_dict(item.get(field)).get("date-parts")
        if not isinstance(parts, list) or not parts:
            continue
        first = parts[0]
        if not isinstance(first, list) or not first:
            continue
        value = first[0]
        if isinstance(value, int) and 1000 <= value <= 3000:
            return value
    return None


def authors_of(item: dict[str, Any]) -> tuple[str, ...]:
    """作者：优先 ``given family``，只有 ``name``（机构作者）时用 ``name``。"""
    names: list[str] = []
    for author in item.get("author") or []:
        if not isinstance(author, dict):
            continue
        given = str(author.get("given") or "").strip()
        family = str(author.get("family") or "").strip()
        name = f"{given} {family}".strip() or str(author.get("name") or "").strip()
        if name:
            names.append(name)
        if len(names) >= MAX_AUTHORS:
            break
    return tuple(names)


def card_from_item(item: dict[str, Any]) -> SourceCard | None:
    """把一条 Crossref work 压成 SourceCard；没有标题或拿不到 URL 的脏数据返回 ``None``。"""
    title = first_text(item.get("title"))
    doi = str(item.get("DOI") or "").strip() or None
    url = str(item.get("URL") or "").strip() or (f"https://doi.org/{doi}" if doi else "")
    if not title or not url:
        return None
    abstract = text_of(str(item.get("abstract") or ""))[:MAX_ABSTRACT_CHARS] or None
    return SourceCard(
        url=url,
        title=title,
        source_type=NAME,
        abstract=abstract,
        authors=authors_of(item),
        year=year_of(item),
        venue=first_text(item.get("container-title")) or None,
        doi=doi,
        external_id=doi,
        extra={
            "provider": NAME,
            "type": str(item.get("type") or ""),
            "publisher": str(item.get("publisher") or ""),
            "cited_by": str(
                item.get("is-referenced-by-count")
                if item.get("is-referenced-by-count") is not None
                else ""
            ),
        },
    )


def parse_results(payload: dict[str, Any], *, limit: int) -> list[SourceCard]:
    """``{"message": {"items": [...]}}`` → SourceCard 列表（结构变了会抛可读 FetchError）。"""
    cards: list[SourceCard] = []
    for item in list_at(payload, "message", "items", label=LABEL):
        if not isinstance(item, dict):
            continue
        card = card_from_item(item)
        if card is not None:
            cards.append(card)
        if len(cards) >= limit:
            break
    return cards


def works_url(*, endpoint: str, text: str, limit: int, mailto: str | None = None) -> str:
    """``query`` + ``rows``；配了联系邮箱就带上 ``mailto``（礼貌池）。"""
    params = {"query": text, "rows": str(max(1, min(limit, 50))), "select": SELECT}
    if mailto:
        params["mailto"] = mailto
    return f"{endpoint}?{urlencode(params)}"


class CrossrefProvider:
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
        return parse_results(payload_of(response, label=LABEL), limit=query.limit)
