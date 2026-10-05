"""模型客户端解析与错误分类（用 httpx.MockTransport，不联网）。"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from scholarscout.config import LlmProfile
from scholarscout.providers.llm import (
    ChatReply,
    LlmError,
    OpenAIChatClient,
    ToolCall,
    create_client,
)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> OpenAIChatClient:
    return OpenAIChatClient(
        base_url="http://model.local/v1",
        api_key="test-key",
        model="test-model",
        timeout_s=1.0,
        transport=httpx.MockTransport(handler),
    )


def _json_response(payload: dict[str, object], status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def test_parses_content_and_usage() -> None:
    payload = {
        "model": "test-model",
        "choices": [{"message": {"content": "你好"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    reply = _client(lambda request: _json_response(payload)).chat(
        [{"role": "user", "content": "hi"}]
    )
    assert isinstance(reply, ChatReply)
    assert reply.content == "你好"
    assert reply.usage == {"prompt_tokens": 5, "completion_tokens": 3}
    assert reply.tool_calls == []


def test_parses_tool_calls() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_9",
                            "type": "function",
                            "function": {"name": "research.search", "arguments": '{"query":"x"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    reply = _client(lambda request: _json_response(payload)).chat([])
    assert reply.content == ""
    assert reply.tool_calls == [
        ToolCall(id="call_9", name="research.search", arguments='{"query":"x"}')
    ]
    assert reply.tool_calls[0].parsed_arguments() == {"query": "x"}
    assert reply.finish_reason == "tool_calls"


def test_tool_call_arguments_must_be_json() -> None:
    call = ToolCall(id="1", name="x", arguments="{坏掉的")
    with pytest.raises(LlmError) as info:
        call.parsed_arguments()
    assert info.value.kind == "bad_response"


def test_empty_arguments_parse_to_empty_dict() -> None:
    assert ToolCall(id="1", name="x", arguments="  ").parsed_arguments() == {}


def test_request_payload_has_no_tools_key_when_absent() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _json_response({"choices": [{"message": {"content": "ok"}}]})

    _client(handler).chat([{"role": "user", "content": "hi"}])
    assert "tools" not in seen
    assert seen["stream"] is False
    assert seen["model"] == "test-model"


def test_request_payload_carries_tools_when_given() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _json_response({"choices": [{"message": {"content": "ok"}}]})

    decl = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    _client(handler).chat([], tools=decl)
    assert seen["tools"] == decl
    assert seen["tool_choice"] == "auto"


def test_auth_error_is_classified() -> None:
    with pytest.raises(LlmError) as info:
        _client(lambda request: httpx.Response(401, text="bad key")).chat([])
    assert info.value.kind == "auth"
    assert info.value.http_status == 401


def test_rate_limit_is_classified() -> None:
    with pytest.raises(LlmError) as info:
        _client(lambda request: httpx.Response(429, text="slow down")).chat([])
    assert info.value.kind == "rate_limited"


def test_server_error_is_classified() -> None:
    with pytest.raises(LlmError) as info:
        _client(lambda request: httpx.Response(503, text="down")).chat([])
    assert info.value.kind == "server"


def test_client_error_is_bad_response() -> None:
    with pytest.raises(LlmError) as info:
        _client(lambda request: httpx.Response(400, text="bad params")).chat([])
    assert info.value.kind == "bad_response"


def test_timeout_is_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("too slow", request=request)

    with pytest.raises(LlmError) as info:
        _client(handler).chat([])
    assert info.value.kind == "timeout"


def test_network_error_is_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(LlmError) as info:
        _client(handler).chat([])
    assert info.value.kind == "network"


def test_non_json_body_is_bad_response() -> None:
    with pytest.raises(LlmError) as info:
        _client(lambda request: httpx.Response(200, text="<html>502</html>")).chat([])
    assert info.value.kind == "bad_response"


def test_missing_choices_is_bad_response() -> None:
    with pytest.raises(LlmError) as info:
        _client(lambda request: _json_response({"error": "nope"})).chat([])
    assert info.value.kind == "bad_response"


def test_unknown_error_kind_rejected() -> None:
    with pytest.raises(ValueError, match="未知错误类型"):
        LlmError("whatever", "x")


def test_create_client_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    profile = LlmProfile(model="m", base_url="http://x/v1", api_key_env="LLM_API_KEY")
    with pytest.raises(LlmError) as info:
        create_client(profile)
    assert info.value.kind == "auth"


def test_create_client_uses_env_key(monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    profile = LlmProfile(model="m", base_url="http://x/v1")
    client = create_client(profile)
    assert client.api_key == "sk-test"
    client.close()


def test_chat_reply_truncated_flag() -> None:
    """finish_reason=length 就是被 max_tokens 截断（推理模型的 reasoning token 也算 completion）。"""
    from scholarscout.providers.llm import ChatReply

    assert ChatReply(content="", finish_reason="length").truncated is True
    assert ChatReply(content="答案", finish_reason="stop").truncated is False
    assert ChatReply(content="答案").truncated is False


def test_default_max_tokens_leaves_room_for_reasoning() -> None:
    """推理模型的实际 completion 里 reasoning 占大头（实测 5379/5586），额度必须留足。"""
    from scholarscout.config import DEFAULT_CHAT, DEFAULT_SUMMARY

    assert DEFAULT_CHAT["max_tokens"] >= 8192
    assert DEFAULT_SUMMARY["max_tokens"] >= 2048
