"""JS ↔ Python 的桥：**只做转发与错误包装**，业务逻辑一律留在后端。

pywebview 会把本类公开方法的返回值交给 JS，但只支持简单类型，
所以统一回 JSON 字符串：``{"ok": true, "result": …}`` / ``{"ok": false, "error": {code, message}}``。

事件（``chat.delta`` / ``chat.tool_call`` / ``chat.tool_result`` / ``chat.done`` / ``chat.error``）
反过来推给 JS 的 ``window.scoutEvent``；窗口还没就绪时先缓冲，避免开窗瞬间丢事件（踩过）。
"""

from __future__ import annotations

import json
import os
import webbrowser
from typing import Any

from scholarscout import __version__
from scholarscout.desktop.rpc_client import RpcCallError, RpcClient

#: 窗口最小尺寸（逻辑像素）。**只有一处定义**：壳用它建窗口，桥用它夹住自绘把手的尺寸。
MIN_WINDOW_SIZE = (960, 640)

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
        self._maximized = False
        self.frameless = False  # attach() 时才确定；默认按"有原生标题行"处理
        self._pending: list[tuple[str, dict[str, Any]]] = []

    # ------------------------------------------------------------ 窗口与事件

    def attach(self, window: Any, *, frameless: bool | None = None) -> None:
        """窗口就绪后调用：把之前缓冲的事件冲出去，之后实时推送。

        为什么要显式传 ``frameless``：pywebview 的窗口对象不保留这个参数，
        界面却要知道"现在是没有原生标题行的模式"才决定显不显示自绘窗控条。
        """
        self._window = window
        self.frameless = bool(frameless) if frameless is not None else False
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

    def open_url(self, url: str) -> str:
        """用系统默认浏览器打开链接。

        为什么需要绕这一圈：pywebview 的窗口其实就是个浏览器，正文里直接点 ``<a>``
        会把**整个界面顶掉**（窗口变成那个网页），用户就回不来了。
        所以前端 markdown 渲染器把链接点击拦下来，交到这里。
        只放行 http/https —— 挡住 ``javascript:`` 之类的伪协议。
        """
        target = str(url or "").strip()
        lowered = target.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            return error_payload(-32602, "只允许 http/https 链接")
        try:
            opened = webbrowser.open(target)
        except Exception as exc:
            return error_payload(-32603, f"打开链接失败：{exc}")
        return ok_payload({"opened": target, "handled": bool(opened)})

    # ---------------------------------------------------------------- 窗口控制
    # 无边框窗口（ui.frameless_window）没有系统按钮，下面四个方法就是它们的替代品。
    # 为什么放在桥而不是 RPC：这几个动作只关乎窗口本身，跟后端会话/数据无关，
    # 没必要绕一圈 RPC，也不该因为后端正忙就点不动关闭。

    def window_mode(self) -> str:
        """窗口模式：界面靠它决定要不要显示自绘窗控条。

        为什么不让 Python 侧在启动时 `evaluate_js` 加个 class：那个时机早于页面加载，
        加载完成后整个 document 被换掉，class 会丢（踩过一次）。让界面自己问最稳。
        """
        return ok_payload(
            {
                "frameless": bool(getattr(self, "frameless", False)),
                "min_width": MIN_WINDOW_SIZE[0],
                "min_height": MIN_WINDOW_SIZE[1],
            }
        )

    def win_minimize(self) -> str:
        """最小化窗口。"""
        window = self._window
        if window is None:
            return error_payload(-32000, "窗口还没就绪")
        try:
            window.minimize()
        except Exception as exc:
            return error_payload(-32603, f"最小化失败：{exc}")
        return ok_payload({"minimized": True})

    def win_toggle_maximize(self) -> str:
        """最大化 / 还原。自己记状态：不依赖后端是否暴露 ``maximized`` 属性。"""
        window = self._window
        if window is None:
            return error_payload(-32000, "窗口还没就绪")
        try:
            if self._maximized:
                window.restore()
            else:
                window.maximize()
        except Exception as exc:
            return error_payload(-32603, f"最大化失败：{exc}")
        self._maximized = not self._maximized
        return ok_payload({"maximized": self._maximized})

    def win_close(self) -> str:
        """关闭窗口（走正常销毁流程，几何会被 ``closing`` 事件记下来）。"""
        window = self._window
        if window is None:
            return error_payload(-32000, "窗口还没就绪")
        try:
            window.destroy()
        except Exception as exc:
            return error_payload(-32603, f"关闭失败：{exc}")
        return ok_payload({"closed": True})

    def win_move(self, x: str = "0", y: str = "0") -> str:
        """移动窗口到绝对坐标（屏幕逻辑像素）。

        为什么必须自己实现：pywebview 的无边框窗口（Windows）既不响应标题区拖动，
        也没有系统边框可拖 —— 实测拖动不生效。所以由界面按下顶栏时算出目标坐标打过来。
        """
        window = self._window
        if window is None:
            return error_payload(-32000, "窗口还没就绪")
        try:
            window.move(int(float(x or 0)), int(float(y or 0)))
        except Exception as exc:
            return error_payload(-32603, f"移动窗口失败：{exc}")
        return ok_payload({"x": int(float(x or 0)), "y": int(float(y or 0))})

    def win_geometry(self) -> str:
        """当前窗口的位置与大小。拖动/缩放开始时取一次基准，之后都按屏幕坐标算增量。"""
        window = self._window
        if window is None:
            return error_payload(-32000, "窗口还没就绪")
        payload: dict[str, Any] = {}
        for key in ("x", "y", "width", "height"):
            value = getattr(window, key, None)
            payload[key] = int(value) if isinstance(value, (int, float)) else None
        payload["min_width"] = MIN_WINDOW_SIZE[0]
        payload["min_height"] = MIN_WINDOW_SIZE[1]
        return ok_payload(payload)

    def win_resize(self, width: str = "0", height: str = "0") -> str:
        """右下角自绘把手用。

        尺寸在**这里**统一夹到最小尺寸 —— 前端传什么都不会把窗口拖成一条缝，
        这条规则只有一处，前端就不必重复一遍。
        """
        window = self._window
        if window is None:
            return error_payload(-32000, "窗口还没就绪")
        try:
            target_w = max(MIN_WINDOW_SIZE[0], int(float(width or 0)))
            target_h = max(MIN_WINDOW_SIZE[1], int(float(height or 0)))
            window.resize(target_w, target_h)
        except Exception as exc:
            return error_payload(-32603, f"调整窗口大小失败：{exc}")
        return ok_payload({"width": target_w, "height": target_h})

    def shutdown(self) -> str:
        """关闭后端进程（关窗口时由壳自己调，JS 也可主动调）。"""
        self._client.close()
        return ok_payload({"closed": True})
