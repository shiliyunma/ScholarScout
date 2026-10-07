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
from scholarscout.runtime import context_budget, registry
from scholarscout.runtime.prompts import build_system_prompt, system_prompt_hash
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state import sessions, traces
from scholarscout.state.db import Database

#: 事件回调：``emit(kind, payload)``（RPC 层把 chat.* 事件转成通知）
Emitter = Callable[[str, dict[str, Any]], None]

_HISTORY_LIMIT = 40
#: 历史正文的字符预算（默认 12,000 ≈ 4,000 tokens）。
#: 实测当前会话规模下历史只有 ~1,000 tokens（12 条消息），所以这条**不是**主要省钱手段 ——
#: 它的作用是**防长会话失控**：40 条消息每轮重发，token 会随轮次线性膨胀。
_HISTORY_CHAR_BUDGET = 12000


def _fit_history(history: list[dict[str, Any]], budget_chars: int) -> list[dict[str, Any]]:
    """按字符预算裁剪历史：**保头（最初的需求）保尾（最近的对话）**，中间丢掉并显式说明。

    为什么不直接砍掉最早的几条：用户最初那句话定义了整个任务（"我想入门 XX"），丢了它
    模型就不知道自己在回答什么；而最近几轮决定当前上下文。所以两头都留。
    为什么丢中间要留一句说明：不说明的话模型会以为对话就这么短，可能重复问已经说过的信息。
    """
    if budget_chars <= 0 or not history:
        return history
    if sum(len(str(item.get("content") or "")) for item in history) <= budget_chars:
        return history

    first_user = next(
        (item for item in history if item.get("role") == "user" and item.get("content")), None
    )
    kept: list[dict[str, Any]] = []
    used = 0
    for item in reversed(history):
        size = len(str(item.get("content") or ""))
        if kept and used + size > budget_chars:
            break
        kept.append(item)
        used += size
    kept.reverse()

    dropped = len(history) - len(kept)
    if dropped <= 0:
        return kept
    prefix: list[dict[str, Any]] = []
    if first_user is not None and all(item is not first_user for item in kept):
        prefix.append(first_user)
    note = (
        f"（为控制上下文成本，中间 {dropped} 条对话已省略；"
        "下面接的是用户最初的需求与最近的对话。需要旧信息时再问用户。）"
    )
    return [*prefix, {"role": "system", "content": note}, *kept]


@dataclass
class TurnContext:
    db: Database
    config: Config
    llm: LlmClient
    http: HttpClient | None = None
    cancelled: set[str] = field(default_factory=set)
    #: `session_id -> turn_id`：这个会话此刻正在跑哪一轮。
    #: 为什么需要它：界面在整轮结束前**拿不到 turn_id**（它随 `chat.done` 才回来），
    #: 所以"停止"必须能按会话取消，而不是按 turn_id（否则这个 RPC 从设计上就没法被界面调用）。
    running: dict[str, str] = field(default_factory=dict)

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
        auto_artifacts=config.generation.auto_artifacts,
    )
    messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
    history, _ = sessions.list_messages(db, session_id, limit=_HISTORY_LIMIT)
    for row in history:
        if row["role"] in {"user", "assistant"} and row["content"].strip():
            messages.append({"role": row["role"], "content": row["content"]})
    # 保头保尾地裁到字符预算内（长会话防失控；短会话原样返回）
    budget = int(getattr(config.runtime, "max_history_chars", _HISTORY_CHAR_BUDGET) or 0)
    return [messages[0], *_fit_history(messages[1:], budget)]


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
    #: 本轮已发给模型的 source_id —— 同一条来源第二次出现时只回锚点，摘要不再重复计费
    seen_source_ids: set[str] = set()
    stop_reason = "completed"
    final_text = ""
    error_message: str | None = None

    # 轮次预算 = 工具预算 + 1：**最后那一轮不给工具**，专门留给模型把结果讲成人话。
    # 否则模型刚好用满工具预算时会被判 tool_limit，最终答案直接丢掉（踩过）。
    tool_budget = max(1, ctx.config.runtime.max_tool_calls_per_turn)
    refs_total: dict[str, list[str]] = {}
    try:
        # 先把"这个会话正在跑哪一轮"记下来：界面点「停止」时按会话找到它（整轮结束前拿不到 turn_id）
        ctx.running[session_id] = turn_id
        for _round in range(tool_budget + 1):
            if ctx.is_cancelled(turn_id):
                stop_reason = "cancelled"
                break
            tools_for_round = None if len(executed) >= tool_budget else tool_decls
            reply = ctx.llm.chat(messages, tools=tools_for_round)
            _accumulate_usage(usage_total, reply)
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
            )
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
                    ctx,
                    session_id,
                    turn_id,
                    len(executed),
                    call,
                    emit,
                    seen_source_ids=seen_source_ids,
                )
                _merge_refs(refs_total, tool_refs)
                executed.append(canonical)
                messages.append(
                    {
                        "role": "tool",
                        "content": result_text,
                        "tool_call_id": call.id,
                        "name": call.name,
                    }
                )
                # 更早的工具结果折叠成一行：带着 3 万 tokens 的历史一路重发是最贵的写法
                context_budget.collapse_old_tool_results(messages)
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
    finally:
        # 无论怎么结束都要摘掉：留着会让"下一次取消"打到一轮已经结束的轮次上
        ctx.running.pop(session_id, None)

    message_id = sessions.append_message(
        ctx.db,
        session_id,
        role="assistant",
        content=final_text,
        token_in=usage_total.get("prompt_tokens"),
        token_out=usage_total.get("completion_tokens"),
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
    *,
    seen_source_ids: set[str] | None = None,
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
            context_budget.project_tool_result(result, seen_source_ids=seen_source_ids),
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
        auto_artifacts=config.generation.auto_artifacts,
    )
    value = system_prompt_hash(prompt)
    db.execute("UPDATE sessions SET system_prompt_hash = ? WHERE id = ?", (value, session_id))
    return value
