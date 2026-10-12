"""流式客户端 ``chat_stream`` 的契约：桩 SSE（httpx 假传输），**不联网、不花钱**。

钉住五件事（都是"看着能跑、其实没做到"最容易漏的）：
1. 增量**分段到达且顺序正确**（不是攒到最后一次性吐出来）；
2. ``on_delta`` 拼接起来 == 非流式的完整正文（两种路径口径一致）；
3. **中途断开**时已收到的部分**保留**在 ``LlmError.partial`` 上、真实原因如实报出（不静默吐半句）；
4. **非流式路径一字不变**：``chat()`` 仍发 ``stream=False``、不带 ``stream_options``；
5. **带工具的一轮也流式**：``tools`` 原样进请求体，工具调用按 ``index`` 拼装回完整 ``tool_calls``
   （参数 JSON 不进正文增量）。

另外钉住计费口径：服务端不回 ``usage`` 时**如实留空**，不编数字。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from scholarscout.providers.llm import LlmError, OpenAIChatClient

BASE = "https://example.test/v1"
MESSAGES = [{"role": "user", "content": "你好"}]


def _sse_body(pieces: list[str], *, include_usage: bool = True, finish: str = "stop") -> bytes:
    """拼一段 OpenAI 兼容的 SSE：每个片段一个 chunk，末尾 [DONE]。"""
    out: list[str] = []
    for piece in pieces:
        event = {"choices": [{"delta": {"content": piece}, "finish_reason": None}]}
        out.append("data: " + json.dumps(event, ensure_ascii=False) + "\n\n")
    if include_usage:
        out.append(
            "data: "
            + json.dumps({"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 3}})
            + "\n\n"
        )
    out.append(
        "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}) + "\n\n"
    )
    out.append("data: [DONE]\n\n")
    return "".join(out).encode("utf-8")


def _stream_client(body: bytes) -> OpenAIChatClient:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    return OpenAIChatClient(
        base_url=BASE, api_key="k", model="m", transport=httpx.MockTransport(handler)
    )


def test_deltas_arrive_in_order_and_join_to_the_full_text() -> None:
    client = _stream_client(_sse_body(["你", "好", "，", "世界"]))
    seen: list[str] = []

    reply = client.chat_stream(MESSAGES, on_delta=seen.append)

    assert seen == ["你", "好", "，", "世界"], "增量必须按到达顺序逐段回调，不能攒到最后一次性给"
    assert "".join(seen) == reply.content == "你好，世界"


def test_streamed_text_equals_the_non_streaming_full_text() -> None:
    """同内容的两种路径必须给出同一份正文（否则"流式只是显示不同"就不成立）。"""

    def json_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "你好，世界"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 9, "completion_tokens": 3},
            },
        )

    plain = OpenAIChatClient(
        base_url=BASE, api_key="k", model="m", transport=httpx.MockTransport(json_handler)
    )
    streamed = _stream_client(_sse_body(["你好", "，", "世界"]))

    one = plain.chat(MESSAGES)
    seen: list[str] = []
    two = streamed.chat_stream(MESSAGES, on_delta=seen.append)

    assert two.content == one.content == "".join(seen) == "你好，世界"
    assert two.usage == one.usage == {"prompt_tokens": 9, "completion_tokens": 3}


def test_missing_usage_is_left_empty_not_guessed() -> None:
    """服务端不回 usage 就如实留空 —— 宁可缺，也不编一个看着像的数字。"""
    client = _stream_client(_sse_body(["只有正文"], include_usage=False))
    reply = client.chat_stream(MESSAGES)
    assert reply.content == "只有正文"
    assert reply.usage == {}


class _RaisingStream(httpx.SyncByteStream):
    """先吐一个增量，再抛连接错误 —— 模拟"中途断开"。"""

    def __iter__(self) -> Any:
        yield 'data: {"choices":[{"delta":{"content":"前半句"}}]}\n\n'.encode()
        raise httpx.ReadError("连接被重置")


class _ResettingTransport(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200, stream=_RaisingStream(), headers={"content-type": "text/event-stream"}
        )


def test_midstream_break_keeps_partial_and_reports_the_real_reason() -> None:
    client = OpenAIChatClient(
        base_url=BASE, api_key="k", model="m", transport=_ResettingTransport()
    )
    seen: list[str] = []

    with pytest.raises(LlmError) as excinfo:
        client.chat_stream(MESSAGES, on_delta=seen.append)

    error = excinfo.value
    assert error.kind == "network"
    assert error.partial == "前半句", "已收到的部分必须保留在 partial 上（不许静默丢掉）"
    assert seen == ["前半句"], "已收到的部分也要实时交给 on_delta"
    assert "已收到 3 字符" in str(error), "真实原因（收到多少 + 为何断）要如实报出来"


def test_chat_still_sends_stream_false_and_no_stream_options() -> None:
    """非流式路径一字不变：仍是 stream=False、不带 stream_options。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [{"message": {"content": "答案"}, "finish_reason": "stop"}],
            },
        )

    client = OpenAIChatClient(
        base_url=BASE, api_key="k", model="m", transport=httpx.MockTransport(handler)
    )
    reply = client.chat(MESSAGES)

    assert reply.content == "答案"
    assert captured["payload"]["stream"] is False
    assert "stream_options" not in captured["payload"]


def test_stream_payload_omits_tools_when_none_are_passed() -> None:
    """不带工具就不发 ``tools``：请求体保持最小（老行为一字不变）。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200, content=_sse_body(["x"]), headers={"content-type": "text/event-stream"}
        )

    client = OpenAIChatClient(
        base_url=BASE, api_key="k", model="m", transport=httpx.MockTransport(handler)
    )
    client.chat_stream(MESSAGES)

    assert captured["payload"]["stream"] is True
    assert captured["payload"]["stream_options"] == {"include_usage": True}
    assert "tools" not in captured["payload"]


def test_streamed_round_keeps_the_tools_it_was_given() -> None:
    """带工具的轮次也要流式：``tools`` 必须**原样**进请求体。

    为什么这条是硬要求：流式只能流**正文**，但需要工具的那一轮必须照旧拿到工具声明 ——
    为了开流而把工具丢掉，模型就只能空口回答（"我来搜一下"然后什么都没搜）。
    """
    captured: dict[str, Any] = {}
    tools = [
        {
            "type": "function",
            "function": {
                "name": "research__search",
                "description": "检索",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200, content=_sse_body(["x"]), headers={"content-type": "text/event-stream"}
        )

    client = OpenAIChatClient(
        base_url=BASE, api_key="k", model="m", transport=httpx.MockTransport(handler)
    )
    client.chat_stream(MESSAGES, tools=tools)

    assert captured["payload"]["tools"] == tools
    assert captured["payload"]["tool_choice"] == "auto"


def _tool_sse_body(*, resend_identity: bool = False) -> bytes:
    """带工具调用的 SSE：``arguments`` 按 token 切成三块。

    ``resend_identity=True`` 时每块都重发完整的 ``id`` / ``name``（DeepSeek 实测行为）——
    无脑相加会拼出 ``call_1call_1`` 这种假 id。
    """
    fragments: list[dict[str, Any]] = [
        {
            "index": 0,
            "id": "call_1",
            "function": {"name": "research__search", "arguments": '{"que'},
        },
        {"index": 0, "function": {"arguments": 'ry": "DKT"'}},
        {"index": 0, "function": {"arguments": "}"}},
    ]
    out: list[str] = []
    for index, fragment in enumerate(fragments):
        call = dict(fragment)
        if resend_identity and index > 0:
            call["id"] = "call_1"
            call["function"] = {"name": "research__search", **fragment["function"]}
        event = {"choices": [{"delta": {"tool_calls": [call]}, "finish_reason": None}]}
        out.append("data: " + json.dumps(event, ensure_ascii=False) + "\n\n")
    out.append(
        "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}) + "\n\n"
    )
    out.append("data: [DONE]\n\n")
    return "".join(out).encode("utf-8")


def test_streamed_tool_call_fragments_are_assembled_by_index() -> None:
    """工具调用在流式里是**分片**到达的：必须按 ``index`` 拼回完整调用。

    漏了拼装 → 带工具的一轮 `reply.tool_calls` 恒为空 → 循环以为模型没要工具、
    模型以为工具调过了：整轮空转（单测只测正文时完全看不出来）。
    参数 JSON 只进 ``tool_calls``，**不能**混进正文增量（那会污染结构化输出）。
    """
    seen: list[str] = []
    reply_ = _stream_client(_tool_sse_body()).chat_stream(MESSAGES, on_delta=seen.append)

    assert [(call.id, call.name, call.arguments) for call in reply_.tool_calls] == [
        ("call_1", "research__search", '{"query": "DKT"}')
    ]
    assert reply_.content == ""
    assert seen == [], "工具参数是结构化数据，不许当正文增量推给界面"


def test_streamed_tool_call_identity_is_not_doubled_when_resent() -> None:
    """有的端点每块重发完整 id/name：拼装只能补**缺的**那部分。"""
    body = _tool_sse_body(resend_identity=True)
    reply_ = _stream_client(body).chat_stream(MESSAGES)

    assert [call.id for call in reply_.tool_calls] == ["call_1"]
    assert [call.name for call in reply_.tool_calls] == ["research__search"]
