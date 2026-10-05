"""单轮对话循环。

流程（docs/01 §3.2 的 ``chat.send`` 语义）：

    落用户消息 → 组上下文 → 调模型 → 执行工具（若有）→ 落助手消息 → 记用量 → 上报事件

约束：
- 工具调用次数受 ``runtime.max_tool_calls_per_turn`` 限制，超限 ``stop_reason="tool_limit"``
- 轮次预算 = 工具预算 + 1：最后一轮**不给工具**，留给模型把结果讲完（用满工具预算≠tool_limit）
- 模型返回了未注册的工具 → 记 trace（``ok=0``）并如实告诉模型，**不伪造结果**
- 助手消息与工具消息都落库，保证会话可回放
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from scholarscout.config import Config
from scholarscout.fetch.http import HttpClient
from scholarscout.providers.llm import ChatReply, LlmClient, LlmError, ToolCall
from scholarscout.runtime import registry
from scholarscout.runtime.prompts import build_system_prompt, system_prompt_hash
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state import sessions, traces
from scholarscout.state.db import Database

#: 事件回调：``emit(kind, payload)``（RPC 层把 chat.* 事件转成通知）
Emitter = Callable[[str, dict[str, Any]], None]

_HISTORY_LIMIT = 40


@dataclass
class TurnContext:
    db: Database
    config: Config
    llm: LlmClient
    http: HttpClient | None = None
    cancelled: set[str] = field(default_factory=set)

    def tools(self, toolsets: Sequence[str] | None) -> list[registry.ToolSpec]:
        return registry.get_tools(toolsets, features=self.config.features)

    def is_cancelled(self, turn_id: str) -> bool:
        return turn_id in self.cancelled

    def tool_context(self, *, session_id: str, turn_id: str) -> ToolContext:
        """给工具用的上下文（注入库、配置、出网、模型与本次会话/轮次标识）。"""
        return ToolContext(
            db=self.db,
            config=self.config,
            http=self.http,
            llm=self.llm,
            session_id=session_id,
            turn_id=turn_id,
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


def _merge_refs(target: dict[str, list[str]], incoming: Any) -> None:
    """把各次工具调用返回的 refs 归并去重（前端靠它显示"这一次生成了什么"，docs/01 §4）。"""
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


def _emit(emit: Emitter | None, kind: str, payload: dict[str, Any]) -> None:
    if emit is not None:
        emit(kind, payload)


def build_messages(
    db: Database,
    session_id: str,
    *,
    config: Config,
    toolsets: Sequence[str] | None,
) -> list[dict[str, Any]]:
    """系统提示 + 会话历史（只取有内容的用户/助手消息）。"""
    prompt = build_system_prompt(
        toolsets=list(toolsets or ["research"]),
        allow_clarify=config.runtime.allow_clarify,
    )
    messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
    history, _ = sessions.list_messages(db, session_id, limit=_HISTORY_LIMIT)
    for row in history:
        if row["role"] in {"user", "assistant"} and row["content"].strip():
            messages.append({"role": row["role"], "content": row["content"]})
    return messages


def run_turn(
    ctx: TurnContext,
    *,
    session_id: str,
    text: str,
    toolsets: Sequence[str] | None = None,
    emit: Emitter | None = None,
) -> TurnResult:
    """跑一轮：发消息、调模型、执行工具、落库。异常一律收敛成 ``stop_reason``。"""
    turn_id = sessions.new_id("trn")
    sessions.append_message(ctx.db, session_id, role="user", content=text)

    specs = ctx.tools(toolsets)
    messages = build_messages(ctx.db, session_id, config=ctx.config, toolsets=toolsets)
    tool_decls = registry.openai_tools(specs) or None

    executed: list[str] = []
    usage_total: dict[str, int] = {}
    stop_reason = "completed"
    final_text = ""
    error_message: str | None = None

    # 轮次预算 = 工具预算 + 1：**最后那一轮不给工具**，专门留给模型把结果讲成人话。
    # 否则模型刚好用满工具预算时会被判 tool_limit，最终答案直接丢掉（踩过）。
    tool_budget = max(1, ctx.config.runtime.max_tool_calls_per_turn)
    refs_total: dict[str, list[str]] = {}
    try:
        for _round in range(tool_budget + 1):
            if ctx.is_cancelled(turn_id):
                stop_reason = "cancelled"
                break
            tools_for_round = None if len(executed) >= tool_budget else tool_decls
            reply = ctx.llm.chat(messages, tools=tools_for_round)
            _accumulate_usage(usage_total, reply)
            final_text = reply.content or final_text

            if not reply.tool_calls:
                break

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

            if len(executed) + len(reply.tool_calls) > tool_budget:
                stop_reason = "tool_limit"
                _emit(
                    emit,
                    "chat.error",
                    {"turn_id": turn_id, "code": -32603, "message": "工具调用超限"},
                )
                break

            for call in reply.tool_calls:
                result_text, tool_refs, canonical = _execute_tool(
                    ctx, session_id, turn_id, len(executed), call, emit
                )
                _merge_refs(refs_total, tool_refs)
                executed.append(canonical)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": result_text,
                    }
                )
                sessions.append_message(
                    ctx.db,
                    session_id,
                    role="tool",
                    content=result_text,
                    tool_call_id=call.id,
                    name=call.name,
                )
        else:
            stop_reason = "tool_limit"
    except LlmError as exc:
        stop_reason = "error"
        error_message = f"{exc.kind}: {exc}"
    except Exception as exc:
        stop_reason = "error"
        error_message = f"{type(exc).__name__}: {exc}"

    message_id = sessions.append_message(
        ctx.db,
        session_id,
        role="assistant",
        content=final_text,
        token_in=usage_total.get("prompt_tokens"),
        token_out=usage_total.get("completion_tokens"),
    )
    traces.record_usage(
        ctx.db,
        model=ctx.config.chat.model,
        purpose="chat",
        session_id=session_id,
        turn_id=turn_id,
        prompt_tokens=usage_total.get("prompt_tokens"),
        completion_tokens=usage_total.get("completion_tokens"),
    )
    _emit(emit, "chat.delta", {"turn_id": turn_id, "seq": 0, "kind": "text", "text": final_text})
    _emit(
        emit,
        "chat.done",
        {"turn_id": turn_id, "stop_reason": stop_reason, "usage": usage_total, "refs": refs_total},
    )
    return TurnResult(
        turn_id=turn_id,
        session_id=session_id,
        message_id=message_id,
        text=final_text,
        stop_reason=stop_reason,
        tool_calls=executed,
        usage=usage_total,
        refs=refs_total,
        error=error_message,
    )


def _accumulate_usage(total: dict[str, int], reply: ChatReply) -> None:
    for key, value in reply.usage.items():
        total[key] = total.get(key, 0) + int(value)


def _execute_tool(
    ctx: TurnContext,
    session_id: str,
    turn_id: str,
    step: int,
    call: ToolCall,
    emit: Emitter | None,
) -> tuple[str, dict[str, Any], str]:
    """执行一次工具调用，返回（喂回模型的文本, 该工具报出的 refs, 规范工具名）。

    失败也要如实回话并记 trace；规范名用于事件、轨迹与对外结果（线上名只在 API 报文里用）。
    """
    spec = registry.get_spec(call.name)
    # 模型回的是**线上名**（research__search），落到 trace/事件里一律用规范名（research.search），
    # 这样 docs/01 的方法名、指标的取数口径与论文里的名字始终是一份
    canonical = spec.name if spec is not None else call.name
    _emit(
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
        _emit(
            emit,
            "chat.tool_result",
            {"turn_id": turn_id, "seq": step, "call_id": call.id, "ok": False, "refs": {}},
        )
        return message, {}, canonical

    import time

    tool_ctx = ctx.tool_context(session_id=session_id, turn_id=turn_id)
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
        _emit(
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
            json.dumps(result, ensure_ascii=False),
            (refs if isinstance(refs, dict) else {}),
            canonical,
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
        _emit(
            emit,
            "chat.tool_result",
            {"turn_id": turn_id, "seq": step, "call_id": call.id, "ok": False, "refs": {}},
        )
        return f"工具 {call.name} 执行失败：{exc}", {}, canonical


def mark_system_prompt(
    db: Database, session_id: str, *, toolsets: Sequence[str] | None, config: Config
) -> str:
    """把当前系统提示词的哈希写进会话（改了提示词，历史就不可比）。"""
    prompt = build_system_prompt(
        toolsets=list(toolsets or ["research"]),
        allow_clarify=config.runtime.allow_clarify,
    )
    value = system_prompt_hash(prompt)
    db.execute("UPDATE sessions SET system_prompt_hash = ? WHERE id = ?", (value, session_id))
    return value
