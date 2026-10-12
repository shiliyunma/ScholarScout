"""跨层接线：一轮**带工具**的真实路径（`serve()` → `run_turn` → 通知）必须真的产生流式增量。

为什么必须跨层（真机 bug，2026-10-12）：流式分支写成"只给 ``tools_for_round is None`` 的那轮开"，
而 `tools=None` 只在**工具预算用满**之后的强制收口轮才出现。模型通常 2–3 次工具调用就自己收口，
于是 `chat_stream` **一次都没被调用**：界面 100 秒里一个字都看不到、最后整段蹦出来，
而 `chat_stream` 的单测与 `loop` 内的事件断言全绿 —— 因为没有任何一个用例走过
"这一轮带着工具、模型回的是正文"这条**最常走**的路。

这个用例走 `serve()`（假 stdin/stdout 就是桌面桥那一层：`stdin` = 界面发的行，
`stdout` = `make_emitter` 写出的 JSON-RPC 通知），断言四件事：

1. 带工具的那一轮**也**走 ``chat_stream``，且 ``tools`` 原样带上（流式不能把工具丢掉）；
2. 第一处 ``chat.delta`` 出现在第一处 ``chat.tool_call`` **之前**（= 工具轮就已经在流）；
3. 工具调用没被流式吃掉（真的执行、结果回来了）；
4. 界面最后收到的正文 == 落库的最终正文（增量是累计值，收尾不重复、也不停在半句话）。
"""

from __future__ import annotations

import dataclasses
import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from scholarscout.providers.llm import ChatReply
from scholarscout.rpc.methods import AppContext
from scholarscout.rpc.server import serve
from scholarscout.runtime import registry
from scholarscout.runtime.registry import ToolSpec
from scholarscout.state import sessions

from ..conftest import reply, tool_call

ECHO_TOOL = "research.echo"
ECHO_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    registry.clear_registry()
    yield
    registry.clear_registry()


class _StreamingBridgeStub:
    """真机形状的桩模型：第一轮**带着工具**回正文+工具调用，第二轮流式收口。

    两轮都走 ``chat_stream``（修复后主循环一律流式），并如实记录每轮拿到的 ``tools``：
    若有人为了"能流式"而把工具从流式请求里摘掉，第 1 条断言立刻红。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None) -> ChatReply:
        # 只有轮次之外的小调用（记忆抽取等）会走到这里；主循环一律流式。
        self.calls.append({"tools": tools, "stream": False})
        return reply("")

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Any = None,
        on_delta: Any = None,
    ) -> ChatReply:
        self.calls.append({"tools": tools, "stream": True})
        if len([call for call in self.calls if call["stream"]]) == 1:
            # 工具轮：模型先出一小段前言（真机就是这样），再要工具
            if on_delta is not None:
                on_delta("我先查一下。")
            return reply(
                "我先查一下。",
                tool_calls=[tool_call(ECHO_TOOL, '{"query":"DKT"}')],
            )
        pieces = ["结论：", "DKT ", "是深度知识追踪。"]
        for piece in pieces:
            if on_delta is not None:
                on_delta(piece)
        return reply("".join(pieces))


class _Out:
    """假 stdout：一行一条 JSON-RPC，响应到了就放开事件。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.lines: list[str] = []
        self._event = threading.Event()

    def write(self, text: str) -> None:
        with self._lock:
            self.lines.append(text)
            if '"id"' in text:
                self._event.set()

    def flush(self) -> None:
        return None

    def wait(self, timeout: float = 20.0) -> list[dict[str, Any]]:
        self._event.wait(timeout)
        with self._lock:
            return [json.loads(line) for line in self.lines if line.strip()]


def _run_turn_over_the_bridge(ctx: AppContext, text: str) -> tuple[list[dict[str, Any]], dict]:
    """把一轮对话按桌面桥的方式跑一遍：一行请求进去，通知与响应从 stdout 出来。"""
    out = _Out()
    request = {"jsonrpc": "2.0", "id": 1, "method": "chat.send", "params": {"text": text}}
    session_id = sessions.create_session(ctx.db, model=ctx.config.chat.model)
    request["params"]["session_id"] = session_id
    threading.Thread(
        target=serve,
        kwargs={
            "ctx": ctx,
            "stdin": iter([json.dumps(request, ensure_ascii=False) + "\n"]),
            "stdout": out,
        },
        daemon=True,
    ).start()

    messages = out.wait()
    response = next(item for item in messages if item.get("id") == 1)
    notifications = [item for item in messages if item.get("id") is None]
    return notifications, response["result"]


def test_tool_calling_turn_streams_over_the_bridge(config: Any, db: Any) -> None:
    def handler(handler_ctx: Any, query: str) -> dict[str, Any]:
        del handler_ctx
        return {"ok": True, "data": {"query": query}, "refs": {"source_ids": ["src_1"]}}

    registry.register(
        ToolSpec(
            name=ECHO_TOOL,
            description="回显查询",
            parameters=ECHO_SCHEMA,
            handler=handler,
            toolset="research",
        )
    )
    runtime = dataclasses.replace(config.runtime, stream=True)
    ctx = AppContext(config=dataclasses.replace(config, runtime=runtime), db=db)
    stub = _StreamingBridgeStub()
    ctx.llm = stub

    notifications, result = _run_turn_over_the_bridge(ctx, "帮我查一下 DKT")
    methods = [item["method"] for item in notifications]

    # ① 每一轮（含带工具那一轮）都走流式，且工具声明原样带上
    stream_calls = [call for call in stub.calls if call["stream"]]
    assert len(stream_calls) >= 2, "工具轮 + 收口轮都该是流式调用"
    assert all(call["tools"] for call in stream_calls), (
        "流式轮也必须带上工具声明 —— 为了开流而摘掉工具，模型就只能空口回答"
    )
    assert not any(not call["stream"] and call["tools"] for call in stub.calls), (
        "主循环不许为了绕开流式回退到一次性 chat"
    )

    # ② 第一处 chat.delta 必须在第一处 chat.tool_call **之前**（工具轮就已经在流）
    assert "chat.delta" in methods, "整轮一条流式增量都没有 = 接线断了"
    first_delta = methods.index("chat.delta")
    assert "chat.tool_call" in methods, "流式轮不能把工具调用吃掉"
    assert first_delta < methods.index("chat.tool_call"), (
        "delta 出现在工具调用之后 = 只有收尾那一条（旧的 bug 形状）"
    )

    # ③ 工具真的执行了、结果回来了
    assert result["tool_calls"] == [ECHO_TOOL]

    # ④ 界面最后收到的正文 == 结果里的最终正文（增量是累计值，收尾不乱补）
    deltas = [item["params"] for item in notifications if item["method"] == "chat.delta"]
    assert deltas[-1]["text"] == result["text"] == "结论：DKT 是深度知识追踪。"
    assert [item["seq"] for item in deltas] == sorted(item["seq"] for item in deltas), (
        "seq 必须单调不减（前端靠它认『同一轮的后续增量』）"
    )
