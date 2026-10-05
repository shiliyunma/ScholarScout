"""arXiv 源的契约：URL 拼装、Atom 解析、必须带 Accept 头、限速。

用 tests/fixtures/arxiv_sample.xml（人工整理的真实结构样例），全离线。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from scholarscout.fetch.http import FetchError, HttpClient
from scholarscout.providers.arxiv import ACCEPT, ArxivProvider, build_url, parse_feed
from scholarscout.providers.base import Query

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "arxiv_sample.xml"


@pytest.fixture
def feed_xml() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_build_url_splits_terms_with_and() -> None:
    """多词查询要按 AND 组合，不能整句加引号（整句短语在真实检索里几乎恒 0 条）。"""
    url = build_url(Query("education agent", limit=5))
    assert "search_query=all%3Aeducation+AND+all%3Aagent" in url
    assert "%22" not in url, "不应出现引号（那会变成精确短语查询）"
    assert "max_results=5" in url
    assert "sortBy=relevance" in url
    assert "sortBy=submittedDate" in build_url(Query("x", sort="date"))


def test_build_query_expression_handles_chinese_and_empty() -> None:
    from scholarscout.providers.arxiv import build_query_expression

    assert build_query_expression("教育智能体") == "all:教育智能体"  # 中文无空格：单词项
    assert build_query_expression("  a   b ") == "all:a AND all:b"  # 多余空白折叠
    assert build_query_expression("") == 'all:""'
    assert (
        build_query_expression(" ".join(f"t{i}" for i in range(20))).count("AND") == 7
    )  # 最多 8 词


def test_parse_feed_extracts_fields(feed_xml: str) -> None:
    cards = parse_feed(feed_xml)
    assert len(cards) == 2

    first = cards[0]
    assert first.source_type == "arxiv"
    assert first.title == "Retrieval-Augmented Education Agents: A Survey", (
        "标题里的换行与多余空白要压平"
    )
    assert first.year == 2025
    assert first.authors == ("Ada Lovelace", "Alan Turing")
    assert first.doi == "10.1000/example.1"
    assert first.external_id == "2501.00001"
    assert first.url == "https://arxiv.org/abs/2501.00001", "要去掉版本号、统一 https"
    assert first.pdf_url == "http://arxiv.org/pdf/2501.00001v2"
    assert first.extra["primary_category"] == "cs.CL"
    assert "42% to 58%" in str(first.abstract)


def test_parse_feed_second_entry_has_no_doi(feed_xml: str) -> None:
    second = parse_feed(feed_xml)[1]
    assert second.doi is None
    assert second.authors == ("Grace Hopper",)
    assert second.extra["primary_category"] == "cs.AI"


def test_parse_feed_rejects_broken_xml() -> None:
    with pytest.raises(FetchError) as info:
        parse_feed("<feed><entry>")
    assert info.value.kind == "bad_request"


def test_canonical_url_drops_version_and_query(feed_xml: str) -> None:
    card = parse_feed(feed_xml)[0]
    assert card.domain == "arxiv.org"
    assert card.to_row()["url_canonical"] == "https://arxiv.org/abs/2501.00001"


def test_provider_sends_arxiv_accept_header(feed_xml: str) -> None:
    """防回归：arXiv 不带 Accept 会 406。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(200, text=feed_xml, headers={"content-type": "application/atom+xml"})

    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    cards = ArxivProvider(http).search(Query("education agent", limit=2))
    assert seen["accept"] == ACCEPT
    assert "application/atom+xml" in seen["accept"]
    assert len(cards) == 2


def test_provider_rate_limits_consecutive_searches(feed_xml: str) -> None:
    slept: list[float] = []

    class Clock:
        t = 0.0

        def __call__(self) -> float:
            return self.t

    clock = Clock()

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.t += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=feed_xml)

    http = HttpClient(
        respect_robots=False,
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=sleep,
    )
    provider = ArxivProvider(http)
    provider.search(Query("a"))
    provider.search(Query("b"))
    assert slept == [3.0], "arXiv 二次请求前必须等 3 秒"


def test_provider_propagates_fetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(406, text="rate limited")

    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    with pytest.raises(FetchError) as info:
        ArxivProvider(http).search(Query("x"))
    assert info.value.kind == "bad_request"
    assert info.value.status == 406
