"""单轮对话循环的**步骤实现**（``loop.run_turn`` 的被调用方）。

为什么单独成模块：``loop.py`` 只留编排（顺序 / 条件 / 返回值），每个\"做一件具体事\"的步骤
都在这里 —— 改一处步骤（比如记账口径）不必在近千行的编排里翻找，也避免编排文件再长出第二个职责。

分层铁律：本模块**不导入** ``scholarscout.rpc``。事件只通过 ``emit`` 回调向上冒泡，
依赖（``ctx`` / ``db`` / ``session_id`` / ``turn_id`` / ``emit``）一律**显式传入**，
本模块不持有任何全局可变状态。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from scholarscout.config import Config
from scholarscout.fetch.http import HttpClient
from scholarscout.providers.llm import ChatReply, LlmClient, ToolCall
from scholarscout.runtime import context_budget, registry
from scholarscout.runtime.toolctx import ToolContext, TurnBudget
from scholarscout.state import sessions, traces
from scholarscout.state.db import Database

logger = logging.getLogger(__name__)

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


def call_model(
    ctx: TurnContext,
    messages: list[dict[str, Any]],
    *,
    stream_enabled: bool,
    tools: list[dict[str, Any]] | None,
    emit: Emitter | None,
    session_id: str,
    turn_id: str,
    deltas_before: int,
    usage_total: dict[str, int],
) -> tuple[ChatReply, int, str]:
    """调一次模型（开流式就边收边推 ``chat.delta``），并把这次调用的钱立即记账。

    返回 ``(reply, 累计已推的增量条数, 本轮收到的流式正文)``。
    """
    streamed_text = ""
    deltas_sent = deltas_before
    if stream_enabled:
        # 这一轮可以是正文（最终答案/前言），也可以是工具调用 —— 两条都走流式：
        # 只流正文增量，工具调用照原样从 `reply.tool_calls` 拿。

        def _on_delta(piece: str) -> None:
            nonlocal streamed_text, deltas_sent
            streamed_text += piece
            deltas_sent += 1
            # 推**累计**正文：前端 chat.delta 的处理是"替换正文"，累计值才能直接接管。
            emit_event(
                emit,
                "chat.delta",
                {
                    "turn_id": turn_id,
                    "seq": deltas_sent,
                    "kind": "text",
                    "text": streamed_text,
                },
            )

        reply = ctx.llm.chat_stream(messages, tools=tools, on_delta=_on_delta)
    else:
        reply = ctx.llm.chat(messages, tools=tools)
    accumulate_usage(usage_total, reply)
    # **每次模型调用立即记账**：原来只在整轮结束记一次，结果是"被停止或中断的轮次
    # 把已经花掉的 token 全丢了"（真机实测：杀掉实例后该会话用量一条不剩）。
    # 代价敏感的用户看到的会话累计必须包含花掉的钱，哪怕这一轮被中途取消。
    traces.record_usage(
        ctx.db,
        model=ctx.config.chat.model,
        purpose="chat",
        session_id=session_id,
        turn_id=turn_id,
        prompt_tokens=reply.usage.get("prompt_tokens"),
        completion_tokens=reply.usage.get("completion_tokens"),
        cached_tokens=reply.usage.get("cached_tokens"),
    )
    return reply, deltas_sent, streamed_text


def append_tool_call_request(
    ctx: TurnContext, *, session_id: str, reply: ChatReply, messages: list[dict[str, Any]]
) -> None:
    """把模型这一轮的 tool_calls 落库，并写进本轮消息（下一轮模型与回放都要看到它）。"""
    assistant_row = sessions.append_message(
        ctx.db,
        session_id,
        role="assistant",
        content=reply.content or "",
        tool_calls_json=json.dumps(
            [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in reply.tool_calls
            ],
            ensure_ascii=False,
        ),
    )
    messages.append(
        {
            "role": "assistant",
            "content": reply.content or "",
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in reply.tool_calls
            ],
        }
    )
    del assistant_row  # 已落库，无需再用


def accumulate_usage(total: dict[str, int], reply: ChatReply) -> None:
    for key, value in reply.usage.items():
        total[key] = total.get(key, 0) + int(value)


def query_from_arguments(arguments: str) -> str:
    """从工具调用的实参里取 query（取不到回空串 —— 记账只认确定的东西）。"""
    try:
        payload = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("query") or "").strip()


def execute_tool(
    ctx: TurnContext,
    session_id: str,
    turn_id: str,
    step: int,
    call: ToolCall,
    emit: Emitter | None,
    *,
    budget: TurnBudget | None = None,
    seen_source_ids: set[str] | None = None,
) -> tuple[str, dict[str, Any], str, bool]:
    """执行一次工具调用，返回（喂回模型的文本, 该工具报出的 refs, 规范工具名, 是否成功）。

    `ok` 单独带出来是给 A3 记账用的：工具**可以不抛异常而回"我失败了"**（例如所有检索来源都挂了），
    那种情况不该把查询记进台账（否则一次网络抖动就把这个方向永久锁死）。

    失败也要如实回话并记 trace；规范名用于事件、轨迹与对外结果（线上名只在 API 报文里用）。
    """
    spec = registry.get_spec(call.name)
    # 模型回的是**线上名**（research__search），落到 trace/事件里一律用规范名（research.search），
    # 这样 docs/01 的方法名、指标的取数口径与论文里的名字始终是一份
    canonical = spec.name if spec is not None else call.name
    emit_event(
        emit,
        "chat.tool_call",
        {
            "turn_id": turn_id,
            "seq": step,
            "call_id": call.id,
            "tool": canonical,
            "args_digest": traces.digest(call.arguments),
            "args_preview": call.arguments[:200],
        },
    )
    if spec is None:
        message = f"工具 {call.name} 未注册（当前阶段可用工具集为空）"
        traces.record_trace(
            ctx.db,
            session_id=session_id,
            turn_id=turn_id,
            step=step,
            tool_name=canonical,
            ok=False,
            args=call.arguments,
            error="tool_not_registered",
        )
        emit_event(
            emit,
            "chat.tool_result",
            {"turn_id": turn_id, "seq": step, "call_id": call.id, "ok": False, "refs": {}},
        )
        return message, {}, canonical, False

    import time

    tool_ctx = ctx.tool_context(session_id=session_id, turn_id=turn_id, budget=budget, emit=emit)
    started = time.perf_counter()
    try:
        arguments = call.parsed_arguments()
        result = registry.invoke(spec, tool_ctx, arguments)
        latency_ms = int((time.perf_counter() - started) * 1000)
        traces.record_trace(
            ctx.db,
            session_id=session_id,
            turn_id=turn_id,
            step=step,
            tool_name=canonical,
            ok=True,
            args=arguments,
            result=result,
            latency_ms=latency_ms,
        )
        refs = result.get("refs", {}) if isinstance(result, dict) else {}
        emit_event(
            emit,
            "chat.tool_result",
            {
                "turn_id": turn_id,
                "seq": step,
                "call_id": call.id,
                "ok": True,
                "digest": traces.digest(result),
                "latency_ms": latency_ms,
                "refs": refs,
            },
        )
        return (
            context_budget.project_tool_result(result, seen_source_ids=seen_source_ids),
            (refs if isinstance(refs, dict) else {}),
            canonical,
            # 工具可以不抛异常而回"我失败了"（例如所有检索来源都挂了）——
            # 判重记账必须按这个 ok 走，否则一次网络抖动就把某个方向永久锁死
            bool(result.get("ok", True)) if isinstance(result, dict) else True,
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        traces.record_trace(
            ctx.db,
            session_id=session_id,
            turn_id=turn_id,
            step=step,
            tool_name=canonical,
            ok=False,
            args=call.arguments,
            error=f"{type(exc).__name__}: {exc}",
            latency_ms=latency_ms,
        )
        emit_event(
            emit,
            "chat.tool_result",
            {"turn_id": turn_id, "seq": step, "call_id": call.id, "ok": False, "refs": {}},
        )
        return f"工具 {call.name} 执行失败：{exc}", {}, canonical, False
