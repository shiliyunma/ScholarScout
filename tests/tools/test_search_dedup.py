"""A3 判重闸门：同一会话里"换词重搜"必须被拦下，并告诉它该往哪走。

真机证据（`scripts/diagnose_turns.py`，63 条工具轨迹）：3 个多轮会话里 2 个出现重复查询，
其中"新调研"那个第 2 轮一个新来源都没发现 —— 换词重搜花了钱、没带来任何新证据。

**写者只有一个**：工具只**读**台账判重，记账由 `runtime/loop.py` 做。
两个写者会丢更新（真机抓到：工具写进去的查询被循环轮末整表覆盖，第 2 轮照样重复搜）。
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


def _seed_query(ctx: AppContext, query: str) -> None:
    """模拟循环记下的历史查询（工具自己不会写）。"""
    state = rs.load_state(ctx.db, SESSION) or rs.empty_state("目标")
    rs.record_query(state, query)
    rs.save_state(ctx.db, SESSION, state)


def test_tool_does_not_write_the_ledger(ctx: AppContext) -> None:
    """工具是**只读者**：记账归循环。写者多了必然丢更新。"""
    research_search(_tool_ctx(ctx, _ok_handler()), "education agent", limit=2)
    state = rs.load_state(ctx.db, SESSION)
    assert state is None or not state.get("used_queries")


def test_word_swapped_repeat_is_rejected_with_a_way_out(ctx: AppContext) -> None:
    handler = _ok_handler()
    _seed_query(ctx, "education agent")

    with pytest.raises(ValueError) as caught:
        research_search(_tool_ctx(ctx, handler), "education agent 相关研究", limit=2)

    message = str(caught.value)
    assert "高度重复" in message and "education agent" in message
    assert "换方向" in message  # 不能只说"不行"，要说"往哪走"


def test_different_angle_passes(ctx: AppContext) -> None:
    handler = _ok_handler()
    _seed_query(ctx, "education agent")
    result = research_search(_tool_ctx(ctx, handler), "graph retrieval benchmark", limit=2)
    assert result["ok"] is True


def test_no_session_means_no_gate(ctx: AppContext) -> None:
    """没有会话上下文（评测脚本/单测直接调工具）时不做判重 —— 判重是会话内的概念。"""
    handler = _ok_handler()
    first = research_search(_tool_ctx(ctx, handler, session_id=None), "education agent", limit=2)
    again = research_search(_tool_ctx(ctx, handler, session_id=None), "education agent", limit=2)
    assert first["ok"] is True and again["ok"] is True


def test_rejection_happens_before_any_network_call(ctx: AppContext) -> None:
    """判重必须在**发请求之前**拦下：拦下来比搜完再丢更省。"""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调用
        raise AssertionError("判重命中时不应发起检索请求")

    _seed_query(ctx, "education agent")
    with pytest.raises(ValueError):
        research_search(_tool_ctx(ctx, handler), "education agent 研究", limit=2)
