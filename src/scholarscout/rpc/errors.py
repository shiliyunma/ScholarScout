"""JSON-RPC 错误对象与异常映射（码表见 `rpc/codes.py`，docs/01 §2）。

码本身定义在 `rpc/codes.py`（**只依赖标准库**，壳层才 import 得动）；本模块负责
"异常 → 码"的映射，并把 `Code` 原样 re-export（历史调用方都是从这里拿的）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from scholarscout.paths import PathOutsideDataError
from scholarscout.providers.llm import LlmError
from scholarscout.rpc.codes import Code

__all__ = ["Code", "RpcError", "from_exception"]


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
