"""五个免 key JSON 源（crossref / datacite / doaj / europe_pmc / hn_algolia）。

全离线：用 ``httpx.MockTransport`` 打桩，**不发真请求**（真机那一次的证据在 docs/25 与提交说明里）。

每个源都钉同样五件事（少一件就等于没接线）：

1. **正常回包** → title/url/年份/作者/期刊这些字段真的落到 ``SourceCard`` 上
2. **空结果** → 回空列表且**不崩**（``total:0`` / ``items:[]`` / ``data:[]`` 三种形状都有）
3. **HTTP 500** → 抛的是可读的 :class:`FetchError`（``kind`` 能进 ``provider_status``），
   **不是** ``KeyError`` / ``TypeError`` 这类让上层只能打印类型名的异常
4. **中文查询** → URL 里是 percent-encoded 的 UTF-8（没编码会被服务端当非法请求）
5. **字段缺失** → 用安全默认值（无年份 → ``None``、无作者 → 空元组），
   既没标题也没链接的脏数据直接丢，不编一个假标题
"""

from __future__ import annotations

import httpx
import pytest

from scholarscout.fetch.http import FetchError, HttpClient
from scholarscout.providers.base import Query
from scholarscout.providers.crossref import CrossrefProvider
from scholarscout.providers.crossref import parse_results as crossref_parse
from scholarscout.providers.datacite import DataCiteProvider
from scholarscout.providers.datacite import parse_results as datacite_parse
from scholarscout.providers.doaj import DoajProvider
from scholarscout.providers.doaj import parse_results as doaj_parse
from scholarscout.providers.europe_pmc import EuropePmcProvider
from scholarscout.providers.europe_pmc import parse_results as epmc_parse
from scholarscout.providers.hn_algolia import HnAlgoliaProvider
from scholarscout.providers.hn_algolia import parse_results as hn_parse

#: 中文查询「教育」的 UTF-8 percent-encoding —— 五个源都必须编成这样
ZH = "%E6%95%99%E8%82%B2"


def _http(handler, *, retries: int = 1) -> HttpClient:
    """离线 HttpClient：不查 robots、只试 1 次（重试会让 500 的用例白等退避）。"""
    return HttpClient(
        respect_robots=False,
        retries=retries,
        transport=httpx.MockTransport(handler),
    )


# ============================================================ Crossref

CROSSREF = {
    "status": "ok",
    "message": {
        "items": [
            {
                "DOI": "10.1145/3569576",
                "URL": "https://doi.org/10.1145/3569576",
                "title": ["<jats:title>Knowledge Tracing</jats:title>: A Survey"],
                "author": [{"given": "Ahmed", "family": "Abdelrahman"}, {"name": "ACM SIGKDD"}],
                "issued": {"date-parts": [[2022, 3, 1]]},
                "container-title": ["ACM Computing Surveys"],
                "publisher": "ACM",
                "abstract": "<jats:p>Knowledge tracing models learner knowledge.</jats:p>",
                "type": "journal-article",
                "is-referenced-by-count": 421,
            },
            {"title": [], "DOI": ""},  # 脏数据：没标题也没 URL
        ]
    },
}


def test_crossref_parse_maps_fields_and_strips_jats() -> None:
    cards = crossref_parse(CROSSREF, limit=5)
    assert [card.title for card in cards] == ["Knowledge Tracing: A Survey"]
    card = cards[0]
    assert card.source_type == "crossref"
    assert card.url == "https://doi.org/10.1145/3569576"
    assert card.doi == "10.1145/3569576"
    assert card.year == 2022
    assert card.venue == "ACM Computing Surveys"
    assert card.authors == ("Ahmed Abdelrahman", "ACM SIGKDD")
    assert card.abstract == "Knowledge tracing models learner knowledge."
    assert card.extra["cited_by"] == "421"
    assert card.extra["publisher"] == "ACM"


def test_crossref_empty_items_is_empty_not_error() -> None:
    assert crossref_parse({"message": {"items": []}}, limit=5) == []


def test_crossref_missing_fields_use_safe_defaults() -> None:
    """没有年份/作者/期刊的条目照样入库 —— 缺字段就用 ``None``，不猜。"""
    payload = {
        "message": {"items": [{"title": ["无元数据的条目"], "URL": "https://example.org/a"}]}
    }
    card = crossref_parse(payload, limit=3)[0]
    assert card.year is None
    assert card.authors == ()
    assert card.venue is None
    assert card.doi is None


def test_crossref_500_raises_readable_fetch_error() -> None:
    http = _http(lambda request: httpx.Response(500, text="upstream is down"))
    with pytest.raises(FetchError) as excinfo:
        CrossrefProvider(http).search(Query(text="x", limit=3))
    assert excinfo.value.kind == "server"
    assert "500" in str(excinfo.value)


def test_crossref_structure_change_is_reported_not_silently_empty() -> None:
    """对方把 ``message.items`` 改没了 → 必须说"结构变了"，不能回空列表骗模型说没资料。"""
    http = _http(lambda request: httpx.Response(200, json={"message": {}}))
    with pytest.raises(FetchError, match="结构变了"):
        CrossrefProvider(http).search(Query(text="x", limit=3))


def test_crossref_chinese_query_is_percent_encoded_and_carries_mailto() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=CROSSREF)

    http = _http(handler)
    cards = CrossrefProvider(http, mailto="me@example.com").search(Query(text="教育", limit=3))
    assert ZH in seen["url"]
    assert "mailto=me%40example.com" in seen["url"]
    assert "rows=3" in seen["url"]
    assert len(cards) == 1


# ============================================================ DataCite

DATACITE = {
    "data": [
        {
            "id": "10.5281/zenodo.1234",
            "type": "dois",
            "attributes": {
                "doi": "10.5281/zenodo.1234",
                "titles": [{"title": "KTDataset: A Benchmark for Knowledge Tracing"}],
                "creators": [{"name": "Zhang, Sihao"}, {"givenName": "Li", "familyName": "Wei"}],
                "publisher": "Zenodo",
                "publicationYear": 2023,
                "types": {"resourceType": "Dataset", "resourceTypeGeneral": "Dataset"},
                "url": "https://zenodo.org/record/1234",
                "descriptions": [
                    {"descriptionType": "Other", "description": "配套代码"},
                    {"descriptionType": "Abstract", "description": "A benchmark for KT."},
                ],
            },
        },
        {"id": "10.1/broken", "attributes": {"doi": "10.1/broken", "titles": []}},
    ]
}


def test_datacite_parse_maps_fields_and_prefers_abstract_description() -> None:
    cards = datacite_parse(DATACITE, limit=5)
    assert [card.title for card in cards] == ["KTDataset: A Benchmark for Knowledge Tracing"]
    card = cards[0]
    assert card.source_type == "datacite"
    assert card.url == "https://zenodo.org/record/1234"
    assert card.doi == "10.5281/zenodo.1234"
    assert card.year == 2023
    assert card.venue == "Zenodo"
    assert card.authors == ("Zhang, Sihao", "Li Wei")
    assert card.abstract == "A benchmark for KT."
    assert card.extra["resource_type"] == "Dataset"
    assert card.extra["type"] == "Dataset"


def test_datacite_empty_data_is_empty_not_error() -> None:
    assert datacite_parse({"data": []}, limit=5) == []
    assert datacite_parse({"data": None}, limit=5) == []


def test_datacite_missing_fields_fall_back_to_doi_url_and_none_year() -> None:
    """``publicationYear`` 缺失 / ``url`` 缺失 → 年份 None、URL 回落到 DOI，不编。"""
    payload = {
        "data": [
            {
                "id": "10.9999/x",
                "attributes": {
                    "doi": "10.9999/x",
                    "titles": [{"title": "无年份数据"}],
                },
            }
        ]
    }
    card = datacite_parse(payload, limit=3)[0]
    assert card.year is None
    assert card.authors == ()
    assert card.url == "https://doi.org/10.9999/x"


def test_datacite_timeout_raises_readable_fetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(FetchError) as excinfo:
        DataCiteProvider(_http(handler)).search(Query(text="x", limit=3))
    assert excinfo.value.kind in {"timeout", "network"}


def test_datacite_non_json_body_is_readable_error() -> None:
    http = _http(lambda request: httpx.Response(200, text="<html>blocked by waf</html>"))
    with pytest.raises(FetchError, match="不是合法 JSON"):
        DataCiteProvider(http).search(Query(text="x", limit=3))


def test_datacite_chinese_query_is_percent_encoded() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=DATACITE)

    cards = DataCiteProvider(_http(handler)).search(Query(text="教育", limit=3))
    assert ZH in seen["url"]
    assert "page%5Bsize%5D=3" in seen["url"]
    assert len(cards) == 1


# ============================================================ DOAJ

DOAJ = {
    "total": 3,
    "results": [
        {
            "id": "d1a2b3",
            "bibjson": {
                "title": "AI in Education: A Review",
                "year": "2021",
                "author": [{"name": "Wang, X"}],
                "abstract": "We review AI in education.",
                "journal": {
                    "title": "Journal of Educational Technology",
                    "publisher": "ET Press",
                    "language": ["EN"],
                    "license": [{"type": "CC BY"}],
                },
                "link": [{"url": "https://example.org/fulltext", "type": "fulltext"}],
                "identifier": [{"type": "doi", "id": "10.1234/doaj.1"}],
            },
        },
        {"id": "no-bibjson"},
    ],
}


def test_doaj_parse_maps_fields_and_prefers_fulltext_link() -> None:
    cards = doaj_parse(DOAJ, limit=5)
    assert [card.title for card in cards] == ["AI in Education: A Review"]
    card = cards[0]
    assert card.source_type == "doaj"
    assert card.url == "https://example.org/fulltext"
    assert card.doi == "10.1234/doaj.1"
    assert card.year == 2021
    assert card.venue == "Journal of Educational Technology"
    assert card.authors == ("Wang, X",)
    assert card.extra["license"] == "CC BY"
    assert card.extra["languages"] == "EN"


def test_doaj_empty_results_is_empty_not_error() -> None:
    assert doaj_parse({"total": 0, "results": []}, limit=5) == []


def test_doaj_url_falls_back_to_doi_then_article_page() -> None:
    """没有 fulltext 链接时：有 DOI 用 DOI，只有 id 就用 DOAJ 文章页（一定打得开）。"""
    payload = {
        "results": [
            {
                "id": "id-1",
                "bibjson": {
                    "title": "只有 DOI",
                    "identifier": [{"type": "doi", "id": "10.9999/d"}],
                },
            },
            {"id": "id-2", "bibjson": {"title": "只有 id"}},
        ]
    }
    cards = doaj_parse(payload, limit=5)
    assert cards[0].url == "https://doi.org/10.9999/d"
    assert cards[1].url == "https://doaj.org/article/id-2"
    assert cards[1].year is None and cards[1].authors == ()


def test_doaj_500_raises_readable_fetch_error() -> None:
    http = _http(lambda request: httpx.Response(500, text="doaj down"))
    with pytest.raises(FetchError) as excinfo:
        DoajProvider(http).search(Query(text="x", limit=3))
    assert excinfo.value.kind == "server"
    assert "500" in str(excinfo.value)


def test_doaj_chinese_query_is_percent_encoded_in_path() -> None:
    """DOAJ 的查询是**路径段**：中文不编码会 404，所以这里必须看到 percent-encoding。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=DOAJ)

    cards = DoajProvider(_http(handler)).search(Query(text="教育", limit=3))
    assert f"/api/search/articles/{ZH}" in seen["url"]
    assert "pageSize=3" in seen["url"]
    assert len(cards) == 1


# ============================================================ Europe PMC

EPMC = {
    "hitCount": 1,
    "resultList": {
        "result": [
            {
                "id": "12345678",
                "source": "MED",
                "title": "Deep learning for EEG.",
                "authorString": "Piech C, Bassen J.",
                "pubYear": "2019",
                "journalTitle": "Nature Medicine",
                "doi": "10.1038/x",
                "abstractText": "We apply RNNs.",
                "citedByCount": 42,
                "pubTypeList": {"pubType": ["Journal Article"]},
                "fullTextUrlList": {
                    "fullTextUrl": [
                        {
                            "availability": "Subscription required",
                            "availabilityCode": "S",
                            "documentStyle": "html",
                            "site": "DOI",
                            "url": "https://doi.org/10.1038/x",
                        },
                        {
                            "availability": "Open access",
                            "availabilityCode": "OA",
                            "documentStyle": "pdf",
                            "site": "Europe_PMC",
                            "url": "https://europepmc.org/articles/PMC1?pdf=render",
                        },
                    ]
                },
            },
            {"id": "", "source": "", "title": ""},
        ]
    },
}


def test_europe_pmc_parse_maps_fields_and_prefers_oa_fulltext() -> None:
    cards = epmc_parse(EPMC, limit=5)
    assert [card.title for card in cards] == ["Deep learning for EEG."]
    card = cards[0]
    assert card.source_type == "europe_pmc"
    assert card.url == "https://europepmc.org/articles/PMC1?pdf=render"
    assert card.doi == "10.1038/x"
    assert card.year == 2019
    assert card.venue == "Nature Medicine"
    assert card.authors == ("Piech C", "Bassen J")
    assert card.external_id == "MED:12345678"
    assert card.extra["cited_by"] == "42"
    assert card.extra["pub_types"] == "Journal Article"


def test_europe_pmc_empty_result_list_is_empty_not_error() -> None:
    assert epmc_parse({"hitCount": 0, "resultList": {"result": []}}, limit=5) == []


def test_europe_pmc_missing_fields_falls_back_to_article_page() -> None:
    payload = {
        "resultList": {
            "result": [
                {"id": "PMC9", "source": "PMC", "title": "无年份无 DOI"},
            ]
        }
    }
    card = epmc_parse(payload, limit=3)[0]
    assert card.url == "https://europepmc.org/article/PMC/PMC9"
    assert card.year is None
    assert card.authors == ()
    assert card.doi is None


def test_europe_pmc_500_raises_readable_fetch_error() -> None:
    http = _http(lambda request: httpx.Response(500, text="ebi down"))
    with pytest.raises(FetchError) as excinfo:
        EuropePmcProvider(http).search(Query(text="x", limit=3))
    assert excinfo.value.kind == "server"


def test_europe_pmc_chinese_query_is_percent_encoded_and_asks_for_core() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=EPMC)

    cards = EuropePmcProvider(_http(handler)).search(Query(text="教育", limit=3))
    assert ZH in seen["url"]
    assert "format=json" in seen["url"]
    assert "resultType=core" in seen["url"], "lite 不带摘要，判权层没法用"
    assert len(cards) == 1


# ============================================================ HN Algolia

HN = {
    "nbHits": 2,
    "hits": [
        {
            "objectID": "26123456",
            "title": "Show HN: A local knowledge tracing app",
            "url": "https://example.org/demo",
            "author": "sihao",
            "created_at": "2021-06-01T10:00:00Z",
            "points": 120,
            "num_comments": 45,
            "story_text": None,
        },
        {
            "objectID": "26123457",
            "title": None,
            "url": None,
            "author": "someone",
            "created_at": "2022-07-02T00:00:00Z",
            "story_text": "Ask HN: how do you evaluate an agent?",
        },
    ],
}


def test_hn_parse_maps_fields_and_falls_back_to_item_page() -> None:
    cards = hn_parse(HN, limit=5)
    assert cards[0].title == "Show HN: A local knowledge tracing app"
    assert cards[0].url == "https://example.org/demo"
    assert cards[0].source_type == "hn_algolia"
    assert cards[0].year == 2021
    assert cards[0].authors == ("sihao",)
    assert cards[0].venue == "Hacker News"
    assert cards[0].extra["points"] == "120"
    assert cards[1].url == "https://news.ycombinator.com/item?id=26123457"
    assert cards[1].title == "Ask HN: how do you evaluate an agent?"
    assert cards[1].snippet == "Ask HN: how do you evaluate an agent?"
    assert cards[1].year == 2022


def test_hn_empty_hits_is_empty_not_error() -> None:
    assert hn_parse({"nbHits": 0, "hits": []}, limit=5) == []


def test_hn_hit_without_title_and_link_is_dropped_not_invented() -> None:
    """既没标题也没链接的 hit 直接丢 —— 编一个"讨论"标题等于造假来源。"""
    payload = {"hits": [{"objectID": "", "title": None, "url": None, "story_text": None}]}
    assert hn_parse(payload, limit=5) == []


def test_hn_500_raises_readable_fetch_error() -> None:
    http = _http(lambda request: httpx.Response(503, text="algolia unavailable"))
    with pytest.raises(FetchError) as excinfo:
        HnAlgoliaProvider(http).search(Query(text="x", limit=3))
    assert excinfo.value.kind == "server"


def test_hn_chinese_query_is_percent_encoded_and_filters_to_stories() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=HN)

    cards = HnAlgoliaProvider(_http(handler)).search(Query(text="教育", limit=3))
    assert ZH in seen["url"]
    assert "tags=story" in seen["url"], "评论没有标题，引不成资料"
    assert len(cards) == 2
