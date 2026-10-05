"""检索来源的公共类型与 URL 规范化。

- ``SearchProvider`` 是各来源的统一协议（arxiv / openalex / semantic_scholar / web / github）
- ``SourceCard`` 是来源返回的**原始事实**：不含 source_id / tier（那些在入库与判权阶段产生）
- ``canonical_url`` 是去重键：``sources.url_canonical`` 唯一约束靠它（docs/02 §3）
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

#: 追踪参数：参与去重会导致同一篇论文被当成两篇
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "spm",
    "from",
    "ref",
}


@dataclass(frozen=True)
class Query:
    text: str
    limit: int = 10
    year_from: int | None = None
    sort: str = "relevance"  # relevance | date


@dataclass(frozen=True)
class SourceCard:
    """一次检索命中的原始信息。"""

    url: str
    title: str
    source_type: str  # arxiv | openalex | semantic_scholar | web | github | official
    abstract: str | None = None
    authors: tuple[str, ...] = ()
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    external_id: str | None = None
    pdf_url: str | None = None
    snippet: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def canonical(self) -> str:
        return canonical_url(self.url)

    @property
    def domain(self) -> str:
        return urlparse(self.url).netloc.lower()

    def to_row(self) -> dict[str, object]:
        """给 state 层入库用的字典（source_id 由 state 层分配）。"""
        return {
            "url": self.url,
            "url_canonical": self.canonical,
            "domain": self.domain,
            "title": self.title,
            "authors_json": list(self.authors),
            "year": self.year,
            "venue": self.venue,
            "doi": self.doi,
            "source_type": self.source_type,
            "meta_json": {"external_id": self.external_id, "pdf_url": self.pdf_url, **self.extra},
        }


class SearchProvider(Protocol):
    """检索来源协议。实现类必须声明 ``name`` 与 ``min_interval_s``。"""

    name: str
    min_interval_s: float

    def search(self, query: Query) -> list[SourceCard]: ...


def canonical_url(url: str) -> str:
    """规范化 URL：去片段、去追踪参数、排序查询、host 小写、去默认端口、去尾斜杠。

    无 scheme 的写法（``example.com/a``）按 https 处理。
    """
    raw = url.strip()
    parsed = urlparse(raw)
    if not parsed.scheme and not parsed.netloc:
        parsed = urlparse(f"https://{raw}")
        scheme = "https"
    else:
        scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()
    netloc = host
    if parsed.port and not (
        (scheme == "http" and parsed.port == 80) or (scheme == "https" and parsed.port == 443)
    ):
        netloc = f"{host}:{parsed.port}"

    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(sorted(kept))
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", query, ""))


def dedupe_cards(cards: Sequence[SourceCard]) -> list[SourceCard]:
    """按 canonical URL 去重，保留首次出现的顺序。"""
    seen: set[str] = set()
    unique: list[SourceCard] = []
    for card in cards:
        key = card.canonical
        if key in seen:
            continue
        seen.add(key)
        unique.append(card)
    return unique


def year_from_iso(value: str | None) -> int | None:
    """``2025-01-01T00:00:00Z`` → ``2025``。"""
    if not value or len(value) < 4 or not value[:4].isdigit():
        return None
    return int(value[:4])
