"""工具层「先问后搜」的**零出网**回归网：低置信时一个 provider 都不许被调用。

这一支钉的是**行为已经成立**（产品零改动），不是修法。它存在的理由有两个：

1. 用户报的现象是「选择卡还没点，搜索已经发生」。把 ``fetch_many`` 换成桩能证明「扇出没执行」，
   但**证明不了没有出网** —— 桩替换发生在更后面，闸门要是被挪到 ``require_http()`` / provider
   装配之后，桩同样安静。所以这两条给的是会数请求的 ``httpx.MockTransport``：
   ``urls == []`` = 真的一个 HTTP 请求都没发。
2. 顺序同理：低置信时连 ``http`` 都没装配也必须先出选项（``http=None`` 一旦被闸门碰到，
   ``require_http()`` 会抛错，结果就从「问用户」退化成「检索失败」）。

| 用例 | 期望 |
|---|---|
| 低置信的检索请求（走整轮 ``run_turn``） | HTTP 传输层计数为 0，且只发一张 7 选项的卡 |
| 低置信直接打工具（``http=None``） | 仍是 ``needs_choice=True`` 而不是失败 —— 闸门在 ``require_http()`` 之前 |
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import httpx
import pytest

from scholarscout.fetch.http import HttpClient
from scholarscout.rpc.methods import AppContext
from scholarscout.runtime import loop, registry, research_state
from scholarscout.runtime import search_intent as si
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state import sessions
from scholarscout.tools import register_tools
from scholarscout.tools import research_search as search_tool

from ..conftest import StubLlm, reply, tool_call

#: 分类器的输出（走 ``search_intent.classify``，是**无工具**的一次极小调用）
LOW_CONFIDENCE = '{"type": "other", "confidence": 0.2}'
#: 泛指句：``looks_ambiguous`` 认「资料」→ 值得花一次分类调用 → 低置信 → 出选择卡
AMBIGUOUS_TEXT = "帮我找点资料"


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    """注册表与分类缓存都是进程级的：不清会把上一条的判定/工具串到这一条。"""
    registry.clear_registry()
    si.clear_cache()
    yield
    registry.clear_registry()
    si.clear_cache()


def _session(ctx: AppContext) -> str:
    return sessions.create_session(ctx.db, model=ctx.config.chat.model)


class _Recorder:
    """记事件，并数清**真的发起了几次检索扇出**（断言「没执行」要看这个）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.fetched: list[str] = []

    def emit(self, kind: str, payload: dict[str, Any]) -> None:
        self.events.append((kind, payload))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]

    def count(self, kind: str) -> int:
        return self.kinds().count(kind)


def _search(query: str, call_id: str) -> Any:
    return tool_call(
        "research.search", json.dumps({"query": query}, ensure_ascii=False), call_id=call_id
    )


def test_low_confidence_choice_makes_zero_network_requests(ctx: AppContext) -> None:
    """低置信 → 先问方向：**一个出网请求都不发**。

    与「把 ``fetch_many`` 换成桩」的用例不同：这里给的是一个会数请求的 HTTP 传输层
    （``httpx.MockTransport``）。所以 ``urls == []`` 证明的是「一个 provider 都没被调用」，
    而不是「桩没被调用」—— 这才是用户报的「卡还没点、搜索已经发生」的直接反证。
    高置信照常搜由 ``tests/runtime/test_choice_stops_tool_batch.py``（扇出两次）与
    ``tests/tools/test_search_intent_gate.py``（只发该族的真实 URL）盯着。
    """
    register_tools()
    recorder = _Recorder()
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={})

    arm = replace(
        ctx,
        http=HttpClient(respect_robots=False, transport=httpx.MockTransport(handler)),
        config=replace(
            ctx.config, runtime=replace(ctx.config.runtime, classify_search_intent=True)
        ),
    )
    session_id = _session(arm)
    arm.llm = StubLlm(
        reply(LOW_CONFIDENCE),
        reply("", tool_calls=[_search("帮我找点资料", "call_1")]),
    )

    result = loop.run_turn(
        arm.turn_context(), session_id=session_id, text=AMBIGUOUS_TEXT, emit=recorder.emit
    )

    assert urls == [], f"低置信时一个请求都不该发（实际发出：{urls}）"
    assert recorder.count("chat.needs_choice") == 1
    _kind, payload = next(item for item in recorder.events if item[0] == "chat.needs_choice")
    assert len(payload["options"]) == 7, "要问方向就必须把可点的选项一起给出去"
    assert result.stop_reason == "completed"


def test_search_tool_asks_before_touching_the_http_client(ctx: AppContext) -> None:
    """工具层的**顺序**证据：低置信时连 HTTP 都没装配，也必须先出选项。

    这条盯的不是文案而是次序 —— 闸门一旦被挪到 ``require_http()`` / provider 装配之后，
    ``http=None`` 就会让 ``require_http()`` 抛 ``LlmError``，结果变成「检索失败」而不是「问用户」，
    这里立刻红。把 ``fetch_many`` 换桩的用例看不见这件事（桩替换发生在更后面）。
    """
    register_tools()
    recorder = _Recorder()
    session_id = _session(ctx)
    turn_id = "trn_gate_order"
    state = research_state.empty_state("帮我找点资料")
    si.set_preference(
        state, preference=None, turn_id=turn_id, intent=si.Intent("other", 0.2, "model")
    )
    research_state.save_state(ctx.db, session_id, state)
    tool_ctx = ToolContext(
        db=ctx.db,
        config=replace(
            ctx.config, runtime=replace(ctx.config.runtime, classify_search_intent=True)
        ),
        http=None,  # 一个出网客户端都没有：闸门不许碰到它
        llm=ctx.llm,
        session_id=session_id,
        turn_id=turn_id,
        emit=recorder.emit,
    )

    result = search_tool.research_search(tool_ctx, "帮我找点资料", sources=["arxiv"], limit=2)

    assert result["ok"] is False
    assert result["needs_choice"] is True
    assert len(result["options"]) == 7
    assert result["data"]["items"] == []
    assert recorder.kinds() == ["chat.needs_choice"]
