"""OpenAlex 与 Semantic Scholar 两个学术索引源。

两个关键点必须钉住：
1. **倒排摘要**：OpenAlex 的 `abstract_inverted_index` 是"词 → 位置数组"，
   不还原就等于丢掉摘要（判权与摘要生成都要用）
2. **S2 无 key 恒 429**（真机实测）→ 按"需要 key"处理，绝不能塞进 auto 白等一次 429
"""

from __future__ import annotations

import httpx
import pytest

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query
from scholarscout.providers.openalex import (
    OpenAlexProvider,
    abstract_from_inverted_index,
    works_url,
)
from scholarscout.providers.openalex import parse_results as openalex_parse
from scholarscout.providers.semantic_scholar import SemanticScholarProvider
from scholarscout.providers.semantic_scholar import parse_results as s2_parse

OPENALEX_PAYLOAD = {
    "meta": {"count": 569233},
    "results": [
        {
            "id": "https://openalex.org/W2963403868",
            "doi": "https://doi.org/10.1145/3569576",
            "display_name": "Knowledge Tracing: A Survey",
            "publication_year": 2022,
            "cited_by_count": 421,
            "type": "review",
            "abstract_inverted_index": {
                "Knowledge": [0],
                "tracing": [1],
                "survey.": [2],
            },
            "authorships": [
                {"author": {"display_name": "Ahmed Abdelrahman"}},
                {"author": {"display_name": "Xi Sun"}},
            ],
            "primary_location": {
                "landing_page_url": "https://doi.org/10.1145/3569576",
                "pdf_url": None,
                "source": {"display_name": "ACM Computing Surveys"},
            },
            "open_access": {"is_oa": False, "oa_status": "closed", "oa_url": None},
        },
        {
            "display_name": "没有 URL 也没有标题的脏数据",
            "primary_location": {},
        },
        {"display_name": "", "id": ""},
    ],
}


def test_abstract_inverted_index_is_reconstructed_in_order() -> None:
    index = {"deep": [0], "learning": [1], "for": [2], "tracing": [3]}
    assert abstract_from_inverted_index(index) == "deep learning for tracing"
    assert abstract_from_inverted_index(None) is None
    assert abstract_from_inverted_index({}) is None
    assert abstract_from_inverted_index({"bad": "not-a-list"}) is None


def test_openalex_parse_maps_fields() -> None:
    cards = openalex_parse(OPENALEX_PAYLOAD, limit=5)
    assert [card.title for card in cards] == ["Knowledge Tracing: A Survey"]
    card = cards[0]
    assert card.source_type == "openalex"
    assert card.doi == "10.1145/3569576"
    assert card.year == 2022
    assert card.venue == "ACM Computing Surveys"
    assert card.authors == ("Ahmed Abdelrahman", "Xi Sun")
    assert card.abstract == "Knowledge tracing survey."
    assert card.extra["cited_by"] == "421"
    assert card.extra["type"] == "review"


def test_openalex_uses_oa_url_when_no_landing_page() -> None:
    payload = {
        "results": [
            {
                "id": "https://openalex.org/W2",
                "display_name": "预印本",
                "primary_location": {},
                "open_access": {"oa_url": "https://example.org/paper.pdf", "is_oa": True},
            }
        ]
    }
    cards = openalex_parse(payload, limit=3)
    assert cards[0].url == "https://example.org/paper.pdf"


def test_openalex_request_carries_mailto_for_polite_pool() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=OPENALEX_PAYLOAD)

    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    provider = OpenAlexProvider(http, mailto="me@example.com")
    cards = provider.search(Query(text="knowledge tracing", limit=3))
    assert "search=knowledge+tracing" in seen["url"]
    assert "mailto=me%40example.com" in seen["url"]
    assert "per-page=3" in seen["url"]
    assert len(cards) == 1


def test_openalex_url_omits_mailto_when_not_configured() -> None:
    url = works_url(endpoint="https://api.openalex.org/works", text="x", limit=5, mailto=None)
    assert "mailto" not in url


def test_openalex_year_from_drops_older_but_keeps_undated() -> None:
    """`year_from` 的口径：**能判定的按年份筛掉，判定不了的留着**（预印本常缺年份，
    宁可留着让判权层去评，也不要静默丢资料）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "display_name": "2022 的",
                    "publication_year": 2022,
                },
                {
                    "id": "https://openalex.org/W2",
                    "display_name": "2024 的",
                    "publication_year": 2024,
                },
                {"id": "https://openalex.org/W3", "display_name": "没年份的"},
            ]
        }
        return httpx.Response(200, json=payload)

    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    cards = OpenAlexProvider(http).search(Query(text="x", limit=5, year_from=2023))
    assert [card.title for card in cards] == ["2024 的", "没年份的"]


S2_PAYLOAD = {
    "total": 1,
    "data": [
        {
            "paperId": "abc123",
            "title": "Deep Knowledge Tracing",
            "abstract": "We apply RNNs to knowledge tracing.",
            "year": 2015,
            "venue": "NeurIPS",
            "citationCount": 1500,
            "publicationTypes": ["JournalArticle"],
            "externalIds": {"DOI": "10.5555/1234", "ArXiv": "1506.05908"},
            "authors": [{"name": "Chris Piech"}, {"name": "Jonathan Bassen"}],
            "openAccessPdf": {"url": "https://example.org/dkt.pdf"},
        },
        {"title": "", "paperId": ""},
    ],
}


def test_semantic_scholar_parse_maps_fields() -> None:
    cards = s2_parse(S2_PAYLOAD, limit=5)
    assert [card.title for card in cards] == ["Deep Knowledge Tracing"]
    card = cards[0]
    assert card.source_type == "semantic_scholar"
    assert card.url == "https://doi.org/10.5555/1234"
    assert card.year == 2015
    assert card.venue == "NeurIPS"
    assert card.pdf_url == "https://example.org/dkt.pdf"
    assert card.extra["cited_by"] == "1500"


def test_semantic_scholar_requires_key() -> None:
    with pytest.raises(ValueError, match="S2_API_KEY"):
        SemanticScholarProvider(HttpClient(respect_robots=False), api_key="")


def test_semantic_scholar_sends_key_header_and_fields() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-api-key", "")
        return httpx.Response(200, json=S2_PAYLOAD)

    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    cards = SemanticScholarProvider(http, api_key="s2-test").search(Query(text="dkt", limit=2))
    assert seen["key"] == "s2-test"
    assert "fields=title%2Cabstract" in seen["url"], "必须显式要 fields，否则 S2 只回极少字段"
    assert len(cards) == 1
