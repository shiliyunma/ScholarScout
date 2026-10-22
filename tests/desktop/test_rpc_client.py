"""RpcClient 契约：帧格式、通知转发、错误与超时、真实子进程往返。"""

from __future__ import annotations

import io
import json
import queue
import sys
import time
from pathlib import Path

import pytest

from scholarscout.desktop.rpc_client import (
    DEFAULT_TIMEOUT,
    LONG_CALL_TIMEOUT_ENV,
    LONG_CALL_TIMEOUTS,
    RpcCallError,
    RpcClient,
    default_command,
    default_env,
    resolve_timeout,
)
from scholarscout.rpc.codes import KNOWN_CODES, Code


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


def test_unknown_error_code_never_leaks_out_of_the_shell() -> None:
    """后端回了个**表外的码**（或没给码）：壳层只能回 `rpc/codes.py` 里的兜底常量。

    为什么必须收敛：前端按 `error.code` 分支，透一个它不认识的码过去等于让错误消失；
    而壳自己写的默认值也必须来自常量表（裸数字一旦和码表脱钩，改口径就会漏一处）。
    """
    assert RpcCallError("壳自己报的错").code == int(Code.NOT_CONFIGURED), (
        "壳层兜底码必须来自 rpc/codes.py"
    )

    for error in ({"code": -99999, "message": "野码"}, {"message": "连码都没有"}):
        proc = FakeProcess()
        client = _client(proc)
        proc.stdout.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "error": error}) + "\n")
        with pytest.raises(RpcCallError) as exc:
            client.call("system.health")
        assert exc.value.code == int(Code.NOT_CONFIGURED), f"{error} 应兜底成 {Code.NOT_CONFIGURED}"
        assert exc.value.code in KNOWN_CODES


def test_timeout_raises_with_clear_message() -> None:
    proc = FakeProcess()
    client = _client(proc)
    with pytest.raises(RpcCallError, match="超时") as exc:
        client.call("system.health", timeout=0.2)
    # 文案里的秒数必须是**这次调用传的值**，不是构造时的 5.0、也不是任何硬编码。
    assert "（0.2 秒）" in str(exc.value)


def test_eof_raises() -> None:
    proc = FakeProcess()
    client = _client(proc)
    proc.stdout.eof()
    with pytest.raises(RpcCallError, match="已退出"):
        client.call("system.health", timeout=0.3)


def test_eof_before_call_is_registered_never_lands_on_timeout() -> None:
    """读线程先读完 EOF、调用方才登记信箱：必须报"已退出"，不能白等到超时。

    真机上这个顺序完全可能发生（后端在读线程里先读到输出结束，前端那一刻才发出调用）。
    早期实现只在 EOF 那一刻给"当时已登记"的信箱投递，晚一步登记的调用就拿不到任何东西，
    只能靠超时兜底 —— 于是"后端已经退出了"被报成"等待后端响应超时（2 秒）"，
    机器一忙（线程调度被挤）就会偶发红。
    """
    proc = FakeProcess()
    client = _client(proc)
    proc.stdout.eof()
    # 等读线程真的把"输出结束"锁存完 —— 于是这次调用一定发生在 EOF 分派之后（顺序固定，不靠 sleep）
    assert client._eof.wait(5.0), "读线程应已把『输出结束』锁存下来"
    started = time.monotonic()
    with pytest.raises(RpcCallError, match="已退出"):
        client.call("system.health", timeout=5.0)
    assert time.monotonic() - started < 2.0, "发现'已退出'不该靠等满 5 秒超时"


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


# ---------------------------------------------------------------------------
# 长轮次超时（2026-10-12 补）：``chat.send`` 不是"一次动作"，而是**一整轮**
# （十几次工具 + 若干次模型调用，真机 3–12 分钟）。180s 的默认上限会在后端已经跑完十几步、
# 正在写库的时候把整轮丢掉，界面按 `RpcCallError` 误判"调用失败"，用户白等一场。
# 下面三条钉住"按方法给预算"这件事，并留一个可配的口子。
# ---------------------------------------------------------------------------


def test_chat_send_gets_a_whole_turn_budget_while_short_methods_keep_the_default() -> None:
    assert resolve_timeout("chat.send", DEFAULT_TIMEOUT, None) == LONG_CALL_TIMEOUTS["chat.send"]
    assert LONG_CALL_TIMEOUTS["chat.send"] >= 1800.0, "一整轮的宽限至少得是 30 分钟级"
    assert resolve_timeout("session.list", DEFAULT_TIMEOUT, None) == DEFAULT_TIMEOUT, (
        "列表类调用不该跟着一起等半小时"
    )
    assert resolve_timeout("chat.send", DEFAULT_TIMEOUT, 12.0) == 12.0, "显式传入永远优先"


def test_long_call_budget_is_configurable_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LONG_CALL_TIMEOUT_ENV, "42")
    assert resolve_timeout("chat.send", DEFAULT_TIMEOUT, None) == 42.0


def test_broken_env_override_falls_back_to_the_built_in_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配错了就退回内置值：宁可等久，也不能让每一次对话都立刻判失败。"""
    monkeypatch.setenv(LONG_CALL_TIMEOUT_ENV, "很久")
    assert resolve_timeout("chat.send", DEFAULT_TIMEOUT, None) == LONG_CALL_TIMEOUTS["chat.send"]
    monkeypatch.setenv(LONG_CALL_TIMEOUT_ENV, "0")
    assert resolve_timeout("chat.send", DEFAULT_TIMEOUT, None) == 1.0, "非正数夹到 1 秒"


def test_chat_send_waits_the_resolved_budget_over_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不传 timeout 时它等的是**解析出来的那个值**（不是构造时的默认值）—— 从超时文案上看得见。"""
    monkeypatch.setenv(LONG_CALL_TIMEOUT_ENV, "1")
    proc = FakeProcess()
    client = _client(proc)  # 构造时的默认是 5.0s
    with pytest.raises(RpcCallError) as info:
        client.call("chat.send", {"text": "嗨"})
    assert "1 秒" in str(info.value), f"应是长轮次预算，实为：{info.value}"
