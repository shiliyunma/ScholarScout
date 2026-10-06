"""`serve()` 必须并发 —— 否则一轮对话在跑时，界面点什么都没反应。

真机症状：模型生成过程中点资料库条目没有反应；「停止」同理会排在队尾（等于失效）。
旧的实现是"读一行 → 分发 → 写回"的单线程循环，一轮 `chat.send` 阻塞 3–12 分钟，
后面的行根本读不到。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from scholarscout.rpc.methods import AppContext
from scholarscout.rpc.server import serve


class Recorder:
    """线程安全的假 stdout：记录写出的行，攒够 N 行就放开事件。"""

    def __init__(self, want: int = 2) -> None:
        self._lock = threading.Lock()
        self.lines: list[str] = []
        self._event = threading.Event()
        self._want = want

    def write(self, text: str) -> None:
        with self._lock:
            self.lines.append(text)
            if len(self.lines) >= self._want:
                self._event.set()

    def flush(self) -> None:
        return None

    def wait(self, timeout: float = 3.0) -> list[str]:
        self._event.wait(timeout)
        with self._lock:
            return list(self.lines)


def _request(request_id: int, method: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method}) + "\n"


def _serve_in_thread(
    ctx: AppContext, lines: list[str], out: Recorder, dispatcher: Any
) -> threading.Thread:
    thread = threading.Thread(
        target=serve,
        kwargs={"ctx": ctx, "stdin": iter(lines), "stdout": out, "dispatcher": dispatcher},
        daemon=True,
    )
    thread.start()
    return thread


def test_slow_request_does_not_block_a_fast_one(ctx: AppContext) -> None:
    """先发的慢请求不该压住后发的快请求（响应乱序是 JSON-RPC 允许的）。"""
    slow_started = threading.Event()

    def fake_dispatch(_ctx: AppContext, name: str, _params: Any) -> dict[str, Any]:
        if name == "slow.method":
            slow_started.set()
            time.sleep(0.6)
            return {"which": "slow"}
        return {"which": "fast"}

    out = Recorder(want=2)
    _serve_in_thread(
        ctx, [_request(1, "slow.method"), _request(2, "fast.method")], out, fake_dispatch
    )
    lines = out.wait()
    ids = [json.loads(line)["id"] for line in lines]
    assert slow_started.is_set()
    assert ids == [2, 1], "快的必须先回；顺序相反说明还是串行"


def test_request_arriving_mid_flight_is_answered(ctx: AppContext) -> None:
    """模拟真机：一轮长任务在跑，中途才发来的取消请求也必须被处理。"""
    in_flight = threading.Event()
    got_cancel = threading.Event()

    def fake_dispatch(_ctx: AppContext, name: str, _params: Any) -> dict[str, Any]:
        if name == "chat.send":
            in_flight.set()
            time.sleep(0.6)
            return {"stop_reason": "cancelled"}
        if name == "chat.cancel":
            got_cancel.set()
            return {"ok": True}
        return {}

    # 第二个请求通过一个"只在长任务开始后才产出"的迭代器给出
    def stream() -> Any:
        yield _request(1, "chat.send")
        in_flight.wait(timeout=3.0)
        yield _request(2, "chat.cancel")

    out = Recorder(want=2)
    thread = threading.Thread(
        target=serve,
        kwargs={"ctx": ctx, "stdin": stream(), "stdout": out, "dispatcher": fake_dispatch},
        daemon=True,
    )
    thread.start()
    lines = out.wait()
    assert got_cancel.is_set(), "长任务期间的取消请求必须立刻被处理（否则「停止」永远不生效）"
    assert sorted(json.loads(line)["id"] for line in lines) == [1, 2]


def test_serve_returns_when_input_ends_even_with_a_task_running(ctx: AppContext) -> None:
    """关窗时不能卡住：stdin 结束就该返回，不等长任务跑完（工作线程是 daemon）。"""

    def fake_dispatch(_ctx: AppContext, _name: str, _params: Any) -> dict[str, Any]:
        time.sleep(2.0)
        return {}

    out = Recorder(want=1)
    thread = _serve_in_thread(ctx, [_request(1, "chat.send")], out, fake_dispatch)
    thread.join(timeout=1.0)
    assert not thread.is_alive(), "stdin 结束后 serve() 应立即返回"


def test_notifications_are_emitted_through_the_locked_writer(ctx: AppContext) -> None:
    """事件（通知）也走同一把写锁：行长完整、不与响应交叉撕裂。"""

    def fake_dispatch(_ctx: AppContext, _name: str, _params: Any) -> dict[str, Any]:
        assert _ctx.emit is not None
        _ctx.emit("chat.delta", {"seq": 1, "text": "半句"})
        return {"ok": True}

    out = Recorder(want=2)
    _serve_in_thread(ctx, [_request(7, "chat.send")], out, fake_dispatch)
    lines = out.wait()
    parsed = [json.loads(line) for line in lines]
    assert any(frame.get("method") == "chat.delta" for frame in parsed)
    assert any(frame.get("id") == 7 for frame in parsed)
