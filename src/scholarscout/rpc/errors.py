"""JSON-RPC 错误码（docs/01 §2 的表格就是这里）。

-32700 ~ -32603 是 JSON-RPC 标准；-32000 起是项目私有码。
"""

from __future__ import annotations

import sqlite3
from enum import IntEnum
from typing import Any

from scholarscout.paths import PathOutsideDataError
from scholarscout.providers.llm import LlmError


class Code(IntEnum):
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    NOT_CONFIGURED = -32000
    SERVICE_UNAVAILABLE = -32001
    NOT_FOUND = -32002
    FETCH_FORBIDDEN = -32003
    TIMEOUT = -32004
    CANCELLED = -32005


class RpcError(Exception):
    def __init__(self, code: Code | int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = message
        self.data = data

    def to_error(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload


def from_exception(exc: BaseException) -> RpcError:
    """把内部异常映射成契约错误码。未分类的一律 INTERNAL_ERROR。"""
    if isinstance(exc, RpcError):
        return exc
    if isinstance(exc, LlmError):
        mapping = {
            "auth": Code.NOT_CONFIGURED,
            "timeout": Code.TIMEOUT,
            "network": Code.SERVICE_UNAVAILABLE,
            "server": Code.SERVICE_UNAVAILABLE,
            "rate_limited": Code.SERVICE_UNAVAILABLE,
            "bad_response": Code.INTERNAL_ERROR,
        }
        return RpcError(mapping.get(exc.kind, Code.INTERNAL_ERROR), str(exc))
    if isinstance(exc, PathOutsideDataError):
        return RpcError(Code.INVALID_PARAMS, str(exc))
    if isinstance(exc, sqlite3.IntegrityError):
        return RpcError(Code.INVALID_PARAMS, f"数据约束不满足：{exc}")
    if isinstance(exc, (KeyError, TypeError, ValueError)):
        return RpcError(Code.INVALID_PARAMS, f"{type(exc).__name__}: {exc}")
    return RpcError(Code.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
