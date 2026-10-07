"""A3 判重闸门：同一会话里"换词重搜"必须被拦下，并告诉它该往哪走。

真机证据（`scripts/diagnose_turns.py`，63 条工具轨迹）：3 个多轮会话里 2 个出现重复查询，
其中"新调研"那个第 2 轮一个新来源都没发现 —— 换词重搜花了钱、没带来任何新证据。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from scholarscout.fetch.http import HttpClient
from scholarscout.rpc.methods import AppContext
from scholarscout.runtime import research_state as rs
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.tools import register_tools
from scholarscout.tools.research_search import research_search

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "arxiv_sample.xml"
SESSION = "ses_dedup"


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_tools()


def _ok_handler() -> Callable[[httpx.Request], httpx.Response]:
    xml = FIXTURE.read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml, headers={"content-type": "application/atom+xml"})

    return handler


def _tool_ctx(
    ctx: AppContext,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    session_id: str | None = SESSION,
) -> ToolContext:
    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))
    return ToolContext(db=ctx.db, config=ctx.config, http=http, llm=ctx.llm, session_id=session_id)


def test_first_search_is_recorded(ctx: AppContext) -> None:
    research_search(_tool_ctx(ctx, _ok_handler()), "education agent", limit=2)
    state = rs.load_state(ctx.db, SESSION)
    assert state is not None
    assert state["used_queries"] == ["education agent"]


def test_word_swapped_repeat_is_rejected_with_a_way_out(ctx: AppContext) -> None:
    handler = _ok_handler()
    research_search(_tool_ctx(ctx, handler), "education agent", limit=2)

    with pytest.raises(ValueError) as caught:
        research_search(_tool_ctx(ctx, handler), "education agent 相关研究", limit=2)

    message = str(caught.value)
    assert "高度重复" in message and "education agent" in message
    assert "换方向" in message  # 不能只说"不行"，要说"往哪走"
    # 被拒绝的查询**不进台账**：否则模型换个说法就把这个方向永久锁死了
    assert rs.load_state(ctx.db, SESSION)["used_queries"] == ["education agent"]


def test_different_angle_passes_and_is_appended(ctx: AppContext) -> None:
    handler = _ok_handler()
    research_search(_tool_ctx(ctx, handler), "education agent", limit=2)
    research_search(_tool_ctx(ctx, handler), "graph retrieval benchmark", limit=2)
    assert rs.load_state(ctx.db, SESSION)["used_queries"] == [
        "education agent",
        "graph retrieval benchmark",
    ]


def test_failed_search_is_not_recorded(ctx: AppContext) -> None:
    """一次网络抖动不能把某个方向永久锁死 —— 只有搜成功才记账。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    result = research_search(_tool_ctx(ctx, handler), "education agent", limit=2)
    assert result["ok"] is False
    state = rs.load_state(ctx.db, SESSION)
    assert state is None or state.get("used_queries") in (None, [])


def test_no_session_means_no_gate(ctx: AppContext) -> None:
    """没有会话上下文（评测脚本/单测直接调工具）时不做判重 —— 判重是会话内的概念。"""
    handler = _ok_handler()
    first = research_search(_tool_ctx(ctx, handler, session_id=None), "education agent", limit=2)
    again = research_search(_tool_ctx(ctx, handler, session_id=None), "education agent", limit=2)
    assert first["ok"] is True and again["ok"] is True
