"""JS ↔ Python 的桥：**只做转发与错误包装**，业务逻辑一律留在后端。

pywebview 会把本类公开方法的返回值交给 JS，但只支持简单类型，
所以统一回 JSON 字符串：``{"ok": true, "result": …}`` / ``{"ok": false, "error": {code, message}}``。

事件（``chat.delta`` / ``chat.tool_call`` / ``chat.tool_result`` / ``chat.done`` / ``chat.error``）
反过来推给 JS 的 ``window.scoutEvent``；窗口还没就绪时先缓冲，避免开窗瞬间丢事件（踩过）。
"""

from __future__ import annotations

import json
import os
from typing import Any

from scholarscout import __version__
from scholarscout.desktop.rpc_client import RpcCallError, RpcClient

#: 前端接收事件用的全局函数名
JS_EVENT_FUNCTION = "window.scoutEvent"


def ok_payload(result: Any) -> str:
    return json.dumps({"ok": True, "result": result}, ensure_ascii=False)


def error_payload(code: int, message: str) -> str:
    return json.dumps(
        {"ok": False, "error": {"code": code, "message": message}}, ensure_ascii=False
    )


class Bridge:
    """挂到 ``webview.create_window(js_api=...)`` 的对象。"""

    def __init__(self, client: RpcClient, *, window: Any = None) -> None:
        self._client = client
        self._window = window
        self._pending: list[tuple[str, dict[str, Any]]] = []

    # ------------------------------------------------------------ 窗口与事件

    def attach(self, window: Any) -> None:
        """窗口就绪后调用：把之前缓冲的事件冲出去，之后实时推送。"""
        self._window = window
        self.flush_pending()

    def flush_pending(self) -> None:
        if self._window is None:
            return
        pending, self._pending = self._pending, []
        for name, params in pending:
            self._push(name, params)

    def on_event(self, name: str, params: dict[str, Any]) -> None:
        """交给 ``RpcClient(on_event=...)``；窗口没好就先留着。"""
        if self._window is None:
            self._pending.append((name, params))
            return
        self._push(name, params)

    def _push(self, name: str, params: dict[str, Any]) -> None:
        script = (
            f"{JS_EVENT_FUNCTION} && {JS_EVENT_FUNCTION}("
            f"{json.dumps({'method': name, 'params': params}, ensure_ascii=False)});"
        )
        try:
            self._window.evaluate_js(script)
        except Exception:
            return

    # --------------------------------------------------------- JS 可调的方法

    def info(self) -> str:
        """壳自身信息（后端能力用 system.health 查，那是唯一真相）。"""
        return ok_payload(
            {
                "app": "ScholarScout",
                "version": __version__,
                "pid": os.getpid(),
                "alive": self._client.alive,
            }
        )

    def rpc(self, method: str, params_json: str = "") -> str:
        """转发一次 JSON-RPC 调用。``params_json`` 为空串表示不带参数。"""
        if not isinstance(method, str) or not method.strip():
            return error_payload(-32600, "方法名不能为空")
        params: dict[str, Any] | None = None
        text = (params_json or "").strip()
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                return error_payload(-32700, f"参数不是合法 JSON：{exc}")
            if not isinstance(parsed, dict):
                return error_payload(-32600, "参数必须是 JSON 对象")
            params = parsed
        try:
            return ok_payload(self._client.call(method, params))
        except RpcCallError as exc:
            return error_payload(exc.code, str(exc))

    def reveal(self, rel_path: str) -> str:
        """在文件管理器中定位一个库内文件（原件、全文、笔记、路线导出）。"""
        return self.rpc("archive.reveal", json.dumps({"path": rel_path}, ensure_ascii=False))

    def shutdown(self) -> str:
        """关闭后端进程（关窗口时由壳自己调，JS 也可主动调）。"""
        self._client.close()
        return ok_payload({"closed": True})
