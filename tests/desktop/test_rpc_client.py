"""RpcClient 契约：帧格式、通知转发、错误与超时、真实子进程往返。"""

from __future__ import annotations

import io
import json
import queue
import sys
from pathlib import Path

import pytest

from scholarscout.desktop.rpc_client import (
    RpcCallError,
    RpcClient,
    default_command,
    default_env,
)


class QueueReader:
    """假 stdout：readline() 从队列取行，取到哨兵返回空串（模拟进程退出）。"""

    def __init__(self) -> None:
        self._q: queue.Queue[str | None] = queue.Queue()

    def feed(self, line: str) -> None:
        self._q.put(line)

    def eof(self) -> None:
        self._q.put(None)

    def readline(self) -> str:
        item = self._q.get()
        return "" if item is None else item


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = io.StringIO()
        self.stdout = QueueReader()
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def sent(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.stdin.getvalue().splitlines() if line.strip()]


def _client(proc: FakeProcess, events: list[tuple[str, dict]] | None = None) -> RpcClient:
    sink = (lambda name, params: events.append((name, params))) if events is not None else None
    client = RpcClient(["fake"], on_event=sink, timeout=5.0, spawn=lambda command: proc)
    client.start()
    return client


def test_call_writes_request_and_returns_result() -> None:
    proc = FakeProcess()
    client = _client(proc)
    proc.stdout.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}) + "\n")

    assert client.call("system.health") == {"ok": True}
    frame = proc.sent()[0]
    assert frame["jsonrpc"] == "2.0"
    assert frame["method"] == "system.health"
    assert "params" not in frame, "没参数就不该塞 params"


def test_call_sends_params_and_increments_id() -> None:
    proc = FakeProcess()
    client = _client(proc)
    proc.stdout.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "result": 1}) + "\n")
    proc.stdout.feed(json.dumps({"jsonrpc": "2.0", "id": 2, "result": 2}) + "\n")

    assert client.call("session.list", {"limit": 5}) == 1
    assert client.call("session.create") == 2
    frames = proc.sent()
    assert frames[0]["params"] == {"limit": 5}
    assert [frame["id"] for frame in frames] == [1, 2]


def test_notifications_are_forwarded_before_response() -> None:
    events: list[tuple[str, dict]] = []
    proc = FakeProcess()
    client = _client(proc, events)
    proc.stdout.feed(
        json.dumps({"jsonrpc": "2.0", "method": "chat.tool_call", "params": {"tool": "x"}}) + "\n"
    )
    proc.stdout.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "result": "done"}) + "\n")

    assert client.call("chat.send", {"text": "hi"}) == "done"
    assert events == [("chat.tool_call", {"tool": "x"})]


def test_noise_lines_are_ignored() -> None:
    proc = FakeProcess()
    client = _client(proc)
    proc.stdout.feed("这是第三方库打出来的日志\n")
    proc.stdout.feed("{ 不是 JSON\n")
    proc.stdout.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "result": "ok"}) + "\n")

    assert client.call("system.health") == "ok"


def test_error_frame_becomes_rpc_call_error() -> None:
    proc = FakeProcess()
    client = _client(proc)
    proc.stdout.feed(
        json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32002, "message": "不存在"}})
        + "\n"
    )

    with pytest.raises(RpcCallError) as exc:
        client.call("session.messages", {"session_id": "ses_nope"})
    assert exc.value.code == -32002
    assert "不存在" in str(exc.value)


def test_timeout_raises_with_clear_message() -> None:
    proc = FakeProcess()
    client = _client(proc)
    with pytest.raises(RpcCallError, match="超时"):
        client.call("system.health", timeout=0.2)


def test_eof_raises() -> None:
    proc = FakeProcess()
    client = _client(proc)
    proc.stdout.eof()
    with pytest.raises(RpcCallError, match="已退出"):
        client.call("system.health", timeout=2.0)


def test_call_before_start_is_rejected() -> None:
    client = RpcClient(["fake"], spawn=lambda command: FakeProcess())
    with pytest.raises(RpcCallError, match="未启动"):
        client.call("system.health")


def test_call_after_close_is_rejected() -> None:
    proc = FakeProcess()
    client = _client(proc)
    client.close()
    assert proc.stdin.closed
    assert proc.returncode == 0
    with pytest.raises(RpcCallError, match="未启动"):
        client.call("system.health")


def test_close_terminates_stubborn_process() -> None:
    class Stubborn(FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            raise TimeoutError("不肯退")

    proc = Stubborn()
    client = _client(proc)
    client.close(timeout=0.05)
    assert proc.terminated, "不肯退的子进程必须被 terminate"


def test_dead_process_is_reported_without_writing() -> None:
    proc = FakeProcess()
    client = _client(proc)
    proc.returncode = 3  # 进程已死
    with pytest.raises(RpcCallError, match="已退出"):
        client.call("system.health")


def test_default_command_matches_manual_rpc_calls() -> None:
    command = default_command(db_path="run/state.db", toolsets="research")
    assert command[0] == sys.executable
    assert command[1:4] == ["-m", "scholarscout", "--rpc"]
    assert command[-2:] == ["--db", "run/state.db"]


def test_default_env_puts_src_on_pythonpath() -> None:
    env = default_env()
    src = Path(__file__).resolve().parents[2] / "src"
    assert str(src) in env["PYTHONPATH"].split(";")[0] or str(src) in env["PYTHONPATH"]
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_real_backend_roundtrip(tmp_path: Path) -> None:
    """真的起一个 ``--rpc`` 子进程：壳与后端能对上话（这是桌面壳的地基）。"""
    env = {**default_env(), "SCHOLARSCOUT_HOME": str(tmp_path)}
    client = RpcClient(default_command(db_path=tmp_path / "state.db"), env=env, timeout=60)
    client.start()
    try:
        health = client.call("system.health")
        assert health["ok"] is True
        assert set(health["counts"]) >= {"sources", "documents", "sessions"}

        created = client.call("session.create", {"title": "桌面壳自测"})
        assert str(created["session_id"]).startswith("ses_")

        listed = client.call("session.list", {})
        assert any(row["session_id"] == created["session_id"] for row in listed["items"])
    finally:
        client.close()
    assert not client.alive
