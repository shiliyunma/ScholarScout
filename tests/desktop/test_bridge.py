"""Bridge 契约：JS 侧的调用包装、参数校验、事件缓冲与冲刷。"""

from __future__ import annotations

import json

from scholarscout.desktop.bridge import Bridge
from scholarscout.desktop.rpc_client import RpcCallError


class RecordingClient:
    """假客户端：记录调用，可指定抛错。"""

    def __init__(self, *, result=None, error: RpcCallError | None = None) -> None:
        self.calls: list[tuple[str, dict | None]] = []
        self.result = result if result is not None else {"ok": True}
        self.error = error
        self.closed = False
        self.alive = True

    def call(self, method, params=None, *, timeout=None):
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        return self.result

    def close(self, *, timeout: float = 5.0) -> None:
        self.closed = True


class FakeWindow:
    def __init__(self, *, fail: bool = False) -> None:
        self.scripts: list[str] = []
        self.fail = fail

    def evaluate_js(self, script: str) -> None:
        if self.fail:
            raise RuntimeError("窗口已经关了")
        self.scripts.append(script)

    def events(self) -> list[dict]:
        out = []
        for script in self.scripts:
            start = script.index("(")
            out.append(json.loads(script[start + 1 : script.rindex(")")]))
        return out


def test_rpc_requires_method_name() -> None:
    bridge = Bridge(RecordingClient())  # type: ignore[arg-type]
    payload = json.loads(bridge.rpc("   "))
    assert payload["ok"] is False
    assert payload["error"]["code"] == -32600


def test_rpc_rejects_bad_json() -> None:
    bridge = Bridge(RecordingClient())  # type: ignore[arg-type]
    payload = json.loads(bridge.rpc("system.health", "{不是 JSON"))
    assert payload["ok"] is False
    assert payload["error"]["code"] == -32700


def test_rpc_rejects_non_object_params() -> None:
    bridge = Bridge(RecordingClient())  # type: ignore[arg-type]
    payload = json.loads(bridge.rpc("system.health", "[1,2]"))
    assert payload["error"]["code"] == -32600


def test_rpc_forwards_and_wraps_result() -> None:
    client = RecordingClient(result={"items": [1]})
    bridge = Bridge(client)  # type: ignore[arg-type]
    payload = json.loads(bridge.rpc("archive.list", '{"query": "教育"}'))
    assert payload == {"ok": True, "result": {"items": [1]}}
    assert client.calls == [("archive.list", {"query": "教育"})]


def test_rpc_without_params_passes_none() -> None:
    client = RecordingClient()
    bridge = Bridge(client)  # type: ignore[arg-type]
    bridge.rpc("system.health")
    assert client.calls == [("system.health", None)]


def test_rpc_surfaces_client_error() -> None:
    client = RecordingClient(error=RpcCallError("后端进程已退出", code=-32000))
    bridge = Bridge(client)  # type: ignore[arg-type]
    payload = json.loads(bridge.rpc("system.health"))
    assert payload["ok"] is False
    assert payload["error"]["code"] == -32000
    assert "已退出" in payload["error"]["message"]


def test_reveal_calls_archive_reveal_with_path() -> None:
    client = RecordingClient()
    bridge = Bridge(client)  # type: ignore[arg-type]
    bridge.reveal("data/notes/not_x.md")
    assert client.calls == [("archive.reveal", {"path": "data/notes/not_x.md"})]


def test_events_buffered_until_window_attached() -> None:
    bridge = Bridge(RecordingClient())  # type: ignore[arg-type]
    bridge.on_event("chat.delta", {"text": "早到的片段"})  # 窗口还没建好

    window = FakeWindow()
    bridge.attach(window)
    assert window.events() == [{"method": "chat.delta", "params": {"text": "早到的片段"}}]

    bridge.on_event("chat.done", {"stop_reason": "completed"})
    assert len(window.events()) == 2
    assert window.events()[1]["method"] == "chat.done"


def test_flush_is_idempotent() -> None:
    bridge = Bridge(RecordingClient())  # type: ignore[arg-type]
    bridge.on_event("chat.delta", {"text": "x"})
    window = FakeWindow()
    bridge.attach(window)
    bridge.flush_pending()
    assert len(window.events()) == 1


def test_broken_window_does_not_raise() -> None:
    bridge = Bridge(RecordingClient())  # type: ignore[arg-type]
    bridge.attach(FakeWindow(fail=True))
    bridge.on_event("chat.delta", {"text": "x"})  # 不该抛


def test_info_reports_app_and_version() -> None:
    bridge = Bridge(RecordingClient())  # type: ignore[arg-type]
    payload = json.loads(bridge.info())
    assert payload["ok"] is True
    assert payload["result"]["app"] == "ScholarScout"
    assert payload["result"]["alive"] is True


def test_shutdown_closes_client() -> None:
    client = RecordingClient()
    bridge = Bridge(client)  # type: ignore[arg-type]
    json.loads(bridge.shutdown())
    assert client.closed is True
