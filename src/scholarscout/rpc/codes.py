"""JSON-RPC 错误码的**唯一常量来源**（只依赖标准库，见 `tests/test_layering.py`）。

为什么单独一个模块（而不是留在 `rpc/errors.py` 里）：`errors.py` 还要 import
``providers.llm`` / ``paths`` 才能把异常映射成码，而桌面壳 `desktop/` 与渠道子进程
`channels/` 只需要一个数字。让它们 import `rpc/errors.py` 等于为了一个常量把
providers、sqlite 例外映射整套拉进壳里；分出来之后壳只 import 本模块，
**该模块没有任何 scholarscout 内部依赖**（`rpc/__init__.py` 也是空的），
所以既不破分层，也不会在壳里多加载半个协议栈。

口径：-32700 ~ -32603 是 JSON-RPC 2.0 标准；-32000 起是项目私有码（docs/01 §2）。
`rpc/errors.py` 直接复用这里的 `Code`（re-export），两边因此不可能各写一份而漂移；
`tests/contract/test_rpc_errors.py` 另有一道闸把"两边取值一致"钉住。
"""

from __future__ import annotations

from enum import IntEnum


class Code(IntEnum):
    """协议错误码。**任何地方都不许再写 -32602 这种裸数字。**"""

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


#: 协议表里全部合法取值。壳层收到表外的码时不能原样透给前端 —— 前端既没法分支，
#: 日志里也查不到出处，所以一律收敛到 :attr:`Code.NOT_CONFIGURED`（传输层兜底）。
KNOWN_CODES: frozenset[int] = frozenset(int(member) for member in Code)


def coerce_code(raw: object) -> int:
    """把一条 ``error.code`` 收敛成协议表里的值（壳层专用，表外的码一律兜底）。

    - 表内的码原样返回（后端说了算的部分不动）；
    - 缺字段 / 非整数 / 整数但表里没有 → ``NOT_CONFIGURED``：壳只承诺说得出口的码。
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return int(Code.NOT_CONFIGURED)
    try:
        code = int(raw)
    except ValueError:
        return int(Code.NOT_CONFIGURED)
    return code if code in KNOWN_CODES else int(Code.NOT_CONFIGURED)


__all__ = ["KNOWN_CODES", "Code", "coerce_code"]
