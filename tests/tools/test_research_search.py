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


def test_provider_failure_does_not_block_other_sources(ctx: AppContext) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="arxiv is down")

    result = research_search(_tool_ctx(ctx, handler), "education agent")
    status = result["data"]["provider_status"][0]
    assert status["ok"] is False
    assert "server" in status["error"]
    assert result["data"]["items"] == []


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
