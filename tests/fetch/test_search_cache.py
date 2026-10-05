"""出网层的缓存纪律与请求形态（用 httpx.MockTransport，不联网）。

两件真机吃过的事故，在这里钉死：
1. **别缓存失败/空响应** —— SearxNG 容器刚起、引擎还没加载完时返回 `200 + results: []`，
   被缓存 6 小时后，每次检索都读这个空响应，表现得像"这个来源永远搜不到东西"，
   而直接 curl 容器明明有 16 条。
2. **检索不吃缓存** —— 检索是时点查询（要的是"现在网上有什么"），
   内容抓取才该吃缓存（省带宽、别把 PDF 拉两遍）。
"""

from __future__ import annotations

import httpx
import pytest

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query
from scholarscout.providers.searxng import SearxngProvider
from scholarscout.providers.serper import SerperProvider

EMPTY_JSON = b'{"results": []}'


def test_empty_body_is_not_cached(tmp_path) -> None:
    """200 但空体不许进缓存 —— 一次瞬时故障不能被钉成 6 小时的"事实"。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"", headers={"Content-Type": "application/json"})

    http = HttpClient(
        respect_robots=False,
        transport=httpx.MockTransport(handler),
        cache_dir=tmp_path,
        cache_ttl_hours=6.0,
    )
    http.fetch("http://x.test/empty", accept="application/json")
    http.fetch("http://x.test/empty", accept="application/json")
    assert len(calls) == 2, "空响应被缓存了"
    http.close()


def test_failed_response_is_not_cached(tmp_path) -> None:
    """5xx 会抛 FetchError，且不许把这次失败缓存下来当成功用。"""
    from scholarscout.fetch.http import FetchError

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(503, json={"error": "temporary"})

    http = HttpClient(
        respect_robots=False,
        transport=httpx.MockTransport(handler),
        cache_dir=tmp_path,
        cache_ttl_hours=6.0,
    )
    for _ in range(2):
        with pytest.raises(FetchError):
            http.fetch("http://x.test/boom", accept="application/json")
    assert len(calls) >= 2, "失败响应被缓存了：第二次没打网络"
    http.close()


def test_successful_response_is_still_cached(tmp_path) -> None:
    """正常响应照旧复用缓存（内容抓取要靠它省带宽，别把 PDF 拉两遍）。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    http = HttpClient(
        respect_robots=False,
        transport=httpx.MockTransport(handler),
        cache_dir=tmp_path,
        cache_ttl_hours=6.0,
    )
    http.fetch("http://x.test/ok", accept="application/json")
    http.fetch("http://x.test/ok", accept="application/json")
    assert calls.count("http://x.test/ok") == 1
    http.close()


def test_search_providers_bypass_cache(tmp_path) -> None:
    """同一个查询检索两次，必须打两次网络 —— 否则用户永远看到旧结果。"""
    hits: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(1)
        return httpx.Response(
            200,
            json={
                "results": [{"url": "https://example.org/a", "title": "结果", "content": "摘要"}]
            },
        )

    http = HttpClient(
        respect_robots=False,
        transport=httpx.MockTransport(handler),
        cache_dir=tmp_path,
        cache_ttl_hours=6.0,
    )
    provider = SearxngProvider(http, base_url="http://searx.test")
    first = provider.search(Query(text="教育智能体", limit=5))
    second = provider.search(Query(text="教育智能体", limit=5))
    assert len(hits) == 2, "检索被缓存了：第二次没打网络"
    assert [card.url for card in first] == [card.url for card in second]
    http.close()


def test_post_search_bypasses_cache_too(tmp_path) -> None:
    """POST 类商用检索同样不吃缓存（缓存键含请求体，命中会把旧结果返回）。"""
    hits: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(1)
        return httpx.Response(
            200, json={"organic": [{"title": "T", "link": "https://example.org/p", "snippet": "S"}]}
        )

    http = HttpClient(
        respect_robots=False,
        transport=httpx.MockTransport(handler),
        cache_dir=tmp_path,
        cache_ttl_hours=6.0,
    )
    provider = SerperProvider(http, api_key="k")
    provider.search(Query(text="llm agent", limit=5))
    provider.search(Query(text="llm agent", limit=5))
    assert len(hits) == 2
    http.close()
