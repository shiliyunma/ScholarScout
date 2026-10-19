"""契约闸 2/4：错误回包**结构统一** + 错误码**只许来自 `rpc/errors.Code`**（docs/01 §2/§7）。

项目的实际口径是 **JSON-RPC 2.0 标准错误对象**（不是 `{ok:false, error:{...}}`）：

```json
{"jsonrpc": "2.0", "id": 7, "error": {"code": -32602, "message": "缺参数：x"}}
```

- 成功走 `result`，失败走 `error`，**同一个响应里不许两者都在**（客户端按 `error` 是否存在分流，
  两个都在会让前端把错误当成功吞掉 —— 项目里已经踩过"没在跑却回成功"这一类坑）；
- `error` 里只有 `code` / `message`（`data` 可选）：码是 `Code` 的成员，消息是可读中文；
- **错误码不散写**：`RpcError(...)` 的第一个参数必须是 `Code.X`，不许写 `-32602` 这种裸数字。
  裸数字散在各处时，改一处口径就会漏掉另一处，而且没人能 grep 出"哪些码在被用"。

分层边界（`tests/test_layering.py`）：`desktop/` 不许 import `runtime/`，而 `rpc/errors.py`
import 了 `providers.llm`，所以**壳层的 `desktop/bridge.py` 无法 import `Code`** —— 它仍在写裸码。
这一条是**已知待修**，单独用 `xfail(strict=True)` 点名（修法是给壳层一份协议常量或让 errors 只依赖标准库）。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from scholarscout.rpc.errors import Code, RpcError, from_exception
from scholarscout.rpc.server import handle_message

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "scholarscout"

#: 标准错误对象里允许出现的键（`data` 是可选的补充信息，`to_error()` 只在非 None 时带）
ERROR_KEYS = {"code", "message", "data"}


def _assert_standard_error(response: dict[str, Any], expected: Code) -> dict[str, Any]:
    assert response["jsonrpc"] == "2.0"
    assert "result" not in response, "失败响应里不许同时出现 result（前端会当成功吞掉）"
    error = response["error"]
    assert set(error) <= ERROR_KEYS, f"error 里有多余字段：{set(error) - ERROR_KEYS}"
    assert error["code"] == int(expected), f"错误码不对：{error['code']} != {int(expected)}"
    assert isinstance(error["message"], str) and error["message"].strip(), (
        "message 必须是非空字符串"
    )
    return error


def test_unknown_method_returns_standard_error_object(ctx: Any) -> None:
    """方法不存在 → `-32601`，且是标准错误对象（不是 ok:false 信封）。"""
    response = handle_message(ctx, {"jsonrpc": "2.0", "id": 7, "method": "no.such.method"})
    assert response is not None
    _assert_standard_error(response, Code.METHOD_NOT_FOUND)


def test_missing_method_returns_invalid_request(ctx: Any) -> None:
    """请求里没有 `method` → `-32600`。"""
    response = handle_message(ctx, {"jsonrpc": "2.0", "id": 8})
    assert response is not None
    _assert_standard_error(response, Code.INVALID_REQUEST)


def test_non_object_params_returns_invalid_params(ctx: Any) -> None:
    """`params` 不是对象 → `-32602`（`registry._params` 的唯一口径）。"""
    response = handle_message(
        ctx, {"jsonrpc": "2.0", "id": 9, "method": "session.rename", "params": [1, 2]}
    )
    assert response is not None
    _assert_standard_error(response, Code.INVALID_PARAMS)


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("session.rename", {}),  # 缺 session_id / title
        ("archive.get", {}),  # 缺 doc_id
        ("note.review", {"note_id": "not_missing"}),  # 缺 card_index / rating
        ("settings.update", {"updates": []}),  # updates 必须是对象
    ],
)
def test_bad_params_always_map_to_a_code_member(
    ctx: Any, method: str, params: dict[str, Any]
) -> None:
    """代表方法的参数错误一律映射到 `Code` 成员（不许出现私有裸码或未定义值）。"""
    response = handle_message(ctx, {"jsonrpc": "2.0", "id": 10, "method": method, "params": params})
    assert response is not None
    code = response["error"]["code"]
    assert code in {int(member) for member in Code}, f"{method} 回了未定义的错误码：{code}"
    assert code == int(Code.INVALID_PARAMS), f"{method} 的参数错误应是 -32602，实际 {code}"


def test_success_response_has_no_error_key(ctx: Any) -> None:
    """成功响应只有 `result`，不能带 `error`（反之亦然，见上面的断言）。"""
    response = handle_message(ctx, {"jsonrpc": "2.0", "id": 11, "method": "system.health"})
    assert response is not None
    assert "error" not in response
    assert response["result"]["ok"] is True


def test_unclassified_exception_maps_to_internal_error() -> None:
    """没分类的异常一律 `-32603`，且消息里带类型名（排查时能定位到是哪类炸的）。"""
    mapped = from_exception(RuntimeError("boom"))
    assert mapped.code == int(Code.INTERNAL_ERROR)
    assert "RuntimeError" in mapped.message and "boom" in mapped.message


def test_rpc_error_payload_keys_are_stable() -> None:
    """`RpcError.to_error()` 的键集固定：`code`/`message`，`data` 只在给了的时候出现。"""
    assert set(RpcError(Code.NOT_FOUND, "没了").to_error()) == {"code", "message"}
    assert set(RpcError(Code.NOT_FOUND, "没了", data={"id": "x"}).to_error()) == {
        "code",
        "message",
        "data",
    }


def _int_literal(node: ast.expr) -> int | None:
    """取出整数字面量。**负号是 `UnaryOp`**：`-32602` 在 AST 里是 `USub(Constant(32602))`。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, int)
    ):
        return -node.operand.value
    return None


def _literal_code_sites(directory: Path) -> list[str]:
    """扫出"把错误码写成裸数字"的调用点（`RpcError(-32602, ...)` / `error_payload(-32000, ...)`）。"""
    offenders: list[str] = []
    for path in sorted(directory.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name not in {"RpcError", "error_payload"} or not node.args:
                continue
            if _int_literal(node.args[0]) is not None:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return offenders


def test_error_codes_are_never_written_as_literals_in_rpc_layer() -> None:
    """`rpc/` 里所有错误码都必须来自 `Code`（裸数字一个都不许有）。"""
    offenders = _literal_code_sites(SRC / "rpc")
    assert offenders == [], f"rpc/ 里出现裸错误码（改用 Code.X）：{offenders}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "待修：rpc/ 之外仍有裸错误码 —— desktop/bridge.py（-32600/-32602/-32603/-32700/-32000）、"
        "channels/remote.py 与 desktop/rpc_client.py（默认 -32000）。"
        "壳层按 tests/test_layering.py 不许 import rpc/errors（它 import 了 providers.llm），"
        "修法是给壳层一份只依赖标准库的协议常量；属「仍需单独排期」。"
    ),
)
def test_error_codes_are_never_written_as_literals_anywhere() -> None:
    """整个 `src/` 都不许散写错误码（现状红，见 xfail 理由）。"""
    offenders = _literal_code_sites(SRC)
    assert offenders == [], f"这些地方把错误码写成了裸数字：{offenders}"
