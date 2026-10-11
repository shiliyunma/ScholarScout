"""契约测试：``chat.subtask_start`` / ``chat.subtask_done`` 必须是**一字不差的 notification 方法名**。

为什么单列这个文件（对齐 ``docs/01 §4`` 事件表与 ``§5.17``）：

``rpc/server.make_emitter`` 对**不在** ``NOTIFICATION_METHODS`` 里的 kind 会自动加 ``chat.`` 前缀 ——
漏登记一个新事件，就会把 ``chat.subtask_start`` 变成 ``chat.chat.subtask_start``，
**界面永远收不到、而且门禁全绿**（静默降级）。所以这里把"事件名 → notification 方法名"的映射钉死，
并把既有事件一并回归，防止改子任务时误伤 ``chat.delta``。
"""

from __future__ import annotations

from typing import Any

from scholarscout.rpc.server import NOTIFICATION_METHODS, make_emitter

#: 子 Agent P1 新增的两类事件（docs/01 §4 / §5.17）
SUBTASK_EVENTS = ("chat.subtask_start", "chat.subtask_done")

#: 引入子任务前就存在的通知（回归：不能被改动或改名）
EXISTING_EVENTS = ("chat.delta", "chat.tool_call", "chat.tool_result", "chat.done", "chat.error")

#: 抽背「不会」的流式增量：**单段增量**（与 chat.delta 的累计正文不同），前端追加显示
PRACTICE_EVENTS = ("practice.delta",)


def _collect() -> tuple[list[dict[str, Any]], Any]:
    written: list[dict[str, Any]] = []
    return written, make_emitter(written.append)


def test_subtask_events_are_registered_as_notifications() -> None:
    for event in SUBTASK_EVENTS:
        assert event in NOTIFICATION_METHODS, f"{event} 没登记进 NOTIFICATION_METHODS"


def test_practice_delta_is_registered_as_a_notification() -> None:
    """漏登记会被 make_emitter 加上 `chat.` 前缀（`chat.practice.delta`）→ 界面永远收不到。"""
    for event in PRACTICE_EVENTS:
        assert event in NOTIFICATION_METHODS, f"{event} 没登记进 NOTIFICATION_METHODS"


def test_emitter_writes_practice_delta_verbatim() -> None:
    written, emit = _collect()
    emit("practice.delta", {"note_id": "not_1", "card_index": 1, "delta": "半"})
    assert written[0]["method"] == "practice.delta"
    assert written[0]["params"] == {"note_id": "not_1", "card_index": 1, "delta": "半"}


def test_existing_events_still_registered() -> None:
    """子任务的加入**不动 chat.delta**，也不许把既有事件挤掉。"""
    for event in EXISTING_EVENTS:
        assert event in NOTIFICATION_METHODS


def test_emitter_writes_subtask_events_verbatim() -> None:
    """事件名原样写出（不出现 ``chat.chat.*`` 的双前缀），载荷原样透传。"""
    written, emit = _collect()

    emit("chat.subtask_start", {"turn_id": "trn_1", "seq": 0, "index": 0, "goal": "g"})
    emit(
        "chat.subtask_done",
        {"turn_id": "trn_1", "seq": 0, "index": 0, "stop_reason": "completed", "refs": {}},
    )

    methods = [item["method"] for item in written]
    assert methods == list(SUBTASK_EVENTS)
    assert all(not method.startswith("chat.chat.") for method in methods)
    # 载荷按契约键原样透传（界面靠这些键渲染子任务进度）
    assert written[0]["params"] == {"turn_id": "trn_1", "seq": 0, "index": 0, "goal": "g"}
    assert written[1]["params"]["stop_reason"] == "completed"
    assert written[1]["params"]["refs"] == {}
    assert written[0]["jsonrpc"] == "2.0"
