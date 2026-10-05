"""JSON-RPC 协议契约：错误码、解析、通知、异常映射（对齐 docs/01 §2）。"""

from __future__ import annotations

import json

import pytest

from scholarscout.providers.llm import LlmError
from scholarscout.rpc.errors import Code, RpcError, from_exception
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.rpc.server import handle_line, handle_message


def _call(ctx: AppContext, method: str, params: object = None, request_id: object = 1):
    return handle_message(
        ctx, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    )


def test_ok_response_shape(ctx: AppContext) -> None:
    response = _call(ctx, "system.health")
    assert response is not None
    assert set(response) == {"jsonrpc", "id", "result"}
    assert response["jsonrpc"] == "2.0" and response["id"] == 1
    assert response["result"]["ok"] is True


def test_parse_error(ctx: AppContext) -> None:
    response = handle_line(ctx, "{不是 JSON")
    assert response is not None
    assert response["error"]["code"] == Code.PARSE_ERROR
    assert response["id"] is None


def test_non_object_message_is_invalid_request(ctx: AppContext) -> None:
    response = handle_line(ctx, "[1, 2, 3]")
    assert response is not None
    assert response["error"]["code"] == Code.INVALID_REQUEST


def test_missing_method_is_invalid_request(ctx: AppContext) -> None:
    response = handle_message(ctx, {"jsonrpc": "2.0", "id": 7})
    assert response is not None
    assert response["error"]["code"] == Code.INVALID_REQUEST


def test_method_not_found(ctx: AppContext) -> None:
    response = _call(ctx, "nope.nothing")
    assert response is not None
    assert response["error"]["code"] == Code.METHOD_NOT_FOUND


def test_params_must_be_object(ctx: AppContext) -> None:
    response = _call(ctx, "session.create", [1, 2])
    assert response is not None
    assert response["error"]["code"] == Code.INVALID_PARAMS


def test_missing_required_param(ctx: AppContext) -> None:
    response = _call(ctx, "session.messages", {})
    assert response is not None
    assert response["error"]["code"] == Code.INVALID_PARAMS


def test_notification_without_id_returns_none(ctx: AppContext) -> None:
    assert handle_message(ctx, {"jsonrpc": "2.0", "method": "system.health"}) is None


def test_blank_line_returns_none(ctx: AppContext) -> None:
    assert handle_line(ctx, "   \n") is None


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("auth", Code.NOT_CONFIGURED),
        ("timeout", Code.TIMEOUT),
        ("network", Code.SERVICE_UNAVAILABLE),
        ("server", Code.SERVICE_UNAVAILABLE),
        ("rate_limited", Code.SERVICE_UNAVAILABLE),
        ("bad_response", Code.INTERNAL_ERROR),
    ],
)
def test_llm_error_kinds_map_to_codes(kind: str, expected: Code) -> None:
    assert from_exception(LlmError(kind, "boom")).code == expected


def test_rpc_error_passes_through() -> None:
    original = RpcError(Code.NOT_FOUND, "没有这个")
    assert from_exception(original) is original


def test_unknown_exception_is_internal_error() -> None:
    assert from_exception(RuntimeError("??")).code == Code.INTERNAL_ERROR


def test_rpc_error_payload_shape() -> None:
    assert RpcError(Code.NOT_FOUND, "x", data={"doc_id": "doc_1"}).to_error() == {
        "code": int(Code.NOT_FOUND),
        "message": "x",
        "data": {"doc_id": "doc_1"},
    }


def test_dispatch_unknown_method_raises(ctx: AppContext) -> None:
    with pytest.raises(RpcError) as info:
        dispatch(ctx, "nope.nothing", {})
    assert info.value.code == Code.METHOD_NOT_FOUND


def test_method_table_covers_contract(ctx: AppContext) -> None:
    """docs/01 §3 里 M2 应实现的方法必须都在。"""
    from scholarscout.rpc.methods import method_names

    names = set(method_names())
    for name in (
        "system.health",
        "config.get",
        "session.create",
        "session.list",
        "session.messages",
        "chat.send",
        "chat.cancel",
        "trace.list",
        "archive.list",
        "archive.get",
        "archive.paths",
    ):
        assert name in names, f"缺方法：{name}"


def test_health_reports_tools_and_key_state(ctx: AppContext) -> None:
    result = dispatch(ctx, "system.health", {})
    assert result["tools"] == []  # M2：工具尚未注册
    assert set(result["counts"])  # 十张业务表都在


def test_config_get_never_exposes_secret(ctx: AppContext, monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-super-secret")
    result = dispatch(ctx, "config.get", {})
    assert result["llm"]["api_key_set"] is True
    assert "sk-super-secret" not in json.dumps(result, ensure_ascii=False)
    assert "api_key" not in result["llm"]
