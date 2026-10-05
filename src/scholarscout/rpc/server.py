"""stdio 传输：一行一个 JSON-RPC 消息（UTF-8，无 BOM）。

设计：**分发逻辑只有一份**（rpc/methods.py），这里只负责"读一行 → 分发 → 写回/上报通知"。
桌面壳（pywebview）走同一份方法表，只是传输不同（见 docs/01 §2 的说明）。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterable
from typing import Any, TextIO

from scholarscout.rpc.errors import Code, RpcError, from_exception
from scholarscout.rpc.methods import AppContext, dispatch

Emitter = Callable[[str, dict[str, Any]], None]

#: 作为通知上报的事件前缀（docs/01 §4）
NOTIFICATION_METHODS = (
    "chat.delta",
    "chat.tool_call",
    "chat.tool_result",
    "chat.done",
    "chat.error",
)


def make_writer(stream: TextIO) -> Callable[[dict[str, Any]], None]:
    def write(payload: dict[str, Any]) -> None:
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        stream.flush()

    return write


def make_emitter(write: Callable[[dict[str, Any]], None]) -> Emitter:
    """把 loop 的事件回调变成 JSON-RPC 通知。"""

    def emit(kind: str, payload: dict[str, Any]) -> None:
        method = kind if kind in NOTIFICATION_METHODS else f"chat.{kind}"
        write({"jsonrpc": "2.0", "method": method, "params": payload})

    return emit


def error_response(request_id: Any, error: RpcError) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": error.to_error()}


def ok_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def handle_message(ctx: AppContext, message: dict[str, Any]) -> dict[str, Any] | None:
    """处理一条已解析的消息；通知（无 id）返回 None。"""
    request_id = message.get("id")
    name = message.get("method")
    if not isinstance(name, str):
        return error_response(request_id, RpcError(Code.INVALID_REQUEST, "缺少 method"))

    # 通知：没有 id 就不回响应（当前没有客户端→服务端通知，先按契约实现）
    if request_id is None:
        try:
            dispatch(ctx, name, message.get("params"))
        except RpcError:
            return None
        return None

    try:
        result = dispatch(ctx, name, message.get("params"))
    except BaseException as exc:
        return error_response(request_id, from_exception(exc))
    return ok_response(request_id, result)


def handle_line(ctx: AppContext, line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text:
        return None
    try:
        message = json.loads(text)
    except json.JSONDecodeError as exc:
        return error_response(None, RpcError(Code.PARSE_ERROR, f"JSON 解析失败：{exc}"))
    if not isinstance(message, dict):
        return error_response(None, RpcError(Code.INVALID_REQUEST, "消息必须是 JSON 对象"))
    return handle_message(ctx, message)


def serve(
    ctx: AppContext,
    *,
    stdin: Iterable[str] | None = None,
    stdout: TextIO | None = None,
) -> int:
    """阻塞式服务：读一行处理一行，直到 stdin 关闭。"""
    input_stream = stdin if stdin is not None else sys.stdin
    output = stdout if stdout is not None else sys.stdout
    write = make_writer(output)
    ctx.emit = make_emitter(write) if ctx.emit is None else ctx.emit
    for line in input_stream:
        response = handle_line(ctx, line)
        if response is not None:
            write(response)
    return 0
