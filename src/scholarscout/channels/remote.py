"""渠道**子进程侧**的 RPC 客户端：它只经这条通道跟父进程说话。

## 为什么必须有这一层（docs/21 §4 末行）

进程模型选 B（独立子进程）之后，**渠道子进程绝不能自己开库** —— 否则又变成多进程写 SQLite，
`docs/20` 的单写纪律当场作废。所以子进程侧的"上下文"是一个**没有 `db` 的替身**
（:class:`RemoteContext`），要数据只能调父进程的 RPC。这不是靠自觉：`RemoteContext` 里
根本没有 `db` 字段，`ctx.db` 取到的就是 `None`。

## 传输形状

与 `desktop/rpc_client.py` / `rpc/server.py` **同一套**：一行一个 JSON-RPC 消息（UTF-8）。

    子进程 → 父进程：{"jsonrpc":"2.0","id":1,"method":"feishu.access","params":{...}}
    父进程 → 子进程：{"jsonrpc":"2.0","id":1,"result":{...}}

父进程可能顺手推来**通知**（没有 `id` 的行，如 `chat.delta`）—— 这里一律丢进 `on_event`
（默认只记日志）：子进程不是在等它，别拿它当响应。

**注意 stdout 是协议通道**：子进程的任何日志都必须走 stderr（`channels/runner.py` 就是这么做的），
往 stdout 打一行普通文字就等于往协议里掺垃圾。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import Any, TextIO

logger = logging.getLogger(__name__)

#: 一次调用的等待上限（秒）。父进程**总会有响应**（哪怕错误），所以这是兜底而非常态。
DEFAULT_TIMEOUT = 600.0


class RemoteError(RuntimeError):
    """父进程回了一个错误（或通道坏了）。"""

    def __init__(self, message: str, *, code: int = -32000) -> None:
        super().__init__(message)
        self.code = int(code)


class RemoteContext:
    """子进程侧的应用上下文替身：**没有 `db`、没有 `llm`、没有工具集**。

    `channels/feishu.py` 只把 `ctx` 原样递给 `dispatcher`，所以它认的是一个**不透明对象**。
    这里刻意做成"要什么没什么"：子进程一旦想直接读库就会当场炸，而不是悄悄写出第二份数据库连接。
    """

    #: 显式声明：渠道子进程**不开库**（`None` 就是机制，不是笔误）
    db: None = None
    #: 没有模型客户端：跑轮次是父进程的事（子进程只把消息递进去）
    llm: None = None
    #: 不出网：出站发送经 `channel.send` 由父进程统一做
    http: None = None

    def __init__(self, dispatcher: Callable[..., Any] | None = None) -> None:
        self.dispatcher = dispatcher
        self.emit: Callable[[str, dict[str, Any]], None] | None = None

    def require_llm(self) -> Any:  # pragma: no cover - 子进程不该走这里
        raise RemoteError("渠道子进程没有模型客户端：跑轮次请经 RPC 交给主进程")

    def close(self) -> None:
        """与 `AppContext.close()` 同形（调用方不必分两种情况写）。子进程没有资源要关。"""


class StdioClient:
    """在 stdin/stdout 上说话的小客户端（**只做一件事**：发一行、等这一行的响应）。

    线程安全：SDK 的事件线程与主线程都会调它，用一把锁把"写请求 + 读到自己的响应"整段串起来
    —— 两段交错就会让 A 读到 B 的响应。
    """

    def __init__(
        self,
        *,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        import sys

        self._stdin = stdin if stdin is not None else sys.stdin
        self._stdout = stdout if stdout is not None else sys.stdout
        self._on_event = on_event
        self._lock = threading.Lock()
        self._next_id = 0

    # ---------------------------------------------------------------- 调用

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """发一次请求并等它的响应（**忽略**期间推来的通知）。"""
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params:
                payload["params"] = params
            self._write(payload)
            return self._await(request_id)

    def call(self, ctx: Any, method: str, params: Any = None) -> Any:
        """`dispatcher` 形状的入口（`channels/feishu.py` 要的就是这个签名）；`ctx` 用不上。"""
        del ctx
        return self.request(method, dict(params) if isinstance(params, dict) else None)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        with self._lock:
            self._write(payload)

    # ---------------------------------------------------------------- 内部

    def _write(self, payload: dict[str, Any]) -> None:
        self._stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._stdout.flush()

    def _await(self, request_id: int) -> Any:
        while True:
            line = self._stdin.readline()
            if not line:
                raise RemoteError("与主进程的通道已关闭（stdin 读到了 EOF）")
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("[Channel] 主进程推来了非 JSON 的一行（忽略）：%s", text[:120])
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") != request_id:
                self._route_event(message)
                continue
            error = message.get("error")
            if isinstance(error, dict):
                raise RemoteError(
                    str(error.get("message") or "主进程返回错误"),
                    code=int(error.get("code") or -32000),
                )
            return message.get("result")

    def _route_event(self, message: dict[str, Any]) -> None:
        """不是我要的响应：当通知处理（默认只记一行，绝不外抛 —— 收包线程不能被它搞崩）。"""
        name = str(message.get("method") or "")
        params = message.get("params")
        payload = params if isinstance(params, dict) else {}
        if self._on_event is not None:
            try:
                self._on_event(name, payload)
            except Exception as exc:  # pragma: no cover - 回调一律不外抛
                logger.warning("[Channel] 通知回调异常（已吞）：%s", exc)
            return
        logger.debug("[Channel] 收到通知（无处理者）：%s", name)
