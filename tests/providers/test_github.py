"""GitHub 仓库检索（官方 Search API，免 key 可用）。

为什么值得单独测：这个源是**免 key 也能用**的，但额度低（Search API 10 次/分钟），
所以"没配 token 时间隔必须是 6s"这件事必须钉住 —— 间隔写错会直接把额度打光，
表现为间歇性 403/429，很难查（跟 arXiv 那次 406 是同一类坑）。
"""

from __future__ import annotations

import json

import httpx

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query
from scholarscout.providers.github import (
    MIN_INTERVAL_S,
    MIN_INTERVAL_WITH_TOKEN_S,
    GithubProvider,
)
from scholarscout.providers.github import parse_results as github_parse

PAYLOAD = {
    "total_count": 852,
    "items": [
        {
            "full_name": "pykt-team/pykt-toolkit",
            "html_url": "https://github.com/pykt-team/pykt-toolkit",
            "description": "pyKT: A Python Library to Benchmark Deep Learning based Knowledge Tracing Models",
            "language": "Python",
            "stargazers_count": 441,
            "pushed_at": "2026-09-22T10:00:00Z",
            "archived": False,
        },
        {
            "full_name": "chrispiech/DeepKnowledgeTracing",
            "html_url": "https://github.com/chrispiech/DeepKnowledgeTracing",
            "description": "source code for the paper Deep Knowledge Tracing",
            "language": "Lua",
            "stargazers_count": 313,
            "pushed_at": "2015-12-16T00:00:00Z",
            "archived": True,
        },
        {"full_name": "", "html_url": ""},  # 缺字段 → 丢掉
    ],
}


def test_parse_maps_repo_fields() -> None:
    cards = github_parse(PAYLOAD, limit=5)
    assert [card.title for card in cards] == [
        "pykt-team/pykt-toolkit",
        "chrispiech/DeepKnowledgeTracing",
    ]
    top = cards[0]
    assert top.source_type == "github"
    assert top.url == "https://github.com/pykt-team/pykt-toolkit"
    assert top.year == 2026  # 用"最近推送"年份，而不是建仓年份
    assert top.venue == "Python"
    assert top.external_id == "pykt-team/pykt-toolkit"
    assert top.extra["stars"] == "441"
    assert top.extra["archived"] == "false"
    # 归档仓库照样收，但状态要能看见（判权与路线安排需要知道"这仓库不维护了"）
    assert cards[1].extra["archived"] == "true"


def test_search_sends_version_header_and_stars_sort() -> None:
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["url"] = str(request.url)
        sent["accept"] = request.headers.get("accept")
        sent["version"] = request.headers.get("x-github-api-version")
        sent["auth"] = request.headers.get("authorization")
        sent["method"] = request.method
        return httpx.Response(200, json=PAYLOAD)

    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    cards = GithubProvider(http).search(Query(text="knowledge tracing", limit=3))
    assert sent["method"] == "GET"
    url = str(sent["url"])
    assert "q=knowledge+tracing" in url
    assert "sort=stars" in url and "per_page=3" in url
    assert sent["version"] == "2022-11-28"
    assert sent["accept"] == "application/vnd.github+json"
    assert sent["auth"] is None, "没配 token 时不该发 Authorization 头"
    assert len(cards) == 2


def test_token_is_sent_when_configured_and_lowers_the_interval() -> None:
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=PAYLOAD)

    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    authed = GithubProvider(http, token="ghp_test")
    authed.search(Query(text="x", limit=1))
    assert sent["auth"] == "Bearer ghp_test"

    # 额度：未认证 10 次/分钟 → 6s；认证 30 次/分钟 → 2s
    assert GithubProvider(http).min_interval_s == MIN_INTERVAL_S == 6.0
    assert authed.min_interval_s == MIN_INTERVAL_WITH_TOKEN_S == 2.0
    assert GithubProvider(http).authenticated is False
    assert authed.authenticated is True


def test_parse_handles_non_dict_payload() -> None:
    assert github_parse({}, limit=3) == []
    assert github_parse({"items": ["junk"]}, limit=3) == []


def test_interval_is_not_overridden_by_the_global_table() -> None:
    """全局限速表**不能**覆盖 github 的间隔。

    真机踩到过：表里写了 "github": 1.0，而 research_search 是"表优先于 provider"，
    于是把 6s 覆盖成 1s —— 未认证 Search API 只有 10 次/分钟，1s 间隔会把额度打光，
    表现为间歇 403（与 arXiv 会被 406 限速同类）。所以表里故意不写 github，
    fallback 走 provider 自己算出来的值。
    """
    from scholarscout.providers.settings import SearchSettings

    settings = SearchSettings()
    assert "github" not in settings.min_interval_s, "表里不许再有 github（会覆盖动态间隔）"
    # 表里查不到 → 用 provider 的值
    assert settings.interval_for("github", MIN_INTERVAL_S) == MIN_INTERVAL_S == 6.0
    assert settings.interval_for("github", MIN_INTERVAL_WITH_TOKEN_S) == 2.0


def test_github_is_not_cached_between_calls() -> None:
    """检索是时点查询：同一个查询打两次必须走两次网络。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.dumps({"q": "x"}))
        return httpx.Response(200, json=PAYLOAD)

    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    provider = GithubProvider(http, min_interval_s=0.0)
    provider.search(Query(text="x", limit=2))
    provider.search(Query(text="x", limit=2))
    assert len(seen) == 2
