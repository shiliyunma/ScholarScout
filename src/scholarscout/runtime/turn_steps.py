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
from scholarscout.runtime import (
    compaction,
    context_budget,
    memory,
    registry,
    research_state,
    search_intent,
)
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


def maybe_compact(ctx: TurnContext, *, session_id: str, turn_id: str) -> None:
    """D3：历史超过阈值就先压一次（**同一段只压一次**，提要与"已覆盖到第几条"存进台账）。

    失败了退回规则式裁剪（_fit_history），本轮照常 —— 压缩是省钱手段，不能因为它弄挂一轮。
    """
    if not (bool(getattr(ctx.config.generation, "compact_by_model", True)) and ctx.llm is not None):
        return
    try:
        threshold = int(
            getattr(ctx.config.runtime, "compact_after_chars", compaction.DEFAULT_AFTER_CHARS)
        )
        if ctx.config.summary_cfg is not None and compaction.should_compact(
            ctx.db, session_id, after_chars=threshold
        ):
            outcome = compaction.compact(
                ctx.llm,
                ctx.db,
                session_id,
                prompts_dir=ctx.config.summary_cfg.prompts_dir,
            )
            if outcome.get("summary"):
                compaction.save_summary(
                    ctx.db,
                    session_id,
                    summary=str(outcome["summary"]),
                    covered=int(outcome["covered"]),
                )
            usage = outcome.get("usage") or {}
            if usage:
                traces.record_usage(
                    ctx.db,
                    model=ctx.config.summary.model,
                    purpose="compact",
                    session_id=session_id,
                    turn_id=turn_id,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    cached_tokens=usage.get("cached_tokens"),
                )
    except Exception as exc:
        # 仍然是"只吞不抛"（压缩失败不该让这一轮开不起来），但必须留痕：
        # 否则压缩/记账一直失败，用户只会看到上下文越来越长、账单对不上，
        # 却没有任何一处日志说明为什么。控制流与原来完全一致。
        logger.warning(
            "压缩与压缩记账失败（本轮照常继续，历史不压缩）：session=%s turn=%s err=%s",
            session_id,
            turn_id,
            exc,
        )


def prepare_research_state(
    ctx: TurnContext, *, session_id: str, text: str, turn_id: str
) -> dict[str, Any]:
    """读跨轮研究台账 → 开局拆子问题（每会话一次）→ 解析本轮检索偏好位（方案 3）。

    「连续无新增」是**轮内**计数：上一轮留下的计数不能带进这一轮，
    否则用户换个话题继续问时，第一轮就会被立刻叫停。
    """
    # ---- A1/A2：把跨轮研究台账与"缺口清单"注入到**本轮消息的最后**。
    # 为什么放最后而不是最前：服务端缓存按"从头的最长公共前缀"命中（A6），
    # 台账每轮都会变（已搜查询、缺口都会增长），放前面等于每轮都让缓存失效。
    state = research_state.load_state(ctx.db, session_id) or research_state.empty_state(
        goal=text.strip()[:200]
    )
    state["empty_rounds"] = 0
    if not state.get("questions") and not state.get("used_queries"):
        # 开局拆一次子问题（每会话一次的小调用）——关掉开关就退化成"只有目标 + 只有缺口"
        if (
            bool(getattr(ctx.config.runtime, "plan_by_model", False))
            and ctx.llm is not None
            and ctx.config.summary_cfg is not None
        ):
            questions, reason = research_state.decompose_with_reason(
                ctx.llm, text, prompts_dir=ctx.config.summary_cfg.prompts_dir
            )
            state["questions"] = questions
            if reason:
                # 留痕：拆不出来时缺口粒度会退化，台账里要看得见（真机栽过一次）
                state["plan_error"] = reason
        research_state.save_state(ctx.db, session_id, state)
    # ---- 方案 3：本轮检索偏好位（`runtime/search_intent.py`）。**不进系统提示** —— 它写进
    # 研究台账的 payload_json，系统提示仍是静态文本，`system_prompt_hash` 与用户点选无关。
    # 规则优先（"只看论文/别搜了"不调模型），模糊查询才花一次极小调用；失败开放，绝不阻断本轮。
    try:
        intent = search_intent.resolve_turn(
            ctx.llm,
            text,
            state=state,
            turn_id=turn_id,
            prompts_dir=ctx.config.summary_cfg.prompts_dir if ctx.config.summary_cfg else None,
            enabled=bool(getattr(ctx.config.runtime, "classify_search_intent", True)),
        )
        if intent is not None and intent.usage:
            # 这次小调用的钱也要记账（purpose="intent"）—— 否则"关掉它能省多少"没人答得出来
            traces.record_usage(
                ctx.db,
                model=ctx.config.chat.model,
                purpose="intent",
                session_id=session_id,
                turn_id=turn_id,
                prompt_tokens=intent.usage.get("prompt_tokens"),
                completion_tokens=intent.usage.get("completion_tokens"),
            )
        research_state.save_state(ctx.db, session_id, state)
    except Exception:
        # 分类是省钱的辅助，不是主流程的前置条件：它挂了，本轮照常检索与回答
        pass
    return state


def render_context_note(ctx: TurnContext, *, session_id: str, state: dict[str, Any]) -> str:
    """本轮末尾那条 system 的内容：台账（目标/缺口/已搜查询）+ 学习者画像 + 跨会话记忆。"""
    gaps = research_state.compute_gaps(ctx.db, session_id, state)
    # 学习者画像（跨会话）：只放"能改变行为"的信息（水平、薄弱块、已做过的主题）。
    # 纯本地汇总、零模型调用；没有卡片也没有产物时这一段是空串，不会白占 token。
    from scholarscout.runtime import learner_profile

    profile_note = learner_profile.render_profile_note(learner_profile.build_profile(ctx.db))
    note = research_state.render_note(state, gaps)
    if profile_note:
        note = note + "\n\n" + profile_note
    # 跨会话记忆（D2）：稳定事实 + "冲突以用户当前说法为准"的规矩
    from scholarscout.runtime import memory

    facts_note = memory.render_facts_note(ctx.db)
    if facts_note:
        note = note + "\n\n" + facts_note
    return note


def account_tool_result(
    ctx: TurnContext,
    *,
    session_id: str,
    state: dict[str, Any],
    call: ToolCall,
    canonical: str,
    tool_refs: Any,
    tool_ok: bool,
    fresh_sources: list[str],
) -> None:
    """A1/A7：把一次工具结果并进跨轮台账 —— 新来源、空轮计数、已搜查询（A3 判重数据源）。"""
    fresh = research_state.new_sources(state, list(tool_refs.get("source_ids") or []))
    fresh_sources.extend(fresh)
    if canonical == "research.search":
        if fresh:
            state["empty_rounds"] = 0
        elif tool_ok:
            # 只有**真的搜过且没新来源**才算"空轮"。没搜（用户选择不检索 / 等用户选类型 /
            # 来源报错）不算 —— 否则会把"没搜"误判成"搜不到"，提前触发停搜提示。
            state["empty_rounds"] = int(state.get("empty_rounds") or 0) + 1
        # 记账（A3 判重的唯一数据源）：**搜成功才记**，失败不记 ——
        # 否则一次网络抖动就把这个方向永久锁死。写者只有循环这一个。
        query = query_from_arguments(call.arguments)
        if tool_ok and query:
            research_state.record_query(state, query)
        research_state.save_state(ctx.db, session_id, state)


def apply_coverage(
    ctx: TurnContext,
    *,
    session_id: str,
    turn_id: str,
    state: dict[str, Any],
    final_text: str,
    fresh_sources: list[str],
) -> str:
    """A1/A2：把模型回填的覆盖状态并进台账，并从可见正文里剥掉记账标记。

    标记解析不出来就什么都不改（保持 open）—— 猜覆盖等于自欺。台账写回失败不拖垮这一轮，
    但必须留痕：静默失败 = 下一轮带着同一份旧台账跑，用户只会觉得"它怎么又忘了"。
    """
    try:
        if research_state.apply_coverage(state, final_text, fresh_sources):
            final_text = research_state.strip_marker(final_text)
        research_state.save_state(ctx.db, session_id, state)
    except Exception as exc:
        logger.warning(
            "跨轮台账写回失败（保持 open，本轮照常收尾）：session=%s turn=%s err=%s",
            session_id,
            turn_id,
            exc,
        )
    return final_text


def extract_memory(ctx: TurnContext, *, session_id: str, turn_id: str) -> None:
    """D2：跨会话记忆。每个会话最多抽一次，且要在"有值得记的迹象"时才抽。

    失败不影响本轮结果（回答已经生成好了）；开销按 purpose="memory" 如实记账，
    免得它变成看不见的暗开销（用户对成本敏感，这条尤其重要）。
    """
    if not (bool(getattr(ctx.config.generation, "memory_by_model", True)) and ctx.llm is not None):
        return
    try:
        # P1-1：nudge_turns=0（默认）时行为与引入前逐字一致（每会话最多一次）
        nudge_turns = int(getattr(ctx.config.generation, "memory_nudge_turns", 0) or 0)
        if ctx.config.summary_cfg is not None and memory.should_extract(
            ctx.db, session_id, nudge_turns=nudge_turns
        ):
            outcome = memory.extract_facts(
                ctx.llm,
                ctx.db,
                session_id,
                prompts_dir=ctx.config.summary_cfg.prompts_dir,
            )
            # 记基准轮（抽到空表也记）—— 否则下一轮会再次触发，白花钱
            memory.mark_extracted(ctx.db, session_id)
            usage = outcome.get("usage") or {}
            if usage:
                traces.record_usage(
                    ctx.db,
                    model=ctx.config.summary.model,
                    purpose="memory",
                    session_id=session_id,
                    turn_id=turn_id,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    cached_tokens=usage.get("cached_tokens"),
                )
    except Exception as exc:
        # 仍然是"只吞不抛"（回答已经生成好了），但记忆抽取失败必须留痕：
        # 它同时会吃掉 purpose="memory" 的那条用量记账，静默就等于暗开销。
        logger.warning(
            "跨会话记忆抽取失败（不影响本轮结果）：session=%s turn=%s err=%s",
            session_id,
            turn_id,
            exc,
        )
