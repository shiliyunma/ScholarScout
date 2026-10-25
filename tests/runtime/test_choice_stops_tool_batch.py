"""缺陷 3：给出选择卡时**必须**终止本轮剩余的工具调用。

用户报的现象：模型回了选择卡（7 个选项，含「先不检索」），同一轮里却已经开始检索 ——
用户还没点，搜索已经发生。

定位（见 ``tools/research_search.py::_choice_event`` 与 ``runtime/loop.py``）：

- 选择卡**不是**模型回的字段，而是 ``research.search`` 在「用户没说清要哪类资料」时
  **自己**推 ``chat.needs_choice`` 事件、并回一个信封顶层带 ``needs_choice=True`` 的结果；
- 同一 reply 里的 ``tool_calls`` 在 ``run_turn`` 里是**按顺序全跑完**的：执行完返回选择请求的
  那次调用后，循环既不看 ``needs_choice``、也不 break —— 后面的调用照跑，模型还会再拿一轮
  工具继续搜（``research.delegate`` 里的子循环不受本轮的意图闸限制，真会出网）；
- 所以修法是「遇选择即终止本轮剩余工具调用」：本轮以等待用户输入收尾，已执行的不回滚。

这里钉八件事（五条主路径 + 两条边界 + 一条零出网）：

| 用例 | 期望 |
|---|---|
| 选择请求 + 第二个工具 | 第二个**一次都不执行**（无 trace / 无 tool_call 事件），只发一张卡 |
| 只有工具、没有选择 | 两个都正常执行（不许把正常一轮弄坏） |
| 只有选择、没有别的工具 | 正常发卡、正常收尾 |
| 选择夹在中间（A → 选择 → B） | A 执行、B 不执行 |
| 点选后的下一轮 | 能正常检索（选择作为下一条用户消息） |
| 边界：选择请求但选项为空 | **不停**（没有可点的东西，停了就是死等） |
| 边界：同一 reply 两次选择请求 | 只执行第一次、只发一张卡 |
| 低置信的检索请求 | **一个出网请求都不发**：HTTP 传输层计数为 0（工具层「先问后搜」的硬证据） |
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
from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state import sessions
from scholarscout.tools import register_tools
from scholarscout.tools import research_search as search_tool

from ..conftest import StubLlm, reply, tool_call

#: 分类器的输出（走 ``search_intent.classify``，是**无工具**的一次极小调用）
LOW_CONFIDENCE = '{"type": "other", "confidence": 0.2}'
#: 泛指句：``looks_ambiguous`` 认「资料」→ 值得花一次分类调用 → 低置信 → 出选择卡
AMBIGUOUS_TEXT = "帮我找点资料"
#: 明说偏好：``_EXPLICIT_RULES`` 直接定 paper，不调分类器（也就不会再出选择卡）
EXPLICIT_TEXT = "只看论文，教育智能体怎么入门"

ECHO_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    """注册表与分类缓存都是进程级的：不清会把上一条的判定/工具串到这一条。"""
    registry.clear_registry()
    si.clear_cache()
    yield
    registry.clear_registry()
    si.clear_cache()


def _arm(ctx: AppContext) -> AppContext:
    """打开意图分类（conftest 默认关着，因为它会多吃一条假模型回复），并装上不出网的 HTTP。

    HTTP 一定要有：``research.search`` 走到扇出之前会 ``require_http()``，没装配就整条回失败
    —— 那样「有没有执行检索」这件事就看不出来了（扇出被换成桩，一个请求都不该发出去）。
    """

    def _no_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"这条用例不该出网：{request.url}")

    return replace(
        ctx,
        http=HttpClient(respect_robots=False, transport=httpx.MockTransport(_no_network)),
        config=replace(
            ctx.config, runtime=replace(ctx.config.runtime, classify_search_intent=True)
        ),
    )


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


def _stub_fetch(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    """把检索扇出换成「记一笔、回空表」——不出网，但**能看出有没有被执行**。"""

    def fake(*args: Any, **kwargs: Any) -> list[Any]:
        del args
        recorder.fetched.append(str(kwargs.get("query")))
        return []

    monkeypatch.setattr(search_tool, "fetch_many", fake)


def _register_echo(handler: Any) -> ToolSpec:
    return registry.register(
        ToolSpec(
            name="research.echo",
            description="回显查询",
            parameters=ECHO_SCHEMA,
            handler=handler,
            toolset="research",
        )
    )


def _search(query: str, call_id: str) -> Any:
    return tool_call(
        "research.search", json.dumps({"query": query}, ensure_ascii=False), call_id=call_id
    )


# ---------------------------------------------------------------- 主路径 1（本次缺陷）


def test_choice_request_stops_the_rest_of_the_tool_batch(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一 reply 里「选择请求 + 另一个检索」→ 另一个**一次都不执行**，只发一张卡。"""
    register_tools()
    recorder = _Recorder()
    _stub_fetch(monkeypatch, recorder)
    arm = _arm(ctx)
    session_id = _session(arm)
    arm.llm = StubLlm(
        reply(LOW_CONFIDENCE),  # 分类器：低置信 → 本轮该问用户
        reply(
            "",
            tool_calls=[
                _search("帮我找点资料", "call_1"),
                _search("教育智能体综述", "call_2"),
            ],
        ),
        reply("我先按你想看的方向整理。"),  # 修好之后这一条**不该被用到**
    )

    result = loop.run_turn(
        arm.turn_context(), session_id=session_id, text=AMBIGUOUS_TEXT, emit=recorder.emit
    )

    assert recorder.count("chat.needs_choice") == 1, "选择卡必须发出去（且只发一张）"
    assert recorder.count("chat.tool_call") == 1, "排在选择请求后面的调用不许被执行"
    assert ctx.db.scalar_int("SELECT count(*) FROM tool_traces") == 1
    assert recorder.fetched == [], "一次检索扇出都不许发生"
    assert result.tool_calls == ["research.search"]
    assert result.stop_reason == "completed", "等用户输入是**正常**收尾，不是错误/上限"


# ---------------------------------------------------------------- 主路径 2（不许弄坏）


def test_tools_without_a_choice_still_all_run(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只有工具、没有选择请求 → 两个都照跑（修法不能把正常一轮变成「只跑第一个」）。"""
    register_tools()
    recorder = _Recorder()
    _stub_fetch(monkeypatch, recorder)
    arm = _arm(ctx)
    session_id = _session(arm)
    arm.llm = StubLlm(
        reply(
            "",
            tool_calls=[
                _search("教育智能体综述", "call_1"),
                _search("知识追踪模型评测", "call_2"),
            ],
        ),
        reply("两路都查过了，结论是……"),
    )

    result = loop.run_turn(
        arm.turn_context(), session_id=session_id, text=EXPLICIT_TEXT, emit=recorder.emit
    )

    assert recorder.count("chat.needs_choice") == 0
    assert recorder.count("chat.tool_call") == 2
    assert ctx.db.scalar_int("SELECT count(*) FROM tool_traces") == 2
    assert len(recorder.fetched) == 2, "明说了偏好就该照常扇出两次"
    assert result.tool_calls == ["research.search", "research.search"]
    assert result.stop_reason == "completed"
    assert result.text == "两路都查过了，结论是……"


# ---------------------------------------------------------------- 主路径 3


def test_choice_with_no_other_tools_emits_the_card(ctx: AppContext) -> None:
    """只有选择请求 → 正常发卡、正常收尾，且不再多花一轮模型调用。"""
    register_tools()
    recorder = _Recorder()
    arm = _arm(ctx)
    session_id = _session(arm)
    stub = StubLlm(
        reply(LOW_CONFIDENCE),
        reply("", tool_calls=[_search("帮我找点资料", "call_1")]),
    )
    arm.llm = stub

    result = loop.run_turn(
        arm.turn_context(), session_id=session_id, text=AMBIGUOUS_TEXT, emit=recorder.emit
    )

    assert recorder.count("chat.needs_choice") == 1
    assert len(stub.calls) == 2, "出卡即收尾：不该再为了「把问题说出来」多调一次模型"
    assert result.stop_reason == "completed"
    assert recorder.kinds()[-1] == "chat.done"
    assert recorder.events[-1][1]["stop_reason"] == "completed"
    _kind, payload = next(item for item in recorder.events if item[0] == "chat.needs_choice")
    assert len(payload["options"]) == 7
    assert payload["query"] == "帮我找点资料"


# ---------------------------------------------------------------- 主路径 4（夹在中间）


def test_choice_in_the_middle_keeps_the_earlier_tool_and_drops_the_later_one(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A → 选择 → B：A 已经执行完（不回滚），B 一次都不执行。"""
    register_tools()
    ran: list[str] = []

    def echo(ctx_arg: Any, query: str) -> dict[str, Any]:
        del ctx_arg
        ran.append(query)
        return {"ok": True, "data": {"query": query}, "refs": {"source_ids": []}}

    _register_echo(echo)
    recorder = _Recorder()
    _stub_fetch(monkeypatch, recorder)
    arm = _arm(ctx)
    session_id = _session(arm)
    arm.llm = StubLlm(
        reply(LOW_CONFIDENCE),
        reply(
            "",
            tool_calls=[
                tool_call("research.echo", '{"query":"A"}', call_id="call_a"),
                _search("帮我找点资料", "call_b"),
                tool_call("research.echo", '{"query":"B"}', call_id="call_c"),
            ],
        ),
    )

    result = loop.run_turn(
        arm.turn_context(), session_id=session_id, text=AMBIGUOUS_TEXT, emit=recorder.emit
    )

    assert ran == ["A"], "A 已经执行完（不回滚），B 必须一次都不执行"
    assert result.tool_calls == ["research.echo", "research.search"]
    assert ctx.db.scalar_int("SELECT count(*) FROM tool_traces") == 2
    assert recorder.count("chat.needs_choice") == 1
    assert result.stop_reason == "completed"
    names = [row["tool_name"] for row in ctx.db.query("SELECT tool_name FROM tool_traces")]
    assert names == ["research.echo", "research.search"], "B 连长名字都不该出现在轨迹里"


# ---------------------------------------------------------------- 主路径 5（点选后下一轮）


def test_next_turn_after_the_selection_searches_normally(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户点选 = 下一条用户消息；下一轮必须能正常检索（修复不能把路堵死）。"""
    register_tools()
    recorder = _Recorder()
    _stub_fetch(monkeypatch, recorder)
    arm = _arm(ctx)
    session_id = _session(arm)

    arm.llm = StubLlm(
        reply(LOW_CONFIDENCE),
        reply("", tool_calls=[_search("帮我找点资料", "call_1")]),
    )
    first = loop.run_turn(
        arm.turn_context(), session_id=session_id, text=AMBIGUOUS_TEXT, emit=recorder.emit
    )
    assert first.stop_reason == "completed"
    assert recorder.fetched == []

    arm.llm = StubLlm(
        reply("", tool_calls=[_search("教育智能体综述", "call_2")]),
        reply("按论文方向，这几篇可以先看。"),
    )
    second = loop.run_turn(
        arm.turn_context(), session_id=session_id, text="只看论文", emit=recorder.emit
    )

    assert recorder.count("chat.needs_choice") == 1, "点选之后不该再问一遍（只该有第一轮那张卡）"
    assert recorder.fetched == ["教育智能体综述"], "下一轮必须真的去检索"
    assert second.tool_calls == ["research.search"]
    assert second.stop_reason == "completed"
    assert second.text == "按论文方向，这几篇可以先看。"


# ---------------------------------------------------------------- 边界 1：选项为空


def test_choice_without_options_does_not_stall_the_turn(ctx: AppContext) -> None:
    """``needs_choice`` 但选项是空表 → 界面渲染不出卡（没人可点），**不能停**。

    停了就是让一轮既没有卡可点、也不再干活 —— 用户看到的是「什么都没发生」。
    """
    ran: list[str] = []

    def handler(ctx_arg: Any, query: str) -> dict[str, Any]:
        del ctx_arg
        ran.append(query)
        if query == "empty":
            return {
                "ok": False,
                "error": "需要用户决定，但这次没给出选项",
                "needs_choice": True,
                "options": [],
            }
        return {"ok": True, "data": {"query": query}, "refs": {"source_ids": []}}

    _register_echo(handler)
    arm = _arm(ctx)
    session_id = _session(arm)
    arm.llm = StubLlm(
        reply(LOW_CONFIDENCE),
        reply(
            "",
            tool_calls=[
                tool_call("research.echo", '{"query":"empty"}', call_id="call_1"),
                tool_call("research.echo", '{"query":"after"}', call_id="call_2"),
            ],
        ),
        reply("没有可选项，我接着往下做。"),
    )

    result = loop.run_turn(arm.turn_context(), session_id=session_id, text=AMBIGUOUS_TEXT)

    assert ran == ["empty", "after"], "空选项的选择请求不该终止本轮"
    assert result.stop_reason == "completed"
    assert result.text == "没有可选项，我接着往下做。"


# ---------------------------------------------------------------- 边界 2：重复选择请求


def test_repeated_choice_requests_execute_once_and_emit_one_card(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一 reply 里两次都命中选择 → 只执行第一次、只发一张卡（不是两张、也不是零张）。"""
    register_tools()
    recorder = _Recorder()
    _stub_fetch(monkeypatch, recorder)
    arm = _arm(ctx)
    session_id = _session(arm)
    arm.llm = StubLlm(
        reply(LOW_CONFIDENCE),
        reply(
            "",
            tool_calls=[
                _search("帮我找点资料", "call_1"),
                _search("帮我找找资料吧", "call_2"),
            ],
        ),
    )

    result = loop.run_turn(
        arm.turn_context(), session_id=session_id, text=AMBIGUOUS_TEXT, emit=recorder.emit
    )

    assert recorder.count("chat.needs_choice") == 1
    assert ctx.db.scalar_int("SELECT count(*) FROM tool_traces") == 1
    assert recorder.count("chat.tool_call") == 1
    assert recorder.fetched == []
    assert result.stop_reason == "completed"


# ---------------------------------------------------------------- 落库形态（回放不残缺）


def test_skipped_calls_still_leave_no_dangling_tool_call(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """被跳过的调用**不写轨迹**，但 assistant 那条 tool_calls 不能留下「有请求没结果」的悬空。

    口径与「会话级上限」那条一致：未执行的调用补一条**如实**的 tool 回复（不进库、不进模型
    的下一轮——因为本轮到此为止），已执行的照常落库。
    """
    register_tools()
    recorder = _Recorder()
    _stub_fetch(monkeypatch, recorder)
    arm = _arm(ctx)
    session_id = _session(arm)
    stub = StubLlm(
        reply(LOW_CONFIDENCE),
        reply(
            "",
            tool_calls=[
                _search("帮我找点资料", "call_1"),
                _search("教育智能体综述", "call_2"),
            ],
        ),
    )
    arm.llm = stub

    loop.run_turn(
        arm.turn_context(), session_id=session_id, text=AMBIGUOUS_TEXT, emit=recorder.emit
    )

    assistant = ctx.db.query_one("SELECT tool_calls_json FROM messages WHERE role='assistant'")
    assert assistant is not None and assistant["tool_calls_json"], "工具请求本身要落库（回放要看）"
    assert "call_2" in str(assistant["tool_calls_json"])
    assert ctx.db.scalar_int("SELECT count(*) FROM messages WHERE role='tool'") == 1, (
        "只有真的执行过的那次才有 tool 消息"
    )
    assert len(stub.calls) == 2, "出卡即收尾：不再拿没执行的结果去喂模型"


def test_chat_done_carries_the_authoritative_text(ctx: AppContext) -> None:
    """收尾事件仍带 ``text`` 与 ``message_id``（界面按它落定，不靠猜）。"""
    register_tools()
    recorder = _Recorder()
    arm = _arm(ctx)
    session_id = _session(arm)
    arm.llm = StubLlm(
        reply(LOW_CONFIDENCE),
        reply("我先确认一下你要哪类资料。", tool_calls=[_search("帮我找点资料", "call_1")]),
    )

    result = loop.run_turn(
        arm.turn_context(), session_id=session_id, text=AMBIGUOUS_TEXT, emit=recorder.emit
    )

    _kind, done = recorder.events[-1]
    assert done["stop_reason"] == "completed"
    assert done["text"] == "我先确认一下你要哪类资料。"
    assert done["message_id"] == result.message_id
    assert isinstance(done["message_id"], str) and done["message_id"]


# ---------------------------------------------------------------- 零出网（工具层「先问后搜」）


def test_low_confidence_choice_makes_zero_network_requests(ctx: AppContext) -> None:
    """低置信 → 先问方向：**一个出网请求都不发**。

    与上面几条不同：这里**不把 ``fetch_many`` 换成桩**，而是给一个会数请求的 HTTP 传输层
    （``httpx.MockTransport``）。所以 ``urls == []`` 证明的是「一个 provider 都没被调用」，
    而不是「桩没被调用」—— 这才是用户报的「卡还没点、搜索已经发生」的直接反证。
    高置信照常搜由 ``test_tools_without_a_choice_still_all_run``（扇出两次）与
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
