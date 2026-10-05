"""``research.search`` 契约：多来源聚合、失败不阻塞、先入库再返回。

用 httpx.MockTransport 打桩 arXiv 响应，全离线。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from scholarscout.fetch.http import HttpClient
from scholarscout.rpc.methods import AppContext
from scholarscout.runtime import registry
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state.search import search_summaries
from scholarscout.tools import register_tools
from scholarscout.tools.research_search import PARAMETERS, SPEC, research_search

from ..conftest import reply  # noqa: F401  （保持与本目录其它用例一致的导入风格）

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "arxiv_sample.xml"


def _tool_ctx(ctx: AppContext, handler: Callable[[httpx.Request], httpx.Response]) -> ToolContext:
    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    return ToolContext(db=ctx.db, config=ctx.config, http=http, llm=ctx.llm)


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_tools()


def test_spec_shape() -> None:
    assert SPEC.name == "research.search"
    assert SPEC.toolset == "research"
    assert PARAMETERS["additionalProperties"] is False
    assert PARAMETERS["required"] == ["query"]
    assert registry.get_spec("research.search") is not None


def test_search_ingests_sources_and_returns_ids(ctx: AppContext) -> None:
    xml = FIXTURE.read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert "export.arxiv.org" in str(request.url)
        assert "Accept" in request.headers
        return httpx.Response(200, text=xml, headers={"content-type": "application/atom+xml"})

    result = research_search(_tool_ctx(ctx, handler), "education agent", limit=2)
    assert result["ok"] is True
    items = result["data"]["items"]
    assert len(items) == 2
    assert all(item["source_id"].startswith("src_") for item in items)
    assert result["refs"]["source_ids"] == [item["source_id"] for item in items]
    # 先入库再返回：库里的条数必须与返回一致
    assert ctx.db.scalar_int("SELECT count(*) FROM sources") == 2
    assert result["data"]["provider_status"] == [
        {"provider": "arxiv", "ok": True, "n": 2, "interval_s": 3.0}
    ]


def test_search_reports_unknown_provider_as_not_implemented(ctx: AppContext) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
        raise AssertionError("未实现的来源不应发起请求")

    result = research_search(_tool_ctx(ctx, handler), "x", sources=["openalex"])
    status = result["data"]["provider_status"]
    assert status == [{"provider": "openalex", "ok": False, "n": 0, "error": "not_implemented"}]
    assert result["data"]["items"] == []
    assert result["refs"]["source_ids"] == []


def test_search_reports_missing_api_key_for_commercial_source(ctx: AppContext, monkeypatch) -> None:
    """商用来源缺 key：必须如实报 missing_api_key。

    "没搜到"与"这层没配"是两件事 —— 静默跳过会让模型以为互联网上没这份资料，
    然后开始拿检索到的旧结果硬凑（比报错更坏）。
    """
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
        raise AssertionError("缺 key 的商用来源不应发起请求")

    result = research_search(_tool_ctx(ctx, handler), "x", sources=["tavily", "serper", "brave"])
    status = {row["provider"]: row["error"] for row in result["data"]["provider_status"]}
    assert status["tavily"] == "missing_api_key: TAVILY_API_KEY（写在 .env 里）"
    assert status["serper"] == "missing_api_key: SERPER_API_KEY（写在 .env 里）"
    assert status["brave"] == "missing_api_key: BRAVE_API_KEY（写在 .env 里）"


def test_search_reports_searxng_missing_config(ctx: AppContext) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("未配置的 searxng 不应发起请求")

    result = research_search(_tool_ctx(ctx, handler), "x", sources=["searxng"])
    error = result["data"]["provider_status"][0]["error"]
    assert error.startswith("missing_config: search.searxng_base_url")


def test_search_rejects_typo_provider_name(ctx: AppContext) -> None:
    """写错来源名必须报 unknown_provider，而不是含糊的 not_implemented。"""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("不存在的来源不应发起请求")

    result = research_search(_tool_ctx(ctx, handler), "x", sources=["bingg"])
    assert result["data"]["provider_status"][0]["error"] == "unknown_provider"


def test_default_providers_is_auto(ctx: AppContext) -> None:
    """默认 ``auto``：能用哪个用哪个，而不是写死一堆用不了的来源。"""
    from scholarscout.providers.settings import AUTO, DEFAULT_PROVIDERS

    assert DEFAULT_PROVIDERS == (AUTO,)


def test_auto_only_includes_usable_providers(monkeypatch) -> None:
    """auto 的纪律：**只带现在真能用的**。

    - 没配 key / 没配实例 → 只剩 arxiv
    - 配了 Tavily key → 多带一个
    - 配了 SearxNG 实例 → 带 searxng
    - **bing / duckduckgo 永不进 auto**：一个 robots 禁、一个要梯子，
      带上就会让每次检索都去撞墙（超时/被拒）
    """
    from scholarscout.providers.settings import SearchSettings

    for env in ("TAVILY_API_KEY", "SERPER_API_KEY", "BRAVE_API_KEY", "GOOGLE_CSE_KEY"):
        monkeypatch.delenv(env, raising=False)

    assert SearchSettings().auto_names() == ("arxiv",)

    monkeypatch.setenv("TAVILY_API_KEY", "k")
    assert SearchSettings().auto_names() == ("arxiv", "tavily")
    assert "bing" not in SearchSettings().auto_names()
    assert "duckduckgo" not in SearchSettings().auto_names()

    configured = SearchSettings(searxng_base_url="https://searx.mine.org")
    assert configured.auto_names(key_lookup=lambda _name: None) == ("arxiv", "searxng")

    # google_cse 要 key + cx 两样齐全才进 auto
    monkeypatch.setenv("GOOGLE_CSE_KEY", "k")
    assert "google_cse" not in SearchSettings().auto_names()
    with_cx = SearchSettings(google_cse_cx="abc123")
    assert "google_cse" in with_cx.auto_names()


def test_bing_blocked_by_robots_until_switch_is_on(ctx: AppContext) -> None:
    """抓搜索结果页默认关闭：报 blocked_by_robots，而不是偷偷抓。

    Bing 的 robots.txt 明确 Disable ``/search``；本项目的口径是"只豁免官方 API 主机，
    网页一律遵守"（docs/03 §7）。要抓必须由用户显式打开开关 —— 那是他的选择。
    """

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("开关关着就不该发请求")

    result = research_search(_tool_ctx(ctx, handler), "x", sources=["bing"])
    error = result["data"]["provider_status"][0]["error"]
    assert error.startswith("blocked_by_robots")
    assert "allow_searchpage_scraping" in error


def test_scraping_switch_registers_robots_exemption(tmp_path: Path) -> None:
    """打开开关 → bing 主机进 robots 豁免名单；关着 → 名单不动。"""
    from scholarscout.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text("search:\n  allow_searchpage_scraping: true\n", encoding="utf-8")
    opened = load_config(path, load_env=False)
    assert "cn.bing.com" in opened.fetch_cfg.robots_exempt_hosts

    path.write_text("search:\n  allow_searchpage_scraping: false\n", encoding="utf-8")
    closed = load_config(path, load_env=False)
    assert "cn.bing.com" not in closed.fetch_cfg.robots_exempt_hosts


def test_total_provider_failure_is_reported_as_failure(ctx: AppContext) -> None:
    """全部来源都失败：必须 ok=False 并带原因。

    只回 `items: []` 会让模型以为"这个方向没资料"，于是反复重试同一个查询直到烧光工具预算
    并开始编造（真机跑 E3 实测过）。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="arxiv is down")

    result = research_search(_tool_ctx(ctx, handler), "education agent")
    status = result["data"]["provider_status"][0]
    assert status["ok"] is False
    assert "server" in status["error"]
    assert result["data"]["items"] == []
    assert result["ok"] is False
    assert "所有检索来源都失败了" in result["error"]
    assert "arxiv" in result["error"]


def test_one_provider_failure_does_not_block_the_others(ctx: AppContext) -> None:
    """有来源成功时，空结果或部分失败都不算整体失败（失败不阻塞，docs/05 §3.3）。"""
    xml = FIXTURE.read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml, headers={"content-type": "application/atom+xml"})

    result = research_search(
        _tool_ctx(ctx, handler), "education agent", sources=["arxiv", "openalex"]
    )
    assert result["ok"] is True
    assert len(result["data"]["items"]) >= 1
    kinds = {entry["provider"]: entry["ok"] for entry in result["data"]["provider_status"]}
    assert kinds == {"arxiv": True, "openalex": False}


def test_duplicate_sources_are_not_inserted_twice(ctx: AppContext) -> None:
    xml = FIXTURE.read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml, headers={"content-type": "application/atom+xml"})

    tool_ctx = _tool_ctx(ctx, handler)
    first = research_search(tool_ctx, "education agent", limit=2)
    second = research_search(tool_ctx, "education agent", limit=2)
    assert ctx.db.scalar_int("SELECT count(*) FROM sources") == 2
    assert first["refs"]["source_ids"] == second["refs"]["source_ids"]


def test_year_filter_drops_older_items(ctx: AppContext) -> None:
    xml = FIXTURE.read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml, headers={"content-type": "application/atom+xml"})

    result = research_search(_tool_ctx(ctx, handler), "education agent", year_from=2502)
    assert [
        item["year"] for item in result["data"]["items"]
    ] == []  # 年份是 4 位数，示例数据全被滤掉


def test_limit_is_respected(ctx: AppContext) -> None:
    xml = FIXTURE.read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml, headers={"content-type": "application/atom+xml"})

    result = research_search(_tool_ctx(ctx, handler), "education agent", limit=1)
    assert len(result["data"]["items"]) == 1


def test_search_result_carries_snippet_but_not_summary_yet(ctx: AppContext) -> None:
    """检索阶段不生成摘要（摘要属于 source.fetch）—— 库里 summaries 必须还是空的。"""
    xml = FIXTURE.read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml, headers={"content-type": "application/atom+xml"})

    research_search(_tool_ctx(ctx, handler), "education agent")
    assert ctx.db.scalar_int("SELECT count(*) FROM summaries") == 0
    assert search_summaries(ctx.db, "调查") == []
