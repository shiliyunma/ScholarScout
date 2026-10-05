"""通用检索与商用检索来源：解析 + 真发请求（httpx.MockTransport，不联网）。

两类断言缺一不可：
- **解析**：各家返回压成 SourceCard（DOM/字段名写错时立刻红）
- **发请求**：GET/POST 方法、认证头、请求体真的按各家 API 的要求发出去了
  （离线只测解析会漏掉"参数名写错/方法用错"这类接口边界坑，docs/05 §3.5）
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query
from scholarscout.providers.bing import BingProvider
from scholarscout.providers.bing import parse_results as bing_parse
from scholarscout.providers.brave import BraveProvider
from scholarscout.providers.brave import parse_results as brave_parse
from scholarscout.providers.duckduckgo import DuckDuckGoProvider
from scholarscout.providers.duckduckgo import parse_results as ddg_parse
from scholarscout.providers.google_cse import GoogleCseProvider
from scholarscout.providers.searxng import SearxngProvider
from scholarscout.providers.searxng import parse_results as searxng_parse
from scholarscout.providers.serper import SerperProvider
from scholarscout.providers.serper import parse_results as serper_parse
from scholarscout.providers.tavily import TavilyProvider
from scholarscout.providers.tavily import parse_results as tavily_parse
from scholarscout.providers.websearch import card_from, is_content_url, unwrap_redirect

# ===== 共用件 =====


def test_unwrap_bing_redirect_shell() -> None:
    target = "https://arxiv.org/abs/2401.00001"
    token = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    shell = f"https://www.bing.com/ck/a?!&&p=abc&u=a1{token}&ntb=1"
    assert unwrap_redirect(shell) == target


def test_unwrap_duckduckgo_redirect() -> None:
    shell = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fpaper&rut=1"
    assert unwrap_redirect(shell) == "https://example.org/paper"


def test_unwrap_plain_url_is_unchanged() -> None:
    assert unwrap_redirect("https://example.org/x") == "https://example.org/x"


def test_unwrap_broken_bing_shell_returns_empty() -> None:
    # 解不开的跳转壳必须丢掉，不能当真实 URL 入库
    assert unwrap_redirect("https://www.bing.com/ck/a?u=a1!!!!not-base64") == ""


@pytest.mark.parametrize(
    "url",
    [
        "https://www.iciba.com/word?w=agent",
        "https://cn.bing.com/search?q=x",
        "https://x.com/bilibili.com/video",
    ],
)
def test_non_content_urls_are_rejected(url: str) -> None:
    assert is_content_url(url) is False


def test_card_from_needs_title_and_real_url() -> None:
    assert card_from(url="https://example.org/a", title="   ") is None
    assert card_from(url="javascript:void(0)", title="标题") is None
    card = card_from(
        url="https://example.org/a", title="A <b>Paper</b> 2024", snippet="<p>摘 要</p>"
    )
    assert card is not None
    assert card.title == "A Paper 2024"
    assert card.snippet == "摘 要"
    assert card.year == 2024
    assert card.source_type == "web"


# ===== Bing（免 key，国内实测可直连）=====


def _bing_block(url: str, title: str, snippet: str) -> str:
    token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return (
        '<li class="b_algo" data-id iid=SERP.1>'
        '<link rel="stylesheet" href="/rp/x.gz.css"/>'
        f'<h2><a href="https://www.bing.com/ck/a?u=a1{token}&ntb=1">{title}</a></h2>'
        f"<div><p>{snippet}</p></div>"
    )


BING_HTML = (
    "<html><body><ol id=b_results>"
    + _bing_block(
        "https://example.org/edu-agent", "Educational agents 2024 综述", "系统回顾了教育智能体。"
    )
    + _bing_block("https://www.iciba.com/word?w=educational", "educational 释义", "词典")
    + _bing_block("https://example.org/tools", "LLM tool use", "工具调用评测。")
    + "</ol></body></html>"
)


def test_bing_parse_results_extracts_rows() -> None:
    # 解析层只负责"把结果抽出来"；非内容结果由 cards_from_pairs 挡在下一层（词典项也在其中）
    rows = bing_parse(BING_HTML, limit=10)
    urls = [row[0] for row in rows]
    assert urls == [
        "https://example.org/edu-agent",
        "https://www.iciba.com/word?w=educational",
        "https://example.org/tools",
    ]
    assert rows[0][1] == "Educational agents 2024 综述"


def test_bing_parse_skips_blocks_without_anchor() -> None:
    assert bing_parse('<li class="b_algo"><div>没有标题</div>', limit=5) == []


def test_bing_provider_sends_get_with_query() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(
            200, text=BING_HTML, headers={"Content-Type": "text/html; charset=utf-8"}
        )

    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    cards = BingProvider(http).search(Query(text="educational agent", limit=5))
    assert seen["method"] == "GET"
    assert "q=educational+agent" in seen["url"]
    assert len(cards) == 2  # 词典项被丢
    assert cards[0].url == "https://example.org/edu-agent"


# ===== DuckDuckGo（免 key，但需梯子）=====

DDG_HTML = (
    '<div class="result results_links results_links_deep web-result">'
    '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fddg%2F1">'
    "Educational Agent Survey</a>"
    '<a class="result__snippet">A survey of <b>educational agents</b>.</a>'
    "</div></div>"
)


def test_duckduckgo_parse_and_fetch() -> None:
    rows = ddg_parse(DDG_HTML, limit=5)
    assert rows == [
        ("https://example.org/ddg/1", "Educational Agent Survey", "A survey of educational agents.")
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "q=edu" in str(request.url)
        return httpx.Response(200, text=DDG_HTML, headers={"Content-Type": "text/html"})

    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    cards = DuckDuckGoProvider(http).search(Query(text="edu", limit=5))
    assert [card.url for card in cards] == ["https://example.org/ddg/1"]


# ===== SearxNG（免 key，需自建实例地地址）=====


def test_searxng_requires_base_url() -> None:
    http = HttpClient(respect_robots=False)
    with pytest.raises(ValueError, match="searxng_base_url"):
        SearxngProvider(http, base_url="  ")


def test_searxng_parse_and_fetch() -> None:
    payload = {
        "results": [
            {"url": "https://example.org/sx", "title": "知识追踪", "content": "知识追踪综述。"},
            {"url": "", "title": "坏数据", "content": "x"},
        ]
    }
    assert len(searxng_parse(payload, limit=5)) == 1  # 无 URL 的结果在解析层就被丢掉（不可用）

    def handler(request: httpx.Request) -> httpx.Response:
        assert "format=json" in str(request.url)
        return httpx.Response(200, json=payload)

    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    cards = SearxngProvider(http, base_url="https://searx.example.org").search(
        Query(text="知识追踪", limit=5)
    )
    assert [card.url for card in cards] == ["https://example.org/sx"]


# ===== Tavily（商用，需 key）=====


def test_tavily_requires_key() -> None:
    with pytest.raises(ValueError, match="TAVILY_API_KEY"):
        TavilyProvider(HttpClient(respect_robots=False), api_key="")


def test_tavily_posts_json_with_key_in_body() -> None:
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["method"] = request.method
        sent["body"] = json.loads(request.content.decode())
        sent["content_type"] = request.headers.get("content-type", "")
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.org/t1",
                        "title": "Tavily 结果",
                        "content": "摘要",
                        "score": 0.91,
                    }
                ]
            },
        )

    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    cards = TavilyProvider(http, api_key="tvly-test").search(Query(text="agent", limit=5))
    assert sent["method"] == "POST"
    assert sent["content_type"].startswith("application/json")
    body = sent["body"]
    assert isinstance(body, dict)
    assert (
        body["api_key"] == "tvly-test"
        and body["query"] == "agent"
        and body["include_answer"] is False
    )
    assert cards[0].url == "https://example.org/t1"
    assert cards[0].extra["score"] == "0.91"


def test_tavily_parse_keeps_score() -> None:
    cards = tavily_parse(
        {"results": [{"url": "https://a.org/x", "title": "T", "content": "C", "score": 0.5}]},
        limit=3,
    )
    assert cards[0].extra == {"provider": "tavily", "score": "0.5"}


# ===== Serper（商用，需 key）=====


def test_serper_requires_key() -> None:
    with pytest.raises(ValueError, match="SERPER_API_KEY"):
        SerperProvider(HttpClient(respect_robots=False), api_key="")


def test_serper_posts_with_api_key_header_and_reads_date() -> None:
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["method"] = request.method
        sent["api_key"] = request.headers.get("x-api-key")
        sent["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "organic": [
                    {
                        "title": "Serper 结果",
                        "link": "https://example.org/s1",
                        "snippet": "片段",
                        "date": "2025-03-02",
                        "position": 1,
                    }
                ]
            },
        )

    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    cards = SerperProvider(http, api_key="sp-test").search(Query(text="llm agent", limit=5))
    assert sent["method"] == "POST"
    assert sent["api_key"] == "sp-test"
    body = sent["body"]
    assert isinstance(body, dict)
    assert body["q"] == "llm agent" and body["num"] == 5
    assert cards[0].url == "https://example.org/s1"
    assert cards[0].year == 2025  # date 字段比标题里猜更准


def test_serper_parse_skips_bad_rows() -> None:
    cards = serper_parse({"organic": [{"title": "", "link": "https://a.org"}, "junk"]}, limit=3)
    assert cards == []


# ===== Brave（商用，需 key）=====


def test_brave_requires_key() -> None:
    with pytest.raises(ValueError, match="BRAVE_API_KEY"):
        BraveProvider(HttpClient(respect_robots=False), api_key="")


def test_brave_gets_with_subscription_header() -> None:
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["method"] = request.method
        sent["token"] = request.headers.get("x-subscription-token")
        sent["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Brave 结果",
                            "url": "https://example.org/b1",
                            "description": "描述",
                            "age": "2025-01-05",
                        }
                    ]
                }
            },
        )

    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    cards = BraveProvider(http, api_key="br-test").search(Query(text="agent", limit=5))
    assert sent["method"] == "GET"
    assert sent["token"] == "br-test"
    assert "count=5" in str(sent["url"])
    assert cards[0].year == 2025


def test_brave_parse_handles_missing_web_key() -> None:
    assert brave_parse({}, limit=3) == []


# ===== Google CSE（商用，免费额度 100 次/天）=====


def test_google_cse_needs_key_and_cx() -> None:
    http = HttpClient(respect_robots=False)
    with pytest.raises(ValueError, match="GOOGLE_CSE_KEY"):
        GoogleCseProvider(http, api_key="", cx="abc")
    with pytest.raises(ValueError, match="google_cse_cx"):
        GoogleCseProvider(http, api_key="k", cx="")


def test_google_cse_gets_with_key_and_cx_in_query() -> None:
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["method"] = request.method
        sent["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "items": [
                    {"title": "CSE 结果", "link": "https://example.org/g1", "snippet": "摘要文本"},
                    {"title": "缺链接", "snippet": "x"},
                ]
            },
        )

    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    cards = GoogleCseProvider(http, api_key="gk-test", cx="cx-test").search(
        Query(text="教育智能体", limit=20)
    )
    assert sent["method"] == "GET"
    url = str(sent["url"])
    assert "key=gk-test" in url and "cx=cx-test" in url
    # CSE 单次最多 10 条：请求时要夹到 10，不能把 20 原样发出去
    assert "num=10" in url
    assert [card.url for card in cards] == ["https://example.org/g1"]
    assert cards[0].source_type == "web"


def test_google_cse_parse_handles_empty_payload() -> None:
    from scholarscout.providers.google_cse import parse_results as cse_parse

    assert cse_parse({}, limit=3) == []
    assert cse_parse({"items": ["junk", {"title": "", "link": "https://a.org"}]}, limit=3) == []
