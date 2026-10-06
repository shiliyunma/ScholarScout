"""并发调用：**一轮对话在跑时，界面还得能点资料库预览、点「停止」**。

真机症状（用户报的）：模型生成过程中点资料库里的条目没有任何反应。
根因两层，两层都要用测试钉住：

1. 后端 `serve()` 原本是"读一行 → 分发 → 写回"的单线程循环 ——
   一轮 `chat.send` 阻塞 3–12 分钟，后面的请求根本读不到；
2. 桌面桥 `RpcClient.call` 原本把锁握到整个等待结束，且 `_await` 会**丢弃**别的 id 的响应 ——
   并发调用必然串台或超时。

这两条同时还能解释另一个更严重的现象：**「停止」按钮其实从来没生效过**（它同样排在队尾）。
"""

from __future__ import annotations

import io
import json
import queue
import threading
import time

from scholarscout.desktop.rpc_client import RpcClient


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

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0


def _client(proc: FakeProcess, *, timeout: float = 5.0) -> RpcClient:
    client = RpcClient(["fake"], timeout=timeout, spawn=lambda command: proc)
    client.start()
    return client


def _sent_frames(proc: FakeProcess) -> list[dict[str, object]]:
    return [json.loads(line) for line in proc.stdin.getvalue().splitlines() if line.strip()]


def _wait_for_frames(
    proc: FakeProcess, count: int, *, timeout: float = 3.0
) -> list[dict[str, object]]:
    """等后端收到 count 个请求（避免用 sleep 赌时序，测试要确定）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frames = _sent_frames(proc)
        if len(frames) >= count:
            return frames
        time.sleep(0.02)
    raise AssertionError(f"只等到 {len(_sent_frames(proc))} 个请求，期望 {count} 个")


def test_two_overlapping_calls_each_get_their_own_response() -> None:
    """后一个调用不该等前一个 —— 这是"生成中点预览没反应"的根因。"""
    proc = FakeProcess()
    client = _client(proc)
    results: dict[str, object] = {}

    def slow_call() -> None:
        results["slow"] = client.call("chat.send", {"session_id": "ses_1", "text": "查一下"})

    def fast_call() -> None:
        results["fast"] = client.call("archive.get", {"doc_id": "doc_1"})

    slow = threading.Thread(target=slow_call)
    slow.start()
    _wait_for_frames(proc, 1)  # slow 已发出，且会一直等（后端还没回）
    fast = threading.Thread(target=fast_call)
    fast.start()

    # 两个请求都应已写到后端（旧的串行实现里第二个请求根本发不出去）
    frames = _wait_for_frames(proc, 2)
    assert [frame["method"] for frame in frames] == ["chat.send", "archive.get"]

    # 后端**倒序**回：快的先回。各自按 id 收，互不串台。
    proc.stdout.feed(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"doc": "ok"}}) + "\n")
    proc.stdout.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"text": "慢慢来"}}) + "\n")

    fast.join(timeout=3.0)
    slow.join(timeout=3.0)
    assert results == {"fast": {"doc": "ok"}, "slow": {"text": "慢慢来"}}


def test_events_are_dispatched_while_a_call_is_waiting() -> None:
    """等自己的响应期间，别人推来的通知（chat.delta 等）必须照常回调。"""
    proc = FakeProcess()
    events: list[tuple[str, dict]] = []
    client = RpcClient(
        ["fake"],
        timeout=5.0,
        on_event=lambda name, params: events.append((name, params)),
        spawn=lambda command: proc,
    )
    client.start()
    done: dict[str, object] = {}

    def call() -> None:
        done["result"] = client.call("chat.send", {"session_id": "ses_1", "text": "x"})

    thread = threading.Thread(target=call)
    thread.start()
    time.sleep(0.1)
    proc.stdout.feed(
        json.dumps({"jsonrpc": "2.0", "method": "chat.delta", "params": {"seq": 1}}) + "\n"
    )
    proc.stdout.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}) + "\n")
    thread.join(timeout=3.0)
    assert done["result"] == {"ok": True}
    assert events == [("chat.delta", {"seq": 1})]


def test_response_arriving_before_the_mailbox_is_still_delivered() -> None:
    """响应先到（测试预置 / 后端极快）也要能取到，不能丢。"""
    proc = FakeProcess()
    client = _client(proc)
    proc.stdout.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "result": 42}) + "\n")
    time.sleep(0.15)
    assert client.call("system.health") == 42


def test_backend_error_on_one_call_does_not_disturb_the_other() -> None:
    """一个调用报错，不该把另一个调用也带崩（错误只投给自己的信箱）。"""
    proc = FakeProcess()
    client = _client(proc)
    outcomes: dict[str, object] = {}

    def good() -> None:
        outcomes["good"] = client.call("system.health")

    def bad() -> None:
        try:
            client.call("archive.get", {"doc_id": "nope"})
        except Exception as exc:
            outcomes["bad"] = str(exc)

    threads = [threading.Thread(target=good), threading.Thread(target=bad)]
    for thread in threads:
        thread.start()
    # 线程抢 id，顺序不确定 → 按方法名反查各自的 id 再回，测试才是确定的
    by_method = {str(frame["method"]): int(frame["id"]) for frame in _wait_for_frames(proc, 2)}
    proc.stdout.feed(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": by_method["archive.get"],
                "error": {"code": -32004, "message": "未找到"},
            }
        )
        + "\n"
    )
    proc.stdout.feed(
        json.dumps({"jsonrpc": "2.0", "id": by_method["system.health"], "result": {"ok": True}})
        + "\n"
    )
    for thread in threads:
        thread.join(timeout=3.0)
    assert outcomes == {"good": {"ok": True}, "bad": "未找到"}
