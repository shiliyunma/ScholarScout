"""单轮对话循环的**上下文与事件原语**（``turn_steps`` / ``loop`` 的共同底座）。

为什么单独成模块：``turn_steps.py`` 里"做一件具体事"的步骤与"这一轮是什么、事件往哪发"
是两件事 —— 后者被编排层、步骤层、会话运行时共同使用，且不随步骤增删变化。
把原语单独放一处，步骤文件才能只讲步骤。

分层铁律：本模块**不导入** ``scholarscout.rpc``，也不导入 ``loop`` / ``turn_steps``
（依赖方向只能是 ``loop → turn_steps → turn_context``）。这里不持有任何全局可变状态。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from scholarscout.config import Config
from scholarscout.fetch.http import HttpClient
from scholarscout.providers.llm import LlmClient
from scholarscout.runtime import registry
from scholarscout.runtime.toolctx import ToolContext, TurnBudget
from scholarscout.state.db import Database

__all__ = [
    "Emitter",
    "TurnContext",
    "TurnResult",
    "bind_emitter",
    "emit_event",
    "merge_refs",
]


#: 事件回调：``emit(kind, payload)``（RPC 层把 chat.* 事件转成通知）
Emitter = Callable[[str, dict[str, Any]], None]


@dataclass
class TurnContext:
    db: Database
    config: Config
    llm: LlmClient
    http: HttpClient | None = None
    cancelled: set[str] = field(default_factory=set)
    #: `session_id -> turn_id`：这个会话此刻正在跑哪一轮。
    #: 为什么需要它：界面在整轮结束前**拿不到 turn_id**（它随 `chat.done` 才回来），
    #: 所以\"停止\"必须能按会话取消，而不是按 turn_id（否则这个 RPC 从设计上就没法被界面调用）。
    running: dict[str, str] = field(default_factory=dict)
    #: 本轮的取消信号（**每轮一个独立 Event**，docs/20 §6.2）。
    #: 多会话并行时由 `SessionRuntime` 按轮注入；`None`（CLI / 老调用点）= 只看 `cancelled` 集合，
    #: 于是改造前的语义一字不变。
    cancel_event: threading.Event | None = None

    def tools(self, toolsets: Sequence[str] | None) -> list[registry.ToolSpec]:
        return registry.get_tools(toolsets, features=self.config.features)

    def is_cancelled(self, turn_id: str) -> bool:
        """这一轮被取消了吗（步间检查）。

        两条通道都认：**每轮的 Event**（并行下的机制保证，只可能点到这一轮）与
        `cancelled` 集合（按 turn_id 的兼容通道；turn_id 全局唯一，所以不会误伤别的会话）。
        """
        if self.cancel_event is not None and self.cancel_event.is_set():
            return True
        return turn_id in self.cancelled

    def tool_context(
        self,
        *,
        session_id: str,
        turn_id: str,
        budget: TurnBudget | None = None,
        emit: Emitter | None = None,
    ) -> ToolContext:
        """给工具用的上下文（注入库、配置、出网、模型与本次会话/轮次标识）。

        ``budget`` 是本轮的工具预算账（:class:`scholarscout.runtime.toolctx.TurnBudget`）——
        子任务工具（``research.delegate``）据此**从主预算里扣**；``emit`` 让它能发 ``chat.subtask_*``。
        """
        return ToolContext(
            db=self.db,
            config=self.config,
            http=self.http,
            llm=self.llm,
            session_id=session_id,
            turn_id=turn_id,
            budget=budget,
            emit=emit,
        )


@dataclass
class TurnResult:
    turn_id: str
    session_id: str
    message_id: str
    text: str
    stop_reason: str
    tool_calls: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    refs: dict[str, list[str]] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "text": self.text,
            "stop_reason": self.stop_reason,
            "tool_calls": self.tool_calls,
            "usage": self.usage,
            "refs": self.refs,
            "error": self.error,
        }


def merge_refs(target: dict[str, list[str]], incoming: Any) -> None:
    """把各次工具调用返回的 refs 归并去重（前端靠它显示\"这一次生成了什么\"，docs/01 §4）。"""
    if not isinstance(incoming, dict):
        return
    for key, values in incoming.items():
        if not isinstance(values, list):
            continue
        bucket = target.setdefault(str(key), [])
        for value in values:
            text = str(value)
            if text not in bucket:
                bucket.append(text)


def bind_emitter(emit: Emitter | None, session_id: str | None) -> Emitter | None:
    """把 ``session_id`` 绑进事件信封 —— 让\"通知必带 session_id\"由**机制**保证（docs/20 §3.1）。

    为什么要有：事件不带 ``session_id`` 时，界面无从判断一条 ``chat.delta`` 属于哪个会话
    （真机症状：切到 B 后 A 的回复写进 B 的气泡）。把绑定做在 emitter 上，而不是要求 8 处
    ``emit_event`` 调用点各自记得写，才不会有人漏。多绑一次无害（键值相同）。

    ``emit`` 为 ``None``（CLI / 无 GUI 的测试）时**原样返回 None**：包一层空回调只会让
    调用点误以为有事件通道。``session_id`` 为空时同样不绑 —— 绑一个空串只会伪造\"合规\"。
    """
    if emit is None or not session_id:
        return emit

    def bound(kind: str, payload: dict[str, Any]) -> None:
        # 调用点自己的键优先（如子任务里更精确的 turn_id），但 session_id 以绑定值为准
        emit(kind, {**payload, "session_id": session_id})

    return bound


def emit_event(emit: Emitter | None, kind: str, payload: dict[str, Any]) -> None:
    if emit is not None:
        emit(kind, payload)
